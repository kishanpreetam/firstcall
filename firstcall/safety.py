"""Guards that every run goes through.

- Live-key refusal: a run starts only if every credential matches its
  company's test-mode pattern.
- URL checks for fetch_url: http(s) only, public addresses only (no
  localhost, private ranges or cloud metadata), and no URL that carries a
  credential.
- robots.txt and a per-host delay, so docs sites are fetched politely.
- Redaction: credentials are scrubbed from anything written to disk.
"""

from __future__ import annotations

import ipaddress
import re
import socket
import threading
import time
from urllib import robotparser
from urllib.parse import unquote, urlparse

import requests

ROBOTS_AGENT = "FirstCall"
MIN_SECONDS_BETWEEN_FETCHES = 1.0

# Backstop patterns for credentials that aren't in the run's own env.
SECRET_PATTERNS = [
    re.compile(p)
    for p in (
        r"sk-ant-[A-Za-z0-9_\-]{20,}",
        r"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{10,}",
        r"\bwhsec_[A-Za-z0-9]{10,}",
        r"\bgh[pousr]_[A-Za-z0-9]{30,}",
        r"\bgithub_pat_[A-Za-z0-9_]{30,}",
        r"\bAKIA[0-9A-Z]{16}\b",
        r"\bAIza[0-9A-Za-z_\-]{35}",
        r"\bxox[baprs]-[A-Za-z0-9-]{10,}",
        r"\beyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}",
    )
]


class UnsafeConfig(Exception):
    """Raised when a run would be unsafe to start. Runs fail closed."""


def check_test_keys(company_key: str, patterns: dict[str, str], env: dict[str, str], required: list[str]) -> None:
    for var in required:
        pattern = patterns.get(var)
        if pattern is None:
            raise UnsafeConfig(f"{company_key}: no test-key pattern for {var}; refusing to run with an unchecked credential")
        if not re.match(pattern, env.get(var, "")):
            raise UnsafeConfig(f"{company_key}: {var} doesn't look like a test-mode credential (must match {pattern}); refusing to run")


class Redactor:
    def __init__(self, secrets: dict[str, str]):
        self.secrets = {name: value for name, value in secrets.items() if value and len(value) >= 8}

    def text(self, s: str) -> str:
        for name, value in self.secrets.items():
            if value in s:
                s = s.replace(value, f"[REDACTED:{name}]")
        for rx in SECRET_PATTERNS:
            s = rx.sub("[REDACTED]", s)
        return s

    def obj(self, o):
        if isinstance(o, str):
            return self.text(o)
        if isinstance(o, dict):
            return {k: self.obj(v) for k, v in o.items()}
        if isinstance(o, (list, tuple)):
            return [self.obj(v) for v in o]
        return o


def check_url(url: str, secret_values: list[str] | tuple[str, ...] = ()) -> str | None:
    """Return why a URL must not be fetched, or None if it's fine."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return "only http and https URLs can be fetched"
    if not parsed.hostname:
        return "the URL has no host"
    decoded = unquote(url)
    for value in secret_values:
        if value and len(value) >= 8 and (value in url or value in decoded):
            return "the URL contains a credential"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        infos = socket.getaddrinfo(parsed.hostname, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        return f"{parsed.hostname} does not resolve"
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if ip.version == 6 and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        if not ip.is_global or ip.is_multicast:
            return f"{parsed.hostname} resolves to a non-public address ({ip})"
    return None


class _Robots:
    def __init__(self) -> None:
        self._cache: dict[str, robotparser.RobotFileParser | None] = {}
        self._lock = threading.Lock()

    def allowed(self, url: str, user_agent: str) -> bool:
        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        with self._lock:
            if origin not in self._cache:
                self._cache[origin] = self._load(origin, user_agent)
            rp = self._cache[origin]
        return True if rp is None else rp.can_fetch(ROBOTS_AGENT, url)

    @staticmethod
    def _load(origin: str, user_agent: str) -> robotparser.RobotFileParser | None:
        try:
            resp = requests.get(origin + "/robots.txt", headers={"User-Agent": user_agent}, timeout=10, allow_redirects=True)
        except requests.RequestException:
            return None
        if resp.status_code != 200:
            return None
        rp = robotparser.RobotFileParser()
        rp.parse(resp.text.splitlines())
        return rp


class _Throttle:
    def __init__(self, min_interval: float) -> None:
        self.min_interval = min_interval
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()

    def wait(self, url: str) -> None:
        host = urlparse(url).netloc
        with self._lock:
            now = time.monotonic()
            ready_at = self._last.get(host, 0.0) + self.min_interval
            self._last[host] = max(now, ready_at)
        if ready_at > now:
            time.sleep(ready_at - now)


robots = _Robots()
throttle = _Throttle(MIN_SECONDS_BETWEEN_FETCHES)
