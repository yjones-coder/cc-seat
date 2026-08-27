#!/usr/bin/env python3
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import cc_seat  # noqa: E402


def write_login(home: Path, email: str, token: str) -> None:
    claude_dir = home / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    (home / ".claude.json").write_text(
        json.dumps({"oauthAccount": {"emailAddress": email, "displayName": email}})
    )
    (claude_dir / ".credentials.json").write_text(
        json.dumps({"claudeAiOauth": {"accessToken": token, "refreshToken": token + "-r"}})
    )
    os.chmod(claude_dir / ".credentials.json", 0o600)


class SeatTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.env = mock.patch.dict(os.environ, {"CC_SEAT_HOME": str(self.home)})
        self.env.start()

    def tearDown(self) -> None:
        self.env.stop()
        self.tmp.cleanup()

    def test_with_resume_replaces_existing_resume(self) -> None:
        got = cc_seat.with_resume(["-r", "old", "--model", "opus"], "new")
        self.assertEqual(got, ["--resume", "new", "--model", "opus"])

    def test_add_two_then_swap(self) -> None:
        write_login(self.home, "a@example.com", "tok-a")
        cc_seat.cmd_add()
        write_login(self.home, "b@example.com", "tok-b")
        cc_seat.cmd_add()
        state = cc_seat.load_state()
        self.assertEqual(state["order"], ["a@example.com", "b@example.com"])
        self.assertEqual(state["active"], "b@example.com")

        old, new = cc_seat.swap_now()
        self.assertEqual((old, new), ("b@example.com", "a@example.com"))
        live = json.loads((self.home / ".claude.json").read_text())
        self.assertEqual(live["oauthAccount"]["emailAddress"], "a@example.com")
        creds = json.loads((self.home / ".claude" / ".credentials.json").read_text())
        self.assertEqual(creds["claudeAiOauth"]["accessToken"], "tok-a")

    def test_third_add_fails(self) -> None:
        write_login(self.home, "a@example.com", "tok-a")
        cc_seat.cmd_add()
        write_login(self.home, "b@example.com", "tok-b")
        cc_seat.cmd_add()
        write_login(self.home, "c@example.com", "tok-c")
        with self.assertRaises(SystemExit):
            cc_seat.cmd_add()

    def test_quiet_third_add_is_noop(self) -> None:
        write_login(self.home, "a@example.com", "tok-a")
        cc_seat.cmd_add()
        write_login(self.home, "b@example.com", "tok-b")
        cc_seat.cmd_add()
        write_login(self.home, "c@example.com", "tok-c")
        cc_seat.cmd_add(quiet=True)
        self.assertEqual(len(cc_seat.load_state()["order"]), 2)

    def test_hook_ignores_unwrapped_session(self) -> None:
        os.environ.pop("CC_SEAT", None)
        stdin = sys.stdin
        sys.stdin = io.StringIO(json.dumps({"session_id": "x", "error": "rate_limit"}))
        try:
            cc_seat.write_rate_limit_signal()
        finally:
            sys.stdin = stdin
        self.assertFalse(any(cc_seat.runtime_dir().glob("signal-*.json")))

    def test_hook_writes_signal(self) -> None:
        os.environ["CC_SEAT"] = "1"
        os.environ["CC_SEAT_PID"] = "4242"
        stdin = sys.stdin
        sys.stdin = io.StringIO(json.dumps({"session_id": "sid-9", "error": "rate_limit"}))
        try:
            cc_seat.write_rate_limit_signal()
        finally:
            sys.stdin = stdin
        data = json.loads((cc_seat.runtime_dir() / "signal-4242.json").read_text())
        self.assertEqual(data["session_id"], "sid-9")
        self.assertNotIn("token", json.dumps(data).lower())

    def test_install_hooks_keeps_foreign_hooks(self) -> None:
        settings = self.home / ".claude" / "settings.json"
        settings.parent.mkdir(parents=True, exist_ok=True)
        settings.write_text(
            json.dumps(
                {
                    "hooks": {
                        "Stop": [{"hooks": [{"type": "command", "command": "echo mine"}]}],
                        "SessionStart": [
                            {
                                "matcher": "startup",
                                "hooks": [{"type": "command", "command": "echo vault"}],
                            }
                        ],
                    }
                }
            )
        )
        cc_seat.cmd_install_hooks()
        data = json.loads(settings.read_text())
        self.assertEqual(data["hooks"]["Stop"][0]["hooks"][0]["command"], "echo mine")
        self.assertTrue(
            any("echo vault" in str(g) for g in data["hooks"]["SessionStart"])
        )
        self.assertEqual(data["hooks"]["StopFailure"][0]["matcher"], "rate_limit")
        cc_seat.cmd_uninstall_hooks()
        data = json.loads(settings.read_text())
        self.assertIn("Stop", data["hooks"])
        self.assertNotIn("StopFailure", data["hooks"])
        self.assertTrue(
            any("echo vault" in str(g) for g in data["hooks"]["SessionStart"])
        )


if __name__ == "__main__":
    unittest.main()
