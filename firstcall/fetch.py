"""HTTP fetching shared by the agent's fetch_url tool and the static checks.

Pages come back as markdown with links preserved, because an agent reading
docs needs the links to navigate. Nothing here retries or caches: the
benchmark should see the docs the way a cold agent does.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from markdownify import markdownify

USER_AGENT = "FirstCall/0.1 (AI agent integration benchmark; +https://github.com/kishanpreetam/firstcall)"
TIMEOUT_S = 20
MAX_BYTES = 5_000_000  # refuse to buffer anything larger than this


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

    @property
    def ok(self) -> bool:
        return self.status is not None and 200 <= self.status < 300

    @property
    def is_html(self) -> bool:
        return "html" in self.content_type


def get(url: str, accept: str | None = None) -> Page:
    headers = {"User-Agent": USER_AGENT}
    if accept:
        headers["Accept"] = accept
    try:
        resp = requests.get(url, headers=headers, timeout=TIMEOUT_S, stream=True, allow_redirects=True)
        body = resp.raw.read(MAX_BYTES + 1, decode_content=True)
    except requests.RequestException as exc:
        return Page(url=url, final_url=url, status=None, error=f"{type(exc).__name__}: {exc}")

    content_type = resp.headers.get("content-type", "").lower()
    page = Page(
        url=url,
        final_url=resp.url,
        status=resp.status_code,
        content_type=content_type,
        raw_bytes=len(body),
    )
    encoding = resp.encoding or "utf-8"
    decoded = body[:MAX_BYTES].decode(encoding, errors="replace")

    if "html" in content_type:
        soup = BeautifulSoup(decoded, "html.parser")
        for tag in soup(["script", "style", "noscript", "svg", "iframe"]):
            tag.decompose()
        # Absolute links, so the agent can fetch whatever it reads without resolving paths itself.
        for a in soup.find_all("a", href=True):
            a["href"] = urljoin(resp.url, a["href"])
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
