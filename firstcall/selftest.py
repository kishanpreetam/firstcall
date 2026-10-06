"""Proves each safety guard works before any run.

`python -m firstcall run` runs this first and refuses to start if any check
fails. `python -m firstcall selftest` prints the full table.
"""

from __future__ import annotations

import json
import secrets
import shutil

from .paths import RESULTS, ROOT
from .safety import Redactor, UnsafeConfig, check_test_keys, check_url
from .sandbox import Sandbox, seatbelt_available

FAKE_KEY = "sk_test_SELFTEST" + "0123456789abcdef"

# Runs inside the sandbox. Each probe records whether the action was blocked.
# The last group first strips the Python-level guard, the way hostile code
# would, to prove the OS-level layers hold on their own.
SANDBOX_PROBES = r'''
import importlib, json, os, resource, socket, subprocess
import requests
CANARY, OUTSIDE = {canary!r}, {outside!r}
out = {{}}
def probe(name, fn):
    try:
        fn(); out[name] = "allowed"
    except Exception as e:
        out[name] = "blocked:" + type(e).__name__
probe("write_outside_workdir", lambda: open(OUTSIDE, "w").write("x"))
probe("read_file_outside_workdir", lambda: open(CANARY).read())
probe("start_program", lambda: subprocess.run(["/bin/echo", "hi"]))
# Deliberate: a fixed string (no input) to prove the sandbox blocks shell calls.
probe("os_system", lambda: os.system("echo hi"))
probe("https_to_disallowed_host", lambda: requests.get("https://example.com", timeout=10).raise_for_status())
probe("plain_http", lambda: requests.get("http://example.com", timeout=10).raise_for_status())
probe("connect_localhost_service", lambda: socket.create_connection(("127.0.0.1", 8000), timeout=5))
probe("dns_lookup", lambda: socket.getaddrinfo("example.com", 443))
probe("https_to_allowed_host", lambda: requests.get("https://api.stripe.com/healthcheck", timeout=15).raise_for_status())
importlib.reload(subprocess); importlib.reload(socket)
probe("bypass_direct_https", lambda: socket.create_connection(("1.1.1.1", 443), timeout=5).close())
probe("bypass_start_program", lambda: subprocess.run(["/bin/echo", "hi"], check=True))
out["env_has_anthropic_key"] = "ANTHROPIC_API_KEY" in os.environ
out["cpu_limit"] = resource.getrlimit(resource.RLIMIT_CPU)[1]
print(json.dumps(out))
'''

EXPECT_BLOCKED = [
    "write_outside_workdir", "read_file_outside_workdir", "start_program", "os_system",
    "https_to_disallowed_host", "plain_http", "connect_localhost_service", "dns_lookup",
    "bypass_direct_https", "bypass_start_program",
]


def run_selftest(verbose: bool = True) -> bool:
    results: list[tuple[str, bool, str]] = []

    # Live keys and unchecked credentials are refused.
    pat = {"KEY": r"^(sk|rk)_test_[A-Za-z0-9]+$"}
    try:
        check_test_keys("selftest", pat, {"KEY": "sk_live_abc123"}, ["KEY"])
        results.append(("live key refused", False, "a live key was accepted"))
    except UnsafeConfig:
        results.append(("live key refused", True, ""))
    try:
        check_test_keys("selftest", {}, {"KEY": FAKE_KEY}, ["KEY"])
        results.append(("credential without a test pattern refused", False, "accepted"))
    except UnsafeConfig:
        results.append(("credential without a test pattern refused", True, ""))

    # Companies whose keys don't reveal test mode are checked through their API.
    # The HubSpot guard must refuse a real account, and refuse when it can't tell.
    import requests

    from .verify import hubspot

    real_get = hubspot._get
    try:
        hubspot._get = lambda env, path: {"accountType": "STANDARD"}
        refuses_real = not hubspot.test_account({"HUBSPOT_ACCESS_TOKEN": "x"})[0]
        hubspot._get = lambda env, path: {"accountType": "DEVELOPER_TEST"}
        accepts_test = hubspot.test_account({"HUBSPOT_ACCESS_TOKEN": "x"})[0]

        def unreachable(env, path):
            raise requests.ConnectionError("offline")

        hubspot._get = unreachable
        refuses_unknown = not hubspot.test_account({"HUBSPOT_ACCESS_TOKEN": "x"})[0]
    finally:
        hubspot._get = real_get
    results.append(("HubSpot guard refuses real accounts", refuses_real and accepts_test, ""))
    results.append(("HubSpot guard refuses when it can't confirm", refuses_unknown, ""))

    # Redaction.
    red = Redactor({"STRIPE_SECRET_KEY": FAKE_KEY}).obj({"a": f"key={FAKE_KEY}", "b": ["sk-ant-api03-" + "x" * 30]})
    results.append(("credentials redacted from logs", "SELFTEST" not in json.dumps(red) and "sk-ant" not in json.dumps(red), json.dumps(red)))

    # URL guards for fetch_url.
    for url in ("http://localhost:8080/", "http://127.0.0.1/", "http://169.254.169.254/latest/meta-data/",
                "http://10.0.0.1/", "http://[::1]/", "file:///etc/passwd", f"https://docs.stripe.com/?k={FAKE_KEY}"):
        reason = check_url(url, (FAKE_KEY,))
        results.append((f"fetch blocked: {url[:50]}", reason is not None, reason or "allowed"))
    reason = check_url("https://docs.stripe.com/api", (FAKE_KEY,))
    results.append(("fetch allowed: public docs page", reason is None, reason or ""))

    # Sandbox.
    if not seatbelt_available():
        results.append(("macOS Seatbelt available", False, "sandbox-exec missing"))
    else:
        token = secrets.token_hex(4)
        work = RESULTS / "work" / f"selftest-{token}"
        canary = ROOT / f".selftest-canary-{token}"
        outside = ROOT / f".selftest-outside-{token}"
        canary.write_text("secret")
        sb = None
        try:
            sb = Sandbox(work, {"FIRSTCALL_RUN_ID": "selftest"}, ["api.stripe.com"])
            res = sb.run(SANDBOX_PROBES.format(canary=str(canary), outside=str(outside)))
            try:
                probes = json.loads(res.stdout.strip().splitlines()[-1])
            except (ValueError, IndexError):
                probes = {}
                results.append(("sandbox probe ran", False, res.render()[:500]))
            for name in EXPECT_BLOCKED:
                verdict = probes.get(name, "missing")
                results.append((f"sandbox blocks {name.replace('_', ' ')}", verdict.startswith("blocked"), verdict))
            if probes:
                verdict = probes.get("https_to_allowed_host", "missing")
                results.append(("sandbox allows the company API host", verdict == "allowed", verdict))
                results.append(("sandbox env has no Anthropic key", probes.get("env_has_anthropic_key") is False, str(probes.get("env_has_anthropic_key"))))
                results.append(("sandbox CPU limit set", probes.get("cpu_limit") == 60, str(probes.get("cpu_limit"))))
            results.append(("nothing written outside workdir", not outside.exists(), ""))
        finally:
            if sb is not None:
                sb.close()
            canary.unlink(missing_ok=True)
            outside.unlink(missing_ok=True)
            shutil.rmtree(work, ignore_errors=True)

    ok = all(passed for _, passed, _ in results)
    if verbose:
        for name, passed, detail in results:
            print(f"{'PASS' if passed else 'FAIL'}  {name}" + (f"  ({detail})" if detail and not passed else ""))
        print("all safety checks passed" if ok else "SAFETY CHECKS FAILED: runs will refuse to start")
    return ok
