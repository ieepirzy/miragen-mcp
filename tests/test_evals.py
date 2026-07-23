"""Deterministic checks for the eval suite (mcp-builder Phase 4), runnable in CI
with no ANTHROPIC_API_KEY.

Two things are verified:
  1. eval.xml stays consistent with the fixtures and only references read-only
     tools (evals/check_evals.check()).
  2. Every checked-in answer is actually reproducible by driving the real
     read-only server tools against the fixture workspace — this is the
     automated form of the skill's "solve each question by hand via the tools"
     requirement, so a broken tool or a wrong answer fails the build.
"""

import sys
from pathlib import Path

import pytest

import server

REPO_ROOT = Path(__file__).resolve().parent.parent
EVALS_DIR = REPO_ROOT / "evals"
FIXTURE_AGENTS = EVALS_DIR / "fixtures" / "agents"

sys.path.insert(0, str(EVALS_DIR))
import check_evals  # noqa: E402
import ground_truth  # noqa: E402


@pytest.fixture
def fixture_workspace(monkeypatch):
    """Point the server's read-only tools at the eval fixtures."""
    monkeypatch.setattr(server, "AGENTS_DIR", FIXTURE_AGENTS)
    # _container_status hits the (mocked) docker socket; fixtures have no
    # containers, so pin it to a constant so list_agents stays deterministic.
    monkeypatch.setattr(server, "_container_status", lambda name: "not found")


# ── suite-level consistency ───────────────────────────────────────────────────


def test_eval_suite_is_consistent():
    problems = check_evals.check()
    assert problems == [], "\n".join(problems)


def test_every_answer_has_a_fixture_derivation():
    evals = check_evals.parse_evals()
    truth = ground_truth.compute_answers()
    assert {e["id"] for e in evals} <= set(truth)


# ── each answer reproduced through the real read-only tools ───────────────────


def _agents_via_tools():
    return {a["name"]: a for a in server.list_agents()["agents"]}


def test_cheapest_model_via_tools(fixture_workspace):
    agents = _agents_via_tools()
    cheapest = min(agents.values(), key=lambda a: ground_truth.MODEL_PRICE_RANK[a["model"]])
    assert cheapest["name"] == "weather-scout"


def test_most_expensive_model_via_tools(fixture_workspace):
    agents = _agents_via_tools()
    priciest = max(agents.values(), key=lambda a: ground_truth.MODEL_PRICE_RANK[a["model"]])
    assert f"{priciest['name']}: {priciest['model']}" == "repo-janitor: anthropic/claude-opus-4-8"


def test_total_tool_count_via_tools(fixture_workspace):
    agents = _agents_via_tools()
    total = sum(server.list_tools(name)["count"] for name in agents)
    assert total == 10


def test_agent_with_most_tools_via_tools(fixture_workspace):
    agents = _agents_via_tools()
    counts = {name: server.list_tools(name)["count"] for name in agents}
    top = max(counts, key=counts.get)
    assert f"{top}: {counts[top]}" == "metrics-reporter: 4"


def test_aliased_tool_via_tools(fixture_workspace):
    # list_tools reports the registered name; the differing function name is only
    # visible in the raw source (get_tool_source is keyed by function name, so it
    # can't fetch an aliased tool by its registered name — reading tools.py can).
    names = {t["name"] for t in server.list_tools("weather-scout")["tools"]}
    assert "fetch_forecast" in names
    src = server.read_agent_file("weather-scout", "tools.py")
    assert '@register("fetch_forecast")' in src
    assert "async def get_forecast" in src  # function name differs from registered name


def test_forecast_docstring_line_via_tools(fixture_workspace):
    tools = {t["name"]: t for t in server.list_tools("weather-scout")["tools"]}
    first_line = tools["fetch_forecast"]["description"].splitlines()[0]
    assert first_line == "Return the 7-day forecast for a city."


def test_report_schedule_via_tools(fixture_workspace):
    import yaml

    content = server.read_agent_file("metrics-reporter", "config/report.yaml")
    assert yaml.safe_load(content)["schedule"] == "daily"


def test_approval_globs_via_tools(fixture_workspace):
    import fnmatch

    import yaml

    agents = _agents_via_tools()
    total = 0
    delete_matchers = []
    fs_non_autonomous = []
    for name in agents:
        profile = yaml.safe_load(server.get_agent(name)["yaml"])
        approvals = profile.get("approvals") or []
        total += len(approvals)
        if any(fnmatch.fnmatch("delete_thread", g) for g in approvals):
            delete_matchers.append(name)
        caps = profile.get("capabilities") or []
        if "filesystem" in caps and profile.get("mode") != "autonomous":
            fs_non_autonomous.append(name)

    assert total == 4
    assert sorted(delete_matchers) == ["inbox-triage", "repo-janitor"]
    assert fs_non_autonomous == ["repo-janitor"]
