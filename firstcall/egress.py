"""Egress firewall for the sandbox.

The Seatbelt profile only lets the agent's code open a connection to this
proxy on localhost. The proxy will only tunnel HTTPS (CONNECT) to the run's
allowlisted hosts on port 443, and only when they resolve to public
addresses. Everything else gets a 403 that names the blocked host. The
proxy runs in the harness, outside the sandbox, so code inside cannot
change the allowlist.
"""

from __future__ import annotations

import ipaddress
import select
import socket
import socketserver
import threading

IDLE_TIMEOUT_S = 120
HEAD_LIMIT = 16_384


def host_allowed(host: str, allowed: tuple[str, ...]) -> bool:
    host = host.lower().rstrip(".")
    return any(host == a or host.endswith("." + a) for a in allowed)


class _Handler(socketserver.BaseRequestHandler):
    server: "EgressProxy"

    def handle(self) -> None:
        client: socket.socket = self.request
        client.settimeout(30)
        data = b""
        try:
            while b"\r\n\r\n" not in data and len(data) < HEAD_LIMIT:
                chunk = client.recv(4096)
                if not chunk:
                    return
                data += chunk
        except OSError:
            return
        head, _, rest = data.partition(b"\r\n\r\n")
        parts = head.split(b"\r\n", 1)[0].decode("latin-1").split()
        if len(parts) < 2 or parts[0].upper() != "CONNECT":
            self.server.record_block(parts[1] if len(parts) > 1 else "?", "not an HTTPS tunnel")
            return _deny(client, 405, "FirstCall firewall: only HTTPS is allowed")
        host, _, port = parts[1].rpartition(":")
        host = host.strip("[]").lower()
        if port != "443" or not host_allowed(host, self.server.allowed):
            self.server.record_block(f"{host}:{port}", "not on the allowlist")
            return _deny(client, 403, f"FirstCall firewall: {host}:{port} is not on this run's allowlist ({', '.join(self.server.allowed)})")
        try:
            upstream = socket.create_connection((host, 443), timeout=15)
        except OSError as exc:
            return _deny(client, 502, f"FirstCall firewall: could not reach {host}: {exc}")
        peer = ipaddress.ip_address(upstream.getpeername()[0])
        if not peer.is_global:
            upstream.close()
            self.server.record_block(host, f"resolved to non-public {peer}")
            return _deny(client, 403, f"FirstCall firewall: {host} resolved to a non-public address")
        client.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
        if rest:
            upstream.sendall(rest)
        _relay(client, upstream)


def _deny(client: socket.socket, code: int, message: str) -> None:
    body = message.encode()
    try:
        client.sendall(
            f"HTTP/1.1 {code} Blocked\r\nContent-Type: text/plain\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode() + body
        )
    except OSError:
        pass


def _relay(a: socket.socket, b: socket.socket) -> None:
    a.setblocking(False)
    b.setblocking(False)
    try:
        while True:
            readable, _, broken = select.select([a, b], [], [a, b], IDLE_TIMEOUT_S)
            if broken or not readable:
                return
            for src in readable:
                dst = b if src is a else a
                data = src.recv(65536)
                if not data:
                    return
                dst.setblocking(True)
                dst.sendall(data)
                dst.setblocking(False)
    except OSError:
        return
    finally:
        for s in (a, b):
            try:
                s.close()
            except OSError:
                pass


class EgressProxy(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, allowed_hosts: list[str]):
        super().__init__(("127.0.0.1", 0), _Handler)
        self.allowed = tuple(h.lower().lstrip("*.").rstrip(".") for h in allowed_hosts)
        self.blocked: list[dict] = []
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self.serve_forever, daemon=True)
        self._thread.start()

    @property
    def port(self) -> int:
        return self.server_address[1]

    def record_block(self, target: str, reason: str) -> None:
        with self._lock:
            self.blocked.append({"target": target, "reason": reason})

    def close(self) -> None:
        self.shutdown()
        self.server_close()
