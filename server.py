import ast
import logging
import os
import shutil
import subprocess
import tempfile
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

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
MIRAGEN_BASE_IMAGE = os.getenv("MIRAGEN_BASE_IMAGE", "ghcr.io/ieepirzy/miragen:latest")

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
        "secrets": {"anthropic_key": {"external": True}},
        "services": {},
        "networks": {"miragen-net": {"external": True}},
    }


def _compose_add_service(name: str) -> None:
    data = _compose_load()
    data.setdefault("services", {})[name] = {
        "image": MIRAGEN_BASE_IMAGE,
        "container_name": name,
        "restart": "unless-stopped",
        "secrets": ["anthropic_key"],
        "environment": {
            "ANTHROPIC_API_KEY_FILE": "/run/secrets/anthropic_key",
            "AGENT_PROFILE": "agent.yaml",
        },
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


# ---------------------------------------------------------------------------
# FastMCP server
# ---------------------------------------------------------------------------

mcp = FastMCP("miragen-mcp")


# ---- Agent management -------------------------------------------------------


@mcp.tool()
def list_agents() -> list[dict]:
    """Return all agents found in the workspace with status, mode, and model."""
    if not AGENTS_DIR.exists():
        return []
    result = []
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
    return result


@mcp.tool()
def get_agent(name: str) -> dict:
    """Return full agent info: raw yaml, container status, and whether tools.py exists."""
    d = _agent_dir(name)
    if not d.exists():
        return {"error": f"ERROR: agent '{name}' not found"}
    yaml_path = d / "agent.yaml"
    return {
        "name": name,
        "yaml": yaml_path.read_text() if yaml_path.exists() else "",
        "status": _container_status(name),
        "has_tools": (d / "tools.py").exists(),
    }


@mcp.tool()
def create_agent(name: str, yaml_source: str) -> str:
    """Create a new agent workspace, register it in the central compose.yml, and start the container."""
    d = _agent_dir(name)
    if d.exists():
        return f"ERROR: agent '{name}' already exists"
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
            return f"ERROR: validation failed:\n{(result.stdout + result.stderr).strip()}"

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

        return f"Agent {name} created and started."
    except Exception as exc:
        shutil.rmtree(d, ignore_errors=True)
        return f"ERROR: {exc}"


@mcp.tool()
def start_agent(name: str) -> str:
    """Start an agent container via docker compose (works even if container doesn't exist yet)."""
    if not _agent_dir(name).exists():
        return f"ERROR: agent '{name}' not found in workspace"
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


@mcp.tool()
def restart_agent(name: str) -> str:
    """Restart the Docker container for an agent."""
    try:
        _docker.containers.get(name).restart()
        return f"Agent {name} restarted."
    except docker.errors.NotFound:
        return f"ERROR: container '{name}' not found"
    except Exception as exc:
        return f"ERROR: {exc}"


@mcp.tool()
def stop_agent(name: str) -> str:
    """Stop the Docker container for an agent."""
    try:
        _docker.containers.get(name).stop()
        return f"Agent {name} stopped."
    except docker.errors.NotFound:
        return f"ERROR: container '{name}' not found"
    except Exception as exc:
        return f"ERROR: {exc}"


@mcp.tool()
def delete_agent(name: str) -> str:
    """Stop and remove the container, remove from central compose.yml, delete workspace."""
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


@mcp.tool()
def get_agent_logs(name: str, tail: int = 50) -> str:
    """Return the last `tail` lines of Docker logs for an agent container."""
    try:
        logs = _docker.containers.get(name).logs(tail=tail, stream=False)
        return logs.decode("utf-8", errors="replace")
    except docker.errors.NotFound:
        return f"ERROR: container '{name}' not found"
    except Exception as exc:
        return f"ERROR: {exc}"


# ---- Tool management --------------------------------------------------------


@mcp.tool()
def list_tools(agent: str) -> list[dict]:
    """List all @register-decorated functions in an agent's tools.py."""
    p = _agent_dir(agent) / "tools.py"
    if not p.exists():
        return []
    return _parse_registered_tools(p.read_text())


@mcp.tool()
def get_tool_source(agent: str, tool_name: str) -> str:
    """Return the full source (including decorator) of a named function in tools.py."""
    p = _agent_dir(agent) / "tools.py"
    if not p.exists():
        return f"ERROR: tools.py not found for agent '{agent}'"
    source = p.read_text()
    span = _find_function_span(source, tool_name)
    if span is None:
        return f"ERROR: tool '{tool_name}' not found in {agent}/tools.py"
    lines = source.splitlines(keepends=True)
    return "".join(lines[span[0]:span[1]])


@mcp.tool()
def register_tool(agent: str, tool_name: str, source: str) -> str:
    """Append a @register-decorated async function to tools.py, update agent.yaml, restart agent."""
    tools_path = _agent_dir(agent) / "tools.py"
    yaml_path = _agent_dir(agent) / "agent.yaml"
    if not tools_path.exists():
        return f"ERROR: tools.py not found for agent '{agent}'"

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


@mcp.tool()
def edit_tool(agent: str, tool_name: str, old_str: str, new_str: str) -> str:
    """str_replace on tools.py — old_str must appear exactly once. Restarts agent on success."""
    tools_path = _agent_dir(agent) / "tools.py"
    if not tools_path.exists():
        return f"ERROR: tools.py not found for agent '{agent}'"
    content = tools_path.read_text()
    count = content.count(old_str)
    if count == 0:
        return "ERROR: old_str not found in tools.py"
    if count > 1:
        return f"ERROR: old_str appears {count} times — must be unique"
    tools_path.write_text(content.replace(old_str, new_str, 1))
    result = restart_agent(agent)
    if result.startswith("ERROR"):
        return f"Tool edited but restart failed: {result}"
    return f"Tool '{tool_name}' edited and {agent} restarted."


@mcp.tool()
def delete_tool(agent: str, tool_name: str) -> str:
    """Remove a named function (and its decorator) from tools.py, remove from yaml, restart agent."""
    tools_path = _agent_dir(agent) / "tools.py"
    yaml_path = _agent_dir(agent) / "agent.yaml"
    if not tools_path.exists():
        return f"ERROR: tools.py not found for agent '{agent}'"

    source = tools_path.read_text()
    span = _find_function_span(source, tool_name)
    if span is None:
        return f"ERROR: tool '{tool_name}' not found"

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


# ---- Filesystem tools -------------------------------------------------------


@mcp.tool()
def read_file(agent: str, path: str) -> str:
    """Read a file from the agent workspace. Path is relative to the agent directory."""
    full, err = _safe_path(agent, path)
    if err:
        return err
    try:
        return full.read_text()
    except FileNotFoundError:
        return f"ERROR: file not found: {path}"
    except Exception as exc:
        return f"ERROR: {exc}"


@mcp.tool()
def write_file(agent: str, path: str, content: str) -> str:
    """Overwrite (or create) a file in the agent workspace."""
    full, err = _safe_path(agent, path)
    if err:
        return err
    try:
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content)
        return f"Written {path}"
    except Exception as exc:
        return f"ERROR: {exc}"


@mcp.tool()
def edit_file(agent: str, path: str, old_str: str, new_str: str) -> str:
    """str_replace on a file in the agent workspace — old_str must appear exactly once."""
    full, err = _safe_path(agent, path)
    if err:
        return err
    try:
        content = full.read_text()
    except FileNotFoundError:
        return f"ERROR: file not found: {path}"
    count = content.count(old_str)
    if count == 0:
        return "ERROR: old_str not found"
    if count > 1:
        return f"ERROR: old_str appears {count} times — must be unique"
    full.write_text(content.replace(old_str, new_str, 1))
    return f"Edited {path}"


# ---- Scheduling -------------------------------------------------------------


async def _fire_trigger(agent: str) -> None:
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.post(f"http://{agent}:8000/run", json={"prompt": ""}, timeout=10)
            resp.raise_for_status()
        except Exception as exc:
            logger.error("retrigger POST to %s failed: %s", agent, exc)


@mcp.tool()
def set_retrigger(agent: str, delay_seconds: int | None = None, at: str | None = None) -> str:
    """Schedule a one-shot POST to the agent's /run endpoint. Provide delay_seconds OR at (ISO datetime)."""
    if (delay_seconds is None) == (at is None):
        return "ERROR: provide exactly one of delay_seconds or at"
    try:
        if delay_seconds is not None:
            fire_at = datetime.now(timezone.utc) + timedelta(seconds=delay_seconds)
        else:
            fire_at = datetime.fromisoformat(at)  # type: ignore[arg-type]
            if fire_at.tzinfo is None:
                fire_at = fire_at.replace(tzinfo=timezone.utc)

        _scheduler.add_job(
            _fire_trigger,
            trigger=DateTrigger(run_date=fire_at),
            args=[agent],
            id=f"retrigger-{agent}-{fire_at.timestamp():.0f}",
            replace_existing=True,
        )
        return f"Retrigger scheduled for {agent} at {fire_at.isoformat()}."
    except Exception as exc:
        return f"ERROR: {exc}"


# ---- Meta -------------------------------------------------------------------


@mcp.tool()
def get_miragen_readme() -> str:
    """Fetch the latest miragen README from GitHub."""
    try:
        resp = httpx.get(
            "https://raw.githubusercontent.com/ieepirzy/miragen/main/README.md",
            timeout=10,
            follow_redirects=True,
        )
        resp.raise_for_status()
        return resp.text
    except Exception as exc:
        return f"ERROR: {exc}"


@mcp.tool()
def validate_yaml(source: str) -> str:
    """Validate a miragen agent YAML profile using the miragen CLI."""
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


auth = OAuthProvider(
    base_url=BASE_URL,
    clients={CLIENT_ID: CLIENT_SECRET},
    token_ttl=604800,
)

app = mcp.http_app(stateless_http=True)

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
app.state.mcp_path = "/mcp"
app.state.storage = auth.storage
app.state.public_registration = auth.public_registration
app.state.auto_approve = auth.auto_approve


_original_lifespan = app.router.lifespan_context


@asynccontextmanager
async def _lifespan(scope):
    async with _original_lifespan(scope):
        _scheduler.start()
        yield
        _scheduler.shutdown(wait=False)


app.router.lifespan_context = _lifespan

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
