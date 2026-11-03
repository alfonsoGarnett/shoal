"""Minimal WebSocket (RFC 6455) on top of an http.server request handler, standard library only.

Enough for the interactive terminal: text and binary messages, fragmentation, ping/pong and
close, plus a relay that carries a browser's WebSocket on to another hub. The caller has
already authenticated the request and checked its Origin.
"""
from __future__ import annotations

import base64
import hashlib
import json
import socket
import ssl
import struct
import threading
import urllib.parse

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
MAX_MESSAGE = 1 << 20  # a pasted block of text fits; anything bigger is not a terminal


class Closed(Exception):
    pass


class WebSocket:
    def __init__(self, handler):
        self.sock, self.rfile = handler.connection, handler.rfile
        self.lock = threading.Lock()
        self.closed = False

    @classmethod
    def accept(cls, handler):
        """Answers the upgrade (101) and returns the socket, or None if this is not a WebSocket request."""
        key = handler.headers.get("Sec-WebSocket-Key")
        if (handler.headers.get("Upgrade") or "").lower() != "websocket" or not key:
            return None
        accept = base64.b64encode(hashlib.sha1((key.strip() + GUID).encode()).digest()).decode()
        handler.close_connection = True
        handler.wfile.write(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                             f"Sec-WebSocket-Accept: {accept}\r\n\r\n").encode())
        handler.wfile.flush()
        handler.connection.settimeout(None)  # the handler's 30 s timeout would cut an idle shell
        return cls(handler)

    def _read(self, n):
        data = self.rfile.read(n)
        if len(data) < n:
            raise Closed()
        return data

    def recv(self):
        """(opcode, payload) of the next text (1) or binary (2) message; answers pings on the way."""
        opcode, parts, size = None, [], 0
        while True:
            try:
                b1, b2 = self._read(2)
            except OSError:
                raise Closed()
            op, length = b1 & 0x0F, b2 & 0x7F
            if length == 126:
                length = struct.unpack("!H", self._read(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._read(8))[0]
            size += length
            if size > MAX_MESSAGE:
                self.close(1009)
                raise Closed()
            mask = self._read(4) if b2 & 0x80 else b""
            data = self._read(length)
            if mask and length:
                key = (mask * (length // 4 + 1))[:length]
                data = (int.from_bytes(data, "big") ^ int.from_bytes(key, "big")).to_bytes(length, "big")
            if op == 8:
                self.close()
                raise Closed()
            if op == 9:
                self._send(10, data)
                continue
            if op == 10:
                continue
            if op:
                opcode = op
            parts.append(data)
            if b1 & 0x80:
                return opcode, b"".join(parts)

    def _send(self, op, data=b""):
        n = len(data)
        head = bytes([0x80 | op]) + (bytes([n]) if n < 126 else
                                     bytes([126]) + struct.pack("!H", n) if n < 65536 else
                                     bytes([127]) + struct.pack("!Q", n))
        with self.lock:
            if self.closed:
                raise Closed()
            try:
                self.sock.sendall(head + data)
            except OSError:
                self.closed = True
                raise Closed()

    def send_text(self, text: str):
        self._send(1, text.encode("utf-8"))

    def send_bytes(self, data: bytes):
        self._send(2, data)

    def ping(self):
        self._send(9)

    def close(self, code=1000):
        try:
            self._send(8, struct.pack("!H", code))
        except Closed:
            pass
        self.closed = True


def relay(handler, base_url, path, headers, timeout=15):
    """Joins the browser's WebSocket to the same endpoint on another hub, byte for byte.

    The other hub answers the upgrade with the browser's own key, so its 101 is passed back as is;
    after that both sides already speak WebSocket and frames are spliced without being decoded
    (the browser masks them, the far hub does not). Returns None once the relay ends, or
    (status, error) if the far hub refused the upgrade.
    """
    key = handler.headers.get("Sec-WebSocket-Key")
    if (handler.headers.get("Upgrade") or "").lower() != "websocket" or not key:
        return 400, "a WebSocket upgrade is required"
    u = urllib.parse.urlsplit(base_url)
    port = u.port or (443 if u.scheme == "https" else 80)
    sock = socket.create_connection((u.hostname, port), timeout=timeout)
    if u.scheme == "https":
        sock = ssl.create_default_context().wrap_socket(sock, server_hostname=u.hostname)
    try:
        lines = [f"GET {u.path.rstrip('/')}{path} HTTP/1.1", f"Host: {u.netloc}", "Upgrade: websocket",
                 "Connection: Upgrade", f"Sec-WebSocket-Key: {key}", "Sec-WebSocket-Version: 13",
                 *(f"{k}: {v}" for k, v in headers.items())]
        sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
        far = sock.makefile("rb")
        status = far.readline().decode("latin-1").split()
        head = []
        while True:
            line = far.readline()
            if line in (b"\r\n", b"\n", b""):
                break
            head.append(line.decode("latin-1").rstrip("\r\n"))
        code = int(status[1]) if len(status) > 1 and status[1].isdigit() else 502
        if code != 101:
            length = next((int(h.split(":", 1)[1]) for h in head if h.lower().startswith("content-length:")), 0)
            body = far.read(min(length, 65536)) if length else b""
            try:
                err = json.loads(body).get("error")
            except ValueError:
                err = None
            return code, err or f"the other hub answered HTTP {code}"
        handler.close_connection = True
        out = [f"HTTP/1.1 101 Switching Protocols", *(h for h in head if h.split(":", 1)[0].lower() in
               ("upgrade", "connection", "sec-websocket-accept"))]
        handler.wfile.write(("\r\n".join(out) + "\r\n\r\n").encode())
        handler.wfile.flush()
        sock.settimeout(None)
        handler.connection.settimeout(None)

        def pump(read, write):
            try:
                while True:
                    data = read(65536)
                    if not data:
                        break
                    write(data)
            except (OSError, ValueError):
                pass
            for s in (sock, handler.connection):  # one side ended: end the other one too
                try:
                    s.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

        # Buffered readers on both sides: either may already hold bytes read past the headers.
        back = threading.Thread(target=pump, args=(far.read1, handler.connection.sendall), daemon=True)
        back.start()
        pump(handler.rfile.read1, sock.sendall)
        back.join(5)
        return None
    finally:
        try:
            sock.close()
        except OSError:
            pass
