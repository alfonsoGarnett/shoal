"""Interactive shell sessions on this OS (a PTY on POSIX, ConPTY on Windows), without a browser.

A fake WebSocket stands in for xterm.js: it answers cursor-position queries the way a terminal does.
Checks: a command runs, a reconnection gets the session back with its output, the exit code comes
through, and ending the session from the UI closes it.
"""
import json
import os
import queue
import re
import sys
import tempfile
import threading
import time
from pathlib import Path

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))

from core import ptyhost  # noqa: E402
from core.ws import Closed  # noqa: E402

ok = 0


def check(cond, label):
    global ok
    if not cond:
        raise AssertionError(label)
    ok += 1
    print(f"  ok  {label}")


class FakeWS:
    def __init__(self):
        self.inbox, self.out, self.texts = queue.Queue(), bytearray(), []

    def send_text(self, t):
        self.texts.append(json.loads(t))

    def send_bytes(self, b):
        self.out.extend(b)
        for _ in range(b.count(b"\x1b[6n")):  # like xterm.js: answer where the cursor is
            self.inbox.put({"t": "i", "d": "\x1b[1;1R"})

    def ping(self):
        pass

    def close(self, code=1000):
        self.inbox.put(None)

    def recv(self):
        m = self.inbox.get()
        if m is None:
            raise Closed()
        return 1, json.dumps(m).encode()


def wait(cond, secs=20):
    end = time.time() + secs
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.2)
    return False


if not ptyhost.AVAILABLE:
    print("no interactive shells on this system (Windows before 10 1809?): skipped")
    sys.exit(0)

shell = "powershell" if os.name == "nt" else "bash"
host = ptyhost.Host(lambda who, text: None, Path(tempfile.mkdtemp(prefix="hub-pty-")))
home = str(Path.home())
ws = FakeWS()
threading.Thread(target=host.serve, args=(ws, {"cols": "100", "rows": "30"}, shell, home, "test"), daemon=True).start()
check(wait(lambda: ws.texts and ws.texts[0].get("t") == "hello"), f"{ptyhost.Session.__name__}: the session says hello")
session = ws.texts[0]["session"]
time.sleep(1.5)
ws.inbox.put({"t": "i", "d": "echo PTY_RUNS\r"})
check(wait(lambda: b"PTY_RUNS\r" in bytes(ws.out) or b"PTY_RUNS\n" in bytes(ws.out)), "a command runs and prints")
# Green in any spelling: ConPTY redraws the output and older Windows builds write bold green as 92.
GREEN = re.compile(rb"\x1b\[(?:[0-9]+;)*(?:32|92)(?:;[0-9]+)*m")
SGR = re.compile(rb"\x1b\[[0-9;]*m")
green = wait(lambda: GREEN.search(bytes(ws.out)), 10)
seen = sorted(set(SGR.findall(bytes(ws.out))))  # printed on failure: what this terminal actually wrote
check(green, "the prompt has colours (user@host in green)" + ("" if green else f": saw {seen}"))
ws.inbox.put({"t": "r", "c": 60, "r": 20})
ws.inbox.put(None)  # the browser goes away; the shell stays
time.sleep(1)
check(len(host.sessions) == 1, "the shell outlives the connection")
ws2 = FakeWS()
threading.Thread(target=host.serve, args=(ws2, {"session": session, "cols": "80", "rows": "24"}, shell, home, "test"),
                 daemon=True).start()
check(wait(lambda: ws2.texts and ws2.texts[0].get("reattached")), "a reconnection gets the same session")
check(wait(lambda: b"PTY_RUNS" in bytes(ws2.out)), "its recent output comes back")
check(b"\x1b[6n" not in bytes(ws2.out).split(b"PTY_RUNS")[0], "the replay carries no stale terminal queries")
ws2.inbox.put({"t": "i", "d": "exit 7\r"})
check(wait(lambda: any(x.get("t") == "exit" for x in ws2.texts)), "the exit is reported")
check([x for x in ws2.texts if x.get("t") == "exit"][0].get("code") == 7, "with the shell's exit code")
check(wait(lambda: not host.sessions), "the ended session is forgotten")
ws3 = FakeWS()
threading.Thread(target=host.serve, args=(ws3, {}, shell, home, "test"), daemon=True).start()
check(wait(lambda: host.sessions), "a new session starts")
time.sleep(1.5)
ws3.inbox.put({"t": "bye"})
check(wait(lambda: not host.sessions), "ending it from the UI closes it")
print(f"\n{ok} checks OK")
