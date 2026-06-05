FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt \
    && groupadd -g 988 docker \
    && adduser --disabled-password --gecos "" mcpuser \
    && usermod -aG docker mcpuser

COPY server.py .

USER mcpuser

EXPOSE 8000
CMD ["python", "server.py"]
