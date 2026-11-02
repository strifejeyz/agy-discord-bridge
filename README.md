# Antigravity CLI (`agy`) <-> Discord VPS Bridge

A lightweight, single-channel Discord daemon that bridges messages to Antigravity CLI (`agy`) with:
- **Zero-tag conversational flow** in one dedicated Discord channel (plus isolated memories per Thread).
- **Persistent conversation memory** (`sessions.json` + `~/.gemini/antigravity-cli/brain/<conversation_id>`).
- **Live activity status updates** edited in-place as Antigravity reads files, searches code, or runs inspections.
- **Interactive Discord `[✅ Approve]` / `[🛑 Deny]` guardrails** via a `PreToolUse` hook:
  - **Auto-approved:** Read-only tools (`view_file`, `list_dir`, `find_by_name`, `grep_search`) and safe system inspection commands (`crontab -l`, `systemctl status`, `pm2 list`, `ls`, `cat`, `df`, `free`, `ps`, `journalctl`, `git status/log/diff`).
  - **Requires Discord button click:** Any file edit/creation, state-changing command (`systemctl restart`, `crontab -e`, `rm`, custom scripts), or reading sensitive paths (`~/.ssh`, `.env`).

---

## Porting to Any New VPS

Choose whichever method is easiest for you:

### Method 1: 1-Command Push From Your Current VPS (Fastest)
From your existing VPS where `agy-discord-bridge` is already configured:
```bash
cd ~/agy-discord-bridge
./deploy-to-vps.sh <NEW_VPS_IP> <NEW_DISCORD_CHANNEL_ID> [SSH_USER]
```
This automatically packages the clean bridge, copies it via SCP, copies your bot token and user ID, sets the new channel ID in `.env`, and starts PM2! If `agy` isn't signed in yet, it will remind you to run `~/.local/bin/agy` once to complete the browser OAuth.

---

### Method 2: Manual Tarball Copy
1. **Create the clean distribution archive:**
   ```bash
   cd ~/agy-discord-bridge
   ./package.sh
   # Creates agy-discord-bridge.tar.gz (excluding .env, venv, and sessions)
   ```
2. **Copy and extract on the new VPS:**
   ```bash
   scp agy-discord-bridge.tar.gz user@<NEW_VPS_IP>:~/
   ssh user@<NEW_VPS_IP>
   mkdir -p ~/agy-discord-bridge && tar -xzf agy-discord-bridge.tar.gz -C ~/agy-discord-bridge
   cd ~/agy-discord-bridge
   ./setup.sh    # Interactive wizard will prompt for your channel ID and start PM2!
   ~/.local/bin/agy   # Sign in once via OAuth link
   ```

---

### Method 3: Private Git Repository (Optional)
Initialize a private Git repository inside `~/agy-discord-bridge`:
```bash
git init
git add bot.py guardrail_hook.py requirements.txt .env.example setup.sh package.sh deploy-to-vps.sh README.md
git commit -m "feat: initial agy-discord-bridge"
```
*(Note: `.env`, `.venv`, and `sessions.json` are excluded from version control).* On any new VPS:
```bash
git clone <your-repo-url> ~/agy-discord-bridge
cd ~/agy-discord-bridge
./setup.sh
```

---

## Discord Slash Commands
- `/new` — Starts a fresh Antigravity conversation memory for the current channel or thread.
- `/cancel` — Immediately stops a running Antigravity turn in the current channel or thread.
