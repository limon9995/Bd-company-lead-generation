#!/usr/bin/env bash
cd "$(dirname "$0")" && docker compose down && echo "Stopped. Data is kept; ./start.sh to start again."
