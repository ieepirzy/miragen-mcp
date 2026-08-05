"""A fake miragend serving the eval fixtures, shared by tests/test_evals.py
and the manual LLM harness (run_evals.py).

Since the lifecycle extraction the server's read-only tools are HTTP
delegates to the miragend daemon, so "point the tools at the fixture
workspace" means installing an httpx.MockTransport on
``server._daemon_transport`` that implements the daemon's read-only API
surface directly over ``evals/fixtures/agents``. Fixtures have no
containers, so every status is the constant "not found".
"""

from __future__ import annotations

import ast
from pathlib import Path

import httpx
import yaml

FIXTURE_AGENTS = Path(__file__).resolve().parent / "fixtures" / "agents"


def parse_registered_tools(source: str) -> list[dict]:
    """Same shape the real daemon returns from GET /agents/{name}/tools."""
    tools = []
    for node in ast.parse(source).body:
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        for dec in node.decorator_list:
            name = node.name
            if isinstance(dec, ast.Name) and dec.id == "register":
                pass
            elif (
                isinstance(dec, ast.Call)
                and isinstance(dec.func, ast.Name)
                and dec.func.id == "register"
                and dec.args
                and isinstance(dec.args[0], ast.Constant)
            ):
                name = dec.args[0].value
            else:
                continue
            tools.append(
                {
                    "name": name,
                    "description": ast.get_docstring(node) or "",
                    "signature": f"({', '.join(a.arg for a in node.args.args)})",
                }
            )
            break
    return tools


def _registered_name(node: ast.AsyncFunctionDef) -> str | None:
    for dec in node.decorator_list:
        if isinstance(dec, ast.Name) and dec.id == "register":
            return node.name
        if (
            isinstance(dec, ast.Call)
            and isinstance(dec.func, ast.Name)
            and dec.func.id == "register"
            and dec.args
            and isinstance(dec.args[0], ast.Constant)
        ):
            return dec.args[0].value
    return None


def _tool_span(source: str, tool_name: str) -> tuple[int, int] | None:
    """(start_line_0indexed, end_line_exclusive) of the async def registered as
    tool_name — alias-aware, matching the real daemon's span resolution."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    fallback = None
    for node in tree.body:
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        start = node.decorator_list[0].lineno - 1 if node.decorator_list else node.lineno - 1
        if _registered_name(node) == tool_name:
            return (start, node.end_lineno)
        if node.name == tool_name and fallback is None:
            fallback = (start, node.end_lineno)
    return fallback


def fixture_daemon(request: httpx.Request) -> httpx.Response:
    """MockTransport handler: the daemon's read-only API over the fixtures."""
    path = request.url.path
    if path == "/agents":
        agents = []
        for entry in sorted(FIXTURE_AGENTS.iterdir()):
            if not entry.is_dir():
                continue
            profile = yaml.safe_load((entry / "agent.yaml").read_text()) or {}
            agents.append(
                {
                    "name": entry.name,
                    "status": "not found",
                    "mode": profile.get("mode", ""),
                    "model": (profile.get("spec") or {}).get("model", ""),
                    "endpoint": f"http://{entry.name}:8000",
                }
            )
        return httpx.Response(200, json={"count": len(agents), "agents": agents})

    parts = path.strip("/").split("/")
    if len(parts) >= 2 and parts[0] == "agents":
        name = parts[1]
        agent_dir = FIXTURE_AGENTS / name
        if not agent_dir.is_dir():
            return httpx.Response(
                404, json={"detail": f"agent '{name}' not found", "code": "agent_not_found"}
            )
        if len(parts) == 2:
            return httpx.Response(
                200,
                json={
                    "name": name,
                    "yaml": (agent_dir / "agent.yaml").read_text(),
                    "status": "not found",
                    "has_tools": (agent_dir / "tools.py").exists(),
                    "endpoint": f"http://{name}:8000",
                },
            )
        if parts[2] == "tools" and len(parts) == 3:
            tools_py = agent_dir / "tools.py"
            tools = parse_registered_tools(tools_py.read_text()) if tools_py.exists() else []
            return httpx.Response(200, json={"count": len(tools), "tools": tools})
        if parts[2] == "tools" and len(parts) == 4:
            tools_py = agent_dir / "tools.py"
            source = tools_py.read_text() if tools_py.exists() else ""
            span = _tool_span(source, parts[3])
            if span is None:
                return httpx.Response(
                    404,
                    json={"detail": f"tool '{parts[3]}' not found", "code": "tool_not_found"},
                )
            lines = source.splitlines(keepends=True)
            return httpx.Response(
                200,
                json={"tool_name": parts[3], "source": "".join(lines[span[0] : span[1]])},
            )
        if parts[2] == "files":
            rel = request.url.params.get("path", "")
            target = agent_dir / rel
            if not target.is_file():
                return httpx.Response(
                    404, json={"detail": f"file not found: {rel}", "code": "file_not_found"}
                )
            return httpx.Response(200, json={"path": rel, "content": target.read_text()})
    return httpx.Response(500, json={"detail": f"unrouted fixture path: {path}"})
