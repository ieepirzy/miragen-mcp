#!/bin/sh
set -e

WORKSPACE="${MIRAGEN_WORKSPACE:-/opt/miragen}"
mkdir -p "${WORKSPACE}/agents"
chown -R mcpuser "${WORKSPACE}"

exec gosu mcpuser python /app/server.py
