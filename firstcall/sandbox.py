"""Executes the agent's Python in a separate process.

Each run gets its own working directory. The child process sees only the
company's test credentials, the run id, and a minimal PATH; HOME points at the
working directory. This keeps runs from seeing each other or the host's
secrets. It is not a security boundary against hostile code: run the
benchmark only with test-mode credentials, on a machine you're comfortable
executing model-written code on.
"""

from __future__ import annotations

import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

TIMEOUT_S = 90
MAX_OUTPUT_CHARS = 8_000


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


class Sandbox:
    def __init__(self, workdir: Path, env: dict[str, str]):
        self.workdir = workdir
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": str(workdir), "TMPDIR": str(workdir), **env}
        self.calls = 0

    def run(self, code: str) -> ExecResult:
        self.calls += 1
        script = self.workdir / f"step_{self.calls:02d}.py"
        script.write_text(code)
        started = time.monotonic()
        try:
            proc = subprocess.run(
                [sys.executable, script.name],
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
