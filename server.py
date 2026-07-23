import ast
import logging
import os
import re
import shutil
import subprocess
import tarfile
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
from apscheduler.jobstores.base import JobLookupError
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger
from fastmcp import FastMCP
from origo import OAuthMiddleware, OAuthProvider
from pydantic import Field

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

# Internal token for the agents' HTTP control APIs. When the agent containers
# set MIRAGEN_INTERNAL_TOKEN, set the same value on this server so its calls
# to /run, /runs/* etc. authenticate. Empty (default) sends no header.
MIRAGEN_INTERNAL_TOKEN = os.getenv("MIRAGEN_INTERNAL_TOKEN", "")

# Executor-tier contract capabilities this MCP build knows how to drive,
# matched against the `capabilities` list a deployed miragen advertises on
# GET /health (miragen issue #33 Phase H: report deployed-version vs
# supported-contract mismatches clearly instead of failing obscurely).
SUPPORTED_CONTRACT_CAPABILITIES = frozenset({
    "edf-resolve/mirarun.io-v1alpha1",
    "executor-launch/v1",
    "run-snapshot/v1",
    "events-cursor/v1",
})

# Hard cap on characters returned by tools that can produce unbounded output
# (logs, file reads, agent responses) so a single call cannot flood an LLM
# context window.
MAX_OUTPUT_CHARS = 50_000

# Agent export/import (miragen_export_agent / miragen_import_agent). Exports are
# tarballs of an agent workspace, excluding run history and caches; imports must
# come from the workspace exports/ directory and extract with tarfile's "data"
# filter (rejects absolute paths, traversal, and links).
EXPORT_EXCLUDE_DIRS = frozenset({"runs", "__pycache__"})
EXPORT_EXCLUDE_FILES = frozenset({"history.json"})
MAX_EXPORT_FILE_BYTES = 10 * 1024 * 1024  # skip individual files larger than this
MAX_ARCHIVE_BYTES = 50 * 1024 * 1024  # refuse to produce an archive larger than this

# Agent names double as directory names, compose service names, and Docker
# container names — restrict them accordingly (also blocks path traversal).
AGENT_NAME_PATTERN = r"^[a-z0-9][a-z0-9_-]{0,62}$"
_AGENT_NAME_RE = re.compile(AGENT_NAME_PATTERN)

# ---------------------------------------------------------------------------
# Shared state
# ---------------------------------------------------------------------------

_docker = docker.from_env()

# Retrigger schedules persist across restarts in a SQLite job store on the
# mounted workspace volume (not the default in-memory store, which drops every
# scheduled prompt when this container restarts). SQLAlchemyJobStore pickles the
# job callable *by reference*, so `_fire_trigger` MUST stay a module-level
# function in this module — do not nest it or turn it into a closure/lambda, or
# unpickling on restart will fail.
RETRIGGER_DB_PATH = WORKSPACE / "retriggers.sqlite"
_scheduler = AsyncIOScheduler(
    jobstores={"default": SQLAlchemyJobStore(url=f"sqlite:///{RETRIGGER_DB_PATH}")}
)

# Grace period for retriggers whose fire time passed while this server was down:
# on restart within this many seconds of the missed fire time the job still
# runs; older misses are dropped (APScheduler default behaviour).
RETRIGGER_MISFIRE_GRACE = 3600

# Scheduled retrigger job ids are "retrigger-<agent>-<unix_ts>". Agent names may
# contain hyphens, so parse the agent as everything between the fixed prefix and
# the trailing "-<digits>" timestamp.
_RETRIGGER_ID_RE = re.compile(r"^retrigger-(?P<agent>.+)-\d+$")

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


def _exports_dir() -> Path:
    """Directory holding agent export tarballs (sibling of the agents dir)."""
    return AGENTS_DIR.parent / "exports"


def _safe_export_path(archive_path: str) -> tuple[Path | None, str | None]:
    """Resolve a caller-supplied archive path and require it to live inside the
    workspace exports/ directory. Accepts an absolute path (as returned by
    miragen_export_agent), 'exports/<file>', or a bare '<file>'."""
    if ".." in archive_path:
        return None, "ERROR: path traversal not allowed"
    base = _exports_dir().resolve()
    p = Path(archive_path)
    if p.is_absolute():
        full = p.resolve()
    else:
        rel = p
        if rel.parts and rel.parts[0] == "exports":
            rel = Path(*rel.parts[1:])
        full = (base / rel).resolve()
    if full != base and not str(full).startswith(str(base) + os.sep):
        return None, (
            f"ERROR: archive_path must be inside the workspace exports/ directory. "
            "Pass the path miragen_export_agent returned, or 'exports/<file>.tar.gz'."
        )
    return full, None


# Test seam: when set (httpx.MockTransport in tests), agent HTTP calls route
# through it instead of the docker network.
_agent_transport = None


def _agent_headers() -> dict:
    return {"X-Miragen-Token": MIRAGEN_INTERNAL_TOKEN} if MIRAGEN_INTERNAL_TOKEN else {}


async def _agent_request(
    agent: str,
    method: str,
    path: str,
    *,
    json_body: dict | None = None,
    params: dict | None = None,
    timeout: float = 30,
):
    """One HTTP call to an agent's control API on the docker network.

    Returns (parsed_json_or_text, None) on 2xx, (None, "ERROR: ...") otherwise,
    with the same connect/timeout/status guidance run_agent has always given.
    """
    try:
        async with httpx.AsyncClient(transport=_agent_transport) as client:
            resp = await client.request(
                method,
                f"http://{agent}:8000{path}",
                json=json_body,
                params=params,
                headers=_agent_headers(),
                timeout=timeout,
            )
            resp.raise_for_status()
            try:
                return resp.json(), None
            except ValueError:
                return resp.text, None
    except httpx.ConnectError:
        return None, (
            f"ERROR: could not connect to agent '{agent}'. The container is probably not "
            "running — check with miragen_list_agents and start it with miragen_start_agent."
        )
    except httpx.TimeoutException:
        return None, (
            f"ERROR: agent '{agent}' did not respond within {timeout:.0f} seconds. "
            "Check miragen_get_agent_logs for what it is doing."
        )
    except httpx.HTTPStatusError as exc:
        body = exc.response.text[:500]
        return None, (
            f"ERROR: agent '{agent}' returned HTTP {exc.response.status_code}: {body}. "
            "Check miragen_get_agent_logs for details."
        )
    except Exception as exc:
        return None, f"ERROR: {exc}"


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
    if MIRAGEN_INTERNAL_TOKEN:
        # Enable the agent's own /run* guard with the same shared token this
        # server authenticates with. Without it a managed agent boots
        # unprotected while we send X-Miragen-Token — the header is required by
        # no one. Forwarded as a plain value, consistent with *_API_KEY below.
        env["MIRAGEN_INTERNAL_TOKEN"] = MIRAGEN_INTERNAL_TOKEN
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

RunId = Annotated[
    str,
    Field(
        description=(
            "Executor run ID as returned by miragen_list_runs (uuid4 hex). A unique prefix "
            "of at least 4 characters is accepted; ambiguous prefixes are rejected by the "
            "agent with the candidate IDs."
        ),
        pattern=r"^[0-9a-f]{4,32}$",
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
    name="miragen_update_agent_config",
    annotations=_annotations("Update Agent Config", destructive=True, idempotent=True),
)
def update_agent_config(
    agent: AgentName,
    yaml_source: Annotated[
        str,
        Field(
            description=(
                "Complete replacement agent.yaml content. Fetch the current version with "
                "miragen_get_agent first and edit it — this replaces the whole file. Must "
                "keep 'name: <agent>'."
            ),
            min_length=1,
        ),
    ],
) -> str:
    """Validate and apply a new agent.yaml for an existing agent, then restart it.

    The candidate YAML is validated with the miragen CLI before anything is touched; on
    validation failure the current config is left untouched. If validation passes but the
    restart fails, the previous config is restored and the agent is restarted again
    (best effort). This is the validated alternative to editing agent.yaml directly with
    miragen_write_agent_file / miragen_edit_agent_file. Returns a diff summary of changed
    top-level keys, or "ERROR: ...".
    """
    err = _check_agent_name(agent)
    if err:
        return err
    d = _agent_dir(agent)
    yaml_path = d / "agent.yaml"
    if not d.exists() or not yaml_path.exists():
        return (
            f"ERROR: agent '{agent}' not found. Use miragen_list_agents to see available "
            "agents, or miragen_create_agent to create it."
        )

    candidate_path = d / "agent.yaml.candidate"
    candidate_path.write_text(yaml_source)

    result = subprocess.run(
        ["miragen", "validate", f"agents/{agent}/agent.yaml.candidate"],
        capture_output=True, text=True, cwd=WORKSPACE,
    )
    if result.returncode != 0:
        candidate_path.unlink(missing_ok=True)
        return (
            f"ERROR: validation failed:\n{(result.stdout + result.stderr).strip()}\n\n"
            "The current config is untouched."
        )

    profile_name = (yaml.safe_load(yaml_source) or {}).get("name")
    if profile_name != agent:
        candidate_path.unlink(missing_ok=True)
        return (
            f"ERROR: profile 'name' field is '{profile_name}' but the agent being updated is "
            f"'{agent}'. They must match — set 'name: {agent}' in the YAML."
        )

    original_content = yaml_path.read_text()
    try:
        old_data = yaml.safe_load(original_content)
    except Exception:
        old_data = {}
    if not isinstance(old_data, dict):
        old_data = {}
    try:
        new_data = yaml.safe_load(yaml_source)
    except Exception:
        new_data = {}
    if not isinstance(new_data, dict):
        new_data = {}

    os.replace(candidate_path, yaml_path)

    restart_result = restart_agent(agent)
    if restart_result.startswith("ERROR"):
        yaml_path.write_text(original_content)
        restart_agent(agent)
        return (
            "ERROR: new config applied but restart failed — previous config restored: "
            f"{restart_result}"
        )

    changed_keys = sorted(
        k for k in (set(old_data) | set(new_data)) if old_data.get(k) != new_data.get(k)
    )
    summary = ", ".join(changed_keys) if changed_keys else "(no top-level keys changed)"
    return f"Config updated and {agent} restarted. Diff summary: {summary}"


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


# ---- Backup & migration -----------------------------------------------------


@mcp.tool(
    name="miragen_export_agent",
    annotations=_annotations("Export Agent", read_only=True, idempotent=True),
)
def export_agent(agent: AgentName) -> dict:
    """Export an agent's workspace to a gzipped tarball for backup or migration.

    The archive is written to the host workspace under exports/<agent>-<timestamp>.tar.gz
    and contains agent.yaml, tools.py, and any data files. Excluded: runs/, history.json,
    __pycache__/, and any single file over 10 MB (skipped and listed in "skipped"). The
    compose entry and secrets are NOT exported — miragen_import_agent regenerates those
    from the current server environment.

    Returns {"agent", "archive_path" (host path), "included" (relative file list),
    "skipped", "size_bytes", "hint"}. Refuses if the archive would exceed 50 MB. The
    archive lives outside every agent workspace, so miragen_read_agent_file cannot fetch
    it — copy it off the host, or import it on another miragen-mcp with
    miragen_import_agent. On failure returns {"error": "ERROR: ..."}.
    """
    err = _check_agent_name(agent)
    if err:
        return {"error": err}
    d = _agent_dir(agent)
    if not d.exists():
        return {"error": f"ERROR: agent '{agent}' not found. Use miragen_list_agents to see available agents."}

    exports = _exports_dir()
    try:
        exports.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        archive_path = exports / f"{agent}-{timestamp}.tar.gz"

        included: list[str] = []
        skipped: list[dict] = []
        members: list[tuple[Path, str]] = []
        for root, dirs, files in os.walk(d):
            dirs[:] = [sub for sub in dirs if sub not in EXPORT_EXCLUDE_DIRS]
            for fname in files:
                fp = Path(root) / fname
                rel = fp.relative_to(d)
                if fname in EXPORT_EXCLUDE_FILES:
                    skipped.append({"path": str(rel), "reason": "excluded (run history)"})
                    continue
                if fp.is_symlink():
                    skipped.append({"path": str(rel), "reason": "symlink not exported"})
                    continue
                size = fp.stat().st_size
                if size > MAX_EXPORT_FILE_BYTES:
                    skipped.append(
                        {"path": str(rel), "reason": f"exceeds 10 MB cap ({size} bytes)"}
                    )
                    continue
                members.append((fp, f"{agent}/{rel.as_posix()}"))

        with tarfile.open(archive_path, "w:gz") as tar:
            for fp, arcname in members:
                tar.add(fp, arcname=arcname, recursive=False)
                included.append(arcname[len(agent) + 1 :])

        size_bytes = archive_path.stat().st_size
        if size_bytes > MAX_ARCHIVE_BYTES:
            archive_path.unlink(missing_ok=True)
            return {
                "error": (
                    f"ERROR: export archive would be {size_bytes} bytes, over the 50 MB cap. "
                    "Trim large files from the agent workspace and retry."
                )
            }

        return {
            "agent": agent,
            "archive_path": str(archive_path),
            "included": sorted(included),
            "skipped": skipped,
            "size_bytes": size_bytes,
            "hint": (
                "This archive is on the host workspace, outside any agent workspace, so "
                "miragen_read_agent_file cannot fetch it. Copy it off the host, or import it "
                f"on another miragen-mcp with miragen_import_agent(name=..., archive_path="
                f"'exports/{archive_path.name}'). The compose entry and secrets are not in the "
                "archive — import regenerates them from the current server environment."
            ),
        }
    except Exception as exc:
        return {"error": f"ERROR: {exc}"}


@mcp.tool(
    name="miragen_import_agent",
    annotations=_annotations("Import Agent"),
)
def import_agent(
    name: AgentName,
    archive_path: Annotated[
        str,
        Field(
            description=(
                "Path to a tarball produced by miragen_export_agent. Must resolve inside the "
                "workspace exports/ directory — pass the archive_path that export returned, or "
                "'exports/<file>.tar.gz'. Arbitrary host paths are refused."
            ),
            min_length=1,
        ),
    ],
    start: Annotated[
        bool,
        Field(description="Start the agent's container after a successful import. Default true."),
    ] = True,
) -> str:
    """Import an agent from an export tarball: extract it under a new name, validate it,
    register it in compose.yml, and (by default) start it.

    Refuses if an agent named `name` already exists (delete it first with
    miragen_delete_agent). The archive must live in the workspace exports/ directory and is
    extracted with tarfile's "data" filter, which rejects absolute paths, path traversal,
    and links. The profile's 'name' field is rewritten to `name` and validated with the
    miragen CLI before anything is registered; any failure rolls the import back completely
    (workspace removed, compose entry removed). The compose entry and secrets are
    regenerated from this server's environment — they are never taken from the archive.
    Returns a success message or "ERROR: ...".
    """
    err = _check_agent_name(name)
    if err:
        return err
    d = _agent_dir(name)
    if d.exists():
        return (
            f"ERROR: agent '{name}' already exists. Delete it with miragen_delete_agent first, "
            "or import under a different name."
        )
    full, perr = _safe_export_path(archive_path)
    if perr:
        return perr
    if not full.exists():
        return (
            f"ERROR: archive not found: {archive_path}. It must be under the workspace exports/ "
            "directory — miragen_export_agent writes archives there."
        )

    staging = None
    created_dir = False
    added_service = False
    try:
        _exports_dir().mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(dir=_exports_dir(), prefix=f".import-{name}-"))
        try:
            with tarfile.open(full, "r:gz") as tar:
                tar.extractall(path=staging, filter="data")
        except (tarfile.TarError, ValueError, OSError) as exc:
            return (
                f"ERROR: could not safely extract '{archive_path}': {exc}. The archive may be "
                "corrupt or contain unsafe (absolute/traversal/link) members."
            )

        # Exports wrap everything under a single "<orig_name>/" directory; unwrap it.
        entries = list(staging.iterdir())
        top_dirs = [p for p in entries if p.is_dir()]
        if len(entries) == 1 and len(top_dirs) == 1:
            src_root = top_dirs[0]
        else:
            src_root = staging

        yaml_src = src_root / "agent.yaml"
        if not yaml_src.exists():
            return (
                "ERROR: archive has no agent.yaml at its root — it does not look like a "
                "miragen_export_agent tarball."
            )

        # Rewrite the top-level name in place, preserving formatting/comments.
        original_text = yaml_src.read_text()
        rewritten, n = re.subn(r"(?m)^name:.*$", f"name: {name}", original_text, count=1)
        if n == 0:
            rewritten = f"name: {name}\n" + original_text
        yaml_src.write_text(rewritten)

        result = subprocess.run(
            ["miragen", "validate", str(yaml_src)],
            capture_output=True, text=True, cwd=WORKSPACE,
        )
        if result.returncode != 0:
            return (
                f"ERROR: imported profile failed validation:\n{(result.stdout + result.stderr).strip()}\n"
                "The import was rolled back. Fix the source agent and re-export."
            )

        d.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src_root), str(d))
        created_dir = True

        _compose_add_service(name)
        added_service = True

        if start:
            up = subprocess.run(
                ["docker", "compose", "up", "-d", name],
                capture_output=True, text=True, cwd=WORKSPACE,
            )
            if up.returncode != 0:
                _compose_remove_service(name)
                shutil.rmtree(d, ignore_errors=True)
                return f"ERROR: agent imported but container failed to start:\n{up.stderr.strip()}"

        tail = (
            f"Agent {name} imported from {Path(archive_path).name} and started."
            if start
            else f"Agent {name} imported from {Path(archive_path).name} (not started; use miragen_start_agent)."
        )
        return tail + " Register any missing secrets and check miragen_get_agent_logs."
    except Exception as exc:
        if added_service:
            _compose_remove_service(name)
        if created_dir:
            shutil.rmtree(d, ignore_errors=True)
        return f"ERROR: {exc}"
    finally:
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)


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
                "Exact text to replace — must appear exactly once within tool_name's function "
                "body (decorator through end of function). Copy it from miragen_get_tool_source; "
                "include enough surrounding lines to make it unique."
            ),
            min_length=1,
        ),
    ],
    new_str: Annotated[str, Field(description="Replacement text.")],
) -> str:
    """Edit one tool in an agent's tools.py via exact string replacement scoped to that
    tool's function, then restart the agent.

    Call miragen_get_tool_source first to get the exact current text. Fails without
    modifying anything if tool_name doesn't exist, or old_str is missing or ambiguous
    within that tool's function body. Returns a success message or "ERROR: ...".
    """
    err = _check_agent_name(agent)
    if err:
        return err
    tools_path = _agent_dir(agent) / "tools.py"
    if not tools_path.exists():
        return f"ERROR: tools.py not found for agent '{agent}'. Use miragen_list_agents to see available agents."

    source = tools_path.read_text()
    span = _find_function_span(source, tool_name)
    if span is None:
        return f"ERROR: tool '{tool_name}' not found. Use miragen_list_tools to see registered tools."

    lines = source.splitlines(keepends=True)
    before = "".join(lines[: span[0]])
    target = "".join(lines[span[0] : span[1]])
    after = "".join(lines[span[1] :])

    count = target.count(old_str)
    if count == 0:
        return (
            f"ERROR: old_str not found within tool '{tool_name}'. Use miragen_get_tool_source to "
            "fetch its current source and copy old_str from it exactly."
        )
    if count > 1:
        return (
            f"ERROR: old_str appears {count} times within tool '{tool_name}' — must be unique. "
            "Include more surrounding context."
        )
    tools_path.write_text(before + target.replace(old_str, new_str, 1) + after)
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
    Writing agent.yaml this way bypasses validation — prefer miragen_update_agent_config
    for that file.
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
        result = f"Written {path}"
        if path == "agent.yaml":
            result += "\nnote: this bypassed validation — prefer miragen_update_agent_config"
        return result
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
    Returns "Edited <path>" or "ERROR: ...". Editing agent.yaml this way bypasses
    validation — prefer miragen_update_agent_config for that file.
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
    result = f"Edited {path}"
    if path == "agent.yaml":
        result += "\nnote: this bypassed validation — prefer miragen_update_agent_config"
    return result


# ---- Scheduling -------------------------------------------------------------


async def _fire_trigger(agent: str, prompt: str) -> None:
    # Module-level by contract: the persistent SQLAlchemy job store pickles this
    # callable by reference (module path + qualname). Keep it top-level so jobs
    # scheduled before a restart can be unpickled and fired afterwards.
    _, err = await _agent_request(agent, "POST", "/run", json_body={"prompt": prompt}, timeout=10)
    if err:
        logger.error("retrigger POST to %s failed: %s", agent, err)


def _retrigger_agent(job_id: str) -> str | None:
    """Extract the agent name from a 'retrigger-<agent>-<ts>' job id, or None."""
    m = _RETRIGGER_ID_RE.fullmatch(job_id)
    return m.group("agent") if m else None


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
    when the schedule fires. Schedules are persisted and survive an MCP server restart
    (a miss during downtime still fires if the server comes back within an hour). The
    returned job_id can be passed to miragen_cancel_retrigger; see all scheduled jobs
    with miragen_list_retriggers. Returns a confirmation with the fire time and job_id,
    or "ERROR: ...".
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

        job_id = f"retrigger-{agent}-{fire_at.timestamp():.0f}"
        _scheduler.add_job(
            _fire_trigger,
            trigger=DateTrigger(run_date=fire_at),
            args=[agent, prompt],
            id=job_id,
            replace_existing=True,
            misfire_grace_time=RETRIGGER_MISFIRE_GRACE,
        )
        return (
            f"Retrigger scheduled for {agent} at {fire_at.isoformat()} (job_id: {job_id}). "
            "Cancel it with miragen_cancel_retrigger, or list all with miragen_list_retriggers."
        )
    except Exception as exc:
        return f"ERROR: {exc}"


@mcp.tool(
    name="miragen_list_retriggers",
    annotations=_annotations("List Scheduled Retriggers", read_only=True, idempotent=True),
)
def list_retriggers(
    agent: Annotated[
        str | None,
        Field(
            description=(
                "Optional agent name to filter by. When given, only retriggers scheduled for "
                "that agent are returned. Omit to list scheduled retriggers for every agent."
            ),
        ),
    ] = None,
) -> dict:
    """List the one-shot prompt retriggers currently scheduled (from miragen_set_retrigger).

    Returns: {"count": int, "retriggers": [{"job_id", "agent", "fire_at" (ISO 8601, or null
    if the job is paused), "prompt_preview" (first 200 chars)}, ...]}. Retriggers persist
    across restarts, so this reflects everything still pending. Cancel one with
    miragen_cancel_retrigger. On an invalid `agent` returns {"error": "ERROR: ..."}.
    """
    if agent is not None:
        err = _check_agent_name(agent)
        if err:
            return {"error": err}
    prefix = f"retrigger-{agent}-" if agent is not None else "retrigger-"
    retriggers = []
    for job in _scheduler.get_jobs():
        if not job.id.startswith(prefix):
            continue
        prompt_preview = ""
        if job.args and len(job.args) > 1:
            prompt_preview = str(job.args[1])[:200]
        next_run = getattr(job, "next_run_time", None)
        retriggers.append(
            {
                "job_id": job.id,
                "agent": _retrigger_agent(job.id),
                "fire_at": next_run.isoformat() if next_run else None,
                "prompt_preview": prompt_preview,
            }
        )
    return {"count": len(retriggers), "retriggers": retriggers}


@mcp.tool(
    name="miragen_cancel_retrigger",
    annotations=_annotations("Cancel Scheduled Retrigger", destructive=False, idempotent=True),
)
def cancel_retrigger(
    job_id: Annotated[
        str,
        Field(
            description="Job id as returned by miragen_list_retriggers or by miragen_set_retrigger.",
            min_length=1,
        ),
    ],
) -> str:
    """Cancel a scheduled retrigger by its job id so it never fires.

    Use miragen_list_retriggers to find job ids. Returns "Retrigger <job_id> cancelled." or,
    if there is no such job, an actionable "ERROR: ..." naming miragen_list_retriggers.
    """
    try:
        _scheduler.remove_job(job_id)
        return f"Retrigger '{job_id}' cancelled."
    except JobLookupError:
        return (
            f"ERROR: no retrigger '{job_id}'. Use miragen_list_retriggers to see scheduled jobs."
        )
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
    body, err = await _agent_request(
        agent, "POST", "/run", json_body={"prompt": prompt}, timeout=120
    )
    if err:
        return err
    if isinstance(body, dict):
        return _truncate(str(body.get("output", body)))
    return _truncate(str(body))


# ---- Executor runs ----------------------------------------------------------
# The executor-tier run surfaces of the agents' control APIs (miragen issue
# #33 Phase H): list/get/events/diff/resume/abandon. These proxy to the agent
# over the docker network and return the agent's JSON verbatim so this server
# never re-interprets run state.


@mcp.tool(
    name="miragen_list_runs",
    annotations=_annotations("List Agent Runs", read_only=True, idempotent=True),
)
async def list_runs(
    agent: AgentName,
    limit: Annotated[
        int, Field(description="Maximum runs to return, newest first. Default 20.", ge=1, le=100)
    ] = 20,
    status: Annotated[
        str | None,
        Field(
            description=(
                "Optional status filter: running, succeeded, failed, interrupted, "
                "suspended, or abandoned."
            ),
        ),
    ] = None,
) -> dict:
    """List an agent's recent run records, newest first.

    Returns the agent's own response: {"count": int, "runs": [RunSummary, ...]} where
    each summary carries run_id, status, trigger, timing, usage, and prompt/output
    previews. Suspended/failed runs are resumable (miragen_resume_run); use
    miragen_get_run for one run's full record. On failure returns {"error": "ERROR: ..."}.
    """
    err = _check_agent_name(agent)
    if err:
        return {"error": err}
    params: dict = {"limit": limit}
    if status is not None:
        params["status"] = status
    body, err = await _agent_request(agent, "GET", "/runs", params=params)
    return body if err is None else {"error": err}


@mcp.tool(
    name="miragen_get_run",
    annotations=_annotations("Get Run Record", read_only=True, idempotent=True),
)
async def get_run(agent: AgentName, run_id: RunId) -> dict:
    """Get the full durable record for one run: status, exit_reason, prompt, output,
    error, usage, thread/workspace handles, diff_path, provenance, and snapshot hash.

    miragen is authoritative for run state — this record IS the run's status. On
    failure returns {"error": "ERROR: ..."}.
    """
    err = _check_agent_name(agent)
    if err:
        return {"error": err}
    body, err = await _agent_request(agent, "GET", f"/runs/{run_id}")
    return body if err is None else {"error": err}


@mcp.tool(
    name="miragen_get_run_events",
    annotations=_annotations("Get Run Events", read_only=True, idempotent=True),
)
async def get_run_events(
    agent: AgentName,
    run_id: RunId,
    limit: Annotated[
        int, Field(description="Maximum events per read. Default 200.", ge=1, le=1000)
    ] = 200,
    after: Annotated[
        int | None,
        Field(
            description=(
                "Cursor: return events with seq > after, oldest first, plus next_after/"
                "has_more for paging. Omit for a tail read (newest `limit` events). "
                "Requires the deployed miragen to advertise 'events-cursor/v1' — check "
                "with miragen_check_deployment."
            ),
            ge=0,
        ),
    ] = None,
) -> dict:
    """Read an executor run's normalized event stream (thread/turn/item events plus
    lifecycle timing), either as a tail read or as a cursor replay.

    Every event carries a per-run monotonic `seq` — (run_id, seq) deduplicates reads.
    On failure returns {"error": "ERROR: ..."}.
    """
    err = _check_agent_name(agent)
    if err:
        return {"error": err}
    params: dict = {"limit": limit}
    if after is not None:
        params["after"] = after
    body, err = await _agent_request(agent, "GET", f"/runs/{run_id}/events", params=params)
    if err is not None:
        return {"error": err}
    if after is not None and isinstance(body, dict) and "next_after" not in body:
        body["warning"] = (
            "the deployed miragen ignored the `after` cursor (pre events-cursor/v1); "
            "this is a tail read — run miragen_check_deployment for details"
        )
    return body


@mcp.tool(
    name="miragen_get_run_diff",
    annotations=_annotations("Get Run Diff", read_only=True, idempotent=True),
)
async def get_run_diff(agent: AgentName, run_id: RunId) -> str:
    """Fetch the diff harvested from an executor run's workspace — set exactly once,
    on terminal success (404 before that; partial work on a suspended/failed run is
    resume state, not a harvested diff).

    Output longer than 50,000 characters is truncated. Returns the unified diff text
    (possibly empty — an empty diff is still a harvested diff) or "ERROR: ...".
    """
    err = _check_agent_name(agent)
    if err:
        return err
    body, err = await _agent_request(agent, "GET", f"/runs/{run_id}/diff")
    if err is not None:
        return err
    return _truncate(body if isinstance(body, str) else str(body))


@mcp.tool(
    name="miragen_resume_run",
    annotations=_annotations("Resume Run", open_world=True),
)
async def resume_run(
    agent: AgentName,
    run_id: RunId,
    prompt: Annotated[
        str,
        Field(
            description=(
                "Prompt for the resumed turn — e.g. what to do differently, or simply "
                "'continue'. The executor thread and workspace from the original run "
                "are reused."
            ),
            min_length=1,
        ),
    ],
) -> dict:
    """Give a suspended or failed executor run another turn on its existing thread and
    workspace (budget suspension, timeout, or crash — all resumable states).

    Synchronous: waits up to 300 seconds for the turn to finish, then returns the
    updated run record. If it times out, the turn may still be running — poll
    miragen_get_run. Only suspended/failed runs are resumable (409 otherwise). On
    failure returns {"error": "ERROR: ..."}.
    """
    err = _check_agent_name(agent)
    if err:
        return {"error": err}
    body, err = await _agent_request(
        agent, "POST", f"/runs/{run_id}/resume", json_body={"prompt": prompt}, timeout=300
    )
    return body if err is None else {"error": err}


@mcp.tool(
    name="miragen_abandon_run",
    annotations=_annotations("Abandon Run", destructive=True),
)
async def abandon_run(
    agent: AgentName,
    run_id: RunId,
    discard_workspace: Annotated[
        bool,
        Field(
            description=(
                "Also delete the run's workspace. Default false keeps it for forensics; "
                "true is irreversible — fetch anything you need first."
            ),
        ),
    ] = False,
) -> dict:
    """Abandon a suspended or failed executor run — the human-terminal state. The only
    place where keep-for-forensics vs discard-workspace is decided.

    Irreversible: an abandoned run cannot be resumed. Returns the final run record, or
    {"error": "ERROR: ..."}.
    """
    err = _check_agent_name(agent)
    if err:
        return {"error": err}
    body, err = await _agent_request(
        agent,
        "POST",
        f"/runs/{run_id}/abandon",
        params={"discard_workspace": str(discard_workspace).lower()},
    )
    return body if err is None else {"error": err}


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


def _local_miragen_version() -> str | None:
    """Version of the miragen package installed in THIS container (used by
    `miragen validate`) — can differ from what the agent containers run."""
    try:
        from importlib.metadata import version

        return version("miragen")
    except Exception:
        return None


@mcp.tool(
    name="miragen_check_deployment",
    annotations=_annotations("Check Deployment Compatibility", read_only=True, idempotent=True),
)
async def check_deployment(agent: AgentName) -> dict:
    """Report the deployed miragen version and contract capabilities of one running
    agent, compared against what this MCP server supports.

    Returns {"agent", "deployed_version", "deployed_capabilities",
    "mcp_local_miragen_version" (the version `miragen validate` uses here),
    "supported_capabilities", "missing" (supported here but absent from the
    deployment), "extra" (advertised but unknown to this server — usually a newer
    miragen), "compatible", "notes"}. Run this before relying on the executor-run
    or EDF contract tools; "missing" names exactly which surfaces will not work.
    On failure returns {"error": "ERROR: ..."}.
    """
    err = _check_agent_name(agent)
    if err:
        return {"error": err}
    health, err = await _agent_request(agent, "GET", "/health", timeout=10)
    if err is not None:
        return {"error": err}
    if not isinstance(health, dict):
        return {"error": f"ERROR: unexpected /health response from '{agent}': {str(health)[:200]}"}

    deployed_version = health.get("version")
    deployed_capabilities = health.get("capabilities") or []
    deployed_set = {c for c in deployed_capabilities if isinstance(c, str)}
    missing = sorted(SUPPORTED_CONTRACT_CAPABILITIES - deployed_set)
    extra = sorted(deployed_set - SUPPORTED_CONTRACT_CAPABILITIES)

    notes: list[str] = []
    if "capabilities" not in health:
        notes.append(
            "deployed miragen predates capability discovery entirely — upgrade the agent "
            "image; executor-run inspection may work but the EDF/launch/cursor contracts "
            "will not"
        )
    elif missing:
        notes.append(
            "the deployment does not serve: " + ", ".join(missing) + " — the corresponding "
            "tools/parameters will fail or silently degrade; upgrade the agent image"
        )
    if extra:
        notes.append(
            "the deployment advertises capabilities unknown to this MCP server: "
            + ", ".join(extra) + " — this server may be the outdated side"
        )
    if not notes:
        notes.append("deployment and MCP server agree on the served contracts")

    return {
        "agent": agent,
        "deployed_version": deployed_version,
        "deployed_capabilities": deployed_capabilities,
        "mcp_local_miragen_version": _local_miragen_version(),
        "supported_capabilities": sorted(SUPPORTED_CONTRACT_CAPABILITIES),
        "missing": missing,
        "extra": extra,
        "compatible": not missing,
        "notes": notes,
    }


# Secondary docs: fixed host + strict relative-path allowlist, so this can
# fetch linked design docs (docs/**.md) but can never be steered to another
# host or a non-docs path. Cached per path after first success.
_DOC_PATH_RE = re.compile(r"^(README\.md|docs(/[A-Za-z0-9._-]+)+\.md)$")
_doc_cache: dict[str, str] = {}


@mcp.tool(
    name="miragen_get_doc",
    annotations=_annotations("Get Miragen Doc", read_only=True, idempotent=True, open_world=True),
)
def get_miragen_doc(
    path: Annotated[
        str,
        Field(
            description=(
                "Repository-relative markdown path in ieepirzy/miragen: 'README.md' or a "
                "path under docs/, e.g. 'docs/executor-tier.md' or "
                "'docs/design/mirarun-substrate-contracts.md'. Only .md files under those "
                "locations are retrievable."
            ),
            min_length=1,
        ),
    ],
) -> str:
    """Fetch one miragen documentation file from GitHub — the README links secondary
    docs (docs/executor-tier.md, docs/design/*.md) that this tool can follow, so
    schema/design questions don't stop at the README.

    Output longer than 50,000 characters is truncated. Cached after the first
    successful fetch. Returns the markdown text or "ERROR: ...".
    """
    if ".." in path or not _DOC_PATH_RE.fullmatch(path):
        return (
            f"ERROR: '{path}' is not a retrievable doc path. Allowed: 'README.md' or a "
            "markdown file under 'docs/', e.g. 'docs/executor-tier.md'."
        )
    if path in _doc_cache:
        return _doc_cache[path]
    try:
        resp = httpx.get(
            f"https://raw.githubusercontent.com/ieepirzy/miragen/main/{path}",
            timeout=10,
            follow_redirects=True,
        )
        if resp.status_code == 404:
            return (
                f"ERROR: '{path}' does not exist in ieepirzy/miragen. The README "
                "(miragen_get_readme) links the docs that do."
            )
        resp.raise_for_status()
        _doc_cache[path] = _truncate(resp.text)
        return _doc_cache[path]
    except Exception as exc:
        return f"ERROR: could not fetch {path}: {exc}. Retry, or check network access from the MCP server."


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


# ---- Resources ---------------------------------------------------------------
# Read-only counterparts to the tools above, for clients that browse MCP
# resources instead of (or alongside) calling tools. Unlike tools, which
# return "ERROR: ..." strings, resources raise on failure -- that's FastMCP's
# convention for resources and lets clients get a proper protocol-level error.

# Cache the README after the first successful fetch so repeat resource reads
# don't re-hit the network; a failed fetch is not cached so it can succeed
# later without a server restart.
_readme_cache: str | None = None

_README_FALLBACK = """# miragen (offline schema summary)

Could not fetch the full README from GitHub. Minimal agent.yaml schema:

    name: <lowercase-hyphenated-name>   # must match the agent's directory/container name
    mode: autonomous | interactive | hybrid
    spec:
      model: <provider/model>
      instructions: |
        <what the agent should do>
    tools: []                          # names registered via miragen_register_tool

Validate any draft with miragen_validate_yaml before miragen_create_agent.
"""


@mcp.resource(
    "miragen://agents",
    name="Agents",
    description="JSON list of every miragen agent in the workspace (same data as miragen_list_agents).",
    mime_type="application/json",
)
def agents_resource() -> dict:
    """Expose the agent list as a browsable resource. Mirrors miragen_list_agents."""
    return list_agents()


@mcp.resource(
    "miragen://agents/{name}/agent.yaml",
    name="Agent Profile",
    description="Raw agent.yaml contents for one agent.",
    mime_type="text/yaml",
)
def agent_yaml_resource(name: AgentName) -> str:
    """Raw agent.yaml text for `name` (same bytes as miragen_read_agent_file for that path).

    Raises ValueError if `name` is invalid or no such agent exists, FileNotFoundError if
    the agent exists but has no agent.yaml.
    """
    err = _check_agent_name(name)
    if err:
        raise ValueError(err)
    if not _agent_dir(name).exists():
        raise ValueError(f"agent '{name}' not found. Read miragen://agents to see existing agents.")
    full, path_err = _safe_path(name, "agent.yaml")
    if path_err:
        raise ValueError(path_err)
    if not full.exists():
        raise FileNotFoundError(f"agent.yaml not found for agent '{name}'.")
    return full.read_text()


@mcp.resource(
    "miragen://agents/{name}/tools.py",
    name="Agent Tools Source",
    description="Raw tools.py contents for one agent.",
    mime_type="text/x-python",
)
def agent_tools_resource(name: AgentName) -> str:
    """Raw tools.py text for `name` (same bytes as miragen_read_agent_file for that path).

    Same error conventions as the agent.yaml resource above.
    """
    err = _check_agent_name(name)
    if err:
        raise ValueError(err)
    if not _agent_dir(name).exists():
        raise ValueError(f"agent '{name}' not found. Read miragen://agents to see existing agents.")
    full, path_err = _safe_path(name, "tools.py")
    if path_err:
        raise ValueError(path_err)
    if not full.exists():
        raise FileNotFoundError(f"tools.py not found for agent '{name}'.")
    return full.read_text()


@mcp.resource(
    "miragen://docs/readme",
    name="Miragen Docs",
    description="The miragen agent profile README, fetched once and cached (offline fallback included).",
    mime_type="text/markdown",
)
def readme_resource() -> str:
    """Serve the miragen README (same source as miragen_get_readme), cached after the first
    successful fetch. Falls back to a short built-in schema summary if the MCP server has
    no network access -- this never blocks or retries indefinitely.
    """
    global _readme_cache
    if _readme_cache is not None:
        return _readme_cache
    fetched = get_miragen_readme()
    if fetched.startswith("ERROR"):
        return _README_FALLBACK
    _readme_cache = fetched
    return _readme_cache


# ---- Prompts ------------------------------------------------------------------


@mcp.prompt(name="create-agent")
def create_agent_prompt(
    purpose: Annotated[str, Field(description="What the new agent should do, in plain language.")],
    mode: Annotated[
        str,
        Field(
            description=(
                "Agent mode: 'autonomous' (runs on its own), 'interactive' (only responds "
                "when prompted), or 'hybrid'."
            )
        ),
    ] = "autonomous",
) -> str:
    """Guide the model through drafting, validating, and creating a new miragen agent."""
    return f"""Create a new miragen agent.

Purpose: {purpose}
Mode: {mode}

1. Read miragen://docs/readme (or call miragen_get_readme) for the full agent.yaml schema:
   fields, supported modes, triggers, capabilities, and the approval flow.
2. Draft an agent.yaml profile for this purpose and mode, starting from this skeleton:

   name: <lowercase-hyphenated-name>
   mode: {mode}
   spec:
     model: <provider/model>
     instructions: |
       <what this agent should do and how>
   tools: []

3. Call miragen_validate_yaml with the draft. If it reports errors, fix the YAML and
   validate again -- repeat until it passes.
4. Call miragen_create_agent with the chosen agent name and the validated YAML.
5. Check miragen_get_agent_logs to confirm it started; use miragen_register_tool next if
   this agent needs custom tools.
"""


# ---------------------------------------------------------------------------
# App assembly
# ---------------------------------------------------------------------------


app = mcp.http_app(stateless_http=True, path=MCP_PATH)

if not NO_AUTH:
    if CLIENT_SECRET == "changeme":
        if os.getenv("MCP_ALLOW_DEFAULT_SECRET", "false").lower() != "true":
            raise RuntimeError(
                "MCP_CLIENT_SECRET is unset and defaulting to the well-known value 'changeme' "
                "while auth is enabled (MCP_NO_AUTH is not 'true'). This server holds the "
                "Docker socket -- starting with a publicly-known OAuth client secret is a full "
                "compromise waiting to happen. Set MCP_CLIENT_SECRET to a real secret, set "
                "MCP_NO_AUTH=true for local development without auth, or set "
                "MCP_ALLOW_DEFAULT_SECRET=true to acknowledge the risk and start anyway."
            )
        logger.warning(
            "MCP_CLIENT_SECRET is unset and defaulting to the well-known value 'changeme' "
            "while auth is enabled. This is INSECURE -- proceeding only because "
            "MCP_ALLOW_DEFAULT_SECRET=true was set. Set a real MCP_CLIENT_SECRET as soon as "
            "possible."
        )

    auth = OAuthProvider(
        base_url=BASE_URL,
        clients={CLIENT_ID: CLIENT_SECRET},
        token_ttl=604800,
        auto_approve=AUTO_APPROVE,
        public_registration=PUBLIC_REGISTRATION,
        mcp_path=MCP_PATH,
    )

    # Take origo's routes and state from the provider's own app rather than
    # re-declaring them here. origo's endpoints read eleven app.state attributes
    # and that set grows between versions (0.1.9 added allow_private_cimd as part
    # of the SSRF fix). A hand-written subset imports fine and then 500s at
    # request time on /authorize -- invisible until a client tries to authorise.
    # Sourcing both from the provider means origo owns its own contract, and we
    # also pick up /userinfo and /.well-known/openid-configuration for free.
    oauth_app = auth.asgi_app()
    for route in reversed(oauth_app.routes):
        app.router.routes.insert(0, route)

    app.add_middleware(OAuthMiddleware, provider=auth)

    for _key, _value in vars(oauth_app.state)["_state"].items():
        setattr(app.state, _key, _value)


_original_lifespan = app.router.lifespan_context


@asynccontextmanager
async def _lifespan(scope):
    async with _original_lifespan(scope):
        # The persistent retrigger store writes retriggers.sqlite here, so the
        # workspace must exist before the scheduler starts.
        WORKSPACE.mkdir(parents=True, exist_ok=True)
        _ensure_agent_network()
        _scheduler.start()
        yield
        _scheduler.shutdown(wait=False)


app.router.lifespan_context = _lifespan

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
