#!/usr/bin/env bash
set -euo pipefail

BRIDGE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ARCHIVE_NAME="agy-discord-bridge.tar.gz"
OUTPUT_PATH="${BRIDGE_DIR}/${ARCHIVE_NAME}"

echo "=== Creating Portable Distribution Archive ==="
tar --exclude='.venv' \
    --exclude='.env' \
    --exclude='sessions.json' \
    --exclude='sessions.tmp' \
    --exclude='__pycache__' \
    --exclude='*.pyc' \
    --exclude='*.log' \
    --exclude='.git' \
    --exclude='workspace/.agents' \
    --exclude='workspace/GEMINI.md' \
    -czf "${OUTPUT_PATH}" -C "${BRIDGE_DIR}" \
    bot.py \
    guardrail_hook.py \
    requirements.txt \
    .env.example \
    setup.sh \
    README.md \
    package.sh \
    deploy-to-vps.sh

echo "✅ Created clean archive at: ${OUTPUT_PATH}"
ls -lh "${OUTPUT_PATH}"
