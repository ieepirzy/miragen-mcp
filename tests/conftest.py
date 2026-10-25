"""
Stubs for heavy deps that have side-effects at import time (OAuth provider,
FastMCP HTTP app). Must be in conftest.py so they are installed into
sys.modules before server.py is first imported by any test.

The docker/apscheduler stubs that used to live here are gone with the
lifecycle extraction: server.py no longer imports either — everything that
touched the Docker socket or the scheduler now lives in the miragend daemon
(miragen repo), and server.py only speaks HTTP to it (mocked per-test via
the server._daemon_transport seam).
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


# fastmcp.server.middleware.Middleware is subclassed by server.py's
# ReadOnlyGuardMiddleware, so it must be a real class (an instance of
# MagicMock cannot be used as a base class) — same for ToolError, which must
# be a real exception type. get_http_request is called and its RuntimeError
# caught, so it must be a real callable too.


class _StubMiddleware:
    async def on_call_tool(self, context, call_next):
        return await call_next(context)

    async def on_list_tools(self, context, call_next):
        return await call_next(context)


class _StubToolError(Exception):
    pass


def _stub_get_http_request():
    raise RuntimeError("no active HTTP request (test stub)")


_fastmcp_middleware_mod = MagicMock()
_fastmcp_middleware_mod.Middleware = _StubMiddleware
sys.modules["fastmcp.server.middleware"] = _fastmcp_middleware_mod

_fastmcp_exceptions_mod = MagicMock()
_fastmcp_exceptions_mod.ToolError = _StubToolError
sys.modules["fastmcp.exceptions"] = _fastmcp_exceptions_mod

_fastmcp_dependencies_mod = MagicMock()
_fastmcp_dependencies_mod.get_http_request = _stub_get_http_request
sys.modules["fastmcp.server.dependencies"] = _fastmcp_dependencies_mod
