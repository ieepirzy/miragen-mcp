# miragen-mcp

An MCP (Model Context Protocol) server for managing [Miragen](https://github.com/ieepirzy/miragen) autonomous agents. Exposes agent lifecycle, tooling, and filesystem operations as MCP tools so AI clients like Claude can create, configure, and control agents in real time.

## Overview

miragen-mcp is a **thin MCP adapter** over the swarm's management plane. The
Docker socket, the workspace (`compose.yml`, `agents/`), and all lifecycle
state belong to **miragend** — the lifecycle daemon that ships in the
[miragen](https://github.com/ieepirzy/miragen) repo (`pip install
miragen[daemon]`, image `ghcr.io/ieepirzy/miragend`). This server translates
MCP tool calls from AI clients into miragend's HTTP API, and talks to the
agents directly (over `miragen-net`) only for run/approval traffic.

```
Claude / AI Client            mirarun (control plane)
       │  MCP (OAuth2)               │  HTTP (bearer token)
       ▼                             │
 miragen-mcp ──────────┐             │
   │ HTTP              │ HTTP        │
   │ (runs/approvals)  ▼             ▼
   │                 miragend ◄──────┘
   │                   │  Docker socket + workspace
 ┌─┴───────────────────┴────────┐
 │          miragen-net         │
 │   ┌──────────┐ ┌──────────┐  │
 │   │ agent-a  │ │ agent-b  │  │
 │   └──────────┘ └──────────┘  │
 └──────────────────────────────┘
```

The split is a security boundary: this container is the OAuth-fronted,
internet-adjacent one, and it holds **no** Docker socket, no workspace mount,
and no persistent state — compromising it no longer means owning the host.
Swarm membership is likewise trustworthy by construction: the registry is
derived from state only miragend maintains, so agents can neither announce
themselves nor discover peers.

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
| `miragen_export_agent` | write | Tar an agent workspace to `exports/` for backup/migration (excludes runs, history, caches) |
| `miragen_import_agent` | write | Import an agent from an export tarball under a new name (safe extraction, validated) |
| `miragen_list_tools` | read-only | List `@register` tools in agent's `tools.py` |
| `miragen_get_tool_source` | read-only | Get source code of a specific tool |
| `miragen_register_tool` | write | Append new tool to `tools.py` and update `agent.yaml` |
| `miragen_edit_tool` | **destructive** | String-replace edit a tool (restarts agent) |
| `miragen_delete_tool` | **destructive** | Remove tool from `tools.py` and `agent.yaml` |
| `miragen_read_agent_file` | read-only | Read a file from agent workspace |
| `miragen_write_agent_file` | **destructive** | Write/create a file in agent workspace |
| `miragen_edit_agent_file` | **destructive** | String-replace edit a file in agent workspace |
| `miragen_run_agent` | open-world | Send a prompt to agent's `/run` endpoint (waits up to 120s; appends `(run_id: ...)` when the agent returns one) |
| `miragen_run_agent_async` | open-world | Start a run on `/run/async` without waiting; returns immediately with a `run_id` to poll |
| `miragen_set_retrigger` | open-world | Schedule a one-shot prompt (delay or absolute time); survives restarts |
| `miragen_list_retriggers` | read-only | List scheduled retriggers (job id, agent, fire time, prompt preview); filter by agent |
| `miragen_cancel_retrigger` | idempotent | Cancel a scheduled retrigger by job id |
| `miragen_list_runs` | read-only | List an agent's run records, newest first (optional status filter) |
| `miragen_get_run` | read-only | Full durable record for one run (status, usage, provenance, handles) |
| `miragen_get_run_events` | read-only | Run event stream: tail read or cursor replay (`after`/`limit`) |
| `miragen_get_run_diff` | read-only | Harvested workspace diff of a succeeded executor run |
| `miragen_resume_run` | open-world | Give a suspended/failed executor run another turn |
| `miragen_abandon_run` | **destructive** | Human-terminal abandon; optional workspace discard |
| `miragen_list_pending_approvals` | read-only | List an agent's pending gated tool-call approval requests |
| `miragen_resolve_approval` | write | Approve or deny a pending gated tool call (see [Observability & approvals](#observability--approvals) for the prompt-injection warning) |
| `miragen_check_deployment` | read-only | Deployed miragen version/capabilities vs what this server supports |
| `miragen_validate_yaml` | read-only | Validate agent YAML via the miragend daemon |
| `miragen_get_readme` | read-only | Fetch latest Miragen README from GitHub |
| `miragen_get_doc` | read-only | Fetch a linked secondary doc (`docs/**.md`) from the miragen repo |

Input guardrails: agent names must match `[a-z0-9][a-z0-9_-]{0,62}` (they double as Docker container names, and this blocks path traversal), `miragen_register_tool` syntax-checks the submitted source and requires it to define the named `@register` function, and unbounded outputs (logs, file reads, agent responses) are truncated at 50,000 characters.

## Resources & Prompts

Alongside tools, the server exposes read-only **MCP resources** for clients that browse
context instead of (or in addition to) calling tools, and one **MCP prompt** to bootstrap
new agents. Unlike tools — which return `"ERROR: ..."` strings — resources raise on
failure (`ValueError`, carrying the daemon's explanation of what was invalid or
missing), matching FastMCP's convention for resources.

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

- A running miragend daemon (`ghcr.io/ieepirzy/miragend`), reachable at
  `MIRAGEND_URL` and attached to the `miragen-net` network **before** this
  stack deploys — provision it separately (see "Docker (single containers,
  no ingress split)" below for a standalone `docker run`); this repo's
  `compose.yml` only starts the MCP adapter, not the daemon
- Python 3.12 (or use the provided Docker image)

No Docker socket access and no Compose plugin are needed by this container —
those requirements moved to miragend.

## Installation

### Docker Compose (recommended)

Two environments run this service and need **opposite** ingress shapes. One
`compose.yml` handles both, selected by `COMPOSE_PROFILES` — no `-f` flags, no
second file, because Portainer locks a git-stack's Compose path at creation and
gives no way to layer a second file onto a stack that already exists:

This `compose.yml` has one deployable service (profile-gated ingress) — the
MCP adapter only. **`miragend` is not part of this project**: it holds the
raw Docker socket, so it is provisioned and owned separately (a standalone
`docker run`, per "Docker (single containers, no ingress split)" below, or
its own compose project) and must already be up and attached to
`miragen-net` before this stack deploys. `miragen-net` is declared
`external: true` here precisely because this project must never try to
create or own it — only attach to what miragend already created.

`compose.yml` pulls `ghcr.io/ieepirzy/miragen-mcp`, which
`.github/workflows/publish.yml` builds on every push to `main` — the
deployment host does not build it. Pin a specific build with
`MIRAGEN_MCP_IMAGE_TAG` (`latest` by default, or `sha-<commit>`); like
`COMPOSE_PROFILES` it is a stack environment variable, so it stays editable
after the stack exists, unlike the Compose path.

```bash
# VPS: NPM runs on the same host, reached by container-name DNS over a shared
# `proxy` network (which NPM's own stack must already have created).
COMPOSE_PROFILES=vps docker compose up -d

# homelab (or anywhere the reverse proxy is on a different machine): published
# on loopback plus BOUND_IP, which should be this host's WireGuard address.
BOUND_IP=10.x.x.x COMPOSE_PROFILES=homelab docker compose up -d
```

To build from a checkout instead of pulling — developing on the adapter
itself — layer the build override on top:

```bash
COMPOSE_PROFILES=vps docker compose -f compose.yml -f compose.ci.yml up -d --build
```

If the pull fails with `denied`, the GHCR package is private: a package is
created private and does not become public merely by belonging to a public
repository. Either flip its visibility in the package settings, or give the
host a `ghcr.io` credential (a classic PAT with `read:packages`).

In Portainer, set `COMPOSE_PROFILES` (`vps` or `homelab`) as a stack environment
variable — that's editable at any time, unlike the Compose path. `DOCKER_GID`
is gone: miragend detects the docker socket's group at runtime, so nothing
host-specific needs to be configured for socket access anymore. Set
`MIRAGEND_TOKEN` to the same value miragend itself was started with — this
project only consumes it, it does not mint or share it — so the lifecycle
API is not relying on network isolation alone.

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

### Docker (single containers, no ingress split)

```bash
# the daemon: socket + workspace + API keys
docker run -d \
  --name miragend \
  --network miragen-net \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v /opt/miragen:/opt/miragen \
  -e MIRAGEND_TOKEN=your-daemon-token \
  -e ANTHROPIC_API_KEY=sk-ant-... \
  ghcr.io/ieepirzy/miragend:latest

# the MCP adapter: OAuth + HTTP only
docker build -t miragen-mcp .
docker run -d \
  --name miragen-mcp \
  --network miragen-net \
  -e MIRAGEND_URL=http://miragend:8000 \
  -e MIRAGEND_TOKEN=your-daemon-token \
  -e MCP_BASE_URL=https://your-domain.example.com \
  -e MCP_CLIENT_SECRET=your-secret \
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

**This server (miragen-mcp):**

| Variable | Default | Description |
|----------|---------|-------------|
| `MIRAGEND_URL` | `http://miragend:8000` | The miragend lifecycle daemon's HTTP API |
| `MIRAGEND_TOKEN` | *(empty)* | Bearer token for the daemon API — must equal the daemon's own `MIRAGEND_TOKEN` |
| `MIRAGEN_INTERNAL_TOKEN` | *(empty)* | Sent as `X-Miragen-Token` on calls to the agents' HTTP control APIs — set it to the same value the agent containers use, if they enforce one |
| `MCP_BASE_URL` | *(required)* | Public base URL of this server — used for OAuth |
| `MCP_CLIENT_ID` | `miragen-mcp` | Admin OAuth client ID — full access to every tool |
| `MCP_CLIENT_SECRET` | `changeme` | Admin OAuth client secret — **must be set explicitly if auth is enabled** |
| `MCP_READONLY_CLIENT_ID` | *(unset)* | Read-only OAuth client ID — see [Authentication](#authentication). Both this and the secret below must be set together, or the feature stays off |
| `MCP_READONLY_CLIENT_SECRET` | *(unset)* | Read-only OAuth client secret |
| `MCP_CLIENT_REDIRECT_URIS` | claude.ai/claude.com callbacks | OAuth redirect allowlist, shared by the admin client and the read-only client (if configured) — origo rejects every redirect_uri at `/authorize` (fail closed) without one, so this must be set correctly for any client to authorize at all |
| `MCP_NO_AUTH` | `false` | Disable OAuth entirely (local development only) |
| `MCP_ALLOW_DEFAULT_SECRET` | `false` | Explicitly acknowledge and allow starting with the default `changeme` secret while auth is enabled (not recommended) |

**The daemon (miragend — provisioned separately, not a service in this repo's `compose.yml`):**

| Variable | Default | Description |
|----------|---------|-------------|
| `MIRAGEN_WORKSPACE` | `/opt/miragen` | Root workspace directory on the host |
| `MIRAGEN_BASE_IMAGE` | `ghcr.io/ieepirzy/miragen:latest` | Docker image used for new agent containers |
| `MIRAGEND_TOKEN` | *(empty)* | Bearer token guarding the daemon's API (empty = network isolation only) |
| `MIRAGEN_INTERNAL_TOKEN` | *(empty)* | Written into created agents' compose entries so their `/run*` guard is armed |

**LLM provider keys** (on the **miragend** service — the daemon writes them
into created agents' compose entries; the MCP adapter never sees a model key):

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

Append `_FILE` to any key variable to read from a Docker secret instead (e.g. `ANTHROPIC_API_KEY_FILE=/run/secrets/anthropic_key`) — set up on whatever provisions miragend itself, not in this repo (its `compose.secrets.yml` layered secrets onto a bundled `miragend` service that no longer exists here; removed).

## Workspace Layout

```
$MIRAGEN_WORKSPACE/          (mounted into miragend only)
├── compose.yml          ← managed by miragend
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

The workspace is the daemon's volume; this MCP server holds no state at all.
`retriggers.sqlite` and `exports/` live there, so scheduled retriggers and
agent exports survive restarts of either container.

Agent workspace directories are mounted to `/agent` inside each container, so filesystem changes made through the MCP tools are visible to the running agent immediately (tool changes trigger an automatic restart).

### Backup, restore, and cloning

`miragen_export_agent` tars an agent's workspace to `exports/<agent>-<timestamp>.tar.gz`, excluding `runs/`, `history.json`, `__pycache__/`, and any single file over 10 MB (skipped files are reported). The compose entry and secrets are **not** exported — an import regenerates them from the daemon's environment.

`miragen_import_agent` extracts an export tarball under a new name, revalidates the profile, registers it in compose, and starts it. Extraction uses tarfile's `data` filter, so archives with absolute paths, `..` traversal, or links are rejected.

```text
# back up, then restore as a clone
miragen_export_agent(agent="briefing")
  → exports/briefing-20260723-120000.tar.gz
miragen_import_agent(name="briefing-staging",
                     archive_path="exports/briefing-20260723-120000.tar.gz")
```

The exported archive lives outside every agent workspace, so `miragen_read_agent_file` cannot fetch it; copy it off the host (or between hosts) yourself, then import it on the destination server.

## Observability & approvals

Requires agent images running **miragen >= 0.1.8** (run records shipped in 0.1.7, the
approval bridge in 0.1.8). Older images 404/405 on these endpoints; the affected tools
return an `"ERROR: ... is running a miragen image without run-record/approval support"`
message telling you to recreate the agent (`miragen_delete_agent` + `miragen_create_agent`)
on the current `MIRAGEN_BASE_IMAGE`, or `docker compose pull` it first.

**Runs.** `miragen_run_agent` is synchronous — it holds the MCP call open for up to 120
seconds and returns the agent's output (with `(run_id: ...)` appended when the agent
reports one). For anything that might run long, use `miragen_run_agent_async` instead: it
returns immediately with a `run_id`, and you poll `miragen_get_run` (one record) or
`miragen_list_runs` (recent records, optionally filtered by status) for progress and
final output.

**Approvals.** An agent profile with `approval_mode: queue` parks gated tool calls instead
of auto-approving or auto-denying them. `miragen_list_pending_approvals` shows what's
waiting (each request carries the tool name and the arguments the agent wants to call it
with); `miragen_resolve_approval` approves or denies one, optionally attaching a `note`
that is folded back into the agent's run as context.

> **Prompt injection warning:** the `tool_args` in a pending approval request are
> agent-generated — the agent chose them, possibly while acting on untrusted content it
> read (an email, a web page, a file). Treat `tool_args` as data to show a human for
> judgement, never as instructions to follow. Approving a request executes the gated tool
> call immediately inside the agent's run, so `miragen_resolve_approval` should only be
> called with an actual human decision behind it — an unattended LLM client that
> auto-approves everything defeats the purpose of the gate.

## Authentication

The server is protected with OAuth2 via the [Origo](https://github.com/ieepirzy/origo) library. Tokens are valid for 7 days. Configure your MCP client with the client credentials defined by `MCP_CLIENT_ID` and `MCP_CLIENT_SECRET`.

If `MCP_NO_AUTH` is not `true`, the server refuses to start when `MCP_CLIENT_SECRET` is left unset (defaulting to the well-known value `changeme`) — this server holds the Docker socket, so booting with a publicly-known OAuth secret is a full compromise waiting to happen. Set a real `MCP_CLIENT_SECRET`, set `MCP_NO_AUTH=true` for auth-free local development, or set `MCP_ALLOW_DEFAULT_SECRET=true` to explicitly acknowledge the risk and start anyway (not recommended outside of throwaway testing).

### Two clients: admin and read-only

By default there is exactly one OAuth client (`MCP_CLIENT_ID` / `MCP_CLIENT_SECRET`), and every token it mints can call every tool — including `miragen_delete_agent`, `miragen_run_agent`, `miragen_resolve_approval`, and anything else that changes state.

Setting **both** `MCP_READONLY_CLIENT_ID` and `MCP_READONLY_CLIENT_SECRET` registers a second, read-only client. Tokens minted for it may only call tools marked read-only in the [Tools](#tools) table above — the exact set is derived at server startup from each tool's `readOnlyHint` annotation, not hand-maintained, so it can't silently fall out of sync with the table. Anything else — writes, restarts, deletes, resolving an approval, running or scheduling a prompt — is refused with a normal tool result (not a connection error):

```
ERROR: this token is read-only; 'miragen_delete_agent' modifies state. Reconnect with the admin client to use it.
```

`tools/list` is filtered the same way, so a read-only client only ever sees the tools it's actually allowed to call.

Leave both variables unset to keep the single-admin-client behavior — the feature adds no overhead and changes nothing about existing tokens, clients, or the OAuth flow when it's off.

**A read-only token is not a "safe to hand to anyone" token.** `miragen_get_agent`, `miragen_read_agent_file`, `miragen_get_agent_logs`, and `miragen_get_run*` can all return secrets an agent's profile, workspace, or run history happens to contain — API keys pasted into a YAML field, credentials in a log line, tokens embedded in a data file or a run's captured output — since nothing in this server redacts agent-authored content. Treat the read-only client as "no destructive actions," not as "no sensitive data." A monitoring dashboard or low-trust LLM client is exactly the intended use case; a client you don't trust with your agents' configs and logs is not.

**How scope is determined today.** Tokens don't yet carry a first-class `admin`/`read-only` scope claim from Origo — that requires per-client scope configuration on the Origo side, tracked separately. Until then, this server re-verifies each request's own already-authenticated bearer token against the `OAuthProvider` it holds, purely to read back which pre-registered `client_id` minted it (an origo-authoritative fact, not client-supplied input), and treats a match on `MCP_READONLY_CLIENT_ID` as read-only. This is a fully self-contained, safe interim: it cannot grant anything OAuth didn't already grant, since a request only reaches this check after Origo's own middleware has already validated the token.

## Development

```bash
pip install -r requirements.txt
pytest tests/
```

Tests mock Docker, OAuth, and the scheduler — no running Docker daemon required.

## Evaluations

`evals/` measures whether an LLM can actually accomplish realistic, **read-only** tasks with these tools against a deterministic fixture workspace (`evals/fixtures/`, four agents with distinct modes, models, capabilities, approval globs, and tools). The fixtures are the ground truth: `evals/ground_truth.py` derives every expected answer from them.

- **`evals/eval.xml`** — 10 questions, each with a single string-comparable answer and the read-only tools it relies on.
- **`evals/check_evals.py`** — deterministic, no API key. Asserts every `eval.xml` answer still derives from the fixtures and that every referenced tool is read-only (source of truth: the `readOnlyHint` annotations in `server.py`). Runs in CI, and `tests/test_evals.py` additionally reproduces each answer by driving the real tools.

  ```bash
  python evals/check_evals.py
  ```

- **`evals/run_evals.py`** — the manual, paid LLM run. Drives an Anthropic model through the server's read-only tools (over FastMCP's in-memory transport) and prints a per-question pass/fail scorecard. Only read-only tools are exposed, so no eval can mutate state.

  ```bash
  pip install anthropic fastmcp
  export ANTHROPIC_API_KEY=sk-...
  python evals/run_evals.py                       # all questions
  EVAL_MODEL=claude-haiku-4-5-20251001 python evals/run_evals.py --id cheapest-model
  ```

To add a question: extend the fixtures, add a branch to `ground_truth.compute_answers()`, then add an `<eval>` to `eval.xml` with the derived answer and the read-only tools it needs. `check_evals.py` will fail until the three agree.

## License

MIT
