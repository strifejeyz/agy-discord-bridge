#!/usr/bin/env python3
"""
PreToolUse lifecycle hook for Antigravity CLI (`agy`).

Reads tool invocation metadata from stdin (JSON) and outputs a permission
decision to stdout (JSON). Auto-approves safe read-only operations and
system inspections while routing file mutations, state-changing commands,
and sensitive credential reads to the local Discord bridge for interactive
[Approve] / [Deny] button confirmation.
"""

import json
import os
import re
import shlex
import sys
import urllib.error
import urllib.request
from pathlib import Path


READ_ONLY_TOOLS = {
    "view_file",
    "read_file",
    "list_dir",
    "find_by_name",
    "grep_search",
    "search_web",
    "read_url_content",
    "list_permissions",
    "list_resources",
    "read_resource",
}

FILE_WRITE_TOOLS = {
    "write_file",
    "edit_file",
    "write_to_file",
    "replace_file_content",
    "multi_replace_file_content",
}

BRIDGE_DIR = Path(__file__).resolve().parent
WORKSPACE_DIR = Path(
    os.environ.get("AGY_WORKSPACE_DIR", str(BRIDGE_DIR / "workspace"))
).resolve()

SENSITIVE_PATH_PATTERNS = [
    r"(^|/)\.ssh(/|$)",
    r"(^|/)\.gnupg(/|$)",
    r"(^|/)\.aws(/|$)",
    r"(^|/)\.env(\..+)?$",
    r"(^|/)agy-discord-bridge/\.env$",
    r"^/etc/(shadow|gshadow|sudoers)",
]

SAFE_SINGLE_WORD_COMMANDS = {
    "ls",
    "pwd",
    "whoami",
    "id",
    "groups",
    "hostname",
    "uname",
    "date",
    "uptime",
    "echo",
    "printf",
    "true",
    "false",
    "cat",
    "head",
    "tail",
    "wc",
    "stat",
    "file",
    "du",
    "df",
    "free",
    "ps",
    "lsof",
    "lsblk",
    "grep",
    "egrep",
    "fgrep",
    "rg",
    "cut",
    "sort",
    "uniq",
    "tr",
    "jq",
    "ss",
    "netstat",
    "dig",
    "nslookup",
    "which",
    "whereis",
}

SAFE_MULTI_WORD_PREFIXES = [
    ("crontab", "-l"),
    ("systemctl", "status"),
    ("systemctl", "list-units"),
    ("systemctl", "list-unit-files"),
    ("systemctl", "list-timers"),
    ("systemctl", "is-active"),
    ("systemctl", "is-enabled"),
    ("pm2", "list"),
    ("pm2", "ls"),
    ("pm2", "status"),
    ("pm2", "show"),
    ("pm2", "describe"),
    ("git", "status"),
    ("git", "log"),
    ("git", "diff"),
    ("git", "branch"),
    ("git", "remote"),
    ("git", "show"),
    ("git", "rev-parse"),
    ("ip", "addr"),
    ("ip", "route"),
    ("ip", "link"),
    ("nginx", "-v"),
    ("nginx", "-V"),
]


def IsSensitivePath(raw_path: str) -> bool:
    """
    Checks whether a file path or command argument references protected
    credentials (.ssh, .gnupg, .aws, .env, or shadow files).
    """
    if not raw_path:
        return False
    normalized_path = os.path.expanduser(raw_path.strip()).replace("\\", "/")
    for pattern in SENSITIVE_PATH_PATTERNS:
        if re.search(pattern, normalized_path):
            return True
    return False


def ResolveTargetPath(raw_path: str) -> Path:
    """Resolves a relative or ~ path against WORKSPACE_DIR into a canonical real path."""
    expanded = os.path.expanduser(raw_path.strip())
    if not os.path.isabs(expanded):
        expanded = str(WORKSPACE_DIR / expanded)
    return Path(os.path.realpath(expanded))


def IsImmutableBridgePath(raw_path: str) -> bool:
    """
    Hard-blocks any attempt to modify the guardrail hook configuration
    (.agents/hooks.json), workspace GEMINI.md, or bridge daemon scripts.
    """
    if not raw_path:
        return False
    real_path = ResolveTargetPath(raw_path)
    normalized_str = str(real_path).replace("\\", "/")

    # Block any path inside a `.agents` folder or the workspace GEMINI.md
    if "/.agents/" in normalized_str or normalized_str.endswith("/.agents"):
        return True
    if real_path == (WORKSPACE_DIR / "GEMINI.md").resolve():
        return True

    # Block modifying the bridge daemon's own files in BRIDGE_DIR (outside WORKSPACE_DIR)
    if real_path == BRIDGE_DIR or (
        BRIDGE_DIR in real_path.parents and WORKSPACE_DIR not in real_path.parents and real_path != WORKSPACE_DIR
    ):
        return True

    return False


def IsSafeWorkspaceWritePath(raw_path: str) -> bool:
    """
    Returns True if the target file is strictly inside WORKSPACE_DIR and is
    not a sensitive credential file or immutable hook/bridge config file.
    """
    if not raw_path:
        return False
    if IsSensitivePath(raw_path) or IsImmutableBridgePath(raw_path):
        return False
    real_path = ResolveTargetPath(raw_path)
    return WORKSPACE_DIR in real_path.parents


def ExtractCandidatePaths(tool_args: dict) -> list[str]:
    """Extracts any file or directory path arguments from a tool call payload."""
    path_keys = (
        "AbsolutePath",
        "Path",
        "TargetFile",
        "DirectoryPath",
        "SearchDirectory",
        "SearchPath",
        "Cwd",
    )
    extracted_paths: list[str] = []
    for key in path_keys:
        val = tool_args.get(key)
        if isinstance(val, str) and val.strip():
            extracted_paths.append(val.strip())
    return extracted_paths


def IsSafeReadCommandSegment(segment_str: str) -> bool:
    """
    Verifies that a single pipeline/chain command segment only performs
    non-destructive inspection and does not touch sensitive credential files.
    """
    stripped_segment = segment_str.strip()
    if not stripped_segment:
        return True

    # Strip benign descriptor/null redirections (2>&1, 2>/dev/null, >/dev/null) before checking > or &
    sanitized_segment = re.sub(
        r"(?:\d*>&\d+|\d*>\s*/dev/null|&>\s*/dev/null)",
        "",
        stripped_segment,
    ).strip()
    if not sanitized_segment:
        return True

    # Reject output redirections, subshells, or background execution in auto-approval
    forbidden_substrings = [">", "$(", "`", "<(", ">(", "&"]
    for token in forbidden_substrings:
        if token in sanitized_segment:
            return False

    try:
        tokens = shlex.split(sanitized_segment)
    except ValueError:
        return False

    if not tokens:
        return True

    # Strip leading environment variable assignments or benign wrappers like timeout
    while tokens and ("=" in tokens[0] and not tokens[0].startswith("-")):
        tokens.pop(0)
    if not tokens:
        return False

    # Inspect every token for sensitive paths (.ssh, .env, etc.)
    for arg_token in tokens:
        if IsSensitivePath(arg_token):
            return False

    cmd_name = os.path.basename(tokens[0])

    # Special checks for `find` and `journalctl` so destructive flags require approval
    if cmd_name == "find":
        dangerous_find_flags = {"-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fls"}
        return not any(flag in dangerous_find_flags for flag in tokens[1:])

    if cmd_name == "journalctl":
        return not any(flag.startswith("--vacuum") or flag.startswith("--rotate") for flag in tokens[1:])

    if cmd_name in SAFE_SINGLE_WORD_COMMANDS:
        return True

    if len(tokens) >= 2:
        cmd_pair = (cmd_name, tokens[1])
        if cmd_pair in SAFE_MULTI_WORD_PREFIXES:
            # Ensure git branch/remote only lists and does not delete/modify
            if cmd_name == "git" and tokens[1] in {"branch", "remote"} and len(tokens) > 2:
                if any(flag not in {"-a", "-r", "-v", "--all", "--remotes"} for flag in tokens[2:]):
                    return False
            return True

    return False


def IsSafeReadCommand(command_line: str) -> bool:
    """
    Splits a shell command line across pipes and logical operators and confirms
    every stage is a read-only inspection command.
    """
    if not command_line or not command_line.strip():
        return False

    # Reject multiline scripts or subshell constructs immediately
    if "\n" in command_line or "\r" in command_line:
        return False

    segments = re.split(r"\|\||&&|\||;", command_line)
    return all(IsSafeReadCommandSegment(seg) for seg in segments)


def RequestDiscordApproval(
    conversation_id: str,
    tool_name: str,
    tool_args: dict,
    reason: str,
) -> dict:
    """
    Contacts the local Discord bridge HTTP server to post an interactive
    [Approve] / [Deny] button prompt in the dedicated Discord channel and
    blocks until the user responds or the request times out.
    """
    approval_host = os.environ.get("APPROVAL_HOST", "127.0.0.1")
    approval_port = int(os.environ.get("APPROVAL_PORT", "8765"))
    timeout_seconds = int(os.environ.get("APPROVAL_TIMEOUT_SECONDS", "280"))

    url = f"http://{approval_host}:{approval_port}/request-approval"
    payload_bytes = json.dumps(
        {
            "conversation_id": conversation_id,
            "tool_name": tool_name,
            "tool_args": tool_args,
            "reason": reason,
        }
    ).encode("utf-8")

    request_obj = urllib.request.Request(
        url,
        data=payload_bytes,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(request_obj, timeout=timeout_seconds) as response:
            response_body = json.loads(response.read().decode("utf-8"))
            approved = bool(response_body.get("approved", False))
            user_note = response_body.get("reason", "")
            if approved:
                return {
                    "decision": "allow",
                    "permissionOverrides": [
                        "read_file(*)",
                        "write_file(*)",
                        "command(*)",
                        "read_url(*)",
                    ],
                }
            return {
                "decision": "deny",
                "reason": user_note or "Denied by user via Discord interactive guardrail.",
            }
    except urllib.error.URLError as exc:
        return {
            "decision": "deny",
            "reason": f"Write/command operation blocked because Discord approval service was unreachable: {exc}",
        }
    except Exception as exc:
        return {
            "decision": "deny",
            "reason": f"Write/command operation blocked due to guardrail error: {exc}",
        }


def EvaluateToolCall(hook_input: dict) -> dict:
    """
    Determines whether a tool call can be auto-approved as a safe read operation
    or must pause for explicit Discord user approval.
    """
    tool_call = hook_input.get("toolCall") or {}
    tool_name = str(tool_call.get("name", ""))
    tool_args = tool_call.get("args") or {}
    conversation_id = str(hook_input.get("conversationId", ""))

    # 1. Check if any explicit path argument touches a sensitive path (.ssh, .env, etc.)
    for candidate_path in ExtractCandidatePaths(tool_args):
        if IsSensitivePath(candidate_path):
            return RequestDiscordApproval(
                conversation_id=conversation_id,
                tool_name=tool_name,
                tool_args=tool_args,
                reason=f"Touches protected path `{candidate_path}`",
            )

    # 2. Pure read-only tools on non-sensitive paths are auto-approved
    if tool_name in READ_ONLY_TOOLS:
        return {
            "decision": "allow",
            "permissionOverrides": ["read_file(*)", "read_url(*)"],
        }

    # 3. File write/edit tools: hard-deny immutable bridge configs, auto-approve inside workspace/, prompt outside
    if tool_name in FILE_WRITE_TOOLS:
        target_path = str(tool_args.get("Path") or tool_args.get("TargetFile") or "").strip()
        if IsImmutableBridgePath(target_path):
            return {
                "decision": "deny",
                "reason": f"Modifying protected bridge/hook configuration (`{target_path}`) is permanently disabled.",
            }
        if IsSafeWorkspaceWritePath(target_path):
            return {
                "decision": "allow",
                "permissionOverrides": ["write_file(*)", "read_file(*)"],
            }
        return RequestDiscordApproval(
            conversation_id=conversation_id,
            tool_name=tool_name,
            tool_args=tool_args,
            reason=f"Writing outside workspace (`{target_path}`) requires approval.",
        )

    # 4. Shell commands: auto-approve safe read/inspection commands; prompt for everything else
    if tool_name == "run_command":
        command_line = str(tool_args.get("CommandLine", ""))
        if IsSafeReadCommand(command_line):
            return {
                "decision": "allow",
                "permissionOverrides": ["command(*)", "read_file(*)"],
            }
        return RequestDiscordApproval(
            conversation_id=conversation_id,
            tool_name=tool_name,
            tool_args=tool_args,
            reason=f"Shell command requires approval: `{command_line}`",
        )

    # 5. All other mutating tools
    return RequestDiscordApproval(
        conversation_id=conversation_id,
        tool_name=tool_name,
        tool_args=tool_args,
        reason=f"Write/mutating tool `{tool_name}` requires approval.",
    )


def Main() -> None:
    """Entry point invoked by Antigravity CLI on PreToolUse."""
    raw_stdin = sys.stdin.read().strip()
    if not raw_stdin:
        sys.stdout.write(json.dumps({"decision": "allow"}) + "\n")
        return

    try:
        hook_input = json.loads(raw_stdin)
    except json.JSONDecodeError as exc:
        sys.stdout.write(
            json.dumps({"decision": "deny", "reason": f"Invalid hook JSON payload: {exc}"}) + "\n"
        )
        return

    result_payload = EvaluateToolCall(hook_input)
    sys.stdout.write(json.dumps(result_payload) + "\n")
    sys.stdout.flush()


if __name__ == "__main__":
    Main()
