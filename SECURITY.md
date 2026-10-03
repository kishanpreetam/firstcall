# Security policy

## Reporting a vulnerability

Please report security issues privately. Don't open a public issue. You can either:

- use GitHub's private reporting: **Security tab → Report a vulnerability**, or
- email **kishanpreetamkommana@gmail.com**.

You'll get an acknowledgement within 3 business days. Reports we especially want:

- **Sandbox escapes:** code run by the agent reading or writing files outside its run folder, starting programs, or reaching the network outside the firewall.
- **Firewall bypasses:** reaching any host that isn't on the allowlist.
- **Credential leaks:** a key appearing in saved traces, console output or published results.
- **Live-key acceptance:** a run starting with a production credential.

## How FirstCall protects the machine and the credentials

The agent writes code, and FirstCall runs that code on your machine. Every layer below is checked by `python -m firstcall selftest`, which runs automatically before every benchmark. If any check fails, the benchmark refuses to start.

| Layer | Guarantee | Enforced by |
|---|---|---|
| Live-key refusal | A run starts only if every credential matches its company's test-mode pattern (for example `sk_test_`). | Harness (fail closed) |
| Filesystem lock | The agent's code can write only inside its own run folder. It can't read files in your home directory (your `.env`, `~/.ssh`, cloud credentials, keychains). | macOS Seatbelt (OS) |
| No other programs | The agent's code can't start processes or shells, even after removing the Python-level guards. | macOS Seatbelt (OS) |
| Egress firewall | The agent's code has no direct internet access and no DNS. Its only way out is a proxy that connects over HTTPS to the company's allowlisted API hosts, and only when they resolve to public addresses. | Seatbelt + proxy in the harness |
| Safe fetching | `fetch_url` accepts only http(s) URLs on public addresses (no localhost, private ranges or cloud metadata), rechecks at every redirect, and refuses URLs that contain a credential. It respects `robots.txt` and waits between requests to the same host. | Harness |
| Minimal environment | The agent's code sees only the company's test credential and the run id. Your Anthropic key is never passed in. | Harness |
| Limits | 90 seconds per script, 60 s of CPU, 50 MB per file, 40 turns per run, and a dollar budget per batch. | Seatbelt, rlimits, harness |
| Redaction | Credentials are scrubbed from every trace before it's written to disk. The agent's scratch folder is deleted after each run. | Harness |

## Known limitations

- **macOS only.** Sandboxing uses `sandbox-exec` (Seatbelt). Apple marks it deprecated, but it still enforces. On other platforms, runs refuse to start.
- **Allowlisted hosts are reachable.** Code could still use the test credential in unintended ways against those hosts. That's why only test-mode keys are accepted.
- **Redaction is limited.** It matches the run's own credential values plus common key formats. A credential that's been transformed (for example, encoded) wouldn't be caught.
- **The MCP condition** sends the test credential to the company's own official MCP server, as that server requires.
