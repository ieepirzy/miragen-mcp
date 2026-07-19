"""
Stubs for heavy deps that have side-effects at import time (docker socket,
OAuth provider, FastMCP HTTP app). Must be in conftest.py so they are
installed into sys.modules before server.py is first imported by any test.
"""
import os
import sys
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock

# server.py refuses to start when auth is enabled and MCP_CLIENT_SECRET is left at
# its "changeme" default (see the MCP_ALLOW_DEFAULT_SECRET guard). OAuthProvider is
# fully mocked below regardless, so the real auth path is never exercised here —
# opt in to acknowledge the default secret rather than have every test set its own.
os.environ.setdefault("MCP_ALLOW_DEFAULT_SECRET", "true")

# ── stdlib-only deps that may not be installed in dev ────────────────────────

sys.modules.setdefault("uvicorn", MagicMock())

_starlette_routing = MagicMock()
_starlette_routing.Route = MagicMock(side_effect=lambda path, endpoint, **kw: (path, endpoint))
sys.modules.setdefault("starlette", MagicMock())
sys.modules.setdefault("starlette.routing", _starlette_routing)
sys.modules.setdefault("starlette.applications", MagicMock())


# ── docker ───────────────────────────────────────────────────────────────────

class _NotFound(Exception):
    pass

_docker_errors = MagicMock()
_docker_errors.NotFound = _NotFound

_docker_mod = MagicMock()
_docker_mod.from_env.return_value = MagicMock()
_docker_mod.errors = _docker_errors

sys.modules["docker"] = _docker_mod
sys.modules["docker.errors"] = _docker_errors


# ── apscheduler ──────────────────────────────────────────────────────────────

sys.modules["apscheduler"] = MagicMock()
sys.modules["apscheduler.schedulers"] = MagicMock()
sys.modules["apscheduler.schedulers.asyncio"] = MagicMock()
sys.modules["apscheduler.triggers"] = MagicMock()
sys.modules["apscheduler.triggers.date"] = MagicMock()


# ── origo ─────────────────────────────────────────────────────────────────────

_origo_provider = MagicMock(
    storage=MagicMock(),
    public_registration=False,
    auto_approve=False,
)

# server.py adopts origo's routes and state from provider.asgi_app() rather than
# re-declaring them, so the mock must expose that shape: an app with iterable
# routes, and a Starlette-style State whose dict lives in `_state` (server.py
# reads it as vars(state)["_state"]).
#
# Stubbed rather than imported from starlette: starlette is mocked out above and
# is NOT installed in CI, which only installs pytest/httpx/pyyaml/pydantic. A real
# `from starlette.datastructures import State` here fails at collection.
_origo_oauth_app = MagicMock()
_origo_oauth_app.routes = []
_origo_oauth_app.state = SimpleNamespace(_state={})
_origo_provider.asgi_app.return_value = _origo_oauth_app

_origo_mod = MagicMock()
_origo_mod.OAuthProvider.return_value = _origo_provider
sys.modules["origo"] = _origo_mod


# ── fastmcp ───────────────────────────────────────────────────────────────────
# mcp.tool() must be a pass-through so the decorated functions remain callable.

def _passthrough_decorator(func):
    return func


@asynccontextmanager
async def _noop_lifespan(scope):
    yield


class _FakeRouter:
    def __init__(self):
        self.routes = []
        self.lifespan_context = _noop_lifespan


class _FakeApp:
    def __init__(self):
        self.router = _FakeRouter()
        self.state = SimpleNamespace()

    def add_middleware(self, *a, **kw):
        pass


_fake_mcp = MagicMock()
_fake_mcp.tool.return_value = _passthrough_decorator
_fake_mcp.resource.return_value = _passthrough_decorator
_fake_mcp.prompt.return_value = _passthrough_decorator
_fake_mcp.http_app.return_value = _FakeApp()

_fastmcp_mod = MagicMock()
_fastmcp_mod.FastMCP.return_value = _fake_mcp
sys.modules["fastmcp"] = _fastmcp_mod
