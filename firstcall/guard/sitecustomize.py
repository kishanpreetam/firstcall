"""Loaded automatically by every sandboxed child process (via PYTHONPATH).

This is the inner, convenience layer. The hard guarantees come from the
macOS Seatbelt profile in sandbox.py and the egress proxy in egress.py,
and nothing in the child can undo those. This file adds:
- readable errors when the code tries to start other programs;
- CPU-time and file-size limits, set as hard limits so the code can't raise them.
"""

import os
import resource
import subprocess


def _no_programs(*args, **kwargs):
    raise PermissionError("FirstCall sandbox: starting other programs is blocked. Use Python and requests.")


subprocess.Popen = _no_programs
os.system = _no_programs
os.popen = _no_programs
for _name in ("execv", "execve", "execl", "execle", "execlp", "execlpe", "execvp", "execvpe",
              "spawnv", "spawnve", "spawnl", "spawnle", "fork", "forkpty", "posix_spawn", "posix_spawnp"):
    if hasattr(os, _name):
        setattr(os, _name, _no_programs)

resource.setrlimit(resource.RLIMIT_CPU, (60, 60))
resource.setrlimit(resource.RLIMIT_FSIZE, (50 * 1024 * 1024, 50 * 1024 * 1024))
