#!/usr/bin/env bash
set -euo pipefail

BRIDGE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKSPACE_DIR="${BRIDGE_DIR}/workspace"
AGY_BIN="${HOME}/.local/bin/agy"

echo "=== [1/5] Checking Antigravity CLI (agy) ==="
if [ ! -x "${AGY_BIN}" ]; then
    echo "Installing Antigravity CLI..."
    curl -fsSL https://antigravity.google/cli/install.sh | bash
else
    echo "Found agy at ${AGY_BIN} ($(${AGY_BIN} --version))"
fi

echo "=== [2/5] Setting up Python Virtual Environment ==="
if ! python3 -m venv "${BRIDGE_DIR}/.venv" 2>/dev/null; then
    echo "Installing python3-venv..."
    sudo apt-get update -y && sudo apt-get install -y python3-venv python3-pip
    python3 -m venv "${BRIDGE_DIR}/.venv"
fi

"${BRIDGE_DIR}/.venv/bin/pip" install --upgrade pip -q
"${BRIDGE_DIR}/.venv/bin/pip" install -r "${BRIDGE_DIR}/requirements.txt" -q
chmod +x "${BRIDGE_DIR}/guardrail_hook.py" "${BRIDGE_DIR}/bot.py"
echo "Python dependencies installed in ${BRIDGE_DIR}/.venv"

echo "=== [3/5] Configuring Workspace & PreToolUse Guardrail Hook ==="
mkdir -p "${WORKSPACE_DIR}/.agents"
chmod u+w "${WORKSPACE_DIR}/.agents" "${WORKSPACE_DIR}/.agents/hooks.json" "${WORKSPACE_DIR}/GEMINI.md" 2>/dev/null || true
cat > "${WORKSPACE_DIR}/.agents/hooks.json" <<EOF
{
  "discord-guardrail": {
    "enabled": true,
    "PreToolUse": [
      {
        "matcher": "*",
        "hooks": [
          {
            "type": "command",
            "command": "${BRIDGE_DIR}/.venv/bin/python ${BRIDGE_DIR}/guardrail_hook.py",
            "timeout": 300
          }
        ]
      }
    ]
  }
}
EOF
cat > "${WORKSPACE_DIR}/GEMINI.md" <<'EOF'
# Discord Output Formatting Rules

You are communicating with the user directly inside a Discord channel via `agy-discord-bridge`.
Follow these formatting rules strictly on every response:

1. **No `file:///` Markdown Links:** Discord does not render `file:///` links. Always format file and directory paths using plain inline code backticks (e.g. `/var/www/gfx-api.duckdns.org` or `sampler.php`), never `[label](file:///...)`.
2. **No Markdown Tables:** Discord does not render Markdown pipe tables (`| col | col |`). Always use bullet lists or fenced code blocks (` ```text `) to present tabular data like `pm2 list` or process tables.
3. **Concise & Direct:** Keep responses clean, direct, and easy to read on desktop and mobile Discord clients.
EOF
chmod 444 "${WORKSPACE_DIR}/.agents/hooks.json" "${WORKSPACE_DIR}/GEMINI.md"
chmod 555 "${WORKSPACE_DIR}/.agents"
echo "Wrote and locked ${WORKSPACE_DIR}/.agents/hooks.json and ${WORKSPACE_DIR}/GEMINI.md"

echo "=== [4/5] Updating ~/.gemini/antigravity-cli/settings.json ==="
mkdir -p "${HOME}/.gemini/antigravity-cli"
"${BRIDGE_DIR}/.venv/bin/python" - "${HOME}/.gemini/antigravity-cli/settings.json" "${WORKSPACE_DIR}" "${HOME}" <<'PYEOF'
import json, sys
from pathlib import Path

settings_path = Path(sys.argv[1])
workspace_dir = sys.argv[2]
home_dir = sys.argv[3]

data = {}
if settings_path.exists():
    try:
        data = json.loads(settings_path.read_text(encoding="utf-8"))
    except Exception:
        data = {}

data["allowNonWorkspaceAccess"] = True
data["toolPermission"] = "always-proceed"
data["artifactReviewPolicy"] = "always-proceed"

trusted = list(data.get("trustedWorkspaces") or [])
for folder in [workspace_dir, "/var/www", home_dir]:
    if folder not in trusted:
        trusted.append(folder)
data["trustedWorkspaces"] = trusted

settings_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
PYEOF
echo "Updated ${HOME}/.gemini/antigravity-cli/settings.json"

echo "=== [5/5] Checking .env & PM2 Service ==="
if [ ! -f "${BRIDGE_DIR}/.env" ]; then
    cp "${BRIDGE_DIR}/.env.example" "${BRIDGE_DIR}/.env"
    sed -i "s|^AGY_BIN=.*|AGY_BIN=${AGY_BIN}|g" "${BRIDGE_DIR}/.env"
    sed -i "s|^AGY_WORKSPACE_DIR=.*|AGY_WORKSPACE_DIR=${WORKSPACE_DIR}|g" "${BRIDGE_DIR}/.env"
    sed -i "s|^AGY_EXTRA_DIRS=.*|AGY_EXTRA_DIRS=/var/www,${HOME}|g" "${BRIDGE_DIR}/.env"
fi

# Interactive configuration wizard if placeholders are detected and terminal is interactive
if grep -q "your_discord_bot_token_here" "${BRIDGE_DIR}/.env" || grep -q "123456789012345678" "${BRIDGE_DIR}/.env"; then
    if [ -t 0 ]; then
        echo ""
        echo "🔧 Setup Wizard: Configure Discord Settings"
        read -r -p "Enter Discord Bot Token: " input_token
        if [ -n "${input_token}" ]; then
            sed -i "s|^DISCORD_BOT_TOKEN=.*|DISCORD_BOT_TOKEN=${input_token}|g" "${BRIDGE_DIR}/.env"
        fi

        read -r -p "Enter Dedicated Discord Channel ID for this VPS: " input_channel
        if [ -n "${input_channel}" ]; then
            sed -i "s|^DISCORD_CHANNEL_ID=.*|DISCORD_CHANNEL_ID=${input_channel}|g" "${BRIDGE_DIR}/.env"
        fi

        read -r -p "Enter Your Discord User ID: " input_user
        if [ -n "${input_user}" ]; then
            sed -i "s|^DISCORD_USER_ID=.*|DISCORD_USER_ID=${input_user}|g" "${BRIDGE_DIR}/.env"
        fi
        chmod 600 "${BRIDGE_DIR}/.env"
        echo "Saved configuration to ${BRIDGE_DIR}/.env"
    fi
fi

if grep -q "your_discord_bot_token_here" "${BRIDGE_DIR}/.env" || grep -q "123456789012345678" "${BRIDGE_DIR}/.env"; then
    echo "⚠️  Set your Discord credentials in ${BRIDGE_DIR}/.env, then run ./setup.sh again:"
    echo "    nano ${BRIDGE_DIR}/.env"
else
    if command -v pm2 >/dev/null 2>&1; then
        pm2 delete agy-discord 2>/dev/null || true
        pm2 start "${BRIDGE_DIR}/bot.py" --name agy-discord --interpreter "${BRIDGE_DIR}/.venv/bin/python" --cwd "${BRIDGE_DIR}"
        pm2 save
        echo "✅ Started agy-discord in PM2!"
    else
        echo "PM2 not found. Run with: ${BRIDGE_DIR}/.venv/bin/python ${BRIDGE_DIR}/bot.py"
    fi
fi
