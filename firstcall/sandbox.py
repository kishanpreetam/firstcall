"""Runs the agent's Python inside a locked-down child process.

Three layers protect the machine and the credentials.

1. macOS Seatbelt (sandbox-exec), enforced by the OS. The child cannot undo it:
   - it can write only inside the run's working directory;
   - it can't read files in the home directory, except the interpreter and the guard;
   - it can't start other programs;
   - its only network access is the egress proxy on localhost: no direct
     internet, no DNS, no other local services.
2. The egress proxy (egress.py), which runs in the harness. It tunnels HTTPS
   only to the company's allowlisted API hosts.
3. guard/sitecustomize.py, loaded before the agent's code: CPU and file-size
   limits, plus readable errors when something is blocked.

The child also gets a minimal environment: the company's test credentials
and the run id. If Seatbelt isn't available, runs refuse to start (fail
closed).
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from .egress import EgressProxy
from .safety import UnsafeConfig

TIMEOUT_S = 90
MAX_OUTPUT_CHARS = 8_000
GUARD_DIR = Path(__file__).resolve().parent / "guard"


@dataclass
class ExecResult:
    exit_code: int | None
    stdout: str
    stderr: str
    seconds: float
    timed_out: bool = False

    def render(self) -> str:
        parts = [f"exit code: {self.exit_code}" + (" (timed out)" if self.timed_out else "")]
        if self.stdout:
            parts.append("stdout:\n" + self.stdout)
        if self.stderr:
            parts.append("stderr:\n" + self.stderr)
        return "\n".join(parts)


def seatbelt_available() -> bool:
    return sys.platform == "darwin" and shutil.which("sandbox-exec") is not None


def _sb(path: Path | str) -> str:
    return '"' + str(path).replace("\\", "\\\\").replace('"', '\\"') + '"'


def seatbelt_profile(workdir: Path, proxy_port: int) -> str:
    home = Path.home()
    venv = Path(sys.prefix).resolve()
    return f"""(version 1)
(allow default)
(deny file-write* (require-not (require-any (subpath {_sb(workdir)}) (literal "/dev/null") (regex #"^/dev/(tty|fd/)"))))
(deny file-read-data (require-all (subpath {_sb(home)}) (require-not (subpath {_sb(venv)})) (require-not (subpath {_sb(GUARD_DIR)})) (require-not (subpath {_sb(workdir)}))))
(deny process-fork)
(deny network-outbound)
(allow network-outbound (remote ip "localhost:{proxy_port}"))
"""


class Sandbox:
    def __init__(self, workdir: Path, env: dict[str, str], allowed_hosts: list[str]):
        if not seatbelt_available():
            raise UnsafeConfig("macOS sandbox-exec is not available; FirstCall won't run model-written code without it")
        if not (GUARD_DIR / "sitecustomize.py").exists():
            raise UnsafeConfig(f"sandbox guard missing at {GUARD_DIR / 'sitecustomize.py'}")
        self.workdir = workdir.resolve()
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.proxy = EgressProxy(allowed_hosts)
        self.profile = seatbelt_profile(self.workdir, self.proxy.port)
        proxy_url = f"http://127.0.0.1:{self.proxy.port}"
        self.env = {
            "PATH": "/usr/bin:/bin",
            "HOME": str(self.workdir),
            "TMPDIR": str(self.workdir),
            "PYTHONPATH": str(GUARD_DIR),
            "PYTHONDONTWRITEBYTECODE": "1",
            **{name: proxy_url for name in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "https_proxy", "http_proxy", "all_proxy")},
            "NO_PROXY": "",
            "no_proxy": "",
            **env,
        }
        self.calls = 0

    @property
    def blocked_egress(self) -> list[dict]:
        return list(self.proxy.blocked)

    def close(self) -> None:
        self.proxy.close()

    def run(self, code: str) -> ExecResult:
        self.calls += 1
        script = self.workdir / f"step_{self.calls:02d}.py"
        script.write_text(code)
        started = time.monotonic()
        try:
            proc = subprocess.run(
                ["/usr/bin/sandbox-exec", "-p", self.profile, sys.executable, script.name],
                cwd=self.workdir,
                env=self.env,
                capture_output=True,
                text=True,
                timeout=TIMEOUT_S,
            )
            return ExecResult(
                exit_code=proc.returncode,
                stdout=_clip(proc.stdout),
                stderr=_clip(proc.stderr),
                seconds=round(time.monotonic() - started, 2),
            )
        except subprocess.TimeoutExpired as exc:
            return ExecResult(
                exit_code=None,
                stdout=_clip(_text(exc.stdout)),
                stderr=_clip(_text(exc.stderr)),
                seconds=TIMEOUT_S,
                timed_out=True,
            )


def _text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    return value.decode(errors="replace") if isinstance(value, bytes) else value


def _clip(text: str) -> str:
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    half = MAX_OUTPUT_CHARS // 2
    return f"{text[:half]}\n... [{len(text) - MAX_OUTPUT_CHARS} chars truncated] ...\n{text[-half:]}"
