#!/usr/bin/env python3
"""
Antigravity CLI (`agy`) <-> Discord Bridge Daemon.

Locks onto a single dedicated Discord channel (and threads within it) for a
single authorized Discord user ID. Streams live tool/thinking status updates
from `agy --output-format stream-json`, persists conversation IDs across
restarts in `sessions.json`, and hosts a localhost webhook for interactive
[Approve] / [Deny] write guardrails.
"""

import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Optional

import aiohttp
from aiohttp import web
import discord
from discord import app_commands
from dotenv import load_dotenv


load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("agy-discord-bridge")

BASE_DIR = Path(__file__).resolve().parent
SESSIONS_FILE = BASE_DIR / "sessions.json"

DISCORD_BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
ALLOWED_CHANNEL_ID = int(os.environ.get("DISCORD_CHANNEL_ID", "0").strip() or "0")
ALLOWED_USER_ID = int(os.environ.get("DISCORD_USER_ID", "0").strip() or "0")

AGY_BIN = os.environ.get("AGY_BIN", str(Path.home() / ".local" / "bin" / "agy")).strip()
AGY_WORKSPACE_DIR = os.environ.get("AGY_WORKSPACE_DIR", str(BASE_DIR / "workspace")).strip()
AGY_EXTRA_DIRS = [
    d.strip()
    for d in os.environ.get("AGY_EXTRA_DIRS", "/var/www,/home/ubuntu").split(",")
    if d.strip()
]

APPROVAL_HOST = os.environ.get("APPROVAL_HOST", "127.0.0.1").strip()
APPROVAL_PORT = int(os.environ.get("APPROVAL_PORT", "8765").strip())
APPROVAL_TIMEOUT_SECONDS = int(os.environ.get("APPROVAL_TIMEOUT_SECONDS", "280").strip())

# Runtime state maps (snake_case per conventions)
channel_locks: dict[int, asyncio.Lock] = {}
active_processes: dict[int, asyncio.subprocess.Process] = {}
conversation_to_channel: dict[str, int] = {}
approval_posted_channels: set[int] = set()
active_fallback_channel_id: Optional[int] = None


def LoadSessions() -> dict[str, str]:
    """Loads the persistent mapping of Discord channel/thread IDs to Antigravity conversation IDs."""
    if not SESSIONS_FILE.exists():
        return {}
    try:
        raw_data = json.loads(SESSIONS_FILE.read_text(encoding="utf-8"))
        if isinstance(raw_data, dict):
            return {str(k): str(v) for k, v in raw_data.items()}
    except Exception as exc:
        logger.warning("Failed to read sessions.json: %s", exc)
    return {}


def SaveSessions(sessions_map: dict[str, str]) -> None:
    """Atomically writes the channel-to-conversation map to disk."""
    temp_file = SESSIONS_FILE.with_suffix(".tmp")
    temp_file.write_text(json.dumps(sessions_map, indent=2), encoding="utf-8")
    temp_file.replace(SESSIONS_FILE)


def GetConversationId(channel_id: int) -> Optional[str]:
    """Returns the saved Antigravity conversation_id for a channel or thread, if any."""
    sessions_map = LoadSessions()
    conv_id = sessions_map.get(str(channel_id))
    if conv_id:
        conversation_to_channel[conv_id] = channel_id
    return conv_id


def SetConversationId(channel_id: int, conversation_id: str) -> None:
    """Persists the Antigravity conversation_id for the given Discord channel or thread."""
    sessions_map = LoadSessions()
    sessions_map[str(channel_id)] = conversation_id
    SaveSessions(sessions_map)
    conversation_to_channel[conversation_id] = channel_id


def ResetConversationId(channel_id: int) -> Optional[str]:
    """Removes the stored conversation_id for a channel/thread so the next turn starts fresh."""
    sessions_map = LoadSessions()
    removed_id = sessions_map.pop(str(channel_id), None)
    SaveSessions(sessions_map)
    return removed_id


def IsAuthorizedChannel(channel: discord.abc.MessageableChannel) -> bool:
    """
    Enforces single-channel isolation: only allows the configured dedicated
    channel or threads spawned inside that dedicated channel.
    """
    channel_id = getattr(channel, "id", 0)
    if channel_id == ALLOWED_CHANNEL_ID:
        return True
    parent_id = getattr(channel, "parent_id", None)
    if isinstance(channel, discord.Thread) and parent_id == ALLOWED_CHANNEL_ID:
        return True
    return False


def FormatToolStatus(tool_name: str, tool_info: dict) -> str:
    """Formats a concise human-readable activity status line from an Antigravity tool step."""
    params = (tool_info or {}).get("parameters") or {}
    if tool_name in {"view_file", "read_file"}:
        target = params.get("AbsolutePath") or params.get("Path") or "file"
        return f"📖 **Reading** `{target}`..."
    if tool_name == "list_dir":
        target = params.get("DirectoryPath") or "directory"
        return f"📂 **Listing** `{target}`..."
    if tool_name == "find_by_name":
        pattern = params.get("Pattern") or "*"
        search_dir = params.get("SearchDirectory") or ""
        return f"🔎 **Finding** `{pattern}` in `{search_dir}`..."
    if tool_name == "grep_search":
        query = params.get("Query") or ""
        search_path = params.get("SearchPath") or ""
        return f"🔍 **Searching** for `{query}` in `{search_path}`..."
    if tool_name == "run_command":
        cmd_line = params.get("CommandLine") or ""
        short_cmd = cmd_line if len(cmd_line) <= 70 else cmd_line[:67] + "..."
        return f"⚡ **Running** `{short_cmd}`..."
    if tool_name in {"write_file", "edit_file", "write_to_file", "replace_file_content", "multi_replace_file_content"}:
        target = params.get("TargetFile") or params.get("Path") or "file"
        return f"✏️ **Editing** `{target}`..."
    if tool_name in {"search_web", "read_url_content"}:
        query_or_url = params.get("query") or params.get("Url") or "web"
        return f"🌐 **Fetching** `{query_or_url}`..."
    return f"⚙️ **Executing** `{tool_name}`..."


def FormatApprovalDetails(tool_name: str, tool_args: dict) -> str:
    """Formats tool parameters into a readable preview for the Discord approval card."""
    if tool_name == "run_command":
        cmd_line = tool_args.get("CommandLine", "")
        cwd = tool_args.get("Cwd", AGY_WORKSPACE_DIR)
        return f"**Command:**\n```bash\n{cmd_line[:1200]}\n```\n**Directory:** `{cwd}`"

    if tool_name in {"write_file", "edit_file", "write_to_file", "replace_file_content", "multi_replace_file_content"}:
        target_file = tool_args.get("TargetFile") or tool_args.get("Path") or "unknown"
        description = tool_args.get("Description") or tool_args.get("Instruction") or "File modification"
        preview = ""
        if "Content" in tool_args or "CodeContent" in tool_args:
            snippet = str(tool_args.get("Content") or tool_args.get("CodeContent") or "")[:700]
            preview = f"\n**Content Preview:**\n```\n{snippet}\n```"
        elif "Replacement" in tool_args or "ReplacementContent" in tool_args:
            snippet = str(tool_args.get("Replacement") or tool_args.get("ReplacementContent") or "")[:700]
            preview = f"\n**Replacement Preview:**\n```\n{snippet}\n```"
        return f"**Target File:** `{target_file}`\n**Change:** {description}{preview}"

    formatted_json = json.dumps(tool_args, indent=2)[:1000]
    return f"**Arguments:**\n```json\n{formatted_json}\n```"


def SanitizeDiscordMarkdown(text: str) -> str:
    """
    Strips IDE-specific `[label](file:///...)` links so paths display cleanly
    in Discord without raw `file:///` URLs.
    """
    # Convert [`label`](file:///...) -> `label`
    cleaned = re.sub(r"\[(`[^`\]]+`)\]\(file://[^\)]+\)", r"\1", text)
    # Convert [label](file:///...) -> `label`
    cleaned = re.sub(r"\[([^\]]+)\]\(file://[^\)]+\)", r"`\1`", cleaned)
    return cleaned


def SplitDiscordMessage(text: str, limit: int = 1900) -> list[str]:
    """
    Splits long agent responses into chunks under Discord's 2,000-character limit
    while preserving line boundaries and fenced code blocks.
    """
    cleaned_text = SanitizeDiscordMarkdown(text).strip()
    if not cleaned_text:
        return ["*(Empty response)*"]
    if len(cleaned_text) <= limit:
        return [cleaned_text]

    chunks: list[str] = []
    remaining = cleaned_text
    while len(remaining) > limit:
        split_idx = remaining.rfind("\n", 0, limit)
        if split_idx <= 0:
            split_idx = limit
        chunk = remaining[:split_idx]
        remaining = remaining[split_idx:].lstrip("\n")

        # Balance unclosed triple-backtick code fences across split boundaries
        if chunk.count("```") % 2 == 1:
            chunk += "\n```"
            remaining = "```\n" + remaining

        chunks.append(chunk)

    if remaining:
        chunks.append(remaining)
    return chunks


class ApprovalView(discord.ui.View):
    """Interactive Discord UI view with [Approve] and [Deny] buttons for tool guardrails."""

    def __init__(self, allowed_user_id: int, timeout_seconds: int) -> None:
        super().__init__(timeout=timeout_seconds)
        self.allowed_user_id = allowed_user_id
        self.decision_future: asyncio.Future[tuple[bool, str]] = (
            asyncio.get_running_loop().create_future()
        )

    async def ResolveDecision(
        self,
        interaction: discord.Interaction,
        approved: bool,
        label_text: str,
        color: discord.Color,
    ) -> None:
        """Validates the user, resolves the pending hook future, and updates the Discord card."""
        if interaction.user.id != self.allowed_user_id:
            await interaction.response.send_message(
                "⛔ Only the authorized VPS owner can approve or deny actions.",
                ephemeral=True,
            )
            return

        if not self.decision_future.done():
            self.decision_future.set_result((approved, label_text))

        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True

        original_embed = interaction.message.embeds[0] if interaction.message and interaction.message.embeds else None
        if original_embed:
            original_embed.color = color
            original_embed.set_footer(text=f"{label_text} by {interaction.user.display_name}")

        await interaction.response.edit_message(embed=original_embed, view=self)
        self.stop()

    @discord.ui.button(label="Approve", style=discord.ButtonStyle.success, emoji="✅")
    async def ApproveButton(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        await self.ResolveDecision(
            interaction=interaction,
            approved=True,
            label_text="✅ Approved",
            color=discord.Color.green(),
        )

    @discord.ui.button(label="Deny", style=discord.ButtonStyle.danger, emoji="🛑")
    async def DenyButton(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        await self.ResolveDecision(
            interaction=interaction,
            approved=False,
            label_text="🛑 Denied by user",
            color=discord.Color.red(),
        )


def BuildAgyCommand(prompt_text: str, conversation_id: Optional[str]) -> list[str]:
    """Constructs the headless `agy` CLI command line with workspace and extra directories."""
    cmd = [
        AGY_BIN,
        "-p",
        prompt_text,
        "--output-format",
        "stream-json",
        "--disable-slash-commands",
    ]
    for extra_dir in AGY_EXTRA_DIRS:
        if os.path.exists(extra_dir):
            cmd.extend(["--add-dir", extra_dir])
    if conversation_id:
        cmd.extend(["--conversation", conversation_id])
    return cmd


async def StreamAgyTurn(
    channel: discord.abc.Messageable,
    prompt_text: str,
) -> None:
    """
    Runs a single turn of `agy` for the given channel/thread, editing a live
    status message as tools execute and replacing it with the final response.
    """
    global active_fallback_channel_id
    channel_id = getattr(channel, "id", ALLOWED_CHANNEL_ID)
    active_fallback_channel_id = channel_id

    if channel_id not in channel_locks:
        channel_locks[channel_id] = asyncio.Lock()

    if channel_locks[channel_id].locked():
        await channel.send("⏳ *Antigravity is finishing the previous request in this channel—queued your message...*")

    async with channel_locks[channel_id]:
        approval_posted_channels.discard(channel_id)
        existing_conv_id = GetConversationId(channel_id)
        status_msg = await channel.send("⏳ **Thinking...**")

        cmd = BuildAgyCommand(prompt_text=prompt_text, conversation_id=existing_conv_id)
        os.makedirs(AGY_WORKSPACE_DIR, exist_ok=True)

        child_env = os.environ.copy()
        child_env["APPROVAL_HOST"] = APPROVAL_HOST
        child_env["APPROVAL_PORT"] = str(APPROVAL_PORT)
        child_env["APPROVAL_TIMEOUT_SECONDS"] = str(APPROVAL_TIMEOUT_SECONDS)

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=AGY_WORKSPACE_DIR,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=child_env,
                limit=10 * 1024 * 1024,
            )
        except Exception as exc:
            await status_msg.edit(content=f"❌ **Failed to launch Antigravity CLI:** `{exc}`")
            return

        active_processes[channel_id] = proc

        last_status_edit_ts = 0.0
        last_status_text = "⏳ **Thinking...**"
        accumulated_deltas: list[str] = []
        final_response_text: Optional[str] = None
        error_text: Optional[str] = None
        activity_trail: list[str] = []

        async def UpdateLiveStatus(new_status: str, force: bool = False) -> None:
            nonlocal last_status_edit_ts, last_status_text
            now = time.monotonic()
            if not force and (now - last_status_edit_ts < 1.2 or new_status == last_status_text):
                return
            last_status_edit_ts = now
            last_status_text = new_status
            try:
                await status_msg.edit(content=new_status[:1950])
            except discord.HTTPException:
                pass

        try:
            assert proc.stdout is not None
            while True:
                raw_line = await proc.stdout.readline()
                if not raw_line:
                    break

                line_str = raw_line.decode("utf-8", errors="replace").strip()
                if not line_str or not line_str.startswith("{"):
                    continue

                try:
                    event_obj = json.loads(line_str)
                except json.JSONDecodeError:
                    continue

                event_type = event_obj.get("event")

                if event_type == "init":
                    conv_id = event_obj.get("conversation_id")
                    if conv_id:
                        SetConversationId(channel_id, conv_id)

                elif event_type == "step_update":
                    step_data = event_obj.get("step_update") or {}
                    conv_id = step_data.get("conversation_id")
                    if conv_id:
                        SetConversationId(channel_id, conv_id)

                    step_type = step_data.get("step_type")
                    step_state = step_data.get("state")

                    if step_type == "tool":
                        tool_name = step_data.get("tool_name") or (step_data.get("tool_info") or {}).get("name") or "tool"
                        tool_info = step_data.get("tool_info") or {}
                        formatted_line = FormatToolStatus(tool_name, tool_info)
                        if not activity_trail or activity_trail[-1] != formatted_line:
                            activity_trail.append(formatted_line)
                        trail_display = "\n".join(activity_trail[-4:])
                        await UpdateLiveStatus(f"{trail_display}\n⏳ *Working...*")

                    elif step_type == "agent_response":
                        delta = step_data.get("text_delta")
                        if isinstance(delta, str) and delta:
                            accumulated_deltas.append(delta)
                            if step_state == "ACTIVE":
                                preview = "".join(accumulated_deltas).strip()
                                if len(preview) > 40:
                                    await UpdateLiveStatus(f"💬 **Responding...**\n\n{preview[:1600]}...")

                elif event_type == "result":
                    result_data = event_obj.get("result") or {}
                    conv_id = result_data.get("conversation_id")
                    if conv_id:
                        SetConversationId(channel_id, conv_id)

                    status_val = result_data.get("status")
                    if status_val == "SUCCESS":
                        final_response_text = result_data.get("response") or "".join(accumulated_deltas)
                    else:
                        error_text = result_data.get("error") or result_data.get("response") or f"Run ended with status: {status_val}"

            stderr_bytes = await proc.stderr.read() if proc.stderr else b""
            await proc.wait()

        finally:
            active_processes.pop(channel_id, None)

        if error_text:
            await status_msg.edit(content=f"⚠️ **Antigravity Error:**\n```\n{error_text[:1800]}\n```")
            return

        if final_response_text is None:
            final_response_text = "".join(accumulated_deltas).strip()

        if not final_response_text:
            stderr_str = stderr_bytes.decode("utf-8", errors="replace").strip()
            if proc.returncode != 0 and stderr_str:
                await status_msg.edit(content=f"⚠️ **CLI exited ({proc.returncode}):**\n```\n{stderr_str[-1700:]}\n```")
            else:
                await status_msg.edit(content="✅ *Done (no text output).*")
            return

        chunks = SplitDiscordMessage(final_response_text)
        if channel_id in approval_posted_channels:
            # An approval card was posted below status_msg; remove status_msg and send final answer at the bottom
            try:
                await status_msg.delete()
            except discord.HTTPException:
                pass
            for chunk in chunks:
                await channel.send(chunk)
        else:
            await status_msg.edit(content=chunks[0])
            for extra_chunk in chunks[1:]:
                await channel.send(extra_chunk)


class AgyDiscordBot(discord.Client):
    """Discord client managing the single-channel bridge and local approval webhook."""

    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        intents.guilds = True
        intents.messages = True
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.web_runner: Optional[web.AppRunner] = None

    async def setup_hook(self) -> None:
        await self.StartApprovalServer()
        await self.tree.sync()

    async def StartApprovalServer(self) -> None:
        """Starts the localhost-only HTTP endpoint used by `guardrail_hook.py`."""
        app = web.Application()
        app.router.add_post("/request-approval", self.HandleApprovalWebhook)
        self.web_runner = web.AppRunner(app)
        await self.web_runner.setup()
        site = web.TCPSite(self.web_runner, APPROVAL_HOST, APPROVAL_PORT)
        await site.start()
        logger.info("Guardrail approval server listening on http://%s:%d", APPROVAL_HOST, APPROVAL_PORT)

    async def HandleApprovalWebhook(self, request: web.Request) -> web.Response:
        """
        Receives a PreToolUse pause request from `guardrail_hook.py`, posts an
        interactive [Approve] / [Deny] card in Discord, and awaits user click.
        """
        try:
            payload = await request.json()
        except Exception as exc:
            return web.json_response({"approved": False, "reason": f"Invalid JSON: {exc}"}, status=400)

        conv_id = str(payload.get("conversation_id", ""))
        tool_name = str(payload.get("tool_name", "unknown_tool"))
        tool_args = payload.get("tool_args") or {}
        reason_str = str(payload.get("reason", "Write or system command requires approval."))

        target_channel_id = (
            conversation_to_channel.get(conv_id)
            or active_fallback_channel_id
            or ALLOWED_CHANNEL_ID
        )
        channel = self.get_channel(target_channel_id)
        if channel is None:
            try:
                channel = await self.fetch_channel(target_channel_id)
            except Exception as exc:
                return web.json_response(
                    {"approved": False, "reason": f"Could not resolve Discord channel: {exc}"}
                )

        embed = discord.Embed(
            title=f"🛡️ Permission Required: `{tool_name}`",
            description=f"{reason_str}\n\n{FormatApprovalDetails(tool_name, tool_args)}",
            color=discord.Color.orange(),
        )
        embed.set_footer(text=f"Waiting up to {APPROVAL_TIMEOUT_SECONDS}s for your decision...")

        view = ApprovalView(
            allowed_user_id=ALLOWED_USER_ID,
            timeout_seconds=APPROVAL_TIMEOUT_SECONDS,
        )

        approval_posted_channels.add(target_channel_id)
        prompt_msg = await channel.send(
            content=f"<@{ALLOWED_USER_ID}>",
            embed=embed,
            view=view,
        )

        try:
            approved, decision_note = await asyncio.wait_for(
                view.decision_future,
                timeout=APPROVAL_TIMEOUT_SECONDS,
            )
            return web.json_response({"approved": approved, "reason": decision_note})
        except asyncio.TimeoutError:
            for child in view.children:
                if isinstance(child, discord.ui.Button):
                    child.disabled = True
            embed.color = discord.Color.dark_grey()
            embed.set_footer(text="⏱️ Timed out — automatically denied")
            try:
                await prompt_msg.edit(embed=embed, view=view)
            except discord.HTTPException:
                pass
            return web.json_response(
                {"approved": False, "reason": "Approval request timed out in Discord."}
            )


    async def on_ready(self) -> None:
        await OnReady()

    async def on_message(self, message: discord.Message) -> None:
        await OnMessage(message)


bot_client = AgyDiscordBot()


@bot_client.tree.command(name="new", description="Start a fresh Antigravity conversation in this channel/thread")
async def CommandNewSession(interaction: discord.Interaction) -> None:
    """Resets the stored conversation_id for the current channel or thread."""
    if interaction.user.id != ALLOWED_USER_ID or not interaction.channel or not IsAuthorizedChannel(interaction.channel):
        await interaction.response.send_message("⛔ Unauthorized channel or user.", ephemeral=True)
        return

    old_conv = ResetConversationId(interaction.channel.id)
    if old_conv:
        await interaction.response.send_message(
            f"🧹 **Started a fresh conversation!** *(Previous session `{old_conv[:8]}...` remains saved on disk)*"
        )
    else:
        await interaction.response.send_message("✨ **This channel/thread is already on a fresh conversation.**")


@bot_client.tree.command(name="cancel", description="Cancel the currently running Antigravity turn in this channel/thread")
async def CommandCancelTurn(interaction: discord.Interaction) -> None:
    """Terminates the active `agy` subprocess for the current channel/thread."""
    if interaction.user.id != ALLOWED_USER_ID or not interaction.channel or not IsAuthorizedChannel(interaction.channel):
        await interaction.response.send_message("⛔ Unauthorized channel or user.", ephemeral=True)
        return

    proc = active_processes.get(interaction.channel.id)
    if proc and proc.returncode is None:
        proc.terminate()
        await interaction.response.send_message("🛑 **Cancelled the active Antigravity run.**")
    else:
        await interaction.response.send_message("ℹ️ *No active Antigravity turn is running in this channel/thread.*", ephemeral=True)


async def OnReady() -> None:
    logger.info(
        "Connected to Discord as %s (Locked to Channel ID: %d, User ID: %d)",
        bot_client.user,
        ALLOWED_CHANNEL_ID,
        ALLOWED_USER_ID,
    )


async def OnMessage(message: discord.Message) -> None:
    """Processes incoming messages in the dedicated channel or its threads without requiring @mentions."""
    if message.author.bot:
        return
    if message.author.id != ALLOWED_USER_ID:
        return
    if not IsAuthorizedChannel(message.channel):
        return

    prompt_text = message.content.strip()
    if not prompt_text:
        return

    asyncio.create_task(StreamAgyTurn(channel=message.channel, prompt_text=prompt_text))


def Main() -> None:
    """Validates configuration and starts the Discord bridge."""
    if not DISCORD_BOT_TOKEN or ALLOWED_CHANNEL_ID == 0 or ALLOWED_USER_ID == 0:
        raise SystemExit(
            "Missing DISCORD_BOT_TOKEN, DISCORD_CHANNEL_ID, or DISCORD_USER_ID in .env"
        )
    bot_client.run(DISCORD_BOT_TOKEN)


if __name__ == "__main__":
    Main()
