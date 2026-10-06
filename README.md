# FirstCall

**Can AI coding agents actually integrate your API?**

Plenty of tools now score how "agent-ready" a company's docs look: whether it has an `llms.txt` file, markdown pages, a discoverable MCP server. FirstCall measures the outcome instead.

FirstCall gives an AI agent a real integration task against a company's real API (in test mode) and checks the result by reading the API afterwards. Each task runs under three conditions:

| Condition | What the agent gets |
|---|---|
| `docs` | The docs URL. It reads pages and writes code the way a developer would. |
| `llms` | The docs URL plus the company's `llms.txt` index. |
| `mcp` | The docs URL plus the company's official MCP server. |

Every run is traced: the pages the agent read, the code it ran, the API errors it hit, and its tokens, cost and time. When an agent fails, the trace shows which doc page or error sent it the wrong way.

> Status: v0.1. The harness is done, and tasks are written for Stripe and HubSpot. Static readiness signals are collected for 18 companies. The first agent results are next.

## Quick start

```bash
python3 -m venv .venv && .venv/bin/pip install anthropic requests beautifulsoup4 markdownify pyyaml
cp .env.example .env          # ANTHROPIC_API_KEY + test-mode company keys

.venv/bin/python -m firstcall selftest                    # proves every safety guard works
.venv/bin/python -m firstcall static                      # readiness signals, no keys needed
.venv/bin/python -m firstcall run stripe --task customer --reps 1 --budget 1
.venv/bin/python -m firstcall report
```

## Method

- **Agent:** Claude Opus 5.5 (`claude-opus-5-5`, effort `high`, adaptive thinking). It has three tools:
  - `fetch_url`: pages come back as markdown with their links.
  - `run_python`: a fresh process with `requests` and only the test credentials.
  - `done`: ends the run.

  No web search, so the agent works from the company's docs alone. Each run is a fresh conversation, capped at 40 turns.
- **Tasks:** three per company, written as first integrations of increasing difficulty.
  - Stripe:
    - create a tagged customer;
    - set up a product, a monthly price and an active subscription with its first invoice paid;
    - charge with a PaymentIntent, then partially refund it.
  - HubSpot:
    - create a contact;
    - create a company and associate a contact with it;
    - create a deal in the default pipeline, associate a contact, and move the deal to Closed won.
- **Verification:** every task tags what it creates with a run id. A verifier then queries the company's API and checks each requirement. The agent's own summary is never trusted.
- **Fallbacks:** requests opt into server-side refusal fallbacks. Any run where another model took over is excluded from pass rates and counted separately.
- **Static signals:** `python -m firstcall static` records:
  - whether `llms.txt` and `llms-full.txt` exist, and their size;
  - whether pages are served as markdown;
  - whether the docs homepage needs JavaScript to show its content;
  - whether `robots.txt` blocks AI agents;
  - whether an MCP endpoint answers.

## Safety

The agent writes code, and FirstCall runs that code on your machine, so the harness is locked down in layers. Before every batch it proves each layer works with `python -m firstcall selftest`, and it refuses to start if anything fails.

- **Live keys are refused.** Credentials must match the company's test-mode pattern, for example `sk_test_`. Some companies' keys don't show test mode; for those, FirstCall asks the company's API before every run and refuses anything but a test account. For example, it refuses any HubSpot account that isn't `DEVELOPER_TEST` or `SANDBOX`.
- **OS sandbox (macOS Seatbelt).** The agent's code can only write inside its own run folder, can't read your home directory (`.env`, `~/.ssh`, cloud credentials), and can't start programs.
- **Egress firewall.** There's no direct internet access and no DNS. The only way out is a proxy that allows HTTPS to the company's API hosts, and nothing else.
- **Safe fetching.**
  - No localhost, private networks or cloud metadata addresses.
  - No URLs that carry a credential.
  - `robots.txt` is respected, with a delay between requests to the same host.
- **Redaction.** Credentials are scrubbed from every saved trace.
- **Limits:**
  - 90 seconds per script;
  - 60 seconds of CPU and 50 MB per file;
  - 40 turns per run;
  - a dollar budget per batch.

Details and known limitations are in [SECURITY.md](SECURITY.md). How runs behave and how results get published (companies see their results first, corrections, no affiliation) is in [RESPONSIBLE_USE.md](RESPONSIBLE_USE.md).

## License

MIT. See [LICENSE](LICENSE).

## Related work

- **Static scores:**
  - Agent readiness: [Mintlify Agent Score](https://www.mintlify.com/blog/agent-score), [Fern Agent Score](https://buildwithfern.com/agent-score), [AFDocs](https://afdocs.dev).
  - API specs: [Jentic](https://jentic.com/blog/api-scoring-tools).
  - Browsing: [Lighthouse agentic browsing](https://www.debugbear.com/blog/lighthouse-agentic-browsing).
- **Agent task benchmarks:** [MCPMark](https://arxiv.org/abs/2509.24002), [MCP-Bench](https://arxiv.org/abs/2508.20453), [Toolathlon](https://arxiv.org/abs/2510.25726).
- **Closest prior work:**
  - [ax-eval](https://github.com/chenmingtang830/ax-eval) runs coding agents against products, with sample reports.
  - FirstCall adds public results for each company, three conditions, and a comparison against static scores.
- **Docs delivery and tool descriptions:** [Vercel's AGENTS.md evals](https://vercel.com/blog/agents-md-outperforms-skills-in-our-agent-evals), [Tool Descriptions Are Smelly](https://arxiv.org/abs/2602.14878).

---

Built by [Kishan Kommana](https://linkedin.com/in/kishan-preetam-kommana).
