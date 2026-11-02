#!/usr/bin/env bash
set -euo pipefail

# ==============================================================================
# 1-Command Remote VPS Deployer for Antigravity-to-Discord Bridge
# Usage:
#   ./deploy-to-vps.sh <TARGET_IP_OR_HOST> <DISCORD_CHANNEL_ID> [SSH_USER]
# Example:
#   ./deploy-to-vps.sh 51.79.200.50 156690123456789012 ubuntu
# ==============================================================================

if [ "$#" -lt 2 ]; then
    echo "Usage: $0 <TARGET_IP_OR_HOST> <DISCORD_CHANNEL_ID> [SSH_USER]"
    echo "Example: $0 51.79.200.50 156690123456789012 ubuntu"
    exit 1
fi

TARGET_HOST="$1"
CHANNEL_ID="$2"
SSH_USER="${3:-ubuntu}"
BRIDGE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REMOTE_DEST="/home/${SSH_USER}/agy-discord-bridge"

echo "=== [1/4] Packaging Clean Files ==="
"${BRIDGE_DIR}/package.sh" >/dev/null

echo "=== [2/4] Shipping to ${SSH_USER}@${TARGET_HOST} ==="
ssh -o StrictHostKeyChecking=accept-new "${SSH_USER}@${TARGET_HOST}" "mkdir -p '${REMOTE_DEST}'"
scp "${BRIDGE_DIR}/agy-discord-bridge.tar.gz" "${SSH_USER}@${TARGET_HOST}:${REMOTE_DEST}/"

echo "=== [3/4] Extracting & Configuring .env on Remote VPS ==="
# Read credentials from current .env if available
BOT_TOKEN=""
USER_ID=""
if [ -f "${BRIDGE_DIR}/.env" ]; then
    BOT_TOKEN="$(grep -E '^DISCORD_BOT_TOKEN=' "${BRIDGE_DIR}/.env" | cut -d'=' -f2- || true)"
    USER_ID="$(grep -E '^DISCORD_USER_ID=' "${BRIDGE_DIR}/.env" | cut -d'=' -f2- || true)"
fi

ssh "${SSH_USER}@${TARGET_HOST}" bash -s <<EOF
set -euo pipefail
cd '${REMOTE_DEST}'
tar -xzf agy-discord-bridge.tar.gz
rm -f agy-discord-bridge.tar.gz

# Populate remote .env
cp .env.example .env
sed -i "s|^DISCORD_BOT_TOKEN=.*|DISCORD_BOT_TOKEN=${BOT_TOKEN}|g" .env
sed -i "s|^DISCORD_CHANNEL_ID=.*|DISCORD_CHANNEL_ID=${CHANNEL_ID}|g" .env
sed -i "s|^DISCORD_USER_ID=.*|DISCORD_USER_ID=${USER_ID}|g" .env
sed -i "s|^AGY_BIN=.*|AGY_BIN=/home/${SSH_USER}/.local/bin/agy|g" .env
sed -i "s|^AGY_WORKSPACE_DIR=.*|AGY_WORKSPACE_DIR=${REMOTE_DEST}/workspace|g" .env
sed -i "s|^AGY_EXTRA_DIRS=.*|AGY_EXTRA_DIRS=/var/www,/home/${SSH_USER}|g" .env
chmod 600 .env

chmod +x setup.sh bot.py guardrail_hook.py
./setup.sh
EOF

echo "=== [4/4] Checking Antigravity Authentication Status on Remote VPS ==="
if ssh "${SSH_USER}@${TARGET_HOST}" "/home/${SSH_USER}/.local/bin/agy -p 'ping' --output-format json >/dev/null 2>&1"; then
    echo "✅ Antigravity CLI is already authenticated on ${TARGET_HOST}!"
    echo "🎉 Bridge is fully LIVE in channel ${CHANNEL_ID}!"
else
    echo "⚠️  Almost done! Antigravity CLI needs one-time Google OAuth on the new VPS."
    echo "    SSH into the new VPS and run agy once to authenticate:"
    echo "    ssh ${SSH_USER}@${TARGET_HOST}"
    echo "    ~/.local/bin/agy"
fi
