"""The SessionEnd transcript hook must publish turns only, never secrets or personal data."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

HOOK = Path(__file__).resolve().parents[1] / ".claude" / "hooks" / "save_transcript.py"
spec = importlib.util.spec_from_file_location("save_transcript", HOOK)
assert spec is not None and spec.loader is not None
hook = importlib.util.module_from_spec(spec)
sys.modules["save_transcript"] = hook
spec.loader.exec_module(hook)


def rec(kind: str, content, **extra) -> str:
    base = {"type": kind, "sessionId": "11111111-2222-3333-4444-555555555555",
            "timestamp": "2026-10-29T10:00:00.000Z", "message": {"role": kind, "content": content}}
    base.update(extra)
    return json.dumps(base)


# Fixture "secrets" are assembled at runtime so this file itself never contains a
# credential-shaped string (the repository's pre-commit scanner would block it).
FAKE_KEY = "sk-ant-" + "api03-" + "abcdefghij" * 4
FAKE_SIG = "CAQS" + "Abc1" * 8
FAKE_FP = "SHA256:" + "Ab12" * 5 + "/" + "Cd34" * 5
FAKE_PW = "hunter2" * 2

RAW = [
    json.dumps({"type": "api-request-blob", "message": {"role": "system",
                "content": "userEmail: owner@example.com organizationUuid 00000000-1111-2222-3333-444444444444"}}),
    rec("user", "<system-reminder>\nThe user's email address is owner@example.com\n</system-reminder>"
                "Please fix the login page; my key is " + FAKE_KEY),
    rec("assistant", [
        {"type": "thinking", "thinking": "", "signature": FAKE_SIG},
        {"type": "text", "text": "I'll look at the settings file."},
        {"type": "tool_use", "id": "t1", "name": "Bash",
         "input": {"command": "cat .env && tailscale status", "description": "Show env and tailscale"}},
    ]),
    rec("user", [{"type": "tool_result", "tool_use_id": "t1",
                  "content": "CMS_SECRET_KEY=supersecretvalue123\n100.100.1.2 example-laptop owner@ macOS"}],
        toolUseResult={"stdout": "CMS_SECRET_KEY=supersecretvalue123"}),
    rec("assistant", [{"type": "text", "text": "Reach me at 740-555-0123 or jon@example.org; "
                                               "host fingerprint " + FAKE_FP + ", "
                                               "path /Users/someone/code/x, ip 203.0.113.10."}]),
    rec("user", "ok", isMeta=True),
    rec("user", "<command-message>handoff-load</command-message>\n<command-name>/handoff-load</command-name>\n"
                "<command-args>mp2-grading-v2</command-args>\n<local-command-stdout>secret output 9.9.9.9</local-command-stdout>"),
]


def test_slash_command_turn_is_kept_without_its_output(tmp_path):
    text, _found = hook.render(RAW, tmp_path)
    assert "/handoff-load mp2-grading-v2" in text
    assert "secret output" not in text and "command-message" not in text


def test_render_keeps_turns_and_drops_everything_else(tmp_path):
    text, _found = hook.render(RAW, tmp_path)
    assert "Please fix the login page" in text
    assert "I'll look at the settings file." in text
    assert "- tool: Bash — Show env and tailscale" in text
    # dropped record types and blocks
    assert "organizationUuid" not in text and "api-request" not in text
    assert "system-reminder" not in text and "The user's email address" not in text
    assert "supersecretvalue123" not in text          # tool output never copied
    assert "cat .env" not in text                     # raw command never copied
    assert FAKE_SIG not in text                       # thinking signature dropped
    assert text.count("## User") == 2                 # meta turn skipped, slash command kept


def test_redaction_covers_secrets_and_personal_data(tmp_path):
    text, found = hook.render(RAW, tmp_path)
    assert "sk-ant-" not in text and found["anthropic-key"] == 1
    assert "jon@example.org" not in text and "owner@" not in text
    assert "740-555-0123" not in text and found["phone"] == 1
    assert FAKE_FP not in text and found["fingerprint"] == 1
    assert "203.0.113.10" not in text and found["ipv4"] == 1
    assert "/Users/someone" not in text and "~/code/x" in text
    assert "[REDACTED:email]" in text


def test_assigned_secret_but_not_placeholder():
    found: dict = {}
    out = hook.redact("CMS_ADMIN_PASSWORD=change-me-admin and " + "password" + ": " + FAKE_PW, found)
    assert "change-me-admin" in out
    assert FAKE_PW not in out and found["assigned-secret"] == 1


def test_next_number_continues_from_highest(tmp_path):
    (tmp_path / "iphs400_mp2-cms_chat-session_03_a-b_20260101.md").write_text("")
    (tmp_path / "iphs400_mp2-cms_chat-session_07_a-b_20260101.md").write_text("")
    assert hook.next_number(tmp_path) == "08"
    (tmp_path / "empty").mkdir()
    assert hook.next_number(tmp_path / "empty") == "01"


def test_main_writes_sanitised_file(tmp_path, monkeypatch, capsys):
    src = tmp_path / "session.jsonl"
    src.write_text("\n".join(RAW) + "\n")
    project = tmp_path / "proj"
    project.mkdir()
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project))
    monkeypatch.setenv("CMS_STUDENT", "Ada Lovelace")
    monkeypatch.setattr(hook, "external_gate", lambda target: None)
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO(json.dumps({"transcript_path": str(src)})))
    assert hook.main() == 0
    files = list((project / "docs" / "transcripts").glob("*.md"))
    assert len(files) == 1 and files[0].name.startswith("iphs400_mp2-cms_chat-session_01_ada-lovelace_")
    body = files[0].read_text()
    assert "sk-ant-" not in body and "supersecretvalue123" not in body
    err = capsys.readouterr().err
    assert "ROTATE" in err and "wrote docs/transcripts/" in err


def test_main_fails_closed_when_gate_rejects(tmp_path, monkeypatch, capsys):
    src = tmp_path / "session.jsonl"
    src.write_text("\n".join(RAW) + "\n")
    project = tmp_path / "proj"
    project.mkdir()
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project))
    monkeypatch.setattr(hook, "external_gate", lambda target: "simulated blocking hit")
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO(json.dumps({"transcript_path": str(src)})))
    assert hook.main() == 0
    assert not list((project / "docs" / "transcripts").glob("*.md"))
    assert "NOT written" in capsys.readouterr().err
