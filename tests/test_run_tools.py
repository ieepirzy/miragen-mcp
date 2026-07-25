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


# ── async runs & approvals (miragen issue #3 / miragen #5, #6) ───────────────


def test_run_agent_appends_run_id_when_present(monkeypatch):
    def handler(request):
        return _json_response({"output": "hello back", "run_id": "abc123"})

    monkeypatch.setattr(server, "_agent_transport", _transport(handler))
    result = asyncio.run(server.run_agent("worker", "hi"))
    assert result.startswith("hello back")
    assert result.endswith("(run_id: abc123)")


def test_run_agent_no_run_id_note_when_absent(monkeypatch):
    def handler(request):
        return _json_response({"output": "hello back"})

    monkeypatch.setattr(server, "_agent_transport", _transport(handler))
    result = asyncio.run(server.run_agent("worker", "hi"))
    assert result == "hello back"
    assert "run_id" not in result


def test_run_agent_async_success_message(monkeypatch):
    seen = {}

    def handler(request):
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(202, json={"run_id": "abc123", "status": "running"})

    monkeypatch.setattr(server, "_agent_transport", _transport(handler))
    result = asyncio.run(server.run_agent_async("worker", "do the thing"))
    assert seen["path"] == "/run/async"
    assert seen["body"] == {"prompt": "do the thing"}
    assert "abc123" in result
    assert "miragen_get_run" in result
    assert "worker" in result


def test_run_agent_async_missing_run_id_is_an_error(monkeypatch):
    def handler(request):
        return _json_response({"status": "running"})

    monkeypatch.setattr(server, "_agent_transport", _transport(handler))
    result = asyncio.run(server.run_agent_async("worker", "do the thing"))
    assert result.startswith("ERROR:")
    assert "no run_id" in result


def test_run_agent_async_connect_error(monkeypatch):
    def handler(request):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(server, "_agent_transport", _transport(handler))
    result = asyncio.run(server.run_agent_async("worker", "hi"))
    assert result.startswith("ERROR: could not connect to agent 'worker'")
    assert "miragen_start_agent" in result


def test_run_agent_async_timeout_error(monkeypatch):
    def handler(request):
        raise httpx.TimeoutException("slow")

    monkeypatch.setattr(server, "_agent_transport", _transport(handler))
    result = asyncio.run(server.run_agent_async("worker", "hi"))
    assert result.startswith("ERROR: agent 'worker' did not respond")
    assert "miragen_get_agent_logs" in result


def test_run_agent_async_degrades_on_404(monkeypatch):
    def handler(request):
        return httpx.Response(404, json={"detail": "not found"})

    monkeypatch.setattr(server, "_agent_transport", _transport(handler))
    result = asyncio.run(server.run_agent_async("worker", "hi"))
    assert result.startswith("ERROR: agent 'worker' is running a miragen image without")
    assert "run-record/approval" in result
    assert "miragen_delete_agent + miragen_create_agent" in result


def test_run_agent_async_degrades_on_405(monkeypatch):
    def handler(request):
        return httpx.Response(405, json={"detail": "method not allowed"})

    monkeypatch.setattr(server, "_agent_transport", _transport(handler))
    result = asyncio.run(server.run_agent_async("worker", "hi"))
    assert "without run-record/approval support" in result


def test_run_agent_async_other_http_error_not_degraded(monkeypatch):
    def handler(request):
        return httpx.Response(500, json={"detail": "boom"})

    monkeypatch.setattr(server, "_agent_transport", _transport(handler))
    result = asyncio.run(server.run_agent_async("worker", "hi"))
    assert "HTTP 500" in result
    assert "without run-record/approval support" not in result


def test_list_pending_approvals_returns_body(monkeypatch):
    def handler(request):
        assert request.url.path == "/approvals"
        return _json_response({
            "count": 1,
            "approvals": [
                {
                    "request_id": "req-1",
                    "request": {"tool_name": "delete_thread", "tool_args": {"id": 42}},
                    "created_at": "2026-07-25T00:00:00Z",
                    "expires_at": "2026-07-25T00:05:00Z",
                }
            ],
        })

    monkeypatch.setattr(server, "_agent_transport", _transport(handler))
    result = asyncio.run(server.list_pending_approvals("worker"))
    assert result["count"] == 1
    assert result["approvals"][0]["request_id"] == "req-1"


def test_list_pending_approvals_invalid_agent_name(monkeypatch):
    result = asyncio.run(server.list_pending_approvals("Bad Name!"))
    assert result["error"].startswith("ERROR: invalid agent name")


def test_list_pending_approvals_degrades_on_404(monkeypatch):
    def handler(request):
        return httpx.Response(404, json={"detail": "not found"})

    monkeypatch.setattr(server, "_agent_transport", _transport(handler))
    result = asyncio.run(server.list_pending_approvals("worker"))
    assert "without run-record/approval support" in result["error"]


def test_resolve_approval_round_trips_note_as_prompt(monkeypatch):
    seen = {}

    def handler(request):
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return _json_response({"resolved": True, "request_id": "req-1"})

    monkeypatch.setattr(server, "_agent_transport", _transport(handler))
    result = asyncio.run(
        server.resolve_approval("worker", "req-1", True, note="looks safe, go ahead")
    )
    assert seen["path"] == "/approvals/req-1"
    assert seen["body"] == {"approved": True, "prompt": "looks safe, go ahead"}
    assert result["resolved"] is True


def test_resolve_approval_denied_without_note(monkeypatch):
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return _json_response({"resolved": True, "request_id": "req-1"})

    monkeypatch.setattr(server, "_agent_transport", _transport(handler))
    asyncio.run(server.resolve_approval("worker", "req-1", False))
    assert seen["body"] == {"approved": False, "prompt": None}


def test_resolve_approval_404_maps_to_degradation_message(monkeypatch):
    # Per the design doc, POST /approvals/{id} also returns 404 for a legitimate
    # "unknown, already resolved, or expired" request_id on an up-to-date agent —
    # indistinguishable at the transport level from "old image, route doesn't exist"
    # without inspecting response-body shape, which the spec doesn't call for. The
    # degradation message wins for any 404/405 on this path; a human reading it can
    # still tell the two apart from context (agent otherwise responsive vs not).
    def handler(request):
        return httpx.Response(404, json={"error": "unknown, already resolved, or expired"})

    monkeypatch.setattr(server, "_agent_transport", _transport(handler))
    result = asyncio.run(server.resolve_approval("worker", "req-missing", True))
    assert "without run-record/approval support" in result["error"]


def test_resolve_approval_invalid_agent_name(monkeypatch):
    result = asyncio.run(server.resolve_approval("Bad Name!", "req-1", True))
    assert result["error"].startswith("ERROR: invalid agent name")
