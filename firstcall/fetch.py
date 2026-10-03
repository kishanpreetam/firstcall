"""HTTP fetching shared by the agent's fetch_url tool and the static checks.

Pages come back as markdown with absolute links, because an agent reading
docs needs the links to navigate. Every request goes through the safety
guards in safety.py:
- public addresses only, checked again at every redirect;
- no URLs that carry credentials;
- robots.txt respected;
- a per-host delay.

Nothing is cached: the benchmark should see the docs the way a cold agent does.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from markdownify import markdownify

from . import safety

USER_AGENT = "FirstCall/0.1 (AI agent integration benchmark; +https://github.com/kishanpreetam/firstcall)"
TIMEOUT_S = 20
MAX_BYTES = 5_000_000  # refuse to buffer anything larger than this
MAX_REDIRECTS = 5


@dataclass
class Page:
    url: str
    final_url: str
    status: int | None
    content_type: str = ""
    raw_bytes: int = 0
    text: str = ""  # markdown for HTML, raw body for text formats
    links: list[str] = field(default_factory=list)
    error: str | None = None
    blocked: bool = False  # refused by a safety guard, not a network failure

    @property
    def ok(self) -> bool:
        return self.status is not None and 200 <= self.status < 300

    @property
    def is_html(self) -> bool:
        return "html" in self.content_type


def get(url: str, accept: str | None = None, *, secrets: tuple[str, ...] = (), respect_robots: bool = True) -> Page:
    headers = {"User-Agent": USER_AGENT}
    if accept:
        headers["Accept"] = accept

    current = url
    for _ in range(MAX_REDIRECTS + 1):
        reason = safety.check_url(current, secrets)
        if reason is None and respect_robots and not safety.robots.allowed(current, USER_AGENT):
            reason = "robots.txt disallows this URL"
        if reason:
            return Page(url=url, final_url=current, status=None, error=f"Blocked: {reason}", blocked=True)
        safety.throttle.wait(current)
        try:
            resp = requests.get(current, headers=headers, timeout=TIMEOUT_S, stream=True, allow_redirects=False)
        except requests.RequestException as exc:
            return Page(url=url, final_url=current, status=None, error=f"{type(exc).__name__}: {exc}")
        if resp.is_redirect and resp.headers.get("location"):
            current = urljoin(current, resp.headers["location"])
            resp.close()
            continue
        break
    else:
        return Page(url=url, final_url=current, status=None, error="too many redirects")

    body = resp.raw.read(MAX_BYTES + 1, decode_content=True)
    content_type = resp.headers.get("content-type", "").lower()
    page = Page(url=url, final_url=current, status=resp.status_code, content_type=content_type, raw_bytes=len(body))
    decoded = body[:MAX_BYTES].decode(resp.encoding or "utf-8", errors="replace")

    if "html" in content_type:
        soup = BeautifulSoup(decoded, "html.parser")
        for tag in soup(["script", "style", "noscript", "svg", "iframe"]):
            tag.decompose()
        # Absolute links, so the agent can fetch whatever it reads without resolving paths itself.
        for a in soup.find_all("a", href=True):
            a["href"] = urljoin(current, a["href"])
        page.links = sorted({a["href"].split("#")[0] for a in soup.find_all("a", href=True)})
        main = soup.find("main") or soup.find("article") or soup.body or soup
        page.text = _squeeze(markdownify(str(main), heading_style="ATX"))
    else:
        page.text = decoded
    return page


def same_site(url: str, base: str) -> bool:
    a, b = urlparse(url).netloc, urlparse(base).netloc
    return a == b or a.endswith("." + _registrable(b)) or b.endswith("." + _registrable(a))


def _registrable(netloc: str) -> str:
    parts = netloc.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else netloc


def _squeeze(text: str) -> str:
    lines = [ln.rstrip() for ln in text.splitlines()]
    out, blank = [], False
    for ln in lines:
        if not ln.strip():
            if not blank:
                out.append("")
            blank = True
        else:
            out.append(ln)
            blank = False
    return "\n".join(out).strip()
