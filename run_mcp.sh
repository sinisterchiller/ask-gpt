#!/bin/bash
# Wrapper script for the MCP server that logs errors
cd /Users/anuragkoushik/Desktop/Proj/ask-gpt
/Users/anuragkoushik/Desktop/Proj/ask-gpt/.venv/bin/python -m server 2>&1
