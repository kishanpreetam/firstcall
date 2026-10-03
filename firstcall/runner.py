"""Runs (company x task x condition x repetition) and stores one JSON per run."""

from __future__ import annotations

import json
import os
import secrets
import statistics
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from threading import Lock

import anthropic

from .agent import DEFAULT_MODEL, run_agent
from .registry import Company, Task
from .sandbox import Sandbox
from .verify import get_verifier

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / "results"


def load_env(path: Path = ROOT / ".env") -> dict[str, str]:
    """os.environ overlaid with KEY=VALUE lines from .env (the file wins)."""
    env = dict(os.environ)
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            env[key.strip()] = value.strip().strip("'\"")
    return env


@dataclass
class Job:
    company: Company
    task: Task
    condition: str


class BudgetExceeded(Exception):
    pass


def run_jobs(
    jobs: list[Job],
    *,
    model: str = DEFAULT_MODEL,
    effort: str = "high",
    max_turns: int = 40,
    parallel: int = 1,
    budget_usd: float = 5.0,
) -> list[dict]:
    env = load_env()
    if "ANTHROPIC_API_KEY" in env:
        os.environ.setdefault("ANTHROPIC_API_KEY", env["ANTHROPIC_API_KEY"])
    client = anthropic.Anthropic(max_retries=4)

    spent = 0.0
    lock = Lock()
    results: list[dict] = []

    def one(job: Job) -> dict:
        nonlocal spent
        with lock:
            if spent >= budget_usd:
                raise BudgetExceeded(f"budget of ${budget_usd:.2f} reached")
        missing = [k for k in job.company.env if not env.get(k)]
        if missing:
            raise RuntimeError(f"{job.company.key}: set {', '.join(missing)} in .env")

        run_id = "r" + secrets.token_hex(4)
        out_dir = RESULTS / "runs" / job.company.key / job.task.id / job.condition
        work = RESULTS / "work" / run_id
        sandbox = Sandbox(work, {k: env[k] for k in job.company.env} | {"FIRSTCALL_RUN_ID": run_id})

        mcp_token = env.get(job.company.mcp_auth_env) if job.company.mcp_auth_env else None
        mcp_url = job.company.mcp[0] if job.company.mcp else None
        trace = run_agent(
            client, job.company, job.task, job.condition, run_id, sandbox,
            model=model, effort=effort, max_turns=max_turns, mcp_token=mcp_token, mcp_url=mcp_url,
        )

        verifier = get_verifier(job.company.key, job.task.verifier)
        try:
            passed, checks, notes = verifier(run_id, env)
        except Exception as exc:  # a verifier crash is a harness bug, not an agent failure
            passed, checks, notes = False, {}, {"verifier_error": f"{type(exc).__name__}: {exc}"}

        record = trace.to_dict() | {"passed": passed, "checks": checks, "verify_notes": notes}
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / f"{run_id}.json").write_text(json.dumps(record, indent=2, default=str))
        with lock:
            spent += trace.cost_usd
        return record

    with ThreadPoolExecutor(max_workers=parallel) as pool:
        futures = {pool.submit(one, job): job for job in jobs}
        for fut in as_completed(futures):
            job = futures[fut]
            try:
                rec = fut.result()
            except BudgetExceeded as exc:
                print(f"skipped {job.company.key}/{job.task.id}/{job.condition}: {exc}")
                continue
            results.append(rec)
            mark = "PASS" if rec["passed"] else "FAIL"
            failed = [k for k, v in rec["checks"].items() if not v]
            print(
                f"{mark} {rec['company']}/{rec['task']}/{rec['condition']} {rec['run_id']}  "
                f"turns={rec['turns']} stop={rec['stop']} ${rec['cost_usd']:.3f} {rec['seconds']}s"
                + (f"  failed checks: {', '.join(failed)}" if failed else "")
            )
    print(f"spent ${spent:.2f} of ${budget_usd:.2f} budget")
    return results


def summarize() -> list[dict]:
    """Aggregate every stored run into per (company, task, condition) rows."""
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for path in (RESULTS / "runs").glob("*/*/*/*.json"):
        rec = json.loads(path.read_text())
        groups[(rec["company"], rec["task"], rec["condition"])].append(rec)

    rows = []
    for (company, task, condition), recs in sorted(groups.items()):
        clean = [r for r in recs if not r.get("fallback_used")]
        rows.append({
            "company": company,
            "task": task,
            "condition": condition,
            "runs": len(clean),
            "passed": sum(r["passed"] for r in clean),
            "pass_rate": round(sum(r["passed"] for r in clean) / len(clean), 3) if clean else None,
            "median_turns": statistics.median(r["turns"] for r in clean) if clean else None,
            "median_cost_usd": round(statistics.median(r["cost_usd"] for r in clean), 4) if clean else None,
            "median_seconds": statistics.median(r["seconds"] for r in clean) if clean else None,
            "excluded_fallback_runs": len(recs) - len(clean),
        })
    RESULTS.mkdir(exist_ok=True)
    (RESULTS / "summary.json").write_text(json.dumps(rows, indent=2))
    return rows
