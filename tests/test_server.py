"""The thin-adapter contract: every lifecycle/schedule/validate tool composes
the right daemon request, maps daemon error codes to LLM-facing guidance, and
preserves its historical success strings. The daemon is faked with an
httpx.MockTransport installed on server._daemon_transport — the same seam
pattern test_run_tools.py uses for agent traffic."""

import asyncio
import json

import httpx
import pytest

import server


# ── fake daemon ───────────────────────────────────────────────────────────────


class FakeDaemon:
    """Programmable daemon: records every request, answers from a route table."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.routes: dict[tuple[str, str], tuple[int, dict]] = {}
        self.raise_connect_error = False

    def route(self, method: str, path: str, status: int, body: dict):
        self.routes[(method, path)] = (status, body)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.raise_connect_error:
            raise httpx.ConnectError("boom", request=request)
        key = (request.method, request.url.path)
        if key not in self.routes:
            return httpx.Response(500, json={"detail": f"unrouted: {key}"})
        status, body = self.routes[key]
        return httpx.Response(status, json=body)

    def last(self) -> httpx.Request:
        return self.requests[-1]

    def last_json(self) -> dict:
        return json.loads(self.last().content)


@pytest.fixture
def daemon(monkeypatch):
    fake = FakeDaemon()
    monkeypatch.setattr(
        server, "_daemon_transport", httpx.MockTransport(fake.handler)
    )
    monkeypatch.setattr(server, "MIRAGEND_TOKEN", "test-daemon-token")
    return fake


def run(coro):
    return asyncio.run(coro)


# ── import smoke test ─────────────────────────────────────────────────────────


def test_module_imports():
    assert server.mcp is not None
    assert server.MIRAGEND_URL.startswith("http")


# ── request composition & auth ───────────────────────────────────────────────


def test_list_agents_hits_daemon_with_bearer_token(daemon):
    daemon.route("GET", "/agents", 200, {"count": 0, "agents": []})
    result = run(server.list_agents())
    assert result == {"count": 0, "agents": []}
    assert daemon.last().headers["authorization"] == "Bearer test-daemon-token"


def test_empty_token_sends_no_auth_header(daemon, monkeypatch):
    monkeypatch.setattr(server, "MIRAGEND_TOKEN", "")
    daemon.route("GET", "/agents", 200, {"count": 0, "agents": []})
    run(server.list_agents())
    assert "authorization" not in daemon.last().headers


def test_create_agent_posts_name_and_yaml(daemon):
    daemon.route("POST", "/agents", 201, {"name": "a", "status": "running"})
    result = run(server.create_agent("a", "name: a\n"))
    assert result.startswith("Agent a created and started.")
    assert daemon.last_json() == {"name": "a", "yaml_source": "name: a\n"}


def test_lifecycle_success_strings(daemon):
    daemon.route("POST", "/agents/a/start", 200, {"name": "a", "status": "running"})
    daemon.route("POST", "/agents/a/stop", 200, {"name": "a", "status": "exited"})
    daemon.route("POST", "/agents/a/restart", 200, {"name": "a", "status": "running"})
    daemon.route("DELETE", "/agents/a", 200, {"name": "a", "deleted": True})

    assert run(server.start_agent("a")) == "Agent a started."
    assert run(server.stop_agent("a")) == "Agent a stopped."
    assert run(server.restart_agent("a")) == "Agent a restarted."
    assert run(server.delete_agent("a")) == "Agent a deleted."


def test_update_agent_config_diff_summary(daemon):
    daemon.route("PUT", "/agents/a/config", 200, {"changed_keys": ["mode", "spec"]})
    result = run(server.update_agent_config("a", "name: a\n"))
    assert result == "Config updated and a restarted. Diff summary: mode, spec"

    daemon.route("PUT", "/agents/a/config", 200, {"changed_keys": []})
    result = run(server.update_agent_config("a", "name: a\n"))
    assert "(no top-level keys changed)" in result


# ── error mapping ─────────────────────────────────────────────────────────────


def test_agent_not_found_maps_to_list_agents_guidance(daemon):
    daemon.route(
        "GET", "/agents/ghost", 404,
        {"detail": "agent 'ghost' not found", "code": "agent_not_found"},
    )
    result = run(server.get_agent("ghost"))
    assert result["error"].startswith("ERROR: agent 'ghost' not found")
    assert "miragen_list_agents" in result["error"]


def test_agent_exists_maps_to_delete_guidance(daemon):
    daemon.route(
        "POST", "/agents", 409,
        {"detail": "agent 'a' already exists", "code": "agent_exists"},
    )
    result = run(server.create_agent("a", "name: a\n"))
    assert result.startswith("ERROR: agent 'a' already exists")
    assert "miragen_delete_agent" in result


def test_validation_failure_passes_daemon_detail_through(daemon):
    detail = "Invalid profile — 1 error(s):\n  mode: unknown field"
    daemon.route(
        "POST", "/agents", 422, {"detail": detail, "code": "validation_failed"}
    )
    result = run(server.create_agent("a", "nope: 1\n"))
    assert detail in result
    assert "miragen_validate_yaml" in result


def test_job_not_found_maps_to_list_retriggers_guidance(daemon):
    daemon.route(
        "DELETE", "/schedules/retrigger-a-1", 404,
        {"detail": "no retrigger 'retrigger-a-1'", "code": "job_not_found"},
    )
    result = run(server.cancel_retrigger("retrigger-a-1"))
    assert "miragen_list_retriggers" in result


def test_daemon_unreachable_reports_daemon_not_agent(daemon):
    daemon.raise_connect_error = True
    result = run(server.start_agent("a"))
    assert "could not reach the miragend lifecycle daemon" in result
    assert server.MIRAGEND_URL in result


def test_unauthorized_names_token_mismatch(daemon):
    daemon.route(
        "GET", "/agents", 401,
        {"detail": "missing or invalid bearer token", "code": "unauthorized"},
    )
    result = run(server.list_agents())
    assert "MIRAGEND_TOKEN" in result["error"]


# ── client-side name validation (no round trip) ──────────────────────────────


@pytest.mark.parametrize("bad", ["UPPER", "-lead", "a b", "a" * 64, "../x"])
def test_invalid_agent_names_rejected_without_daemon_call(daemon, bad):
    result = run(server.get_agent(bad))
    assert result["error"].startswith("ERROR: invalid agent name")
    assert daemon.requests == []


@pytest.mark.parametrize("good", ["a", "morning-briefing", "x1_y2", "a" * 63])
def test_valid_agent_names_accepted(good):
    assert server._check_agent_name(good) is None


# ── tools ─────────────────────────────────────────────────────────────────────


def test_register_tool_success_string_and_payload(daemon):
    daemon.route(
        "POST", "/agents/a/tools", 201, {"tool_name": "greet", "registered": True}
    )
    src = "@register\nasync def greet(ctx): ..."
    result = run(server.register_tool("a", "greet", src))
    assert result == "Tool greet registered on a and agent restarted."
    assert daemon.last_json() == {"tool_name": "greet", "source": src}


def test_edit_tool_conflict_guidance(daemon):
    daemon.route(
        "PATCH", "/agents/a/tools/greet", 409,
        {
            "detail": "old_str appears 2 times within tool 'greet' — must be unique",
            "code": "edit_conflict",
            "occurrences": 2,
        },
    )
    result = run(server.edit_tool("a", "greet", "x", "y"))
    assert "must be unique" in result
    assert "surrounding context" in result


def test_delete_tool_returns_restart_message(daemon):
    daemon.route(
        "DELETE", "/agents/a/tools/greet", 200, {"tool_name": "greet", "deleted": True}
    )
    assert run(server.delete_tool("a", "greet")) == "Agent a restarted."


def test_get_tool_source_unwraps_source(daemon):
    daemon.route(
        "GET", "/agents/a/tools/greet", 200,
        {"tool_name": "greet", "source": "@register\nasync def greet(ctx): ..."},
    )
    assert run(server.get_tool_source("a", "greet")).startswith("@register")


# ── files ─────────────────────────────────────────────────────────────────────


def test_read_agent_file_unwraps_and_truncates(daemon):
    daemon.route(
        "GET", "/agents/a/files", 200, {"path": "big.txt", "content": "x" * 60_000}
    )
    result = run(server.read_agent_file("a", "big.txt"))
    assert len(result) < 60_000
    assert "TRUNCATED" in result


def test_write_agent_file_yaml_note(daemon):
    daemon.route("PUT", "/agents/a/files", 200, {"path": "agent.yaml", "written": True})
    result = run(server.write_agent_file("a", "agent.yaml", "name: a\n"))
    assert result.startswith("Written agent.yaml")
    assert "bypassed validation" in result

    daemon.route("PUT", "/agents/a/files", 200, {"path": "notes.md", "written": True})
    result = run(server.write_agent_file("a", "notes.md", "hi"))
    assert result == "Written notes.md"


def test_edit_agent_file_yaml_note(daemon):
    daemon.route("PATCH", "/agents/a/files", 200, {"path": "agent.yaml", "edited": True})
    result = run(server.edit_agent_file("a", "agent.yaml", "x", "y"))
    assert result.startswith("Edited agent.yaml")
    assert "bypassed validation" in result


def test_get_agent_logs_unwraps_and_truncates(daemon):
    daemon.route("GET", "/agents/a/logs", 200, {"name": "a", "logs": "y" * 60_000})
    result = run(server.get_agent_logs("a", tail=100))
    assert "TRUNCATED" in result
    assert daemon.last().url.params["tail"] == "100"


# ── export / import ───────────────────────────────────────────────────────────


def test_export_agent_adds_client_side_hint(daemon):
    daemon.route(
        "POST", "/agents/a/export", 200,
        {
            "agent": "a",
            "archive_path": "/opt/miragen/exports/a-20260805-120000.tar.gz",
            "included": ["agent.yaml"],
            "skipped": [],
            "size_bytes": 123,
        },
    )
    result = run(server.export_agent("a"))
    assert "exports/a-20260805-120000.tar.gz" in result["hint"]
    assert "miragen_import_agent" in result["hint"]


def test_import_agent_success_strings(daemon):
    daemon.route(
        "POST", "/agents/import", 201,
        {"name": "b", "imported": True, "started": True},
    )
    result = run(server.import_agent("b", "exports/a-1.tar.gz"))
    assert result.startswith("Agent b imported from a-1.tar.gz and started.")
    assert daemon.last_json() == {
        "name": "b", "archive_path": "exports/a-1.tar.gz", "start": True,
    }

    result = run(server.import_agent("b", "exports/a-1.tar.gz", start=False))
    assert "not started; use miragen_start_agent" in result
    assert daemon.last_json()["start"] is False


# ── schedules ─────────────────────────────────────────────────────────────────


def test_set_retrigger_delegates_and_reports(daemon):
    daemon.route(
        "POST", "/schedules", 201,
        {
            "job_id": "retrigger-a-1700000000",
            "agent": "a",
            "fire_at": "2026-08-05T13:00:00+00:00",
        },
    )
    result = run(server.set_retrigger("a", "wake up", delay_seconds=60))
    assert "Retrigger scheduled for a at 2026-08-05T13:00:00+00:00" in result
    assert "job_id: retrigger-a-1700000000" in result
    assert daemon.last_json() == {"agent": "a", "prompt": "wake up", "delay_seconds": 60}


def test_set_retrigger_neither_or_both_args_rejected_locally(daemon):
    assert "exactly one" in run(server.set_retrigger("a", "x"))
    assert "exactly one" in run(
        server.set_retrigger("a", "x", delay_seconds=5, at="2030-01-01T00:00:00")
    )
    assert daemon.requests == []


def test_list_retriggers_filters_by_agent(daemon):
    daemon.route("GET", "/schedules", 200, {"count": 0, "retriggers": []})
    run(server.list_retriggers("a"))
    assert daemon.last().url.params["agent"] == "a"

    run(server.list_retriggers())
    assert "agent" not in daemon.last().url.params


def test_list_retriggers_invalid_agent(daemon):
    result = run(server.list_retriggers("NOT-VALID"))
    assert result["error"].startswith("ERROR: invalid agent name")
    assert daemon.requests == []


def test_cancel_retrigger_success(daemon):
    daemon.route(
        "DELETE", "/schedules/retrigger-a-1", 200,
        {"job_id": "retrigger-a-1", "cancelled": True},
    )
    assert run(server.cancel_retrigger("retrigger-a-1")) == "Retrigger 'retrigger-a-1' cancelled."


# ── validate ─────────────────────────────────────────────────────────────────


def test_validate_yaml_formats_summary(daemon):
    daemon.route(
        "POST", "/validate", 200,
        {
            "valid": True,
            "profile": {
                "name": "a",
                "mode": "interactive",
                "triggers": ["http"],
                "tools": [],
                "model": "test:whatever",
                "capabilities": [],
            },
        },
    )
    result = run(server.validate_yaml("name: a\n"))
    assert "✓ 'a' is valid" in result
    assert "mode:         interactive" in result
    assert "model:        test:whatever" in result


def test_validate_yaml_reports_daemon_errors(daemon):
    daemon.route(
        "POST", "/validate", 422,
        {"detail": "Invalid profile — 2 error(s):", "code": "validation_failed"},
    )
    result = run(server.validate_yaml("nope"))
    assert result.startswith("ERROR: Invalid profile")


# ── check_deployment (agent health + daemon version) ─────────────────────────


def _agent_transport_returning(monkeypatch, payload: dict):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/health"
        return httpx.Response(200, json=payload)

    monkeypatch.setattr(server, "_agent_transport", httpx.MockTransport(handler))


def test_check_deployment_compatible(daemon, monkeypatch):
    _agent_transport_returning(
        monkeypatch,
        {
            "version": "0.1.8",
            "capabilities": sorted(server.SUPPORTED_CONTRACT_CAPABILITIES),
        },
    )
    daemon.route("GET", "/health", 200, {"status": "ok", "version": "0.1.9"})
    result = run(server.check_deployment("a"))
    assert result["compatible"] is True
    assert result["daemon_miragen_version"] == "0.1.9"
    assert result["missing"] == []


def test_check_deployment_daemon_down_still_reports_agent(daemon, monkeypatch):
    _agent_transport_returning(monkeypatch, {"version": "0.1.8", "capabilities": []})
    daemon.raise_connect_error = True
    result = run(server.check_deployment("a"))
    assert result["daemon_miragen_version"] is None
    assert result["compatible"] is False


# ── resources ─────────────────────────────────────────────────────────────────


def test_agents_resource_matches_list_agents(daemon):
    daemon.route("GET", "/agents", 200, {"count": 1, "agents": [{"name": "a"}]})
    assert run(server.agents_resource()) == {"count": 1, "agents": [{"name": "a"}]}


def test_agent_yaml_resource_returns_content(daemon):
    daemon.route(
        "GET", "/agents/a/files", 200, {"path": "agent.yaml", "content": "name: a\n"}
    )
    assert run(server.agent_yaml_resource("a")) == "name: a\n"
    assert daemon.last().url.params["path"] == "agent.yaml"


def test_agent_tools_resource_returns_content(daemon):
    daemon.route(
        "GET", "/agents/a/files", 200, {"path": "tools.py", "content": "# tools\n"}
    )
    assert run(server.agent_tools_resource("a")) == "# tools\n"
    assert daemon.last().url.params["path"] == "tools.py"


def test_resources_raise_on_error(daemon):
    daemon.route(
        "GET", "/agents/a/files", 404,
        {"detail": "file not found: agent.yaml", "code": "file_not_found"},
    )
    with pytest.raises(ValueError):
        run(server.agent_yaml_resource("a"))
    with pytest.raises(ValueError):
        run(server.agent_yaml_resource("NOT-VALID"))


# ── truncation helper ─────────────────────────────────────────────────────────


def test_truncate_short_passthrough():
    assert server._truncate("short") == "short"


def test_truncate_long_output():
    out = server._truncate("z" * 60_000)
    assert len(out) < 60_000
    assert "TRUNCATED" in out


# ── prompts (unchanged surface) ──────────────────────────────────────────────


def test_create_agent_prompt_default_mode():
    text = server.create_agent_prompt("watch the news")
    assert "Purpose: watch the news" in text
    assert "Mode: autonomous" in text


def test_create_agent_prompt_references_real_tools():
    text = server.create_agent_prompt("x", mode="interactive")
    for tool in (
        "miragen_validate_yaml",
        "miragen_create_agent",
        "miragen_get_agent_logs",
        "miragen_register_tool",
    ):
        assert tool in text


def test_readme_resource_falls_back_when_fetch_fails(monkeypatch):
    monkeypatch.setattr(server, "_readme_cache", None)
    monkeypatch.setattr(
        server, "get_miragen_readme", lambda: "ERROR: could not fetch README"
    )
    assert "offline schema summary" in server.readme_resource()


def test_readme_resource_caches_after_success(monkeypatch):
    monkeypatch.setattr(server, "_readme_cache", None)
    calls = {"n": 0}

    def fetch():
        calls["n"] += 1
        return "# miragen docs"

    monkeypatch.setattr(server, "get_miragen_readme", fetch)
    assert server.readme_resource() == "# miragen docs"
    assert server.readme_resource() == "# miragen docs"
    assert calls["n"] == 1
