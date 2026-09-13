#!/usr/bin/env bash
set -euo pipefail

cd /Users/anuragkoushik/Desktop/Proj/ask-gpt

export PYTHONUNBUFFERED=1

exec /Users/anuragkoushik/Desktop/Proj/ask-gpt/.venv/bin/python -m server
