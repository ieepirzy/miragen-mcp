# Thin MCP adapter: no docker CLI, no compose plugin, no socket group, no
# workspace volume. All of that moved to the miragend daemon (miragen repo,
# Dockerfile.miragend); this container only speaks HTTP — to miragend for
# lifecycle, to the agents for run/approval traffic.

FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt \
    && adduser --disabled-password --gecos "" mcpuser

COPY server.py ./

USER mcpuser

EXPOSE 8000

CMD ["python", "/app/server.py"]
