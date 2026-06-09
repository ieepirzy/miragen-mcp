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

| Tool | Description |
|------|-------------|
| `list_agents` | List all agents with status, mode, and model |
| `get_agent` | Full agent info: YAML config, container status, tools |
| `create_agent` | Create workspace, register in compose, start container |
| `start_agent` | Start agent container |
| `restart_agent` | Restart agent container |
| `stop_agent` | Stop agent container |
| `delete_agent` | Stop, remove container, and delete workspace |
| `get_agent_logs` | Tail Docker container logs |
| `list_tools` | List `@register` tools in agent's `tools.py` |
| `get_tool_source` | Get source code of a specific tool |
| `register_tool` | Append new tool to `tools.py` and update `agent.yaml` |
| `edit_tool` | String-replace edit a tool (restarts agent) |
| `delete_tool` | Remove tool from `tools.py` and `agent.yaml` |
| `read_agent_file` | Read a file from agent workspace |
| `write_agent_file` | Write/create a file in agent workspace |
| `edit_agent_file` | String-replace edit a file in agent workspace |
| `run_agent` | Send a prompt to agent's `/run` endpoint |
| `set_retrigger` | Schedule a one-shot prompt (delay or absolute time) |
| `validate_yaml` | Validate agent YAML using miragen CLI |
| `get_miragen_readme` | Fetch latest Miragen README from GitHub |

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
