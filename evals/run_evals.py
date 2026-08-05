"""LLM eval harness for miragen-mcp (mcp-builder Phase 4).

Drives a real Anthropic model through the miragen-mcp **read-only** tools and
checks whether it can answer each question in ``eval.xml`` from the fixture
workspace. This measures the tools' *descriptions*: if a docstring or parameter
hint is unclear, the model picks the wrong tool or wrong argument and the answer
comes out wrong.

Requires ``ANTHROPIC_API_KEY`` and the ``anthropic`` and ``fastmcp`` packages.
The deterministic, no-API-key consistency check lives in ``check_evals.py`` and
runs in CI; this script is the manual, paid run.

Usage:
    export ANTHROPIC_API_KEY=sk-...
    python evals/run_evals.py                 # run every eval
    EVAL_MODEL=claude-haiku-4-5-20251001 python evals/run_evals.py
    python evals/run_evals.py --id cheapest-model   # run one

Design notes:
  * The server is imported in-process with its heavy, side-effecting deps
    (docker socket, OAuth provider, scheduler) stubbed out — mirroring
    tests/conftest.py — while FastMCP stays real, so the model talks to the
    genuine registered tools over FastMCP's in-memory transport.
  * Only tools whose annotations set readOnlyHint=true are exposed, so the
    model *cannot* mutate state; every eval is read-only by construction. Each
    run also asserts no non-read-only tool was called.
  * Per-question cap of MAX_TOOL_CALLS tool calls guards against loops.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

EVALS_DIR = Path(__file__).resolve().parent
REPO_ROOT = EVALS_DIR.parent

MAX_TOOL_CALLS = 15
DEFAULT_MODEL = "claude-haiku-4-5-20251001"

SYSTEM_PROMPT = (
    "You are answering a factual question about a set of miragen agents using the "
    "provided read-only tools. Investigate with the tools, then reply with ONLY the "
    "exact answer — no preamble, no explanation, no trailing punctuation beyond what "
    "the answer itself requires."
)


def _install_stubs() -> None:
    """Stub the server's side-effecting deps so it imports cleanly, keeping
    fastmcp real. Mirrors tests/conftest.py.

    Post lifecycle-extraction there is no docker socket or scheduler to stub —
    the server's only heavy dependency left is origo (OAuth), skipped at
    runtime by MCP_NO_AUTH=true; an import-only stub suffices. The fixture
    workspace itself is served by a fake miragend transport installed on
    server._daemon_transport AFTER the import (see _run)."""
    os.environ.setdefault("MCP_NO_AUTH", "true")

    origo_provider = MagicMock(storage=MagicMock(), public_registration=False, auto_approve=False)
    oauth_app = MagicMock()
    oauth_app.routes = []
    oauth_app.state = SimpleNamespace(_state={})
    origo_provider.asgi_app.return_value = oauth_app
    origo_mod = MagicMock()
    origo_mod.OAuthProvider.return_value = origo_provider
    sys.modules["origo"] = origo_mod


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip()).lower().rstrip(".")


def _is_correct(expected: str, actual: str) -> bool:
    exp, act = _normalize(expected), _normalize(actual)
    return exp == act or exp in act


def _tool_is_read_only(tool) -> bool:
    ann = getattr(tool, "annotations", None)
    if ann is None:
        return False
    hint = getattr(ann, "readOnlyHint", None)
    if hint is None and isinstance(ann, dict):
        hint = ann.get("readOnlyHint")
    return bool(hint)


def _result_text(result) -> str:
    """Best-effort extraction of text from a FastMCP call_tool result."""
    content = getattr(result, "content", None)
    if content:
        parts = [getattr(b, "text", "") for b in content if getattr(b, "type", "") == "text"]
        if any(parts):
            return "\n".join(p for p in parts if p)
    data = getattr(result, "data", None)
    if data is not None:
        return str(data)
    return str(result)


async def _answer_question(anthropic_client, model, mcp_client, anthropic_tools, tool_lookup, question):
    """Run one tool-use loop; return (answer_text, tool_calls)."""
    messages = [{"role": "user", "content": question}]
    tool_calls: list[str] = []
    for _ in range(MAX_TOOL_CALLS + 1):
        resp = await asyncio.to_thread(
            anthropic_client.messages.create,
            model=model,
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            tools=anthropic_tools,
            messages=messages,
        )
        messages.append({"role": "assistant", "content": resp.content})
        tool_uses = [b for b in resp.content if b.type == "tool_use"]
        if resp.stop_reason != "tool_use" or not tool_uses:
            answer = "".join(b.text for b in resp.content if b.type == "text").strip()
            return answer, tool_calls

        if len(tool_calls) + len(tool_uses) > MAX_TOOL_CALLS:
            return "(tool-call budget exceeded)", tool_calls

        results = []
        for tu in tool_uses:
            tool_calls.append(tu.name)
            if tu.name not in tool_lookup:
                results.append({"type": "tool_result", "tool_use_id": tu.id,
                                "content": f"ERROR: tool {tu.name} is not available", "is_error": True})
                continue
            result = await mcp_client.call_tool(tu.name, tu.input or {})
            results.append({"type": "tool_result", "tool_use_id": tu.id, "content": _result_text(result)})
        messages.append({"role": "user", "content": results})
    return "(no final answer)", tool_calls


async def _run(selected_id: str | None) -> int:
    import check_evals  # local; also puts EVALS_DIR on sys.path via its import

    _install_stubs()
    sys.path.insert(0, str(REPO_ROOT))
    import server  # noqa: E402  (imported after stubs are installed)

    # The read-only tools are HTTP delegates to miragend; serve the fixture
    # workspace through the same fake daemon the pytest suite uses.
    import httpx
    from fixture_daemon import fixture_daemon

    server._daemon_transport = httpx.MockTransport(fixture_daemon)

    import anthropic
    from fastmcp import Client

    model = os.environ.get("EVAL_MODEL", DEFAULT_MODEL)
    evals = check_evals.parse_evals()
    if selected_id:
        evals = [e for e in evals if e["id"] == selected_id]
        if not evals:
            print(f"no eval with id {selected_id!r}")
            return 2

    anthropic_client = anthropic.Anthropic()
    passed = 0
    async with Client(server.mcp) as mcp_client:
        all_tools = await mcp_client.list_tools()
        read_only = [t for t in all_tools if _tool_is_read_only(t)]
        tool_lookup = {t.name: t for t in read_only}
        anthropic_tools = [
            {"name": t.name, "description": t.description or "", "input_schema": t.inputSchema}
            for t in read_only
        ]
        print(f"model={model}  read-only tools exposed={len(anthropic_tools)}  evals={len(evals)}\n")

        for e in evals:
            answer, tool_calls = await _answer_question(
                anthropic_client, model, mcp_client, anthropic_tools, tool_lookup, e["question"]
            )
            # Defense in depth: we only exposed read-only tools, so no mutating
            # tool can have been called. Fail loudly if that invariant breaks.
            mutating = [c for c in tool_calls if c not in tool_lookup]
            ok = _is_correct(e["answer"], answer) and not mutating
            passed += ok
            status = "PASS" if ok else "FAIL"
            print(f"[{status}] {e['id']}")
            print(f"       expected: {e['answer']!r}")
            print(f"       got:      {answer!r}")
            print(f"       tools:    {', '.join(tool_calls) or '(none)'}")
            if mutating:
                print(f"       !! mutating tool call(s): {mutating}")
            print()

    print(f"TOTAL: {passed}/{len(evals)} passed")
    return 0 if passed == len(evals) else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--id", help="run only the eval with this id")
    args = parser.parse_args()
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY is not set — this is the paid LLM run. For the free, "
              "deterministic consistency check use: python evals/check_evals.py")
        return 2
    return asyncio.run(_run(args.id))


if __name__ == "__main__":
    raise SystemExit(main())
