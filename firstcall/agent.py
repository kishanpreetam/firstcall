"""The agent under test: Claude with fetch_url, run_python and done.

One run = one task, one condition, a fresh conversation. The loop is
append-only (the full assistant content goes back every turn) so prompt
caching and thinking blocks stay valid. Every step is recorded so a
failure can be traced to the page or the API error that caused it.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field

import anthropic

from . import fetch
from .registry import Company, Task
from .sandbox import Sandbox

DEFAULT_MODEL = "claude-opus-5-5"
FETCH_CHARS = 30_000  # what one fetch_url call may put into context

# $ per million tokens: (input, output, cache read). Cache writes are billed at 1.25x input.
PRICES = {
    "claude-opus-5-5": (4.00, 20.00, 0.20),
    "claude-sonnet-5-5": (2.00, 10.00, 0.20),
    "claude-haiku-4-5": (1.00, 5.00, 0.10),
}

CONDITIONS = ("docs", "llms", "mcp")

TOOLS = [
    {
        "name": "fetch_url",
        "description": "Fetch a web page, such as a documentation page, and return it as markdown with its links. Long pages are truncated.",
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {"url": {"type": "string", "description": "Absolute http(s) URL."}},
            "required": ["url"],
            "additionalProperties": False,
        },
    },
    {
        "name": "run_python",
        "description": (
            "Run a Python 3 script in a fresh process and return its exit code, stdout and stderr. "
            "The `requests` package is installed. Files written to the working directory persist between calls. "
            "Credentials are available through os.environ."
        ),
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {"code": {"type": "string", "description": "The complete script to run."}},
            "required": ["code"],
            "additionalProperties": False,
        },
    },
    {
        "name": "done",
        "description": "Finish the task. Call exactly once, whether you succeeded or got stuck.",
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "summary": {"type": "string", "description": "What you did, or what blocked you."},
                "created_ids": {"type": "array", "items": {"type": "string"}, "description": "IDs of objects you created."},
            },
            "required": ["summary", "created_ids"],
            "additionalProperties": False,
        },
    },
]

SYSTEM = """You are a software engineer integrating with {name}'s API for the first time. \
Complete the task end to end against the real API, then call `done`.

Environment:
- Test-mode credentials are in these environment variables: {env}. Read them with os.environ inside your code. Never print them.
- `run_python` runs a Python 3 script in a fresh process. `requests` is installed; install nothing else.
- `fetch_url` returns a web page as markdown.
{condition}
Rules:
- Use {name}'s official documentation as your reference.
- Tag what you create exactly as the task says. The result is checked by reading the API afterwards.
- Call `done` once at the end with a short summary and the IDs you created, including when you could not finish."""

CONDITION_TEXT = {
    "docs": "- Documentation starts at {docs}.\n",
    "llms": "- Documentation starts at {docs}. {name} publishes an llms.txt index of its docs; it is included with the task.\n",
    "mcp": "- Documentation starts at {docs}. You are also connected to {name}'s official MCP server, and its tools are available to you.\n",
}


@dataclass
class Step:
    turn: int
    kind: str  # thinking | text | tool_call | tool_result | mcp_call | mcp_result | refusal | note
    name: str = ""
    content: str = ""
    meta: dict = field(default_factory=dict)


@dataclass
class RunTrace:
    run_id: str
    company: str
    task: str
    condition: str
    model: str
    effort: str
    steps: list[Step] = field(default_factory=list)
    turns: int = 0
    finished: bool = False  # the agent called done
    done_summary: str = ""
    created_ids: list[str] = field(default_factory=list)
    stop: str = ""  # why the loop ended
    served_by: set[str] = field(default_factory=set)
    fallback_used: bool = False
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    pages_fetched: list[str] = field(default_factory=list)
    python_calls: int = 0
    mcp_calls: int = 0
    seconds: float = 0.0

    @property
    def cost_usd(self) -> float:
        p_in, p_out, p_read = PRICES.get(self.model, PRICES[DEFAULT_MODEL])
        return round(
            (self.input_tokens * p_in + self.cache_write_tokens * p_in * 1.25 + self.cache_read_tokens * p_read + self.output_tokens * p_out) / 1e6,
            4,
        )

    def to_dict(self) -> dict:
        d = asdict(self)
        d["served_by"] = sorted(self.served_by)
        d["cost_usd"] = self.cost_usd
        return d


def run_agent(
    client: anthropic.Anthropic,
    company: Company,
    task: Task,
    condition: str,
    run_id: str,
    sandbox: Sandbox,
    *,
    model: str = DEFAULT_MODEL,
    effort: str = "high",
    max_turns: int = 40,
    mcp_token: str | None = None,
    mcp_url: str | None = None,
) -> RunTrace:
    trace = RunTrace(run_id=run_id, company=company.key, task=task.id, condition=condition, model=model, effort=effort)
    started = time.monotonic()

    system = SYSTEM.format(
        name=company.name,
        env=", ".join(company.env) or "(none)",
        condition=CONDITION_TEXT[condition].format(docs=company.docs, name=company.name),
    )
    first_user: list[dict] = []
    if condition == "llms":
        llms = fetch.get(company.llms_txt) if company.llms_txt else None
        if llms is None or not llms.ok:
            raise RuntimeError(f"{company.key}: llms.txt unavailable, cannot run the llms condition")
        first_user.append({"type": "text", "text": f"<llms_txt url=\"{llms.final_url}\">\n{llms.text[:200_000]}\n</llms_txt>"})
    first_user.append({"type": "text", "text": "Task:\n" + task.prompt.format(run_id=run_id).strip()})
    messages: list[dict] = [{"role": "user", "content": first_user}]

    tools: list[dict] = list(TOOLS)
    betas = ["server-side-fallback-2026-07-01"]
    extra: dict = {}
    if condition == "mcp":
        if not (mcp_url and mcp_token):
            raise RuntimeError(f"{company.key}: the mcp condition needs an MCP URL and token")
        betas.append("mcp-client-2025-11-20")
        extra["mcp_servers"] = [{"type": "url", "url": mcp_url, "name": company.key, "authorization_token": mcp_token}]
        tools.append({"type": "mcp_toolset", "mcp_server_name": company.key})

    for turn in range(1, max_turns + 1):
        trace.turns = turn
        try:
            resp = client.beta.messages.create(
                model=model,
                max_tokens=16000,
                system=system,
                tools=tools,
                messages=messages,
                betas=betas,
                thinking={"type": "adaptive", "display": "summarized"},
                output_config={"effort": effort},
                cache_control={"type": "ephemeral"},
                extra_body={"fallbacks": "default"},
                **extra,
            )
        except anthropic.BadRequestError as exc:
            trace.stop = f"api_error: {exc.message}"
            break

        _record_usage(trace, resp)
        _record_content(trace, turn, resp.content)

        if resp.stop_reason == "refusal":
            category = getattr(resp.stop_details, "category", None) if resp.stop_details else None
            trace.steps.append(Step(turn, "refusal", content=str(category)))
            trace.stop = "refusal"
            break

        messages.append({"role": "assistant", "content": resp.content})

        if resp.stop_reason == "pause_turn":  # a long server-side MCP turn; resend to let it continue
            continue
        if resp.stop_reason == "max_tokens":
            trace.stop = "max_tokens"
            break

        tool_uses = [b for b in resp.content if b.type == "tool_use"]
        if not tool_uses:
            trace.stop = "ended_without_done"
            break

        results = []
        for block in tool_uses:
            output, is_error = _execute(block.name, block.input, trace, turn, sandbox)
            results.append({"type": "tool_result", "tool_use_id": block.id, "content": output, "is_error": is_error})
            if block.name == "done":
                trace.finished = True
        messages.append({"role": "user", "content": results})
        if trace.finished:
            trace.stop = "done"
            break
    else:
        trace.stop = "max_turns"

    trace.seconds = round(time.monotonic() - started, 1)
    return trace


def _execute(name: str, args: dict, trace: RunTrace, turn: int, sandbox: Sandbox) -> tuple[str, bool]:
    trace.steps.append(Step(turn, "tool_call", name=name, content=args.get("code") or args.get("url") or args.get("summary", "")))
    if name == "fetch_url":
        page = fetch.get(args["url"])
        trace.pages_fetched.append(args["url"])
        if page.error or not page.ok:
            out = f"Fetch failed: {page.error or f'HTTP {page.status}'}"
            trace.steps.append(Step(turn, "tool_result", name=name, content=out, meta={"status": page.status}))
            return out, True
        body = page.text
        if len(body) > FETCH_CHARS:
            body = body[:FETCH_CHARS] + f"\n\n[Truncated: page is {len(page.text)} chars; showing the first {FETCH_CHARS}.]"
        out = f"URL: {page.final_url}\n\n{body}"
        trace.steps.append(Step(turn, "tool_result", name=name, content=out[:2000], meta={"status": page.status, "chars": len(page.text)}))
        return out, False
    if name == "run_python":
        trace.python_calls += 1
        result = sandbox.run(args["code"])
        out = result.render()
        trace.steps.append(Step(turn, "tool_result", name=name, content=out, meta={"exit_code": result.exit_code, "seconds": result.seconds}))
        return out, False
    if name == "done":
        trace.done_summary = args.get("summary", "")
        trace.created_ids = list(args.get("created_ids", []))
        return "Recorded. The run is over.", False
    return f"Unknown tool {name}", True


def _record_usage(trace: RunTrace, resp) -> None:
    u = resp.usage
    trace.input_tokens += u.input_tokens or 0
    trace.output_tokens += u.output_tokens or 0
    trace.cache_read_tokens += getattr(u, "cache_read_input_tokens", 0) or 0
    trace.cache_write_tokens += getattr(u, "cache_creation_input_tokens", 0) or 0
    trace.served_by.add(resp.model)
    if any(getattr(it, "type", "") == "fallback_message" for it in (getattr(u, "iterations", None) or [])):
        trace.fallback_used = True


def _record_content(trace: RunTrace, turn: int, content) -> None:
    for block in content:
        kind = block.type
        if kind == "thinking" and getattr(block, "thinking", ""):
            trace.steps.append(Step(turn, "thinking", content=block.thinking))
        elif kind == "text" and block.text.strip():
            trace.steps.append(Step(turn, "text", content=block.text))
        elif kind == "mcp_tool_use":
            trace.mcp_calls += 1
            trace.steps.append(Step(turn, "mcp_call", name=block.name, content=str(block.input)[:4000]))
        elif kind == "mcp_tool_result":
            if isinstance(block.content, str):
                text = block.content
            else:
                text = " ".join(getattr(c, "text", "") for c in (block.content or []) if getattr(c, "type", "") == "text")
            trace.steps.append(Step(turn, "mcp_result", content=text[:4000], meta={"is_error": bool(getattr(block, "is_error", False))}))
        elif kind == "fallback":
            trace.fallback_used = True
            trace.steps.append(Step(turn, "note", content="fallback model took over"))
