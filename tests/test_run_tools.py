"""Executor-run surfaces, deployment compatibility discovery, and secondary
doc retrieval (miragen issue #33 Phase H).

Agent HTTP calls route through the `server._agent_transport` seam with an
httpx.MockTransport, so no docker network is needed. Async tools are driven
with asyncio.run — CI installs no pytest-asyncio.
"""

import asyncio
import json

import httpx
import pytest

import server


def _transport(handler):
    return httpx.MockTransport(handler)


def _json_response(body, status=200):
    return httpx.Response(status, json=body)


# ── proxying to the agent control API ────────────────────────────────────────


def test_list_runs_proxies_params_and_body(monkeypatch):
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        return _json_response({"count": 1, "runs": [{"run_id": "abc123", "status": "suspended"}]})

    monkeypatch.setattr(server, "_agent_transport", _transport(handler))
    result = asyncio.run(server.list_runs("worker", limit=5, status="suspended"))
    assert result["count"] == 1
    assert result["runs"][0]["status"] == "suspended"
    assert "http://worker:8000/runs" in seen["url"]
    assert "limit=5" in seen["url"] and "status=suspended" in seen["url"]


def test_get_run_returns_record(monkeypatch):
    def handler(request):
        assert request.url.path == "/runs/abc123"
        return _json_response({"run_id": "abc123", "status": "succeeded", "exit_reason": None})

    monkeypatch.setattr(server, "_agent_transport", _transport(handler))
    result = asyncio.run(server.get_run("worker", "abc123"))
    assert result["status"] == "succeeded"


def test_connect_error_gives_guidance(monkeypatch):
    def handler(request):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(server, "_agent_transport", _transport(handler))
    result = asyncio.run(server.get_run("worker", "abc123"))
    assert result["error"].startswith("ERROR: could not connect to agent 'worker'")
    assert "miragen_start_agent" in result["error"]


def test_http_error_surfaces_status_and_body(monkeypatch):
    def handler(request):
        return httpx.Response(404, json={"detail": {"error": "unknown run_id 'zzzz'"}})

    monkeypatch.setattr(server, "_agent_transport", _transport(handler))
    result = asyncio.run(server.get_run("worker", "zzzz"))
    assert "HTTP 404" in result["error"] and "unknown run_id" in result["error"]


def test_invalid_agent_name_rejected_before_any_request():
    result = asyncio.run(server.list_runs("Bad Name!"))
    assert result["error"].startswith("ERROR: invalid agent name")


def test_internal_token_header_sent_when_configured(monkeypatch):
    seen = {}

    def handler(request):
        seen["token"] = request.headers.get("X-Miragen-Token")
        return _json_response({"count": 0, "runs": []})

    monkeypatch.setattr(server, "_agent_transport", _transport(handler))
    monkeypatch.setattr(server, "MIRAGEN_INTERNAL_TOKEN", "sekrit")
    asyncio.run(server.list_runs("worker"))
    assert seen["token"] == "sekrit"


# ── events: cursor + degradation warning ─────────────────────────────────────


def test_get_run_events_cursor_passthrough(monkeypatch):
    def handler(request):
        assert request.url.params["after"] == "3"
        return _json_response({
            "run_id": "abc123", "count": 2,
            "events": [{"seq": 4, "type": "turn.started"}, {"seq": 5, "type": "turn.completed"}],
            "next_after": 5, "has_more": False,
        })

    monkeypatch.setattr(server, "_agent_transport", _transport(handler))
    result = asyncio.run(server.get_run_events("worker", "abc123", after=3))
    assert result["next_after"] == 5
    assert "warning" not in result


def test_get_run_events_warns_when_deployment_ignores_cursor(monkeypatch):
    def handler(request):
        # a pre-cursor miragen ignores unknown query params and tail-reads
        return _json_response({"run_id": "abc123", "count": 1, "events": [{"type": "x"}]})

    monkeypatch.setattr(server, "_agent_transport", _transport(handler))
    result = asyncio.run(server.get_run_events("worker", "abc123", after=3))
    assert "pre events-cursor/v1" in result["warning"]


# ── diff, resume, abandon ────────────────────────────────────────────────────


def test_get_run_diff_returns_plain_text(monkeypatch):
    def handler(request):
        return httpx.Response(200, text="diff --git a/x b/x\n+new line\n")

    monkeypatch.setattr(server, "_agent_transport", _transport(handler))
    result = asyncio.run(server.get_run_diff("worker", "abc123"))
    assert result.startswith("diff --git")


def test_resume_run_posts_prompt(monkeypatch):
    seen = {}

    def handler(request):
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return _json_response({"run_id": "abc123", "status": "succeeded"})

    monkeypatch.setattr(server, "_agent_transport", _transport(handler))
    result = asyncio.run(server.resume_run("worker", "abc123", "keep going"))
    assert seen["path"] == "/runs/abc123/resume"
    assert seen["body"] == {"prompt": "keep going"}
    assert result["status"] == "succeeded"


def test_abandon_run_passes_discard_flag(monkeypatch):
    seen = {}

    def handler(request):
        seen["params"] = dict(request.url.params)
        return _json_response({"run_id": "abc123", "status": "abandoned"})

    monkeypatch.setattr(server, "_agent_transport", _transport(handler))
    result = asyncio.run(server.abandon_run("worker", "abc123", discard_workspace=True))
    assert seen["params"] == {"discard_workspace": "true"}
    assert result["status"] == "abandoned"


# ── deployment compatibility ─────────────────────────────────────────────────


def _health(version="0.2.0", capabilities=None, include_caps=True):
    body = {"status": "ok", "agent": "worker", "version": version}
    if include_caps:
        body["capabilities"] = capabilities if capabilities is not None else sorted(
            server.SUPPORTED_CONTRACT_CAPABILITIES
        )
    return body


def test_check_deployment_compatible(monkeypatch):
    monkeypatch.setattr(
        server, "_agent_transport", _transport(lambda r: _json_response(_health()))
    )
    result = asyncio.run(server.check_deployment("worker"))
    assert result["compatible"] is True
    assert result["missing"] == [] and result["extra"] == []
    assert result["deployed_version"] == "0.2.0"
    assert "agree" in result["notes"][0]


def test_check_deployment_reports_missing_capabilities(monkeypatch):
    monkeypatch.setattr(
        server,
        "_agent_transport",
        _transport(lambda r: _json_response(_health(capabilities=["executor-launch/v1"]))),
    )
    result = asyncio.run(server.check_deployment("worker"))
    assert result["compatible"] is False
    assert "events-cursor/v1" in result["missing"]
    assert any("upgrade the agent image" in n for n in result["notes"])


def test_check_deployment_flags_pre_capability_miragen(monkeypatch):
    def handler(request):
        return _json_response({"status": "ok", "agent": "worker", "last_run": None})

    monkeypatch.setattr(server, "_agent_transport", _transport(handler))
    result = asyncio.run(server.check_deployment("worker"))
    assert result["compatible"] is False
    assert result["deployed_version"] is None
    assert any("predates capability discovery" in n for n in result["notes"])


def test_check_deployment_flags_newer_deployment(monkeypatch):
    caps = sorted(server.SUPPORTED_CONTRACT_CAPABILITIES) + ["quantum-runs/v9"]
    monkeypatch.setattr(
        server,
        "_agent_transport",
        _transport(lambda r: _json_response(_health(capabilities=caps))),
    )
    result = asyncio.run(server.check_deployment("worker"))
    assert result["compatible"] is True  # everything WE need is served
    assert result["extra"] == ["quantum-runs/v9"]
    assert any("outdated side" in n for n in result["notes"])


# ── secondary docs ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("path", [
    "../secrets.md",
    "docs/../server.py",
    "/etc/passwd",
    "server.py",
    "docs/notes.txt",
    "docs/",
    "README.md.evil/x.md",
])
def test_get_doc_rejects_non_doc_paths(path):
    result = server.get_miragen_doc(path)
    assert result.startswith("ERROR:")


def test_get_doc_fetches_and_caches(monkeypatch):
    calls = []

    def fake_get(url, **kw):
        calls.append(url)
        return httpx.Response(
            200, text="# Executor tier\n...", request=httpx.Request("GET", url)
        )

    monkeypatch.setattr(server.httpx, "get", fake_get)
    monkeypatch.setattr(server, "_doc_cache", {})
    first = server.get_miragen_doc("docs/executor-tier.md")
    second = server.get_miragen_doc("docs/executor-tier.md")
    assert first.startswith("# Executor tier") and second == first
    assert calls == ["https://raw.githubusercontent.com/ieepirzy/miragen/main/docs/executor-tier.md"]


def test_get_doc_404_names_the_readme(monkeypatch):
    def fake_get(url, **kw):
        return httpx.Response(404, text="Not Found", request=httpx.Request("GET", url))

    monkeypatch.setattr(server.httpx, "get", fake_get)
    monkeypatch.setattr(server, "_doc_cache", {})
    result = server.get_miragen_doc("docs/nope.md")
    assert result.startswith("ERROR:") and "does not exist" in result
