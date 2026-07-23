"""Derive every eval answer directly from the fixture files.

The fixtures under ``evals/fixtures`` ARE the ground truth: this module reads
them and computes the expected answer for each eval id. ``check_evals.py`` (and
the CI test) assert that the answers checked into ``eval.xml`` still equal what
this module derives, so a fixture change that would invalidate an answer fails
loudly instead of silently rotting the suite.

No LLM and no network are involved — this is pure file parsing.
"""

from __future__ import annotations

import ast
import fnmatch
from pathlib import Path

import yaml

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"

# Relative price ranking of the model strings used in the fixtures (lower is
# cheaper). Kept here, next to the fixtures, so "cheapest/most expensive"
# questions have a deterministic answer independent of any live pricing.
MODEL_PRICE_RANK = {
    "anthropic/claude-haiku-4-5": 1,
    "anthropic/claude-sonnet-5": 2,
    "anthropic/claude-opus-4-8": 3,
}


def _agents_dir(fixtures_dir: Path) -> Path:
    return fixtures_dir / "agents"


def _load_agent(agent_dir: Path) -> dict:
    """Parse one agent fixture into a dict of the fields the evals probe."""
    profile = yaml.safe_load((agent_dir / "agent.yaml").read_text()) or {}
    tools = _parse_tools((agent_dir / "tools.py").read_text())
    return {
        "name": profile.get("name", agent_dir.name),
        "mode": profile.get("mode", ""),
        "model": (profile.get("spec") or {}).get("model", ""),
        "capabilities": list(profile.get("capabilities") or []),
        "approvals": list(profile.get("approvals") or []),
        "tools": tools,  # list of {"func_name", "registered_name", "docstring"}
    }


def _parse_tools(source: str) -> list[dict]:
    """Return one entry per @register-decorated async def, capturing both the
    Python function name and the registered (possibly aliased) tool name."""
    tree = ast.parse(source)
    out: list[dict] = []
    for node in tree.body:
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        for dec in node.decorator_list:
            registered = node.name
            if isinstance(dec, ast.Name) and dec.id == "register":
                pass
            elif (
                isinstance(dec, ast.Call)
                and isinstance(dec.func, ast.Name)
                and dec.func.id == "register"
                and dec.args
                and isinstance(dec.args[0], ast.Constant)
            ):
                registered = dec.args[0].value
            else:
                continue
            out.append(
                {
                    "func_name": node.name,
                    "registered_name": registered,
                    "docstring": ast.get_docstring(node) or "",
                }
            )
            break
    return out


def load_agents(fixtures_dir: Path = FIXTURES_DIR) -> list[dict]:
    agents = [
        _load_agent(d)
        for d in sorted(_agents_dir(fixtures_dir).iterdir())
        if d.is_dir()
    ]
    return agents


def compute_answers(fixtures_dir: Path = FIXTURES_DIR) -> dict[str, str]:
    """Compute the expected answer for every eval id from the fixtures."""
    agents = load_agents(fixtures_dir)
    by_name = {a["name"]: a for a in agents}

    # cheapest / most expensive model (unique by construction)
    cheapest = min(agents, key=lambda a: MODEL_PRICE_RANK[a["model"]])
    priciest = max(agents, key=lambda a: MODEL_PRICE_RANK[a["model"]])

    # the single tool whose registered name differs from its function name
    aliased = [
        t
        for a in agents
        for t in a["tools"]
        if t["registered_name"] != t["func_name"]
    ]
    alias_name = aliased[0]["registered_name"] if aliased else ""

    # docstring first line of the tool registered as "fetch_forecast"
    forecast_first_line = ""
    for a in agents:
        for t in a["tools"]:
            if t["registered_name"] == "fetch_forecast":
                forecast_first_line = t["docstring"].splitlines()[0] if t["docstring"] else ""

    total_tools = sum(len(a["tools"]) for a in agents)
    total_approvals = sum(len(a["approvals"]) for a in agents)

    fs_non_autonomous = [
        a["name"]
        for a in agents
        if "filesystem" in a["capabilities"] and a["mode"] != "autonomous"
    ]

    most_tools = max(agents, key=lambda a: len(a["tools"]))

    report_schedule = (
        yaml.safe_load(
            (_agents_dir(fixtures_dir) / "metrics-reporter" / "config" / "report.yaml").read_text()
        )
        or {}
    ).get("schedule", "")

    delete_matchers = sorted(
        a["name"]
        for a in agents
        if any(fnmatch.fnmatch("delete_thread", glob) for glob in a["approvals"])
    )

    return {
        "cheapest-model": cheapest["name"],
        "most-expensive-model": f"{priciest['name']}: {priciest['model']}",
        "aliased-tool-name": alias_name,
        "total-tool-count": str(total_tools),
        "total-approval-globs": str(total_approvals),
        "filesystem-non-autonomous": fs_non_autonomous[0] if fs_non_autonomous else "",
        "forecast-docstring-line": forecast_first_line,
        "agent-with-most-tools": f"{most_tools['name']}: {len(most_tools['tools'])}",
        "report-schedule": str(report_schedule),
        "delete-glob-agents": ", ".join(delete_matchers),
    }


if __name__ == "__main__":  # pragma: no cover - convenience for manual runs
    for _id, _answer in compute_answers().items():
        print(f"{_id}: {_answer!r}")
