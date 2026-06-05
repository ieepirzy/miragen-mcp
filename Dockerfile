FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && adduser --disabled-password --gecos "" mcpuser

COPY server.py .

USER mcpuser

EXPOSE 8000
CMD ["python", "server.py"]
