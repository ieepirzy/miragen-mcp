FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN adduser --disabled-password --gecos "" mcpuser \
    && groupadd -g 988 docker \
    && usermod -aG docker mcpuser

COPY server.py .

USER mcpuser

EXPOSE 8000
CMD ["python", "server.py"]
