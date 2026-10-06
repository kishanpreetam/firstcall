"""Static "agent-readiness" signals for each company's docs.

These are the kind of checks existing agent-readiness scores are built from
(llms.txt, markdown docs, robots rules, MCP discoverability). FirstCall
records them so they can be compared with what agents actually achieve.
"""

from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import urlparse

import requests

from . import fetch
from .registry import Company, load_companies

AI_AGENTS = ["ClaudeBot", "Claude-User", "anthropic-ai", "GPTBot", "ChatGPT-User"]
# A server-rendered docs page has far more text than this; a client-rendered
# shell has almost none.
JS_SHELL_TEXT_CHARS = 400


@dataclass
class StaticReport:
    company: str
    docs_status: int | None = None
    docs_html_kb: float | None = None
    docs_text_chars: int | None = None
    docs_js_shell: bool | None = None
    llms_txt_url: str | None = None
    llms_txt_kb: float | None = None
    llms_txt_links: int | None = None
    llms_full_txt_kb: float | None = None
    markdown_variant: str | None = None  # how a docs page was obtainable as markdown, if at all
    robots_blocks: list[str] = field(default_factory=list)  # AI agents disallowed from the docs path
    openapi_hint: str | None = None
    mcp_url: str | None = None
    mcp_probe: str | None = None
    notes: list[str] = field(default_factory=list)


def check(company: Company) -> StaticReport:
    r = StaticReport(company=company.key)
    docs = fetch.get(company.docs)
    r.docs_status = docs.status
    if not docs.ok:
        r.notes.append(f"docs root returned {docs.status or docs.error}")
    else:
        r.docs_html_kb = round(docs.raw_bytes / 1024, 1)
        r.docs_text_chars = len(docs.text)
        r.docs_js_shell = docs.is_html and len(docs.text) < JS_SHELL_TEXT_CHARS

    _check_llms_txt(company, docs, r)
    _check_markdown(company, docs, r)
    _check_robots(company, r)
    _check_mcp(company, r)
    return r


def _check_llms_txt(company: Company, docs: fetch.Page, r: StaticReport) -> None:
    roots = _candidate_roots(company.docs, docs.final_url if docs.ok else None)
    for root in roots:
        page = fetch.get(root + "/llms.txt")
        if page.ok and not page.is_html and page.text.strip():
            r.llms_txt_url = page.final_url
            r.llms_txt_kb = round(page.raw_bytes / 1024, 1)
            r.llms_txt_links = len(re.findall(r"\]\((https?://[^)]+)\)", page.text))
            if r.openapi_hint is None:
                m = re.search(r"https?://[^\s)]+(?:openapi|swagger)[^\s)]*", page.text, re.I)
                if m:
                    r.openapi_hint = m.group(0)
            full = _head(root + "/llms-full.txt")
            if full is not None:
                r.llms_full_txt_kb = round(full / 1024, 1)
            return


def _check_markdown(company: Company, docs: fetch.Page, r: StaticReport) -> None:
    if not docs.ok:
        return
    sample = _sample_docs_page(company, docs)
    if sample is None:
        return
    for label, url, accept in (
        ("Accept: text/markdown", sample, "text/markdown"),
        (".md suffix", sample.rstrip("/") + ".md", None),
    ):
        page = fetch.get(url, accept=accept)
        if page.ok and ("markdown" in page.content_type or "text/plain" in page.content_type) and page.text.lstrip().startswith(("#", "---", "[")):
            r.markdown_variant = f"{label} ({url})"
            return


def _check_robots(company: Company, r: StaticReport) -> None:
    parsed = urlparse(company.docs)
    robots = fetch.get(f"{parsed.scheme}://{parsed.netloc}/robots.txt")
    if not robots.ok:
        return
    path = parsed.path or "/"
    for agent in AI_AGENTS:
        if _disallowed(robots.text, agent, path):
            r.robots_blocks.append(agent)


def _check_mcp(company: Company, r: StaticReport) -> None:
    for url in company.mcp:
        verdict = _probe_mcp(url)
        if verdict.startswith("found"):
            r.mcp_url, r.mcp_probe = url, verdict
            return
        r.mcp_probe = verdict


def _probe_mcp(url: str) -> str:
    """POST an MCP initialize; any MCP-ish answer (incl. 401 asking for auth) counts as found."""
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "firstcall-probe", "version": "0.1"},
        },
    }
    headers = {
        "User-Agent": fetch.USER_AGENT,
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }
    try:
        resp = requests.post(url, json=body, headers=headers, timeout=fetch.TIMEOUT_S, stream=True)
        status = resp.status_code
        auth = resp.headers.get("www-authenticate", "")
        resp.close()
    except requests.RequestException as exc:
        return f"unreachable ({type(exc).__name__})"
    if status in (200, 202):
        return "found (open)"
    if status in (401, 403):
        return "found (auth required)" + (" - OAuth" if "resource_metadata" in auth or "Bearer" in auth else "")
    if status in (400, 405, 406, 415):
        return f"found? (HTTP {status} to initialize)"
    return f"not found (HTTP {status})"


def _sample_docs_page(company: Company, docs: fetch.Page) -> str | None:
    host = urlparse(docs.final_url).netloc
    candidates = [
        link for link in docs.links
        if urlparse(link).netloc == host
        and urlparse(link).path.count("/") >= 2
        and not re.search(r"\.(png|jpg|svg|pdf|zip|json|xml|txt)$", link)
    ]
    return candidates[len(candidates) // 2] if candidates else None


def _candidate_roots(docs_url: str, final_url: str | None) -> list[str]:
    roots: list[str] = []
    for u in filter(None, [docs_url, final_url]):
        p = urlparse(u)
        base = f"{p.scheme}://{p.netloc}"
        segs = [s for s in p.path.split("/") if s]
        # docs path itself, then its first segment, then the host root
        for depth in range(len(segs), -1, -1):
            cand = base + ("/" + "/".join(segs[:depth]) if depth else "")
            if cand not in roots:
                roots.append(cand)
    return roots


def _head(url: str) -> int | None:
    try:
        resp = requests.get(url, headers={"User-Agent": fetch.USER_AGENT}, timeout=fetch.TIMEOUT_S, stream=True)
        if resp.status_code != 200 or "html" in resp.headers.get("content-type", ""):
            resp.close()
            return None
        size = int(resp.headers.get("content-length") or 0) or len(resp.raw.read(fetch.MAX_BYTES, decode_content=True))
        resp.close()
        return size
    except requests.RequestException:
        return None


def _disallowed(robots: str, agent: str, path: str) -> bool:
    """Minimal robots.txt evaluation: the most specific group for `agent`, longest-match rules."""
    groups: list[tuple[list[str], list[tuple[str, str]]]] = []
    agents: list[str] = []
    rules: list[tuple[str, str]] = []
    for raw in robots.splitlines():
        line = raw.split("#", 1)[0].strip()
        if ":" not in line:
            continue
        key, val = (s.strip() for s in line.split(":", 1))
        key = key.lower()
        if key == "user-agent":
            if rules:
                groups.append((agents, rules))
                agents, rules = [], []
            agents.append(val.lower())
        elif key in ("allow", "disallow"):
            rules.append((key, val))
    if agents:
        groups.append((agents, rules))

    specific = [g for g in groups if agent.lower() in g[0]]
    chosen = specific or [g for g in groups if "*" in g[0]]
    best_len, verdict = -1, False
    for _, group_rules in chosen:
        for kind, pattern in group_rules:
            if pattern and path.startswith(pattern.rstrip("*")) and len(pattern) > best_len:
                best_len, verdict = len(pattern), kind == "disallow"
    return verdict


def run(keys: list[str] | None, out_dir: Path) -> list[StaticReport]:
    registry = load_companies()
    companies = [c for c in registry if not keys or c.key in keys]
    with ThreadPoolExecutor(max_workers=8) as pool:
        reports = list(pool.map(check, companies))
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / "static.json"
    # A partial run updates its companies and keeps everyone else's results.
    merged = {r["company"]: r for r in json.loads(out.read_text())} if keys and out.exists() else {}
    merged.update({r.company: asdict(r) for r in reports})
    order = [c.key for c in registry]
    rows = sorted(merged.values(), key=lambda r: order.index(r["company"]) if r["company"] in order else len(order))
    out.write_text(json.dumps(rows, indent=2))
    return reports
