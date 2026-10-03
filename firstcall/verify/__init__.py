"""Per-company verifiers. Each module exposes the functions its tasks file names."""

from __future__ import annotations

import importlib
from typing import Callable

Verifier = Callable[[str, dict], "tuple[bool, dict, dict]"]


def get_verifier(company: str, name: str) -> Verifier:
    module = importlib.import_module(f"{__name__}.{company}")
    return getattr(module, name)
