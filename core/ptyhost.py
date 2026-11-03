"""Interactive shells on a pseudo-terminal: a PTY on POSIX (Linux, macOS, Android/Termux) and a
ConPTY pseudo console on Windows 10 1809+ (standard library only, through ctypes).

A session is a login shell on its own terminal. It outlives the browser tab: when the WebSocket
drops (a phone puts the page in the background, the network changes) the shell keeps running
and its recent output is kept, so the next connection with the same session id picks up where
it was. A session nobody is attached to is closed after DETACHED_TTL; all of them close when
the remote terminal is switched off.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import signal
import struct
import sys
import threading
import time
from pathlib import Path

try:
    import fcntl
    import pty
    import select
    import termios
except ImportError:  # Windows: ConPTY below
    pty = None

from .ws import Closed

MAX_SESSIONS = 4
DETACHED_TTL = 15 * 60
SCROLLBACK = 256 * 1024
WINDOWS = sys.platform == "win32"
# Terminal queries (cursor position, status, device attributes). A replay must not carry them:
# the browser's terminal would answer questions that were asked minutes ago.
QUERIES = re.compile(rb"\x1b\[(?:6n|5n|>?0?c)")


def _env():
    env = {**os.environ, "TERM": "xterm-256color", "COLORTERM": "truecolor", "HUB_TERMINAL": "1"}
    for k in ("HUB_SUPERVISED", "HUB_CORE_HOME"):
        env.pop(k, None)
    return env


def _int(v, default):
    try:
        return int(v) or default
    except (TypeError, ValueError):
        return default


def _size(cols, rows):
    return max(10, min(_int(cols, 80), 500)), max(4, min(_int(rows, 24), 300))


class _Session:
    """What every kind of session shares: the attached socket, the scrollback and the reaping."""

    def __init__(self, owner, who):
        self.owner, self.who = owner, who
        self.id = secrets.token_urlsafe(12)
        self.started = self.seen = time.time()
        self.buffer = bytearray()
        self.ws = None
        self.lock = threading.Lock()
        self.code = None

    def attach(self, ws):
        """Points the output at a new connection and replays what the shell printed meanwhile."""
        with self.lock:
            old, self.ws = self.ws, ws
            replay = QUERIES.sub(b"", bytes(self.buffer))
        if old and old is not ws:
            old.close(4000)  # opened somewhere else: the newest connection wins
        self.seen = time.time()
        if replay:
            ws.send_bytes(replay)

    def detach(self, ws):
        with self.lock:
            if self.ws is ws:
                self.ws = None
        self.seen = time.time()

    def _output(self, data: bytes):
        with self.lock:
            self.buffer += data
            if len(self.buffer) > SCROLLBACK:
                del self.buffer[: len(self.buffer) - SCROLLBACK]
        ws = self.ws
        if ws:
            try:
                ws.send_bytes(data)
            except Closed:
                self.detach(ws)

    def _idle(self):
        """Nothing printed for a while: keep the socket alive, or end a session nobody watches.
        Returns False once the session is being closed."""
        ws = self.ws
        if ws:
            try:
                ws.ping()  # keeps proxies from closing an idle shell
            except Closed:
                self.detach(ws)
        elif time.time() - self.seen > DETACHED_TTL:
            self.close()
            return False
        return True

    def _ended(self):
        ws = self.ws
        if ws:
            try:
                ws.send_text(json.dumps({"t": "exit", "code": self.code}))
                ws.close()
            except Closed:
                pass
        self.owner.forget(self)


def _login_shell(default):
    for cand in (os.environ.get("SHELL"), default):
        if cand and os.path.isabs(cand) and os.access(cand, os.X_OK):
            return cand
    for name in (default, "bash", "zsh", "sh"):
        for d in os.environ.get("PATH", "/usr/bin:/bin").split(os.pathsep):
            p = os.path.join(d, name or "")
            if name and os.access(p, os.X_OK):
                return p
    return "/bin/sh"


# Shell colours. Each session first loads the user's own setup (what a login shell reads) and only
# then adds colours where the user has none: a prompt with user@host in green and the folder in
# blue, and colour for ls and grep. The ANSI colours follow the hub's theme in the browser.
BASHRC = r'''# Written by the Shoal hub for its terminal sessions (core/ptyhost.py); it is rewritten each time.
for f in /etc/profile "${PREFIX:-/nonexistent}/etc/profile"; do [ -f "$f" ] && . "$f"; done
if [ -f "$HOME/.bash_profile" ]; then . "$HOME/.bash_profile"
elif [ -f "$HOME/.bash_login" ]; then . "$HOME/.bash_login"
elif [ -f "$HOME/.profile" ]; then . "$HOME/.profile"; fi
if [ -f "$HOME/.bashrc" ] && ! grep -qs bashrc "$HOME/.bash_profile" "$HOME/.bash_login" "$HOME/.profile"; then
  . "$HOME/.bashrc"
fi
case "$PS1" in
  *'\e['*|*'\033['*|*'\E['*|*$'\e'*) ;;   # the user's prompt already has colours: keep it
  *) PS1='\[\e[1;32m\]\u@\h\[\e[0m\]:\[\e[1;34m\]\w\[\e[0m\]\$ ' ;;
esac
if ! alias ls >/dev/null 2>&1; then
  if ls --color=auto -d / >/dev/null 2>&1; then alias ls='ls --color=auto'; else export CLICOLOR=1; fi
fi
alias grep >/dev/null 2>&1 || alias grep='grep --color=auto'
[ -z "$LS_COLORS" ] && command -v dircolors >/dev/null 2>&1 && eval "$(dircolors -b)"
'''
# PowerShell: the same prompt, only while the prompt is still the built-in one.
PS_PROMPT = r'''if ((Get-Command prompt).ScriptBlock.ToString() -match 'PS \$\(\$executionContext\.SessionState\.Path\.CurrentLocation\)') {
  function global:prompt {
    $e = [char]27
    $p = $executionContext.SessionState.Path.CurrentLocation.Path
    if ($HOME -and $p.StartsWith($HOME, [StringComparison]::OrdinalIgnoreCase)) { $p = '~' + $p.Substring($HOME.Length) }
    "$e[1;32m$env:USERNAME@$env:COMPUTERNAME$e[0m:$e[1;34m$p$e[0m$('>' * ($nestedPromptLevel + 1)) "
  }
}'''


def _shell_args(shell, rc_dir):
    """How to start this shell for an interactive session (with the colours above when it can)."""
    name = Path(shell).name.lower()
    if name.startswith(("pwsh", "powershell")):
        import base64
        return [shell, "-NoLogo", "-NoExit", "-EncodedCommand", base64.b64encode(PS_PROMPT.encode("utf-16-le")).decode()]
    if name.startswith("bash") and rc_dir:
        try:
            rc_dir.mkdir(parents=True, exist_ok=True)
            rc = rc_dir / "bashrc"
            rc.write_text(BASHRC, encoding="utf-8")
            return [shell, "--rcfile", str(rc), "-i"]
        except OSError:
            pass
    if name.startswith("cmd"):
        return [shell]
    return [shell, "-l"]


class PosixSession(_Session):
    def __init__(self, owner, shell, cwd, cols, rows, who):
        super().__init__(owner, who)
        self.shell = _login_shell(shell)
        env = _env()
        args = _shell_args(self.shell, owner.rc_dir)
        pid, fd = pty.fork()
        if pid == 0:  # child: become the shell right away (nothing else is safe after fork)
            try:
                os.chdir(cwd)
            except OSError:
                pass
            try:
                os.execve(self.shell, args, env)
            finally:
                os._exit(127)
        self.pid, self.fd = pid, fd
        self.resize(cols, rows)
        threading.Thread(target=self._pump, daemon=True, name=f"pty-{self.id}").start()

    def resize(self, cols, rows):
        cols, rows = _size(cols, rows)
        try:
            fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        except OSError:
            pass

    def write(self, data: bytes):
        self.seen = time.time()
        try:
            os.write(self.fd, data)
        except OSError:
            pass

    def _pump(self):
        while True:
            try:
                ready, _, _ = select.select([self.fd], [], [], 25)
            except (OSError, ValueError):
                break
            if not ready:
                if not self._idle():
                    break
                continue
            try:
                data = os.read(self.fd, 65536)
            except OSError:
                data = b""
            if not data:
                break
            self._output(data)
        try:
            _, status = os.waitpid(self.pid, 0)
            self.code = os.waitstatus_to_exitcode(status) if hasattr(os, "waitstatus_to_exitcode") else status >> 8
        except ChildProcessError:
            pass
        try:
            os.close(self.fd)
        except OSError:
            pass
        self._ended()

    def close(self):
        """Hangs up the shell's process group; the pump thread reaps it. SIGKILL if it ignores that."""
        def hangup():
            for sig, wait in ((signal.SIGHUP, 3), (signal.SIGKILL, 0)):
                if self.code is not None or self.id not in self.owner.sessions:
                    return
                try:
                    os.killpg(self.pid, sig)
                except (ProcessLookupError, PermissionError):
                    return
                time.sleep(wait)
        threading.Thread(target=hangup, daemon=True).start()


# ---------- Windows: ConPTY ----------
if WINDOWS:
    import ctypes
    from ctypes import wintypes

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _HPCON = wintypes.HANDLE
    _EXTENDED_STARTUPINFO_PRESENT = 0x00080000
    _CREATE_UNICODE_ENVIRONMENT = 0x00000400
    _PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE = 0x00020016
    _WAIT_OBJECT_0, _INFINITE = 0, 0xFFFFFFFF
    _INHERIT_CURSOR = 0x1  # start where the browser's cursor is (below the banner)

    class _COORD(ctypes.Structure):
        _fields_ = [("X", ctypes.c_short), ("Y", ctypes.c_short)]

    class _STARTUPINFOW(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("lpReserved", wintypes.LPWSTR), ("lpDesktop", wintypes.LPWSTR),
                    ("lpTitle", wintypes.LPWSTR), ("dwX", wintypes.DWORD), ("dwY", wintypes.DWORD),
                    ("dwXSize", wintypes.DWORD), ("dwYSize", wintypes.DWORD), ("dwXCountChars", wintypes.DWORD),
                    ("dwYCountChars", wintypes.DWORD), ("dwFillAttribute", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                    ("wShowWindow", wintypes.WORD), ("cbReserved2", wintypes.WORD), ("lpReserved2", ctypes.c_void_p),
                    ("hStdInput", wintypes.HANDLE), ("hStdOutput", wintypes.HANDLE), ("hStdError", wintypes.HANDLE)]

    class _STARTUPINFOEXW(ctypes.Structure):
        _fields_ = [("StartupInfo", _STARTUPINFOW), ("lpAttributeList", ctypes.c_void_p)]

    class _PROCESS_INFORMATION(ctypes.Structure):
        _fields_ = [("hProcess", wintypes.HANDLE), ("hThread", wintypes.HANDLE),
                    ("dwProcessId", wintypes.DWORD), ("dwThreadId", wintypes.DWORD)]

    _k32.CreatePseudoConsole.argtypes = [_COORD, wintypes.HANDLE, wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(_HPCON)]
    _k32.CreatePseudoConsole.restype = ctypes.c_long
    _k32.ResizePseudoConsole.argtypes = [_HPCON, _COORD]
    _k32.ResizePseudoConsole.restype = ctypes.c_long
    _k32.ClosePseudoConsole.argtypes = [_HPCON]
    _k32.ClosePseudoConsole.restype = None
    _k32.CreatePipe.argtypes = [ctypes.POINTER(wintypes.HANDLE), ctypes.POINTER(wintypes.HANDLE), ctypes.c_void_p, wintypes.DWORD]
    _k32.InitializeProcThreadAttributeList.argtypes = [ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(ctypes.c_size_t)]
    _k32.UpdateProcThreadAttribute.argtypes = [ctypes.c_void_p, wintypes.DWORD, ctypes.c_size_t, ctypes.c_void_p,
                                               ctypes.c_size_t, ctypes.c_void_p, ctypes.c_void_p]
    _k32.DeleteProcThreadAttributeList.argtypes = [ctypes.c_void_p]
    _k32.CreateProcessW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.c_void_p, ctypes.c_void_p, wintypes.BOOL,
                                    wintypes.DWORD, ctypes.c_void_p, wintypes.LPCWSTR, ctypes.c_void_p,
                                    ctypes.POINTER(_PROCESS_INFORMATION)]
    _k32.ReadFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
    _k32.WriteFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
    _k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    _k32.WaitForSingleObject.restype = wintypes.DWORD
    _k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    _k32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]

    def _check(ok, what):
        if not ok:
            raise OSError(ctypes.get_last_error(), f"{what} failed")

    def _windows_shell(default):
        """PowerShell 7 if installed, else Windows PowerShell; cmd as the last resort."""
        for name in ("pwsh.exe", "powershell.exe", "cmd.exe"):
            p = shutil.which(name)
            if p:
                return p
        return os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "cmd.exe")

    class WindowsSession(_Session):
        def __init__(self, owner, shell, cwd, cols, rows, who):
            super().__init__(owner, who)
            self.shell = _windows_shell(shell)
            self.alive = True
            self.pending, self.asked = 0, False  # the console's first cursor question waits for the browser
            self._pc_lock, self._pc_open = threading.Lock(), False
            in_r, in_w, out_r, out_w = (wintypes.HANDLE() for _ in range(4))
            _check(_k32.CreatePipe(ctypes.byref(in_r), ctypes.byref(in_w), None, 0), "CreatePipe")
            _check(_k32.CreatePipe(ctypes.byref(out_r), ctypes.byref(out_w), None, 0), "CreatePipe")
            self.hpc = _HPCON()
            c, r = _size(cols, rows)
            hr = _k32.CreatePseudoConsole(_COORD(c, r), in_r, out_w, _INHERIT_CURSOR, ctypes.byref(self.hpc))
            _k32.CloseHandle(in_r)   # the pseudo console holds its own ends now
            _k32.CloseHandle(out_w)
            if hr != 0:
                _k32.CloseHandle(in_w)
                _k32.CloseHandle(out_r)
                raise OSError(hr, "CreatePseudoConsole failed (Windows 10 1809 or later is required)")
            self.in_w, self.out_r = in_w, out_r
            self._pc_open = True
            size = ctypes.c_size_t()
            _k32.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(size))
            attrs = ctypes.create_string_buffer(size.value)
            _check(_k32.InitializeProcThreadAttributeList(attrs, 1, 0, ctypes.byref(size)), "InitializeProcThreadAttributeList")
            _check(_k32.UpdateProcThreadAttribute(attrs, 0, _PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE, self.hpc,
                                                  ctypes.sizeof(_HPCON), None, None), "UpdateProcThreadAttribute")
            si = _STARTUPINFOEXW()
            si.StartupInfo.cb = ctypes.sizeof(_STARTUPINFOEXW)
            # Null std handles: otherwise a hub whose own output is redirected (a log file, a pipe)
            # hands those to the shell, which then writes there instead of to the pseudo console.
            si.StartupInfo.dwFlags = 0x00000100  # STARTF_USESTDHANDLES
            si.lpAttributeList = ctypes.cast(attrs, ctypes.c_void_p)
            env = "".join(f"{k}={v}\0" for k, v in sorted(_env().items())) + "\0"
            env_buf = ctypes.create_unicode_buffer(env, len(env))
            args = _shell_args(self.shell, owner.rc_dir)
            cmdline = ctypes.create_unicode_buffer(" ".join(f'"{a}"' if " " in a else a for a in args))
            pi = _PROCESS_INFORMATION()
            try:
                _check(_k32.CreateProcessW(None, cmdline, None, None, False,
                                           _EXTENDED_STARTUPINFO_PRESENT | _CREATE_UNICODE_ENVIRONMENT,
                                           env_buf, cwd if os.path.isdir(cwd) else None, ctypes.byref(si), ctypes.byref(pi)),
                       "CreateProcess")
            except OSError:
                _k32.ClosePseudoConsole(self.hpc)
                _k32.CloseHandle(self.in_w)
                _k32.CloseHandle(self.out_r)
                raise
            finally:
                _k32.DeleteProcThreadAttributeList(attrs)
            _k32.CloseHandle(pi.hThread)
            self.process, self.pid = pi.hProcess, pi.dwProcessId
            threading.Thread(target=self._pump, daemon=True, name=f"conpty-{self.id}").start()
            threading.Thread(target=self._wait, daemon=True, name=f"conpty-wait-{self.id}").start()
            threading.Thread(target=self._keepalive, daemon=True, name=f"conpty-idle-{self.id}").start()

        def resize(self, cols, rows):
            c, r = _size(cols, rows)
            if self.alive:
                _k32.ResizePseudoConsole(self.hpc, _COORD(c, r))

        def write(self, data: bytes):
            self.seen = time.time()
            if not self.alive or not data:
                return
            done = wintypes.DWORD()
            _k32.WriteFile(self.in_w, data, len(data), ctypes.byref(done), None)

        def _output(self, data: bytes):
            # The Windows console asks where the cursor is (at start, after a resize) and holds the
            # input until it gets an answer. The first question belongs to the browser that is about
            # to attach: it has just drawn the banner, and the shell must start below it. Later
            # ones, with nobody attached, are answered here.
            n = data.count(b"\x1b[6n")
            if n and self.ws is None:
                if not self.asked:  # the one at start: it waits for the browser
                    self.asked, self.pending = True, 1
                    n -= 1
                if n:  # anything else must not wait: a resize would block until it is answered
                    self.write(b"\x1b[1;1R" * n)
            elif n:
                self.asked = True
            super()._output(data)

        def attach(self, ws):
            super().attach(ws)
            if self.pending:  # the console's first question: answered by the terminal that just drew the banner
                self.pending = 0
                try:
                    ws.send_bytes(b"[6n")
                except Closed:
                    pass

        def _pump(self):
            buf = ctypes.create_string_buffer(65536)
            got = wintypes.DWORD()
            while _k32.ReadFile(self.out_r, buf, len(buf), ctypes.byref(got), None) and got.value:
                self._output(buf.raw[:got.value])

        def _keepalive(self):
            while self.alive:
                time.sleep(25)
                if self.alive and time.time() - self.seen > 25 and not self._idle():
                    return

        def _wait(self):
            _k32.WaitForSingleObject(self.process, _INFINITE)
            code = wintypes.DWORD()
            _k32.GetExitCodeProcess(self.process, ctypes.byref(code))
            self.code = code.value
            self.alive = False
            time.sleep(0.2)  # let the last output through before the console goes away
            self._close_pc()  # ends the reader: its pipe breaks
            for h in (self.in_w, self.out_r, self.process):
                _k32.CloseHandle(h)
            self._ended()

        def _close_pc(self):
            with self._pc_lock:
                if self._pc_open:
                    self._pc_open = False
                    _k32.ClosePseudoConsole(self.hpc)

        def close(self):
            """Closes the pseudo console (the shell gets CTRL_CLOSE); terminates it if it lingers."""
            def hangup():
                if not self.alive:
                    return
                self._close_pc()
                if _k32.WaitForSingleObject(self.process, 3000) != _WAIT_OBJECT_0 and self.alive:
                    _k32.TerminateProcess(self.process, 1)
            threading.Thread(target=hangup, daemon=True).start()

    def _conpty_available():
        try:
            return bool(getattr(_k32, "CreatePseudoConsole", None)) and sys.getwindowsversion().build >= 17763
        except Exception:
            return False

    Session = WindowsSession
    AVAILABLE = _conpty_available()
else:
    Session = PosixSession
    AVAILABLE = pty is not None


class Host:
    def __init__(self, log, rc_dir: Path | None = None):
        self.rc_dir = rc_dir  # the hub's own data folder: the shell setup lives there, not in a shared /tmp
        self.sessions = {}
        self.lock = threading.Lock()
        self.log = log

    def forget(self, s):
        with self.lock:
            self.sessions.pop(s.id, None)
        self.log(s.who, f"pty session {s.id} ended (code {s.code})")

    def close_all(self):
        with self.lock:
            sessions = list(self.sessions.values())
        for s in sessions:
            s.close()

    def serve(self, ws, query, shell, cwd, who):
        """Runs one connection: attach to its session (or start one) and feed it the input."""
        cols, rows = query.get("cols"), query.get("rows")
        with self.lock:
            s = self.sessions.get(query.get("session") or "")
        if s is None:
            with self.lock:
                if len(self.sessions) >= MAX_SESSIONS:
                    ws.send_text(json.dumps({"t": "error", "error": "too many open sessions"}))
                    ws.close(1013)
                    return
            try:
                s = Session(self, shell, cwd, cols, rows, who)
            except OSError as e:
                ws.send_text(json.dumps({"t": "error", "error": f"could not start the shell: {e}"}))
                ws.close(1011)
                return
            with self.lock:
                self.sessions[s.id] = s
            self.log(who, f"pty session {s.id} started ({s.shell})")
            reattached = False
        else:
            s.resize(cols, rows)
            reattached = True
        ws.send_text(json.dumps({"t": "hello", "session": s.id, "shell": Path(s.shell).name,
                                 "reattached": reattached}))
        s.attach(ws)
        try:
            while True:
                op, data = ws.recv()
                if op == 2:
                    s.write(data)
                    continue
                try:
                    msg = json.loads(data.decode("utf-8", "replace"))
                except ValueError:
                    continue
                if msg.get("t") == "i":
                    s.write(str(msg.get("d") or "").encode("utf-8"))
                elif msg.get("t") == "r":
                    s.resize(msg.get("c"), msg.get("r"))
                elif msg.get("t") == "bye":  # the user ends the session: its exit message closes this socket
                    s.close()
        except Closed:
            pass
        finally:
            s.detach(ws)
