# miragen-mcp

An MCP (Model Context Protocol) server for managing [Miragen](https://github.com/ieepirzy/miragen) autonomous agents. Exposes agent lifecycle, tooling, and filesystem operations as MCP tools so AI clients like Claude can create, configure, and control agents in real time.

## Overview

miragen-mcp acts as an orchestration layer over Docker. Each Miragen agent runs in its own container within a shared `miragen-net` bridge network. The MCP server manages the central `compose.yml`, agent workspaces on the host filesystem, and communicates with running agents over HTTP.

```
Claude / AI Client
       │  MCP (OAuth2)
       ▼
 miragen-mcp server
       │  Docker socket + HTTP
       ▼
 ┌──────────────────────────────┐
 │        miragen-net           │
 │  ┌──────────┐ ┌──────────┐  │
 │  │ agent-a  │ │ agent-b  │  │
 │  └──────────┘ └──────────┘  │
 └──────────────────────────────┘
```

## Features

- **Agent lifecycle** — create, start, stop, restart, delete agents
- **Tool management** — register, edit, and delete `@register`-decorated tools in an agent's `tools.py` using AST-based parsing
- **Filesystem access** — read, write, and edit files in agent workspaces with path traversal protection
- **Prompt delivery** — send prompts to running agents and retrieve responses
- **Scheduling** — schedule, list, and cancel one-shot prompts with a delay or at a specific time (ISO 8601); schedules persist in a SQLite job store on the workspace volume and survive an MCP server restart
- **Backup & migration** — export an agent workspace to a tarball and re-import it under a new name, on the same host or another (safe extraction, profile revalidated)
- **Validation** — validate agent YAML profiles before applying them, and update a running agent's `agent.yaml` through a validate → apply → restart → rollback flow instead of a raw file write
- **Logging** — tail Docker container logs per agent

## Tools

All tools carry a `miragen_` prefix so they stay unambiguous alongside other MCP servers, and each declares MCP tool annotations (`readOnlyHint`, `destructiveHint`, `idempotentHint`, `openWorldHint`) so clients can distinguish safe reads from destructive operations.

| Tool | Hints | Description |
|------|-------|-------------|
| `miragen_list_agents` | read-only | List all agents with status, mode, and model |
| `miragen_get_agent` | read-only | Full agent info: YAML config, container status, tools |
| `miragen_create_agent` | write | Create workspace, register in compose, start container |
| `miragen_update_agent_config` | **destructive**, idempotent | Validate and replace an agent's `agent.yaml`, then restart (rolls back on failure) |
| `miragen_start_agent` | write, idempotent | Start agent container |
| `miragen_restart_agent` | write, idempotent | Restart agent container |
| `miragen_stop_agent` | write, idempotent | Stop agent container |
| `miragen_delete_agent` | **destructive** | Stop, remove container, and delete workspace |
| `miragen_get_agent_logs` | read-only | Tail Docker container logs (max 1000 lines) |
| `miragen_export_agent` | read-only | Tar an agent workspace to `exports/` for backup/migration (excludes runs, history, caches) |
| `miragen_import_agent` | write | Import an agent from an export tarball under a new name (safe extraction, validated) |
| `miragen_list_tools` | read-only | List `@register` tools in agent's `tools.py` |
| `miragen_get_tool_source` | read-only | Get source code of a specific tool |
| `miragen_register_tool` | write | Append new tool to `tools.py` and update `agent.yaml` |
| `miragen_edit_tool` | **destructive** | String-replace edit a tool (restarts agent) |
| `miragen_delete_tool` | **destructive** | Remove tool from `tools.py` and `agent.yaml` |
| `miragen_read_agent_file` | read-only | Read a file from agent workspace |
| `miragen_write_agent_file` | **destructive** | Write/create a file in agent workspace |
| `miragen_edit_agent_file` | **destructive** | String-replace edit a file in agent workspace |
| `miragen_run_agent` | open-world | Send a prompt to agent's `/run` endpoint |
| `miragen_set_retrigger` | open-world | Schedule a one-shot prompt (delay or absolute time); survives restarts |
| `miragen_list_retriggers` | read-only | List scheduled retriggers (job id, agent, fire time, prompt preview); filter by agent |
| `miragen_cancel_retrigger` | idempotent | Cancel a scheduled retrigger by job id |
| `miragen_list_runs` | read-only | List an agent's run records, newest first (optional status filter) |
| `miragen_get_run` | read-only | Full durable record for one run (status, usage, provenance, handles) |
| `miragen_get_run_events` | read-only | Run event stream: tail read or cursor replay (`after`/`limit`) |
| `miragen_get_run_diff` | read-only | Harvested workspace diff of a succeeded executor run |
| `miragen_resume_run` | open-world | Give a suspended/failed executor run another turn |
| `miragen_abandon_run` | **destructive** | Human-terminal abandon; optional workspace discard |
| `miragen_check_deployment` | read-only | Deployed miragen version/capabilities vs what this server supports |
| `miragen_validate_yaml` | read-only | Validate agent YAML using miragen CLI |
| `miragen_get_readme` | read-only | Fetch latest Miragen README from GitHub |
| `miragen_get_doc` | read-only | Fetch a linked secondary doc (`docs/**.md`) from the miragen repo |

Input guardrails: agent names must match `[a-z0-9][a-z0-9_-]{0,62}` (they double as Docker container names, and this blocks path traversal), `miragen_register_tool` syntax-checks the submitted source and requires it to define the named `@register` function, and unbounded outputs (logs, file reads, agent responses) are truncated at 50,000 characters.

## Resources & Prompts

Alongside tools, the server exposes read-only **MCP resources** for clients that browse
context instead of (or in addition to) calling tools, and one **MCP prompt** to bootstrap
new agents. Unlike tools — which return `"ERROR: ..."` strings — resources raise on
failure (`ValueError` for an invalid or unknown agent name, `FileNotFoundError` if the
agent exists but the specific file doesn't), matching FastMCP's convention for resources.

| Resource | MIME type | Description |
|----------|-----------|--------------|
| `miragen://agents` | `application/json` | Same data as `miragen_list_agents` |
| `miragen://agents/{name}/agent.yaml` | `text/yaml` | Raw `agent.yaml` for one agent |
| `miragen://agents/{name}/tools.py` | `text/x-python` | Raw `tools.py` source for one agent |
| `miragen://docs/readme` | `text/markdown` | The miragen README, fetched once and cached, with a built-in offline fallback |

| Prompt | Arguments | Description |
|--------|-----------|--------------|
| `create-agent` | `purpose` (required), `mode` (default `"autonomous"`) | Walks the model through reading the schema docs, drafting an `agent.yaml`, validating it with `miragen_validate_yaml`, and creating it with `miragen_create_agent` |

## Requirements

- Docker with the Compose plugin
- Python 3.12 (or use the provided Docker image)
- Access to the Docker socket

## Installation

### Docker Compose (recommended)

Two environments run this service and need **opposite** ingress shapes. One
`compose.yml` handles both, selected by `COMPOSE_PROFILES` — no `-f` flags, no
second file, because Portainer locks a git-stack's Compose path at creation and
gives no way to layer a second file onto a stack that already exists:

```bash
# VPS: NPM runs on the same host, reached by container-name DNS over a shared
# `proxy` network (which NPM's own stack must already have created).
DOCKER_GID=$(getent group docker | cut -d: -f3) \
  COMPOSE_PROFILES=vps docker compose up -d --build

# homelab (or anywhere the reverse proxy is on a different machine): published
# on loopback plus BOUND_IP, which should be this host's WireGuard address.
DOCKER_GID=$(getent group docker | cut -d: -f3) BOUND_IP=10.x.x.x \
  COMPOSE_PROFILES=homelab docker compose up -d --build
```

In Portainer, set `COMPOSE_PROFILES` (`vps` or `homelab`) as a stack environment
variable — that's editable at any time, unlike the Compose path. `DOCKER_GID` is
always required — it must match `getent group docker | cut -d: -f3` **on the
deployment host**, not wherever you're reading this from, or the container
cannot reach `/var/run/docker.sock`.

> **Do not make `BOUND_IP` a required var (`:?`) in compose.yml.** Compose
> interpolates every service block in the file up front regardless of which
> profile is active, so a required var referenced only by the homelab service
> would block a VPS deploy that (correctly) never sets it. It defaults to
> `127.0.0.1` instead — forgetting it fails safe (loopback-only, unreachable)
> rather than failing the whole file to load. Verified with `docker compose up`
> under both profiles, not just `config` — `config` alone doesn't surface this,
> since interpolation happens either way.
>
> **Do not give both ingress shapes to one service block.** That broke
> production once already: adding the homelab's port-publish unconditionally
> meant also dropping the VPS's `proxy` network (compose hard-fails if
> `external: true` is declared for a network the host doesn't have), and NPM
> lost `miragen-mcp:8000` — a live 502 until this was split into two
> profile-gated services in the same file.

### Docker (single container, no ingress split)

```bash
docker build --build-arg DOCKER_GID=$(getent group docker | cut -d: -f3) -t miragen-mcp .
docker run -d \
  --name miragen-mcp \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v /opt/miragen:/opt/miragen \
  -e MCP_BASE_URL=https://your-domain.example.com \
  -e MCP_CLIENT_SECRET=your-secret \
  -e ANTHROPIC_API_KEY=sk-ant-... \
  -p 127.0.0.1:8000:8000 \
  miragen-mcp
```

### Local (development)

```bash
pip install -r requirements.txt
MCP_BASE_URL=http://localhost:8000 MCP_NO_AUTH=true uvicorn server:app --host 0.0.0.0 --port 8000
```

`MCP_NO_AUTH=true` skips OAuth entirely, which is the simplest way to run locally
without provisioning a client secret. If you'd rather exercise the OAuth flow
locally, set a real `MCP_CLIENT_SECRET` instead — the server refuses to start with
auth enabled and no `MCP_CLIENT_SECRET` set (see [Authentication](#authentication)).

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `MIRAGEN_WORKSPACE` | `/opt/miragen` | Root workspace directory on the host |
| `MIRAGEN_BASE_IMAGE` | `ghcr.io/ieepirzy/miragen:latest` | Docker image used for new agent containers |
| `MIRAGEN_INTERNAL_TOKEN` | *(empty)* | Sent as `X-Miragen-Token` on calls to the agents' HTTP control APIs — set it to the same value the agent containers use, if they enforce one |
| `MCP_BASE_URL` | *(required)* | Public base URL of this server — used for OAuth |
| `MCP_CLIENT_ID` | `miragen-mcp` | OAuth client ID |
| `MCP_CLIENT_SECRET` | `changeme` | OAuth client secret — **must be set explicitly if auth is enabled** |
| `MCP_NO_AUTH` | `false` | Disable OAuth entirely (local development only) |
| `MCP_ALLOW_DEFAULT_SECRET` | `false` | Explicitly acknowledge and allow starting with the default `changeme` secret while auth is enabled (not recommended) |

**LLM provider keys** (pass at least one):

| Variable | Notes |
|----------|-------|
| `ANTHROPIC_API_KEY` | |
| `OPENAI_API_KEY` | |
| `DEEPSEEK_API_KEY` | |
| `GEMINI_API_KEY` | |
| `XAI_API_KEY` | |
| `MISTRAL_API_KEY` | |
| `GROQ_API_KEY` | |
| `COHERE_API_KEY` | |

Append `_FILE` to any key variable to read from a Docker secret instead (e.g. `ANTHROPIC_API_KEY_FILE=/run/secrets/anthropic_key`).

## Workspace Layout

```
$MIRAGEN_WORKSPACE/
├── compose.yml          ← managed by miragen-mcp
├── retriggers.sqlite    ← persistent scheduled-retrigger store (APScheduler)
├── exports/             ← agent export tarballs (miragen_export_agent)
└── agents/
    ├── agent-a/
    │   ├── agent.yaml   ← Miragen profile
    │   └── tools.py     ← @register decorated tools
    └── agent-b/
        ├── agent.yaml
        └── tools.py
```

Both `retriggers.sqlite` and `exports/` live on the mounted workspace volume, so scheduled retriggers and agent exports survive container restarts.

Agent workspace directories are mounted to `/agent` inside each container, so filesystem changes made through the MCP tools are visible to the running agent immediately (tool changes trigger an automatic restart).

### Backup, restore, and cloning

`miragen_export_agent` tars an agent's workspace to `exports/<agent>-<timestamp>.tar.gz`, excluding `runs/`, `history.json`, `__pycache__/`, and any single file over 10 MB (skipped files are reported). The compose entry and secrets are **not** exported — an import regenerates them from the importing server's environment.

`miragen_import_agent` extracts an export tarball under a new name, revalidates the profile, registers it in compose, and starts it. Extraction uses tarfile's `data` filter, so archives with absolute paths, `..` traversal, or links are rejected.

```text
# back up, then restore as a clone
miragen_export_agent(agent="briefing")
  → exports/briefing-20260723-120000.tar.gz
miragen_import_agent(name="briefing-staging",
                     archive_path="exports/briefing-20260723-120000.tar.gz")
```

The exported archive lives outside every agent workspace, so `miragen_read_agent_file` cannot fetch it; copy it off the host (or between hosts) yourself, then import it on the destination server.

## Authentication

The server is protected with OAuth2 via the [Origo](https://github.com/ieepirzy/origo) library. Tokens are valid for 7 days. Configure your MCP client with the client credentials defined by `MCP_CLIENT_ID` and `MCP_CLIENT_SECRET`.

If `MCP_NO_AUTH` is not `true`, the server refuses to start when `MCP_CLIENT_SECRET` is left unset (defaulting to the well-known value `changeme`) — this server holds the Docker socket, so booting with a publicly-known OAuth secret is a full compromise waiting to happen. Set a real `MCP_CLIENT_SECRET`, set `MCP_NO_AUTH=true` for auth-free local development, or set `MCP_ALLOW_DEFAULT_SECRET=true` to explicitly acknowledge the risk and start anyway (not recommended outside of throwaway testing).

## Development

```bash
pip install -r requirements.txt
pytest tests/
```

Tests mock Docker, OAuth, and the scheduler — no running Docker daemon required.

## License

MIT
