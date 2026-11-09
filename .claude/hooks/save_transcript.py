#!/usr/bin/env python3
"""SessionEnd hook: write a SANITISED transcript of this session into docs/transcripts/.

Transcripts are required evidence (rubric H2). Saving them automatically means
you cannot forget. The name follows the required pattern; set CMS_STUDENT=first-last
in .env (or your shell) so it is right.

Why not copy the raw session file? Claude Code's session log (*.jsonl) is an API
log, not a chat log. It holds the full system prompt on every request (your login
email, organisation ids, your global instruction files, machine facts), every tool
result verbatim (command output, file contents) and runtime metadata. Publishing it
is the same as publishing your ~/.claude folder. This hook therefore keeps only the
user and assistant turns, names the tools used without their output, and redacts
emails, phone numbers, IP addresses, key fingerprints, home paths and anything that
looks like a credential. If a credential was redacted, the hook says so: rotate it.

The hook fails closed: on any error nothing is written and the reason is printed.

Manual use (regenerate a transcript):
    echo '{"transcript_path": "~/.claude/projects/<proj>/<session>.jsonl"}' \\
        | python3 .claude/hooks/save_transcript.py
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

# --- redaction -----------------------------------------------------------------

# Credentials of known shapes. Redacted AND reported, because the only correct
# response to a leaked credential is to rotate it.
SECRET_PATTERNS = [
    ("anthropic-key", re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}")),
    ("openai-style-key", re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}")),
    ("github-token", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}")),
    ("aws-access-key", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    ("slack-token", re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}")),
    ("google-api-key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}")),
    ("private-key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)")),
    ("bearer-token", re.compile(r"\bBearer\s+[A-Za-z0-9._\-]{20,}")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}")),
    ("assigned-secret", re.compile(
        r"(?i)\b(?:api[_-]?key|auth[_-]?token|access[_-]?token|secret[_-]?key|client[_-]?secret|password|passwd|pwd)"
        r"\s*[:=]\s*['\"]?(?!change-me|\$\{|\$[A-Z_]|<)[^\s'\"<>]{8,}")),
]

# Personal and infrastructure data. Redacted silently.
PII_PATTERNS = [
    ("email", re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")),
    ("phone", re.compile(r"(?<![\d.])(?:\+?1[ .\-]?)?\(?\d{3}\)?[ .\-]\d{3}[ .\-]\d{4}(?![\d.])")),
    ("fingerprint", re.compile(r"\bSHA256:[A-Za-z0-9+/]{20,}={0,2}")),
    ("ipv4", re.compile(r"(?<![\d.])(?!127\.|0\.0\.0\.0)(?:\d{1,3}\.){3}\d{1,3}(?!\.?\d)")),
    ("ipv6", re.compile(r"\b(?:[0-9a-fA-F]{1,4}:){4,7}[0-9a-fA-F]{1,4}\b")),
    ("uuid", re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b")),
]
HOME_PATH = re.compile(r"/(?:Users|home)/[A-Za-z0-9._\-]+")
SYSTEM_REMINDER = re.compile(r"<system-reminder>[\s\S]*?</system-reminder>\s*")
# Slash commands arrive as tagged blocks: keep "/name args", drop the expanded text and any output.
COMMAND_NAME = re.compile(r"<command-name>([\s\S]*?)</command-name>")
COMMAND_ARGS = re.compile(r"<command-args>([\s\S]*?)</command-args>")
PRIVATE_TAGS = re.compile(r"<(local-command-stdout|local-command-stderr|command-message|command-name|command-args)>"
                          r"[\s\S]*?</\1>\s*")


def user_text(text: str) -> str:
    """Strip injected context from a user turn; keep what the user typed."""
    text = SYSTEM_REMINDER.sub("", text)
    name = COMMAND_NAME.search(text)
    if name:
        args = COMMAND_ARGS.search(text)
        command = name.group(1).strip() + (" " + args.group(1).strip() if args and args.group(1).strip() else "")
        text = PRIVATE_TAGS.sub("", text).strip()
        return (command + ("\n\n" + text if text else "")).strip()
    return PRIVATE_TAGS.sub("", text).strip()


def redact(text: str, found: dict[str, int]) -> str:
    """Replace secrets and personal data in text; count what was replaced by kind."""
    if not text:
        return text
    for kind, pat in SECRET_PATTERNS:
        text, n = pat.subn(f"[REDACTED:{kind}]", text)
        if n:
            found[kind] = found.get(kind, 0) + n
    for kind, pat in PII_PATTERNS:
        text, n = pat.subn(f"[REDACTED:{kind}]", text)
        if n:
            found[kind] = found.get(kind, 0) + n
    text = HOME_PATH.sub("~", text)
    return text


# --- transcript rendering -------------------------------------------------------

def _blocks(content):
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return content if isinstance(content, list) else []


def _tool_line(block: dict, project: Path) -> str:
    """One line naming the tool and, where harmless, what it targeted. Never its output."""
    name = block.get("name", "tool")
    inp = block.get("input") or {}
    target = ""
    if name == "Bash":
        target = inp.get("description") or "(command not shown)"
    elif name in ("Read", "Edit", "Write", "MultiEdit", "NotebookEdit"):
        target = inp.get("file_path") or inp.get("notebook_path") or ""
    elif name in ("Glob", "Grep"):
        target = inp.get("pattern") or ""
    elif name in ("Agent", "Task"):
        target = inp.get("description") or ""
    elif name == "Skill":
        target = inp.get("skill") or ""
    elif name in ("WebFetch", "WebSearch"):
        target = inp.get("url") or inp.get("query") or ""
    target = str(target).replace(str(project), ".")
    return f"- tool: {name}" + (f" — {target}" if target else "")


def render(lines, project: Path) -> tuple[str, dict]:
    """Turn raw JSONL records into readable, sanitised Markdown. Returns (text, redaction counts)."""
    found: dict[str, int] = {}
    out: list[str] = []
    session_id = None
    started = None
    turns = 0
    for raw in lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            rec = json.loads(raw)
        except json.JSONDecodeError:
            continue
        kind = rec.get("type")
        if kind not in ("user", "assistant"):
            continue  # api-request*, attachment, system, bridge-session, cost-state … all dropped
        session_id = session_id or rec.get("sessionId")
        started = started or rec.get("timestamp")
        if rec.get("isMeta"):
            continue
        message = rec.get("message") or {}
        blocks = _blocks(message.get("content"))
        if kind == "user":
            texts = []
            for b in blocks:
                if b.get("type") == "text":
                    t = user_text(b.get("text", ""))
                    if t:
                        texts.append(t)
                # tool_result blocks are tool OUTPUT: never copied.
            if not texts:
                continue
            turns += 1
            stamp = (rec.get("timestamp") or "")[:19].replace("T", " ")
            out.append(f"\n## User ({stamp})\n")
            out.append(redact("\n\n".join(texts), found))
        else:
            parts = []
            for b in blocks:
                bt = b.get("type")
                if bt == "text" and b.get("text", "").strip():
                    parts.append(redact(b["text"].strip(), found))
                elif bt == "tool_use":
                    parts.append(redact(_tool_line(b, project), found))
                # thinking blocks and signatures are dropped
            if parts:
                out.append("\n**Assistant**\n")
                out.append("\n".join(parts))
    header = [
        "# Claude Code session transcript (sanitised)",
        "",
        f"- session: {session_id or 'unknown'}",
        f"- started: {started or 'unknown'}",
        f"- exported: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        f"- user turns: {turns}",
        "",
        "Only user prompts and assistant replies are kept. Tool calls are named; their "
        "output, the system prompt and runtime metadata are not included. Emails, phone "
        "numbers, IP addresses, key fingerprints, home paths and credential-shaped strings "
        "are replaced by `[REDACTED:<kind>]`.",
        "",
        "---",
    ]
    return "\n".join(header + out).rstrip() + "\n", found


# --- naming and gate --------------------------------------------------------------

def student_slug(project: Path) -> str:
    name = os.environ.get("CMS_STUDENT")
    if not name:
        env = project / ".env"
        if env.exists():
            match = re.search(r"^CMS_STUDENT=(.+)$", env.read_text(), re.M)
            name = match.group(1).strip() if match else None
    return re.sub(r"[^a-z-]", "", (name or "your-name").lower().replace(" ", "-"))


def next_number(out: Path) -> str:
    nums = [int(m.group(1)) for p in out.glob("*.md")
            if (m := re.match(r"iphs400_mp2-cms_chat-session_(\d{2})_", p.name))]
    return f"{(max(nums) + 1) if nums else 1:02d}"


def external_gate(target: Path) -> str | None:
    """If transcript-scan is installed, use it as a second opinion. Returns an error text or None."""
    exe = shutil.which("transcript-scan")
    if not exe:
        return None
    try:
        proc = subprocess.run([exe, "--tier", "block", "--strict-pii", "--quiet", str(target)],
                              capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        return f"transcript-scan could not run: {exc}"
    if proc.returncode == 1:
        return "transcript-scan still finds blocking hits:\n" + (proc.stdout + proc.stderr).strip()
    return None


def main() -> int:
    event = json.load(sys.stdin)
    source = Path(os.path.expanduser(event.get("transcript_path", "")))
    if not source.exists():
        print(f"save_transcript: no session file at {source}", file=sys.stderr)
        return 0
    project = Path(os.environ.get("CLAUDE_PROJECT_DIR") or event.get("cwd") or ".").resolve()
    out = project / "docs" / "transcripts"
    out.mkdir(parents=True, exist_ok=True)
    target = out / (f"iphs400_mp2-cms_chat-session_{next_number(out)}_"
                    f"{student_slug(project)}_{datetime.now().strftime('%Y%m%d')}.md")
    if target.exists():
        print(f"save_transcript: {target} exists; nothing written", file=sys.stderr)
        return 0

    with source.open(encoding="utf-8", errors="replace") as fh:
        text, found = render(fh, project)
    target.write_text(text, encoding="utf-8")

    error = external_gate(target)
    if error:
        target.unlink(missing_ok=True)
        print(f"save_transcript: NOT written. {error}", file=sys.stderr)
        return 0

    secrets = {k: v for k, v in found.items() if k in dict(SECRET_PATTERNS)}
    rel = target.relative_to(project) if target.is_relative_to(project) else target
    print(f"save_transcript: wrote {rel}", file=sys.stderr)
    if found:
        print("  redacted: " + ", ".join(f"{k} x{v}" for k, v in sorted(found.items())), file=sys.stderr)
    if secrets:
        print("  WARNING: credential-shaped strings were in this session. The transcript does not "
              "contain them, but the session file on disk does. ROTATE those credentials now.",
              file=sys.stderr)
    print("  Review the file before committing it.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # fail closed: nothing written, reason printed
        print(f"save_transcript: failed, nothing written ({exc!r})", file=sys.stderr)
        sys.exit(0)
