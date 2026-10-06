"""Loads the company registry and per-company task files."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

PKG = Path(__file__).parent


@dataclass
class Task:
    id: str
    prompt: str
    verifier: str  # function name in firstcall/verify/<company>.py


@dataclass
class Company:
    key: str
    name: str
    docs: str
    mcp: list[str] = field(default_factory=list)
    # Filled from tasks/<key>.yaml when the company has runnable tasks:
    env: list[str] = field(default_factory=list)  # credentials the agent's code may use
    test_key_patterns: dict[str, str] = field(default_factory=dict)  # env var -> regex a test-mode credential must match
    allowed_hosts: list[str] = field(default_factory=list)  # the only hosts the agent's code may connect to
    test_account_check: str | None = None  # verifier function that confirms the credential is a test account
    mcp_auth_env: str | None = None  # env var holding a bearer token for the MCP server
    llms_txt: str | None = None
    tasks: list[Task] = field(default_factory=list)


def load_companies() -> list[Company]:
    raw = yaml.safe_load((PKG / "companies.yaml").read_text())
    companies = []
    for key, spec in raw.items():
        company = Company(key=key, name=spec["name"], docs=spec["docs"], mcp=spec.get("mcp") or [])
        task_file = PKG / "tasks" / f"{key}.yaml"
        if task_file.exists():
            t = yaml.safe_load(task_file.read_text())
            company.env = t.get("env", [])
            company.test_key_patterns = t.get("test_key_patterns", {})
            company.allowed_hosts = t.get("allowed_hosts", [])
            company.test_account_check = t.get("test_account_check")
            company.mcp_auth_env = t.get("mcp_auth_env")
            company.llms_txt = t.get("llms_txt")
            company.tasks = [Task(**task) for task in t["tasks"]]
        companies.append(company)
    return companies


def get_company(key: str) -> Company:
    for company in load_companies():
        if company.key == key:
            return company
    raise KeyError(f"unknown company {key!r}; see firstcall/companies.yaml")
