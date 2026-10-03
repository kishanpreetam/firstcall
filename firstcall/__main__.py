"""CLI.

  python -m firstcall selftest
  python -m firstcall static [company ...]
  python -m firstcall run stripe [--task customer] [--condition docs] [--reps 3] [--budget 5]
  python -m firstcall report
"""

from __future__ import annotations

import argparse
import sys

from .agent import CONDITIONS, DEFAULT_MODEL
from .paths import RESULTS
from .registry import get_company
from .runner import Job, run_jobs, summarize
from .safety import UnsafeConfig
from .selftest import run_selftest
from .static_checks import run as run_static


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="firstcall")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("selftest", help="prove every safety guard works (runs automatically before `run`)")

    p_static = sub.add_parser("static", help="static agent-readiness signals (no API keys needed)")
    p_static.add_argument("companies", nargs="*")

    p_run = sub.add_parser("run", help="run agents against a company's real API (test mode only)")
    p_run.add_argument("company")
    p_run.add_argument("--task", action="append", help="task id (repeatable); default: all")
    p_run.add_argument("--condition", action="append", choices=CONDITIONS, help="default: docs and llms")
    p_run.add_argument("--reps", type=int, default=1)
    p_run.add_argument("--model", default=DEFAULT_MODEL)
    p_run.add_argument("--effort", default="high", choices=["low", "medium", "high", "xhigh", "max"])
    p_run.add_argument("--max-turns", type=int, default=40)
    p_run.add_argument("--parallel", type=int, default=1)
    p_run.add_argument("--budget", type=float, default=5.0, help="stop starting new runs after this many USD")

    sub.add_parser("report", help="aggregate stored runs")

    args = parser.parse_args(argv)

    if args.cmd == "selftest":
        return 0 if run_selftest(verbose=True) else 1

    if args.cmd == "static":
        reports = run_static(args.companies or None, RESULTS)
        for r in reports:
            print(f"{r.company:<11} llms.txt={'yes' if r.llms_txt_url else 'no ':<3} markdown={'yes' if r.markdown_variant else 'no ':<3} "
                  f"js_shell={r.docs_js_shell!s:<5} robots_blocks={','.join(r.robots_blocks) or '-':<10} mcp={r.mcp_probe or '-'}")
        print(f"wrote {RESULTS / 'static.json'}")
        return 0

    if args.cmd == "run":
        company = get_company(args.company)
        if not company.tasks:
            print(f"{company.key} has no tasks yet; add firstcall/tasks/{company.key}.yaml", file=sys.stderr)
            return 2
        tasks = [t for t in company.tasks if not args.task or t.id in args.task]
        conditions = args.condition or ["docs", "llms"]
        jobs = [Job(company, t, c) for t in tasks for c in conditions for _ in range(args.reps)]
        print(f"{len(jobs)} runs: {company.name} x {[t.id for t in tasks]} x {conditions} x {args.reps} reps, model {args.model}")
        try:
            run_jobs(jobs, model=args.model, effort=args.effort, max_turns=args.max_turns,
                     parallel=args.parallel, budget_usd=args.budget)
        except UnsafeConfig as exc:
            print(f"refusing to run: {exc}", file=sys.stderr)
            return 2
        return 0

    if args.cmd == "report":
        rows = summarize()
        print(f"{'company':<10} {'task':<15} {'cond':<5} {'pass':>8} {'turns':>6} {'cost':>8} {'secs':>6}")
        for r in rows:
            print(f"{r['company']:<10} {r['task']:<15} {r['condition']:<5} {r['passed']:>3}/{r['runs']:<4} "
                  f"{r['median_turns']!s:>6} {('$' + format(r['median_cost_usd'], '.3f')) if r['median_cost_usd'] is not None else '-':>8} {r['median_seconds']!s:>6}")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
