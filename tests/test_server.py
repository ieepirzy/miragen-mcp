"""The thin-adapter contract: every lifecycle/schedule/validate tool composes
the right daemon request, maps daemon error codes to LLM-facing guidance, and
preserves its historical success strings. The daemon is faked with an
httpx.MockTransport installed on server._daemon_transport — the same seam
pattern test_run_tools.py uses for agent traffic."""

import asyncio
import importlib
import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace

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


# ── OAuth redirect allowlist ─────────────────────────────────────────────────
# origo fails closed (rejects every redirect_uri at /authorize) without an
# explicit allowlist — these pin down that the default claude.ai/claude.com
# callbacks are always present and operator extras merge rather than replace.


def test_redirect_uris_default_to_claude_callbacks(monkeypatch):
    monkeypatch.delenv("MCP_CLIENT_REDIRECT_URIS", raising=False)
    uris = server._client_redirect_uris()
    assert "https://claude.ai/api/mcp/auth_callback" in uris
    assert "https://claude.com/api/mcp/auth_callback" in uris


def test_redirect_uris_empty_env_falls_back_to_claude_defaults(monkeypatch):
    # Compose always exports a declared variable, so an unset
    # MCP_CLIENT_REDIRECT_URIS in Portainer reaches this process as "" — must
    # not be read as "allowlist nothing".
    monkeypatch.setenv("MCP_CLIENT_REDIRECT_URIS", "")
    uris = server._client_redirect_uris()
    assert "https://claude.ai/api/mcp/auth_callback" in uris
    assert "https://claude.com/api/mcp/auth_callback" in uris


def test_redirect_uris_extras_merge_with_defaults(monkeypatch):
    monkeypatch.setenv(
        "MCP_CLIENT_REDIRECT_URIS",
        "https://other.example/cb, https://claude.ai/api/mcp/auth_callback",
    )
    uris = server._client_redirect_uris()
    assert uris.count("https://claude.ai/api/mcp/auth_callback") == 1  # deduped
    assert "https://other.example/cb" in uris
    assert "https://claude.com/api/mcp/auth_callback" in uris  # defaults kept


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


# ── caller-supplied path components stay inside their endpoint ────────────────


def test_tool_name_cannot_escape_its_endpoint(daemon):
    # tool_name is a free string (min_length=1, no pattern) interpolated into
    # the request path; raw, "../../schedules/x" is dot-segment-normalized by
    # httpx before sending and walks a nominal tools route onto any daemon
    # route. Assert on raw_path — url.path is the percent-DECODED view and
    # cannot distinguish an encoded segment from a traversal.
    run(server.get_tool_source(agent="my-agent", tool_name="../../schedules/x"))
    sent = bytes(daemon.last().url.raw_path).split(b"?")[0]
    assert sent.startswith(b"/agents/my-agent/tools/"), sent
    assert b"/schedules/" not in sent, sent


def test_job_id_cannot_escape_its_endpoint(daemon):
    # job_id feeds a DELETE — raw interpolation would turn a retrigger cancel
    # into an arbitrary-route DELETE on the daemon.
    run(server.cancel_retrigger(job_id="../agents/prod-agent"))
    sent = bytes(daemon.last().url.raw_path).split(b"?")[0]
    assert sent.startswith(b"/schedules/"), sent
    assert b"/agents/" not in sent, sent


def test_dot_segment_ids_do_not_collapse_the_path(daemon):
    # "." and ".." are unreserved, so percent-quoting alone leaves them bare
    # and httpx still normalizes them away; they must arrive encoded.
    for evil in (".", ".."):
        run(server.get_tool_source(agent="my-agent", tool_name=evil))
        sent = bytes(daemon.last().url.raw_path).split(b"?")[0]
        assert sent.startswith(b"/agents/my-agent/tools/"), (evil, sent)


# ── OAuth client secret fails closed ─────────────────────────────────────────


def test_empty_client_secret_fails_closed(monkeypatch):
    # compose.yml declares MCP_CLIENT_SECRET: ${MCP_CLIENT_SECRET}, and
    # Compose exports a declared-but-unset variable as an EMPTY string (issue
    # #57's declared-but-empty shape) — so "" must hit the same fail-closed
    # guard as the well-known "changeme" default instead of registering the
    # client with an empty secret.
    import importlib

    monkeypatch.setenv("MCP_CLIENT_SECRET", "")
    monkeypatch.setenv("MCP_ALLOW_DEFAULT_SECRET", "false")
    monkeypatch.delenv("MCP_NO_AUTH", raising=False)
    try:
        with pytest.raises(RuntimeError, match="MCP_CLIENT_SECRET"):
            importlib.reload(server)
    finally:
        # Restore the conftest env (MCP_ALLOW_DEFAULT_SECRET=true) and
        # re-execute the module so later tests see a fully-initialized state.
        monkeypatch.undo()
        importlib.reload(server)


# ── read-only access control: _readonly_tool_names ───────────────────────────


def test_readonly_tool_names_basic_extraction():
    source = (
        "@mcp.tool(name='a', annotations=_annotations('A', read_only=True))\n"
        "def a(): pass\n\n"
        "@mcp.tool(name='b', annotations=_annotations('B'))\n"
        "def b(): pass\n\n"
        "@mcp.tool(name='c', annotations=_annotations('C', read_only=True, destructive=True))\n"
        "async def c(): pass\n"
    )
    assert server._readonly_tool_names(source) == {"a", "c"}


def test_readonly_tool_names_ignores_other_decorators():
    source = (
        "@register\n"
        "async def not_an_mcp_tool(ctx): pass\n\n"
        "@mcp.tool(name='real', annotations=_annotations('Real', read_only=True))\n"
        "def real(): pass\n"
    )
    assert server._readonly_tool_names(source) == {"real"}


def test_readonly_tool_names_invalid_syntax():
    assert server._readonly_tool_names("def (broken:") == set()


def test_readonly_tool_names_empty_source():
    assert server._readonly_tool_names("") == set()


def _readme_readonly_tool_names() -> set[str]:
    """Ground truth pulled from the README's tool table (Hints column) —
    written by hand and independent of _readonly_tool_names' own AST parsing,
    so this cross-check can actually catch the two drifting apart."""
    readme = (Path(server.__file__).parent / "README.md").read_text()
    names = set()
    for line in readme.splitlines():
        m = re.match(r"\| `(miragen_\w+)` \| ([^|]+) \|", line)
        if m and "read-only" in m.group(2):
            names.add(m.group(1))
    return names


def test_readonly_tool_names_matches_real_server_annotations():
    """The permitted set for real read-only tokens, derived at runtime from
    this file's own @mcp.tool(..., annotations=_annotations(..., read_only=True))
    calls, equals the actual set of read-only-hinted tools — checked here
    against the independently hand-written README table rather than by
    re-deriving the same AST logic under test."""
    source = Path(server.__file__).read_text()
    derived = server._readonly_tool_names(source)
    assert derived  # sanity: the real file does have read-only tools
    assert derived == _readme_readonly_tool_names()


def test_readonly_tool_names_matches_evals_ground_truth():
    """evals/check_evals.py's tool_readonly_map() derives the same fact
    (read-only tools) from the same source independently, for a different
    consumer (the eval suite). The two must agree."""
    evals_dir = Path(server.__file__).parent / "evals"
    sys.path.insert(0, str(evals_dir))
    try:
        import check_evals
    finally:
        sys.path.remove(str(evals_dir))
    source = Path(server.__file__).read_text()
    readonly_map = check_evals.tool_readonly_map(source)
    expected = {name for name, read_only in readonly_map.items() if read_only}
    assert server._readonly_tool_names(source) == expected


# ── read-only access control: _request_is_readonly ───────────────────────────


class _FakeAuth:
    resource_identifier = "https://mcp.example.com/mcp"

    def __init__(self, tokens: dict):
        self._tokens = tokens

    def verify_token(self, token, resource=None):
        return self._tokens.get(token)


def _fake_request(auth_header: str | None):
    headers = {"authorization": auth_header} if auth_header is not None else {}
    return SimpleNamespace(headers=headers)


def test_request_is_readonly_feature_off_always_false():
    auth = _FakeAuth({"tok": {"client_id": "ro-client"}})
    request = _fake_request("Bearer tok")
    assert server._request_is_readonly(auth, None, request) is False
    assert server._request_is_readonly(auth, "", request) is False


def test_request_is_readonly_matches_readonly_client():
    auth = _FakeAuth({"tok": {"client_id": "ro-client"}})
    request = _fake_request("Bearer tok")
    assert server._request_is_readonly(auth, "ro-client", request) is True


def test_request_is_readonly_admin_client_is_false():
    auth = _FakeAuth({"tok": {"client_id": "admin-client"}})
    request = _fake_request("Bearer tok")
    assert server._request_is_readonly(auth, "ro-client", request) is False


def test_request_is_readonly_invalid_token_is_false():
    auth = _FakeAuth({})
    request = _fake_request("Bearer garbage")
    assert server._request_is_readonly(auth, "ro-client", request) is False


def test_request_is_readonly_missing_bearer_prefix_is_false():
    auth = _FakeAuth({"tok": {"client_id": "ro-client"}})
    request = _fake_request("tok")
    assert server._request_is_readonly(auth, "ro-client", request) is False


def test_request_is_readonly_no_auth_header_is_false():
    auth = _FakeAuth({})
    request = _fake_request(None)
    assert server._request_is_readonly(auth, "ro-client", request) is False


# ── read-only access control: ReadOnlyGuardMiddleware ────────────────────────


class _FakeContext:
    def __init__(self, name):
        self.message = SimpleNamespace(name=name)


async def _call_next_ok(context):
    return "OK"


def test_guard_allows_readonly_tool_for_readonly_token(monkeypatch):
    auth = _FakeAuth({"tok": {"client_id": "ro-client"}})
    guard = server.ReadOnlyGuardMiddleware(auth, "ro-client", {"safe_tool"})
    monkeypatch.setattr(server, "get_http_request", lambda: _fake_request("Bearer tok"))

    result = run(guard.on_call_tool(_FakeContext("safe_tool"), _call_next_ok))
    assert result == "OK"


def test_guard_blocks_mutating_tool_for_readonly_token(monkeypatch):
    auth = _FakeAuth({"tok": {"client_id": "ro-client"}})
    guard = server.ReadOnlyGuardMiddleware(auth, "ro-client", {"safe_tool"})
    monkeypatch.setattr(server, "get_http_request", lambda: _fake_request("Bearer tok"))

    with pytest.raises(server.ToolError) as exc_info:
        run(guard.on_call_tool(_FakeContext("dangerous_tool"), _call_next_ok))
    assert str(exc_info.value) == (
        "ERROR: this token is read-only; 'dangerous_tool' modifies state. "
        "Reconnect with the admin client to use it."
    )


def test_guard_allows_mutating_tool_for_admin_token(monkeypatch):
    auth = _FakeAuth({"tok": {"client_id": "admin-client"}})
    guard = server.ReadOnlyGuardMiddleware(auth, "ro-client", {"safe_tool"})
    monkeypatch.setattr(server, "get_http_request", lambda: _fake_request("Bearer tok"))

    result = run(guard.on_call_tool(_FakeContext("dangerous_tool"), _call_next_ok))
    assert result == "OK"


def test_guard_on_list_tools_filters_for_readonly_token(monkeypatch):
    auth = _FakeAuth({"tok": {"client_id": "ro-client"}})
    guard = server.ReadOnlyGuardMiddleware(auth, "ro-client", {"safe_tool"})
    monkeypatch.setattr(server, "get_http_request", lambda: _fake_request("Bearer tok"))

    all_tools = [SimpleNamespace(name="safe_tool"), SimpleNamespace(name="dangerous_tool")]

    async def call_next(context):
        return all_tools

    result = run(guard.on_list_tools(_FakeContext(None), call_next))
    assert [t.name for t in result] == ["safe_tool"]


def test_guard_on_list_tools_unfiltered_for_admin_token(monkeypatch):
    auth = _FakeAuth({"tok": {"client_id": "admin-client"}})
    guard = server.ReadOnlyGuardMiddleware(auth, "ro-client", {"safe_tool"})
    monkeypatch.setattr(server, "get_http_request", lambda: _fake_request("Bearer tok"))

    all_tools = [SimpleNamespace(name="safe_tool"), SimpleNamespace(name="dangerous_tool")]

    async def call_next(context):
        return all_tools

    result = run(guard.on_list_tools(_FakeContext(None), call_next))
    assert [t.name for t in result] == ["safe_tool", "dangerous_tool"]


def test_guard_no_http_request_context_fails_open(monkeypatch):
    """get_http_request() raising RuntimeError (no live HTTP request) must
    not itself deny a call — it means _is_readonly_request can't prove the
    token is read-only, so the request is treated like any other unscoped
    (admin) call."""
    auth = _FakeAuth({"tok": {"client_id": "ro-client"}})
    guard = server.ReadOnlyGuardMiddleware(auth, "ro-client", {"safe_tool"})

    def _raise():
        raise RuntimeError("no request")

    monkeypatch.setattr(server, "get_http_request", _raise)
    result = run(guard.on_call_tool(_FakeContext("dangerous_tool"), _call_next_ok))
    assert result == "OK"


# ── read-only access control: env-var wiring, inert by default ──────────────


def test_readonly_feature_inert_when_env_unset(monkeypatch):
    monkeypatch.delenv("MCP_READONLY_CLIENT_ID", raising=False)
    monkeypatch.delenv("MCP_READONLY_CLIENT_SECRET", raising=False)
    server.mcp.add_middleware.reset_mock()
    try:
        importlib.reload(server)
        assert server.READONLY_CLIENT_ID is None
        assert server.READONLY_CLIENT_SECRET is None
        guard_calls = [
            c for c in server.mcp.add_middleware.call_args_list
            if c.args and isinstance(c.args[0], server.ReadOnlyGuardMiddleware)
        ]
        assert guard_calls == []
        origo_mock = sys.modules["origo"]
        clients_passed = origo_mock.OAuthProvider.call_args.kwargs["clients"]
        assert clients_passed == {server.CLIENT_ID: server.CLIENT_SECRET}
    finally:
        monkeypatch.undo()
        importlib.reload(server)


def test_readonly_client_and_guard_added_when_env_set(monkeypatch):
    monkeypatch.setenv("MCP_READONLY_CLIENT_ID", "ro-client")
    monkeypatch.setenv("MCP_READONLY_CLIENT_SECRET", "ro-secret")
    server.mcp.add_middleware.reset_mock()
    try:
        importlib.reload(server)
        assert server.READONLY_CLIENT_ID == "ro-client"

        origo_mock = sys.modules["origo"]
        clients_passed = origo_mock.OAuthProvider.call_args.kwargs["clients"]
        assert clients_passed == {server.CLIENT_ID: server.CLIENT_SECRET, "ro-client": "ro-secret"}

        guard_calls = [
            c.args[0] for c in server.mcp.add_middleware.call_args_list
            if c.args and isinstance(c.args[0], server.ReadOnlyGuardMiddleware)
        ]
        assert len(guard_calls) == 1
        assert guard_calls[0]._readonly_client_id == "ro-client"
        assert guard_calls[0]._readonly_tool_names == server._readonly_tool_names(
            Path(server.__file__).read_text()
        )
    finally:
        monkeypatch.undo()
        importlib.reload(server)


def test_readonly_warns_when_only_one_var_set(monkeypatch):
    monkeypatch.setenv("MCP_READONLY_CLIENT_ID", "ro-client")
    monkeypatch.delenv("MCP_READONLY_CLIENT_SECRET", raising=False)
    try:
        with pytest.warns(UserWarning, match="must both be set"):
            importlib.reload(server)
        # Partial config must not half-enable the feature.
        assert server.READONLY_CLIENT_ID == "ro-client"
        guard_calls = [
            c for c in server.mcp.add_middleware.call_args_list
            if c.args and isinstance(c.args[0], server.ReadOnlyGuardMiddleware)
        ]
        assert guard_calls == []
    finally:
        monkeypatch.undo()
        importlib.reload(server)
