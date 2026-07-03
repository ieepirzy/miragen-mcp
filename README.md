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
- **Scheduling** — schedule one-shot prompts with a delay or at a specific time (ISO 8601)
- **Validation** — validate agent YAML profiles before applying them
- **Logging** — tail Docker container logs per agent

## Tools

All tools carry a `miragen_` prefix so they stay unambiguous alongside other MCP servers, and each declares MCP tool annotations (`readOnlyHint`, `destructiveHint`, `idempotentHint`, `openWorldHint`) so clients can distinguish safe reads from destructive operations.

| Tool | Hints | Description |
|------|-------|-------------|
| `miragen_list_agents` | read-only | List all agents with status, mode, and model |
| `miragen_get_agent` | read-only | Full agent info: YAML config, container status, tools |
| `miragen_create_agent` | write | Create workspace, register in compose, start container |
| `miragen_start_agent` | write, idempotent | Start agent container |
| `miragen_restart_agent` | write, idempotent | Restart agent container |
| `miragen_stop_agent` | write, idempotent | Stop agent container |
| `miragen_delete_agent` | **destructive** | Stop, remove container, and delete workspace |
| `miragen_get_agent_logs` | read-only | Tail Docker container logs (max 1000 lines) |
| `miragen_list_tools` | read-only | List `@register` tools in agent's `tools.py` |
| `miragen_get_tool_source` | read-only | Get source code of a specific tool |
| `miragen_register_tool` | write | Append new tool to `tools.py` and update `agent.yaml` |
| `miragen_edit_tool` | **destructive** | String-replace edit a tool (restarts agent) |
| `miragen_delete_tool` | **destructive** | Remove tool from `tools.py` and `agent.yaml` |
| `miragen_read_agent_file` | read-only | Read a file from agent workspace |
| `miragen_write_agent_file` | **destructive** | Write/create a file in agent workspace |
| `miragen_edit_agent_file` | **destructive** | String-replace edit a file in agent workspace |
| `miragen_run_agent` | open-world | Send a prompt to agent's `/run` endpoint |
| `miragen_set_retrigger` | open-world | Schedule a one-shot prompt (delay or absolute time) |
| `miragen_validate_yaml` | read-only | Validate agent YAML using miragen CLI |
| `miragen_get_readme` | read-only | Fetch latest Miragen README from GitHub |

Input guardrails: agent names must match `[a-z0-9][a-z0-9_-]{0,62}` (they double as Docker container names, and this blocks path traversal), `miragen_register_tool` syntax-checks the submitted source and requires it to define the named `@register` function, and unbounded outputs (logs, file reads, agent responses) are truncated at 50,000 characters.

## Requirements

- Docker with the Compose plugin
- Python 3.12 (or use the provided Docker image)
- Access to the Docker socket

## Installation

### Docker (recommended)

```bash
docker build -t miragen-mcp .
docker run -d \
  --name miragen-mcp \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v /opt/miragen:/opt/miragen \
  -e MCP_BASE_URL=https://your-domain.example.com \
  -e MCP_CLIENT_SECRET=your-secret \
  -e ANTHROPIC_API_KEY=sk-ant-... \
  -p 8000:8000 \
  miragen-mcp
```

### Local (development)

```bash
pip install -r requirements.txt
MCP_BASE_URL=http://localhost:8000 uvicorn server:app --host 0.0.0.0 --port 8000
```

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `MIRAGEN_WORKSPACE` | `/opt/miragen` | Root workspace directory on the host |
| `MIRAGEN_BASE_IMAGE` | `ghcr.io/ieepirzy/miragen:latest` | Docker image used for new agent containers |
| `MCP_BASE_URL` | *(required)* | Public base URL of this server — used for OAuth |
| `MCP_CLIENT_ID` | `miragen-mcp` | OAuth client ID |
| `MCP_CLIENT_SECRET` | `changeme` | OAuth client secret — **change in production** |

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
└── agents/
    ├── agent-a/
    │   ├── agent.yaml   ← Miragen profile
    │   └── tools.py     ← @register decorated tools
    └── agent-b/
        ├── agent.yaml
        └── tools.py
```

Agent workspace directories are mounted to `/agent` inside each container, so filesystem changes made through the MCP tools are visible to the running agent immediately (tool changes trigger an automatic restart).

## Authentication

The server is protected with OAuth2 via the [Origo](https://github.com/ieepirzy/origo) library. Tokens are valid for 7 days. Configure your MCP client with the client credentials defined by `MCP_CLIENT_ID` and `MCP_CLIENT_SECRET`.

## Development

```bash
pip install -r requirements.txt
pytest tests/
```

Tests mock Docker, OAuth, and the scheduler — no running Docker daemon required.

## License

MIT
