import ast
import logging
import os
import re
import shutil
import subprocess
import tempfile
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated

logger = logging.getLogger(__name__)

import uvicorn

import docker
import httpx
import yaml
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger
from fastmcp import FastMCP
from origo import OAuthMiddleware, OAuthProvider
from origo.endpoints import authorize, oauth_metadata, protected_resource_metadata, register, token
from pydantic import Field
from starlette.routing import Route

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

WORKSPACE = Path(os.getenv("MIRAGEN_WORKSPACE", "/opt/miragen"))
AGENTS_DIR = WORKSPACE / "agents"
COMPOSE_FILE = WORKSPACE / "compose.yml"
BASE_URL = os.getenv("MCP_BASE_URL")
CLIENT_ID = os.getenv("MCP_CLIENT_ID", "miragen-mcp")
CLIENT_SECRET = os.getenv("MCP_CLIENT_SECRET", "changeme")
AUTO_APPROVE = os.getenv("MCP_AUTO_APPROVE", "false").lower() == "true"
PUBLIC_REGISTRATION = os.getenv("MCP_PUBLIC_REGISTRATION", "false").lower() == "true"
NO_AUTH = os.getenv("MCP_NO_AUTH", "false").lower() == "true"
MCP_PATH = os.getenv("MCP_PATH", "/mcp")
MIRAGEN_BASE_IMAGE = os.getenv("MIRAGEN_BASE_IMAGE", "ghcr.io/ieepirzy/miragen:latest")

# Hard cap on characters returned by tools that can produce unbounded output
# (logs, file reads, agent responses) so a single call cannot flood an LLM
# context window.
MAX_OUTPUT_CHARS = 50_000

# Agent names double as directory names, compose service names, and Docker
# container names — restrict them accordingly (also blocks path traversal).
AGENT_NAME_PATTERN = r"^[a-z0-9][a-z0-9_-]{0,62}$"
_AGENT_NAME_RE = re.compile(AGENT_NAME_PATTERN)

# ---------------------------------------------------------------------------
# Shared state
# ---------------------------------------------------------------------------

_docker = docker.from_env()
_scheduler = AsyncIOScheduler()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _agent_dir(name: str) -> Path:
    return AGENTS_DIR / name


def _check_agent_name(name: str) -> str | None:
    """Return an error string if `name` is not a valid agent name, else None."""
    if not _AGENT_NAME_RE.fullmatch(name):
        return (
            f"ERROR: invalid agent name '{name}'. Agent names must match {AGENT_NAME_PATTERN} "
            "(lowercase letters, digits, hyphens, underscores; max 63 chars). "
            "Use miragen_list_agents to see existing agents."
        )
    return None


def _truncate(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return (
        text[:limit]
        + f"\n... [TRUNCATED: output was {len(text)} characters, showing first {limit}]"
    )


def _safe_path(agent: str, rel: str) -> tuple[Path | None, str | None]:
    """Resolve a relative path inside an agent workspace and guard against traversal."""
    if ".." in rel:
        return None, "ERROR: path traversal not allowed"
    p = Path(rel)
    if p.is_absolute():
        return None, "ERROR: path traversal not allowed"
    base = _agent_dir(agent).resolve()
    full = (base / p).resolve()
    if not str(full).startswith(str(base) + os.sep) and full != base:
        return None, "ERROR: path traversal not allowed"
    return full, None


def _container_status(name: str) -> str:
    try:
        return _docker.containers.get(name).status
    except docker.errors.NotFound:
        return "not found"
    except Exception as exc:
        return f"error: {exc}"


def _read_yaml(path: Path) -> dict:
    with open(path) as f:
        return yaml.safe_load(f) or {}


def _write_yaml(path: Path, data: dict) -> None:
    with open(path, "w") as f:
        yaml.dump(data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)


def _compose_load() -> dict:
    if COMPOSE_FILE.exists():
        return _read_yaml(COMPOSE_FILE)
    return {
        "secrets": {k: {"external": True} for k in _secret_names()},
        "services": {},
        "networks": {"miragen-net": {"external": True}},
    }


def _secret_names() -> list[str]:
    """Derive Docker secret names from non-empty *_API_KEY_FILE env vars on this container."""
    secrets = []
    for k, v in os.environ.items():
        if k.endswith("_API_KEY_FILE") and v:
            secrets.append(Path(v).name)
    return secrets


def _ensure_agent_network() -> None:
    try:
        _docker.networks.get("miragen-net")
    except docker.errors.NotFound:
        _docker.networks.create("miragen-net", driver="bridge", attachable=True)


def _compose_add_service(name: str) -> None:
    _ensure_agent_network()
    secret_names = _secret_names()
    env = {"AGENT_PROFILE": "agent.yaml"}
    for k, v in os.environ.items():
        if (k.endswith("_API_KEY_FILE") or k.endswith("_API_KEY")) and v:
            env[k] = v

    data = _compose_load()
    data["networks"] = {"miragen-net": {"external": True}}
    data.setdefault("secrets", {}).update({s: {"external": True} for s in secret_names})
    data.setdefault("services", {})[name] = {
        "image": MIRAGEN_BASE_IMAGE,
        "container_name": name,
        "restart": "unless-stopped",
        "secrets": secret_names,
        "environment": env,
        "volumes": [f"./agents/{name}:/agent"],
        "networks": ["miragen-net"],
    }
    _write_yaml(COMPOSE_FILE, data)


def _compose_remove_service(name: str) -> None:
    if not COMPOSE_FILE.exists():
        return
    data = _compose_load()
    data.get("services", {}).pop(name, None)
    _write_yaml(COMPOSE_FILE, data)


def _parse_registered_tools(source: str) -> list[dict]:
    """Return metadata for every @register-decorated async def in source."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    tools = []
    for node in tree.body:
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        for dec in node.decorator_list:
            tool_name = node.name
            if isinstance(dec, ast.Name) and dec.id == "register":
                pass
            elif (
                isinstance(dec, ast.Call)
                and isinstance(dec.func, ast.Name)
                and dec.func.id == "register"
                and dec.args
                and isinstance(dec.args[0], ast.Constant)
            ):
                tool_name = dec.args[0].value
            else:
                continue
            args = [a.arg for a in node.args.args]
            tools.append(
                {
                    "name": tool_name,
                    "description": ast.get_docstring(node) or "",
                    "signature": f"({', '.join(args)})",
                }
            )
            break
    return tools


def _find_function_span(source: str, func_name: str) -> tuple[int, int] | None:
    """Return (start_line_0indexed, end_line_exclusive) for the named top-level async def."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    for node in tree.body:
        if isinstance(node, ast.AsyncFunctionDef) and node.name == func_name:
            start = node.decorator_list[0].lineno - 1 if node.decorator_list else node.lineno - 1
            return start, node.end_lineno
    return None


def _annotations(
    title: str,
    *,
    read_only: bool = False,
    destructive: bool = False,
    idempotent: bool = False,
    open_world: bool = False,
) -> dict:
    return {
        "title": title,
        "readOnlyHint": read_only,
        "destructiveHint": destructive,
        "idempotentHint": idempotent,
        "openWorldHint": open_world,
    }


# ---------------------------------------------------------------------------
# Shared parameter schemas
# ---------------------------------------------------------------------------

AgentName = Annotated[
    str,
    Field(
        description=(
            "Agent name as returned by miragen_list_agents. Lowercase letters, digits, "
            "hyphens and underscores only (max 63 chars); doubles as the Docker container "
            "name. Example: 'morning-briefing'."
        ),
        pattern=AGENT_NAME_PATTERN,
    ),
]

ToolName = Annotated[
    str,
    Field(
        description=(
            "Registered tool name as returned by miragen_list_tools. This is the name the "
            "agent calls the tool by — either the function name or the explicit name passed "
            "to @register('name'). Example: 'get_weather'."
        ),
        min_length=1,
    ),
]

WorkspacePath = Annotated[
    str,
    Field(
        description=(
            "File path relative to the agent's workspace root (mounted as /agent inside the "
            "agent container). No absolute paths or '..' segments. Example: 'agent.yaml' or "
            "'data/notes.md'."
        ),
        min_length=1,
    ),
]


# ---------------------------------------------------------------------------
# FastMCP server
# ---------------------------------------------------------------------------

mcp = FastMCP("miragen_mcp")


# ---- Agent management -------------------------------------------------------


@mcp.tool(
    name="miragen_list_agents",
    annotations=_annotations("List Agents", read_only=True, idempotent=True),
)
def list_agents() -> dict:
    """List every miragen agent in the workspace.

    Returns: {"count": int, "agents": [{"name", "status", "mode", "model"}, ...]}.
    "status" is the Docker container state ("running", "exited", "not found", ...).
    Start here to discover valid agent names for the other miragen tools.
    """
    result = []
    if AGENTS_DIR.exists():
        for entry in sorted(AGENTS_DIR.iterdir()):
            if not entry.is_dir():
                continue
            name = entry.name
            mode = model = ""
            yaml_path = entry / "agent.yaml"
            if yaml_path.exists():
                try:
                    data = _read_yaml(yaml_path)
                    mode = data.get("mode", "")
                    model = data.get("spec", {}).get("model", "")
                except Exception:
                    pass
            result.append({"name": name, "status": _container_status(name), "mode": mode, "model": model})
    return {"count": len(result), "agents": result}


@mcp.tool(
    name="miragen_get_agent",
    annotations=_annotations("Get Agent Details", read_only=True, idempotent=True),
)
def get_agent(name: AgentName) -> dict:
    """Get full details for one agent.

    Returns: {"name", "yaml" (raw agent.yaml text), "status" (container state),
    "has_tools" (whether tools.py exists)}. On failure returns {"error": "ERROR: ..."}.
    Use miragen_list_tools to inspect the individual tools in tools.py.
    """
    err = _check_agent_name(name)
    if err:
        return {"error": err}
    d = _agent_dir(name)
    if not d.exists():
        return {"error": f"ERROR: agent '{name}' not found. Use miragen_list_agents to see available agents."}
    yaml_path = d / "agent.yaml"
    return {
        "name": name,
        "yaml": yaml_path.read_text() if yaml_path.exists() else "",
        "status": _container_status(name),
        "has_tools": (d / "tools.py").exists(),
    }


@mcp.tool(
    name="miragen_create_agent",
    annotations=_annotations("Create Agent"),
)
def create_agent(
    name: AgentName,
    yaml_source: Annotated[
        str,
        Field(
            description=(
                "Complete miragen agent profile YAML. Its top-level 'name' field must equal "
                "the `name` argument. Validate drafts first with miragen_validate_yaml; see "
                "miragen_get_readme for the full profile schema."
            ),
            min_length=1,
        ),
    ],
) -> str:
    """Create a new agent: write its workspace, register it in compose.yml, start its container.

    The YAML is validated before anything is started; on any failure the workspace is
    rolled back and an "ERROR: ..." string explains what to fix. On success the agent
    is running — use miragen_get_agent_logs to watch it, miragen_run_agent to talk to it.
    Fails if an agent with this name already exists (use miragen_delete_agent first to replace it).
    """
    err = _check_agent_name(name)
    if err:
        return err
    d = _agent_dir(name)
    if d.exists():
        return (
            f"ERROR: agent '{name}' already exists. Use miragen_get_agent to inspect it, "
            "or miragen_delete_agent first if you want to recreate it."
        )
    try:
        d.mkdir(parents=True)
        yaml_path = d / "agent.yaml"
        yaml_path.write_text(yaml_source)

        result = subprocess.run(
            ["miragen", "validate", f"agents/{name}/agent.yaml"],
            capture_output=True, text=True, cwd=WORKSPACE,
        )
        if result.returncode != 0:
            shutil.rmtree(d)
            return (
                f"ERROR: profile validation failed:\n{(result.stdout + result.stderr).strip()}\n"
                "Fix the YAML and retry. miragen_validate_yaml checks a profile without creating anything."
            )

        profile_name = (yaml.safe_load(yaml_source) or {}).get("name")
        if profile_name != name:
            shutil.rmtree(d)
            return (
                f"ERROR: profile 'name' field is '{profile_name}' but the agent is being created as "
                f"'{name}'. They must match — set 'name: {name}' in the YAML."
            )

        (d / "tools.py").write_text(f"from miragen import register\n\n# Tools for {name}\n")

        _compose_add_service(name)

        up = subprocess.run(
            ["docker", "compose", "up", "-d", name],
            capture_output=True, text=True, cwd=WORKSPACE,
        )
        if up.returncode != 0:
            _compose_remove_service(name)
            shutil.rmtree(d)
            return f"ERROR: container failed to start:\n{up.stderr.strip()}"

        return (
            f"Agent {name} created and started. "
            f"Next: miragen_get_agent_logs to verify startup, miragen_register_tool to add tools."
        )
    except Exception as exc:
        shutil.rmtree(d, ignore_errors=True)
        return f"ERROR: {exc}"


@mcp.tool(
    name="miragen_start_agent",
    annotations=_annotations("Start Agent", idempotent=True),
)
def start_agent(name: AgentName) -> str:
    """Start an agent's container via docker compose.

    Works even if the container was never created (compose creates it). Idempotent —
    starting a running agent is a no-op. Returns "Agent <name> started." or "ERROR: ...".
    """
    err = _check_agent_name(name)
    if err:
        return err
    if not _agent_dir(name).exists():
        return (
            f"ERROR: agent '{name}' not found in workspace. Use miragen_list_agents to see "
            "available agents, or miragen_create_agent to create it."
        )
    try:
        result = subprocess.run(
            ["docker", "compose", "up", "-d", name],
            capture_output=True, text=True, cwd=WORKSPACE,
        )
        if result.returncode != 0:
            return f"ERROR: {result.stderr.strip()}"
        return f"Agent {name} started."
    except Exception as exc:
        return f"ERROR: {exc}"


@mcp.tool(
    name="miragen_restart_agent",
    annotations=_annotations("Restart Agent", idempotent=True),
)
def restart_agent(name: AgentName) -> str:
    """Restart an agent's running Docker container (e.g. to pick up config changes).

    Note: miragen_register_tool / miragen_edit_tool / miragen_delete_tool already restart
    the agent for you. Returns "Agent <name> restarted." or "ERROR: ...".
    """
    err = _check_agent_name(name)
    if err:
        return err
    try:
        _docker.containers.get(name).restart()
        return f"Agent {name} restarted."
    except docker.errors.NotFound:
        return (
            f"ERROR: container '{name}' not found. If the agent exists but was never started, "
            "use miragen_start_agent instead."
        )
    except Exception as exc:
        return f"ERROR: {exc}"


@mcp.tool(
    name="miragen_stop_agent",
    annotations=_annotations("Stop Agent", idempotent=True),
)
def stop_agent(name: AgentName) -> str:
    """Stop an agent's Docker container without deleting anything.

    The workspace and compose entry remain; use miragen_start_agent to bring it back.
    Returns "Agent <name> stopped." or "ERROR: ...".
    """
    err = _check_agent_name(name)
    if err:
        return err
    try:
        _docker.containers.get(name).stop()
        return f"Agent {name} stopped."
    except docker.errors.NotFound:
        return f"ERROR: container '{name}' not found. Use miragen_list_agents to check agent status."
    except Exception as exc:
        return f"ERROR: {exc}"


@mcp.tool(
    name="miragen_delete_agent",
    annotations=_annotations("Delete Agent", destructive=True, idempotent=True),
)
def delete_agent(name: AgentName) -> str:
    """Permanently delete an agent: stop and remove its container, remove it from
    compose.yml, and delete its entire workspace (agent.yaml, tools.py, all files).

    This is irreversible — read anything you need first (miragen_get_agent,
    miragen_read_agent_file). Returns "Agent <name> deleted." or "ERROR: ...".
    """
    err = _check_agent_name(name)
    if err:
        return err
    d = _agent_dir(name)
    try:
        try:
            container = _docker.containers.get(name)
            container.stop()
            container.remove()
        except docker.errors.NotFound:
            pass
        except Exception as exc:
            return f"ERROR: {exc}"
        _compose_remove_service(name)
        if d.exists():
            shutil.rmtree(d)
        return f"Agent {name} deleted."
    except Exception as exc:
        return f"ERROR: {exc}"


@mcp.tool(
    name="miragen_get_agent_logs",
    annotations=_annotations("Get Agent Logs", read_only=True, idempotent=True),
)
def get_agent_logs(
    name: AgentName,
    tail: Annotated[
        int,
        Field(
            description="Number of log lines to return, counted from the end. Default 50.",
            ge=1,
            le=1000,
        ),
    ] = 50,
) -> str:
    """Return the most recent Docker log lines for an agent container.

    Use this to diagnose startup failures and watch autonomous runs. Output longer than
    50,000 characters is truncated (reduce `tail` if that happens). Returns the raw log
    text or "ERROR: ...".
    """
    err = _check_agent_name(name)
    if err:
        return err
    try:
        logs = _docker.containers.get(name).logs(tail=min(max(tail, 1), 1000), stream=False)
        return _truncate(logs.decode("utf-8", errors="replace"))
    except docker.errors.NotFound:
        return (
            f"ERROR: container '{name}' not found. The agent may never have been started — "
            "use miragen_start_agent, or miragen_list_agents to check status."
        )
    except Exception as exc:
        return f"ERROR: {exc}"


# ---- Tool management --------------------------------------------------------


@mcp.tool(
    name="miragen_list_tools",
    annotations=_annotations("List Agent Tools", read_only=True, idempotent=True),
)
def list_tools(agent: AgentName) -> dict:
    """List the @register-decorated tool functions in an agent's tools.py.

    Returns: {"count": int, "tools": [{"name", "description" (docstring), "signature"}, ...]}.
    An empty list means the agent has no local tools yet (add one with miragen_register_tool).
    On failure returns {"error": "ERROR: ..."}.
    """
    err = _check_agent_name(agent)
    if err:
        return {"error": err}
    p = _agent_dir(agent) / "tools.py"
    if not p.exists():
        tools: list[dict] = []
    else:
        tools = _parse_registered_tools(p.read_text())
    return {"count": len(tools), "tools": tools}


@mcp.tool(
    name="miragen_get_tool_source",
    annotations=_annotations("Get Tool Source", read_only=True, idempotent=True),
)
def get_tool_source(agent: AgentName, tool_name: ToolName) -> str:
    """Return the full Python source (decorator included) of one tool in the agent's tools.py.

    Read this before miragen_edit_tool so your old_str matches exactly.
    Returns the source text or "ERROR: ...".
    """
    err = _check_agent_name(agent)
    if err:
        return err
    p = _agent_dir(agent) / "tools.py"
    if not p.exists():
        return f"ERROR: tools.py not found for agent '{agent}'. Use miragen_list_agents to see available agents."
    source = p.read_text()
    span = _find_function_span(source, tool_name)
    if span is None:
        return (
            f"ERROR: tool '{tool_name}' not found in {agent}/tools.py. "
            "Use miragen_list_tools to see registered tools."
        )
    lines = source.splitlines(keepends=True)
    return "".join(lines[span[0]:span[1]])


@mcp.tool(
    name="miragen_register_tool",
    annotations=_annotations("Register Agent Tool"),
)
def register_tool(
    agent: AgentName,
    tool_name: ToolName,
    source: Annotated[
        str,
        Field(
            description=(
                "Complete Python source of one async function decorated with @register (or "
                "@register('<tool_name>')). First parameter must be `ctx`; give the function a "
                "docstring — it becomes the tool description the agent sees. Example:\n"
                "@register\nasync def get_weather(ctx, city: str) -> str:\n"
                '    """Return current weather for a city."""\n    ...'
            ),
            min_length=1,
        ),
    ],
) -> str:
    """Add a new tool to an agent: append the function to tools.py, whitelist it in
    agent.yaml, and restart the agent so it takes effect.

    The source is syntax-checked and must define a @register-decorated async function whose
    registered name equals `tool_name`. If the restart fails, all file changes are rolled
    back. Returns a success message or "ERROR: ...".
    """
    err = _check_agent_name(agent)
    if err:
        return err
    tools_path = _agent_dir(agent) / "tools.py"
    yaml_path = _agent_dir(agent) / "agent.yaml"
    if not tools_path.exists():
        return (
            f"ERROR: tools.py not found for agent '{agent}'. "
            "Use miragen_list_agents to see available agents."
        )

    try:
        ast.parse(source)
    except SyntaxError as exc:
        return f"ERROR: source is not valid Python: {exc}. Fix the syntax and retry."
    parsed = _parse_registered_tools(source)
    if not any(t["name"] == tool_name for t in parsed):
        found = [t["name"] for t in parsed] or "none"
        return (
            f"ERROR: source does not define a @register-decorated async function registered as "
            f"'{tool_name}' (found: {found}). The function must be `async def`, decorated with "
            "@register, and its name (or @register('name') argument) must equal tool_name."
        )

    original_tools = tools_path.read_text()
    original_yaml = yaml_path.read_text() if yaml_path.exists() else None

    try:
        tools_path.write_text(original_tools.rstrip("\n") + "\n\n" + source.strip() + "\n")

        if yaml_path.exists():
            data = _read_yaml(yaml_path)
            tools_list: list = data.get("tools", [])
            if tool_name not in tools_list:
                tools_list.append(tool_name)
                data["tools"] = tools_list
                _write_yaml(yaml_path, data)

        result = restart_agent(agent)
        if result.startswith("ERROR"):
            tools_path.write_text(original_tools)
            if original_yaml is not None:
                yaml_path.write_text(original_yaml)
            return f"ERROR: tool written but restart failed (rolled back): {result}"

        return f"Tool {tool_name} registered on {agent} and agent restarted."
    except Exception as exc:
        tools_path.write_text(original_tools)
        if original_yaml is not None:
            yaml_path.write_text(original_yaml)
        return f"ERROR: {exc}"


@mcp.tool(
    name="miragen_edit_tool",
    annotations=_annotations("Edit Agent Tool", destructive=True),
)
def edit_tool(
    agent: AgentName,
    tool_name: ToolName,
    old_str: Annotated[
        str,
        Field(
            description=(
                "Exact text to replace in tools.py — must appear exactly once in the whole file. "
                "Copy it from miragen_get_tool_source; include enough surrounding lines to make "
                "it unique."
            ),
            min_length=1,
        ),
    ],
    new_str: Annotated[str, Field(description="Replacement text.")],
) -> str:
    """Edit an agent's tools.py via exact string replacement, then restart the agent.

    Call miragen_get_tool_source first to get the exact current text. Fails without
    modifying anything if old_str is missing or ambiguous. Returns a success message
    or "ERROR: ...".
    """
    err = _check_agent_name(agent)
    if err:
        return err
    tools_path = _agent_dir(agent) / "tools.py"
    if not tools_path.exists():
        return f"ERROR: tools.py not found for agent '{agent}'. Use miragen_list_agents to see available agents."
    content = tools_path.read_text()
    count = content.count(old_str)
    if count == 0:
        return (
            "ERROR: old_str not found in tools.py. Use miragen_get_tool_source to fetch the "
            "current source and copy old_str from it exactly."
        )
    if count > 1:
        return f"ERROR: old_str appears {count} times — must be unique. Include more surrounding context."
    tools_path.write_text(content.replace(old_str, new_str, 1))
    result = restart_agent(agent)
    if result.startswith("ERROR"):
        return f"Tool edited but restart failed: {result}"
    return f"Tool '{tool_name}' edited and {agent} restarted."


@mcp.tool(
    name="miragen_delete_tool",
    annotations=_annotations("Delete Agent Tool", destructive=True, idempotent=True),
)
def delete_tool(agent: AgentName, tool_name: ToolName) -> str:
    """Remove a tool from an agent: delete the function from tools.py, remove it from the
    agent.yaml whitelist, and restart the agent.

    Irreversible — use miragen_get_tool_source first if you may want the code back.
    Returns a success message or "ERROR: ...".
    """
    err = _check_agent_name(agent)
    if err:
        return err
    tools_path = _agent_dir(agent) / "tools.py"
    yaml_path = _agent_dir(agent) / "agent.yaml"
    if not tools_path.exists():
        return f"ERROR: tools.py not found for agent '{agent}'. Use miragen_list_agents to see available agents."

    source = tools_path.read_text()
    span = _find_function_span(source, tool_name)
    if span is None:
        return f"ERROR: tool '{tool_name}' not found. Use miragen_list_tools to see registered tools."

    lines = source.splitlines(keepends=True)
    tools_path.write_text("".join(lines[: span[0]] + lines[span[1] :]))

    if yaml_path.exists():
        data = _read_yaml(yaml_path)
        tools_list: list = data.get("tools", [])
        if tool_name in tools_list:
            tools_list.remove(tool_name)
            data["tools"] = tools_list
            _write_yaml(yaml_path, data)

    return restart_agent(agent)


# ---- Agent filesystem tools -------------------------------------------------


@mcp.tool(
    name="miragen_read_agent_file",
    annotations=_annotations("Read Agent File", read_only=True, idempotent=True),
)
def read_agent_file(agent: AgentName, path: WorkspacePath) -> str:
    """Read a file from an agent's workspace on the shared volume (mounted as /agent inside
    the agent container — NOT the MCP server's own filesystem).

    Output longer than 50,000 characters is truncated. Returns the file text or "ERROR: ...".
    """
    err = _check_agent_name(agent)
    if err:
        return err
    full, err = _safe_path(agent, path)
    if err:
        return err
    try:
        return _truncate(full.read_text())
    except FileNotFoundError:
        return f"ERROR: file not found: {path}"
    except Exception as exc:
        return f"ERROR: {exc}"


@mcp.tool(
    name="miragen_write_agent_file",
    annotations=_annotations("Write Agent File", destructive=True, idempotent=True),
)
def write_agent_file(
    agent: AgentName,
    path: WorkspacePath,
    content: Annotated[str, Field(description="Full new file content (overwrites any existing content).")],
) -> str:
    """Write (or overwrite) a file in an agent's workspace on the shared volume — NOT the
    MCP server's filesystem. Parent directories are created as needed; the file is
    immediately visible inside the agent container at /agent/<path>.

    Overwrites without warning — use miragen_read_agent_file first, or
    miragen_edit_agent_file for partial changes. Returns "Written <path>" or "ERROR: ...".
    """
    err = _check_agent_name(agent)
    if err:
        return err
    full, err = _safe_path(agent, path)
    if err:
        return err
    try:
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content)
        return f"Written {path}"
    except Exception as exc:
        return f"ERROR: {exc}"


@mcp.tool(
    name="miragen_edit_agent_file",
    annotations=_annotations("Edit Agent File", destructive=True),
)
def edit_agent_file(
    agent: AgentName,
    path: WorkspacePath,
    old_str: Annotated[
        str,
        Field(
            description=(
                "Exact text to replace — must appear exactly once in the file. Copy it from "
                "miragen_read_agent_file; include surrounding lines to make it unique."
            ),
            min_length=1,
        ),
    ],
    new_str: Annotated[str, Field(description="Replacement text.")],
) -> str:
    """Edit a file in an agent's workspace via exact string replacement (shared volume —
    NOT the MCP server's filesystem). The change is immediately visible inside the agent
    container at /agent/<path>.

    Fails without modifying anything if old_str is missing or ambiguous.
    Returns "Edited <path>" or "ERROR: ...".
    """
    err = _check_agent_name(agent)
    if err:
        return err
    full, err = _safe_path(agent, path)
    if err:
        return err
    try:
        content = full.read_text()
    except FileNotFoundError:
        return f"ERROR: file not found: {path}"
    count = content.count(old_str)
    if count == 0:
        return (
            "ERROR: old_str not found. Use miragen_read_agent_file to fetch the current "
            "content and copy old_str from it exactly."
        )
    if count > 1:
        return f"ERROR: old_str appears {count} times — must be unique. Include more surrounding context."
    full.write_text(content.replace(old_str, new_str, 1))
    return f"Edited {path}"


# ---- Scheduling -------------------------------------------------------------


async def _fire_trigger(agent: str, prompt: str) -> None:
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.post(f"http://{agent}:8000/run", json={"prompt": prompt}, timeout=10)
            resp.raise_for_status()
        except Exception as exc:
            logger.error("retrigger POST to %s failed: %s", agent, exc)


@mcp.tool(
    name="miragen_set_retrigger",
    annotations=_annotations("Schedule Agent Prompt", open_world=True),
)
def set_retrigger(
    agent: AgentName,
    prompt: Annotated[
        str,
        Field(description="Prompt text to send to the agent's /run endpoint when the schedule fires.", min_length=1),
    ],
    delay_seconds: Annotated[
        int | None,
        Field(
            description="Fire after this many seconds from now (>= 1). Mutually exclusive with `at`.",
            ge=1,
        ),
    ] = None,
    at: Annotated[
        str | None,
        Field(
            description=(
                "Fire at this ISO 8601 datetime, e.g. '2026-07-02T15:30:00+00:00'. Naive "
                "datetimes are treated as UTC. Mutually exclusive with `delay_seconds`."
            ),
        ),
    ] = None,
) -> str:
    """Schedule a one-shot prompt delivery to a running agent's /run endpoint.

    Provide exactly one of `delay_seconds` or `at`. The delivery is fire-and-forget —
    check miragen_get_agent_logs afterwards to see the run. The agent must be running
    when the schedule fires. Returns a confirmation with the fire time, or "ERROR: ...".
    """
    err = _check_agent_name(agent)
    if err:
        return err
    if (delay_seconds is None) == (at is None):
        return "ERROR: provide exactly one of delay_seconds or at"
    try:
        if delay_seconds is not None:
            if delay_seconds < 1:
                return "ERROR: delay_seconds must be >= 1"
            fire_at = datetime.now(timezone.utc) + timedelta(seconds=delay_seconds)
        else:
            try:
                fire_at = datetime.fromisoformat(at)  # type: ignore[arg-type]
            except ValueError:
                return (
                    f"ERROR: '{at}' is not a valid ISO 8601 datetime. "
                    "Use e.g. '2026-07-02T15:30:00+00:00' or pass delay_seconds instead."
                )
            if fire_at.tzinfo is None:
                fire_at = fire_at.replace(tzinfo=timezone.utc)
            if fire_at <= datetime.now(timezone.utc):
                return f"ERROR: {fire_at.isoformat()} is in the past. Provide a future datetime."

        _scheduler.add_job(
            _fire_trigger,
            trigger=DateTrigger(run_date=fire_at),
            args=[agent, prompt],
            id=f"retrigger-{agent}-{fire_at.timestamp():.0f}",
            replace_existing=True,
        )
        return f"Retrigger scheduled for {agent} at {fire_at.isoformat()}."
    except Exception as exc:
        return f"ERROR: {exc}"


# ---- Agent communication ----------------------------------------------------


@mcp.tool(
    name="miragen_run_agent",
    annotations=_annotations("Run Agent Prompt", open_world=True),
)
async def run_agent(
    agent: AgentName,
    prompt: Annotated[
        str,
        Field(description="Prompt text to send to the agent. The agent's own instructions and mode determine how it responds.", min_length=1),
    ],
) -> str:
    """Send a prompt to a running agent's /run endpoint and return its response.

    Synchronous: waits up to 120 seconds for the agent to finish its run. The agent may
    call its own tools and side-effect the outside world during the run. Responses longer
    than 50,000 characters are truncated. Returns the agent's output or "ERROR: ...".
    """
    err = _check_agent_name(agent)
    if err:
        return err
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"http://{agent}:8000/run",
                json={"prompt": prompt},
                timeout=120,
            )
            resp.raise_for_status()
            return _truncate(resp.json().get("output", resp.text))
    except httpx.ConnectError:
        return (
            f"ERROR: could not connect to agent '{agent}'. The container is probably not "
            "running — check with miragen_list_agents and start it with miragen_start_agent."
        )
    except httpx.TimeoutException:
        return (
            f"ERROR: agent '{agent}' did not respond within 120 seconds. The run may still be "
            "in progress — check miragen_get_agent_logs for its output."
        )
    except httpx.HTTPStatusError as exc:
        body = exc.response.text[:500]
        return (
            f"ERROR: agent '{agent}' returned HTTP {exc.response.status_code}: {body}. "
            "Check miragen_get_agent_logs for details."
        )
    except Exception as exc:
        return f"ERROR: {exc}"


# ---- Meta -------------------------------------------------------------------


@mcp.tool(
    name="miragen_get_readme",
    annotations=_annotations("Get Miragen Docs", read_only=True, idempotent=True, open_world=True),
)
def get_miragen_readme() -> str:
    """Fetch the latest miragen README from GitHub — the authoritative reference for the
    agent profile YAML schema, modes, triggers, capabilities, and the approval flow.

    Read this before writing YAML for miragen_create_agent or miragen_validate_yaml.
    Returns the README markdown or "ERROR: ...".
    """
    try:
        resp = httpx.get(
            "https://raw.githubusercontent.com/ieepirzy/miragen/main/README.md",
            timeout=10,
            follow_redirects=True,
        )
        resp.raise_for_status()
        return resp.text
    except Exception as exc:
        return f"ERROR: could not fetch README: {exc}. Retry, or check network access from the MCP server."


@mcp.tool(
    name="miragen_validate_yaml",
    annotations=_annotations("Validate Agent YAML", read_only=True, idempotent=True),
)
def validate_yaml(
    source: Annotated[
        str,
        Field(
            description="Agent profile YAML text to validate (the would-be agent.yaml contents).",
            min_length=1,
        ),
    ],
) -> str:
    """Validate a miragen agent profile YAML using the miragen CLI, without creating or
    touching any agent.

    Use this to check drafts before miragen_create_agent. Returns the validator's verdict —
    a summary of the parsed profile if valid, otherwise the specific schema errors to fix.
    """
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".yaml", delete=False
    ) as f:
        f.write(source)
        tmp = Path(f.name)
    try:
        result = subprocess.run(
            ["miragen", "validate", str(tmp)],
            capture_output=True, text=True, cwd=WORKSPACE,
        )
        return (result.stdout + result.stderr).strip() or "OK"
    finally:
        tmp.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# App assembly
# ---------------------------------------------------------------------------


app = mcp.http_app(stateless_http=True, path=MCP_PATH)

if not NO_AUTH:
    auth = OAuthProvider(
        base_url=BASE_URL,
        clients={CLIENT_ID: CLIENT_SECRET},
        token_ttl=604800,
        auto_approve=AUTO_APPROVE,
        public_registration=PUBLIC_REGISTRATION,
    )

    for route in [
        Route("/.well-known/oauth-authorization-server", oauth_metadata, methods=["GET"]),
        Route("/.well-known/oauth-protected-resource", protected_resource_metadata, methods=["GET"]),
        Route("/register", register, methods=["POST"]),
        Route("/authorize", authorize, methods=["GET", "POST"]),
        Route("/token", token, methods=["POST"]),
    ]:
        app.router.routes.insert(0, route)

    app.add_middleware(OAuthMiddleware, provider=auth)

    app.state.base_url = BASE_URL
    app.state.mcp_path = MCP_PATH
    app.state.storage = auth.storage
    app.state.public_registration = auth.public_registration
    app.state.auto_approve = auth.auto_approve


_original_lifespan = app.router.lifespan_context


@asynccontextmanager
async def _lifespan(scope):
    async with _original_lifespan(scope):
        _ensure_agent_network()
        _scheduler.start()
        yield
        _scheduler.shutdown(wait=False)


app.router.lifespan_context = _lifespan

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
