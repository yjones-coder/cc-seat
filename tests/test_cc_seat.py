#!/usr/bin/env python3
from __future__ import annotations

import base64
import io
import json
import os
import signal
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

    @mock.patch("subprocess.call", return_value=0)
    def test_cmd_login_delegates_and_snapshots(self, mock_call: mock.MagicMock) -> None:
        write_login(self.home, "login-user@example.com", "tok-login")
        cc_seat.cmd_login("claude")
        mock_call.assert_called_once()
        state = cc_seat.load_state("claude")
        self.assertIn("login-user@example.com", state["order"])
        self.assertEqual(state["active"], "login-user@example.com")

    def test_swap_saves_rotated_token_before_install(self) -> None:
        write_login(self.home, "a@example.com", "tok-a")
        cc_seat.cmd_add()
        write_login(self.home, "b@example.com", "tok-b")
        cc_seat.cmd_add()
        write_login(self.home, "b@example.com", "tok-b-rotated")
        cc_seat.swap_now()
        saved = json.loads(
            (self.home / ".claude" / "accounts" / "slots" / "b@example.com" / "credentials.json").read_text()
        )
        self.assertEqual(saved["claudeAiOauth"]["accessToken"], "tok-b-rotated")
        live = json.loads((self.home / ".claude" / ".credentials.json").read_text())
        self.assertEqual(live["claudeAiOauth"]["accessToken"], "tok-a")

    def test_swap_refuses_a_seat_whose_login_does_not_match(self) -> None:
        write_login(self.home, "a@example.com", "tok-a")
        cc_seat.cmd_add()
        write_login(self.home, "b@example.com", "tok-b")
        cc_seat.cmd_add()
        oauth = self.home / ".claude" / "accounts" / "slots" / "a@example.com" / "oauth.json"
        oauth.write_text(json.dumps({"emailAddress": "other@example.com"}))
        with self.assertRaises(RuntimeError):
            cc_seat.swap_now()
        live = json.loads((self.home / ".claude.json").read_text())
        self.assertEqual(live["oauthAccount"]["emailAddress"], "b@example.com")

    def test_limit_resumes_the_same_session_on_the_other_seat(self) -> None:
        write_login(self.home, "a@example.com", "tok-a")
        cc_seat.cmd_add()
        write_login(self.home, "b@example.com", "tok-b")
        cc_seat.cmd_add()
        write_login(self.home, "b@example.com", "tok-b-live")
        argv_log = self.home / "argv.log"
        fake = self.home / "fake-claude"
        fake.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, subprocess, sys, time\n"
            "from pathlib import Path\n"
            "log = Path(os.environ['SEAT_ARGV_LOG'])\n"
            "with log.open('a') as fh:\n"
            "    fh.write(json.dumps(sys.argv[1:]) + '\\n')\n"
            "if '--resume' in sys.argv:\n"
            "    raise SystemExit(0)\n"
            "payload = json.dumps({'session_id': 'sid-kept', 'error': 'rate_limit'})\n"
            "subprocess.run(\n"
            "    [sys.executable, os.environ['SEAT_TOOL'], 'hook', '--harness', 'claude'],\n"
            "    input=payload.encode(),\n"
            ")\n"
            "try:\n"
            "    time.sleep(30)\n"
            "except KeyboardInterrupt:\n"
            "    raise SystemExit(0)\n"
        )
        fake.chmod(0o755)
        previous = signal.getsignal(signal.SIGINT), signal.getsignal(signal.SIGTERM)
        os.environ["CLAUDE_BIN"] = str(fake)
        os.environ["SEAT_ARGV_LOG"] = str(argv_log)
        os.environ["SEAT_TOOL"] = str(ROOT / "cc_seat.py")
        try:
            with self.assertRaises(SystemExit) as caught:
                cc_seat.cmd_wrap("claude", ["--model", "opus"])
        finally:
            signal.signal(signal.SIGINT, previous[0])
            signal.signal(signal.SIGTERM, previous[1])
        self.assertEqual(caught.exception.code, 0)
        launched = [json.loads(line) for line in argv_log.read_text().splitlines()]
        self.assertEqual(launched, [["--model", "opus"], ["--resume", "sid-kept", "--model", "opus"]])
        live = json.loads((self.home / ".claude.json").read_text())
        self.assertEqual(live["oauthAccount"]["emailAddress"], "a@example.com")
        left = json.loads(
            (self.home / ".claude" / "accounts" / "slots" / "b@example.com" / "credentials.json").read_text()
        )
        self.assertEqual(left["claudeAiOauth"]["accessToken"], "tok-b-live")

    def test_a_second_limit_within_20_seconds_does_not_switch_back(self) -> None:
        write_login(self.home, "a@example.com", "tok-a")
        cc_seat.cmd_add()
        write_login(self.home, "b@example.com", "tok-b")
        cc_seat.cmd_add()
        argv_log = self.home / "argv.log"
        fake = self.home / "fake-claude"
        fake.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, subprocess, sys, time\n"
            "from pathlib import Path\n"
            "log = Path(os.environ['SEAT_ARGV_LOG'])\n"
            "with log.open('a') as fh:\n"
            "    fh.write(json.dumps(sys.argv[1:]) + '\\n')\n"
            "payload = json.dumps({'session_id': 'sid-kept', 'error': 'rate_limit'})\n"
            "subprocess.run(\n"
            "    [sys.executable, os.environ['SEAT_TOOL'], 'hook', '--harness', 'claude'],\n"
            "    input=payload.encode(),\n"
            ")\n"
            "try:\n"
            "    time.sleep(30)\n"
            "except KeyboardInterrupt:\n"
            "    raise SystemExit(0)\n"
        )
        fake.chmod(0o755)
        previous = signal.getsignal(signal.SIGINT), signal.getsignal(signal.SIGTERM)
        os.environ["CLAUDE_BIN"] = str(fake)
        os.environ["SEAT_ARGV_LOG"] = str(argv_log)
        os.environ["SEAT_TOOL"] = str(ROOT / "cc_seat.py")
        try:
            with self.assertRaises(SystemExit) as caught:
                cc_seat.cmd_wrap("claude", [])
        finally:
            signal.signal(signal.SIGINT, previous[0])
            signal.signal(signal.SIGTERM, previous[1])
        self.assertNotEqual(caught.exception.code, 0)
        launched = [json.loads(line) for line in argv_log.read_text().splitlines()]
        self.assertEqual(launched, [[], ["--resume", "sid-kept"]])
        live = json.loads((self.home / ".claude.json").read_text())
        self.assertEqual(live["oauthAccount"]["emailAddress"], "a@example.com")


def _jwt(email: str) -> str:
    payload = json.dumps({"email": email}).encode()
    body = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    return f"header.{body}.sig"


class OtherHarnessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.env = mock.patch.dict(os.environ, {"CC_SEAT_HOME": str(self.home)})
        self.env.start()

    def tearDown(self) -> None:
        self.env.stop()
        self.tmp.cleanup()

    def test_grok_swap_keeps_raw_auth_of_the_seat_we_leave(self) -> None:
        grok = self.home / ".grok"
        grok.mkdir()
        auth = grok / "auth.json"

        def write(email: str, token: str) -> None:
            auth.write_bytes(json.dumps({"acct": {"email": email, "refresh_token": token}}).encode())

        write("a@example.com", "ga")
        cc_seat.cmd_add("grok")
        write("b@example.com", "gb")
        cc_seat.cmd_add("grok")
        write("b@example.com", "gb-rotated")
        old, new = cc_seat.swap_now(harness_name="grok")
        self.assertEqual((old, new), ("b@example.com", "a@example.com"))
        left = json.loads((grok / "accounts" / "slots" / "b@example.com" / "auth.json").read_text())
        self.assertEqual(left["acct"]["refresh_token"], "gb-rotated")
        live = json.loads(auth.read_text())
        self.assertEqual(live["acct"]["refresh_token"], "ga")

    def test_codex_swap_sets_file_store_and_ignores_sentry_file(self) -> None:
        codex = self.home / ".codex"
        codex.mkdir()
        (codex / "config.toml").write_text('model = "gpt-5"\n')
        auth = codex / "auth.json"
        sentry = codex / ".credentials.json"
        sentry.write_text('{"sentry|keep": true}\n')

        def write(email: str, token: str) -> None:
            auth.write_text(
                json.dumps(
                    {
                        "auth_mode": "chatgpt",
                        "tokens": {"refresh_token": token, "id_token": _jwt(email), "account_id": email},
                    }
                )
            )

        write("a@example.com", "ca")
        cc_seat.cmd_add("codex")
        write("b@example.com", "cb")
        cc_seat.cmd_add("codex")
        write("b@example.com", "cb-rotated")
        cc_seat.swap_now(harness_name="codex")
        left = json.loads((codex / "accounts" / "slots" / "b@example.com" / "auth.json").read_text())
        self.assertEqual(left["tokens"]["refresh_token"], "cb-rotated")
        live = json.loads(auth.read_text())
        self.assertEqual(live["tokens"]["refresh_token"], "ca")
        self.assertFalse((codex / "accounts" / "slots" / "a@example.com" / "credentials.json").exists())
        config = (codex / "config.toml").read_text()
        self.assertIn('cli_auth_credentials_store = "file"', config)
        self.assertIn('model = "gpt-5"', config)
        self.assertEqual(sentry.read_text(), '{"sentry|keep": true}\n')

    def test_codex_log_cursor_ignores_old_rows(self) -> None:
        import sqlite3

        codex = self.home / ".codex"
        codex.mkdir()
        db = codex / "logs_2.sqlite"
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE logs (id INTEGER PRIMARY KEY, thread_id TEXT, feedback_log_body TEXT)")
        conn.execute("INSERT INTO logs VALUES (1, 'old', 'hit your usage limit')")
        conn.commit()
        seat = cc_seat.CodexSeat()
        cursor = seat.log_cursor()
        cursor, hit = seat.poll_limit(cursor)
        self.assertIsNone(hit)
        conn.execute("INSERT INTO logs VALUES (2, 'thread-2', 'error 429 on the socket')")
        conn.commit()
        cursor, hit = seat.poll_limit(cursor)
        self.assertIsNone(hit)
        conn.execute("INSERT INTO logs VALUES (3, 'thread-3', 'You hit your usage limit')")
        conn.commit()
        _cursor, hit = seat.poll_limit(cursor)
        self.assertEqual(hit["session_id"], "thread-3")
        conn.close()

    def test_codex_usage_limit_resumes_that_thread_on_the_other_seat(self) -> None:
        codex = self.home / ".codex"
        codex.mkdir()
        auth = codex / "auth.json"

        def write(email: str, token: str) -> None:
            auth.write_text(
                json.dumps(
                    {
                        "auth_mode": "chatgpt",
                        "tokens": {"refresh_token": token, "id_token": _jwt(email), "account_id": email},
                    }
                )
            )

        write("a@example.com", "ca")
        cc_seat.cmd_add("codex")
        write("b@example.com", "cb")
        cc_seat.cmd_add("codex")
        write("b@example.com", "cb-live")
        argv_log = self.home / "argv.log"
        db = codex / "logs_2.sqlite"
        fake = self.home / "fake-codex"
        fake.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sqlite3, sys, time\n"
            "from pathlib import Path\n"
            "log = Path(os.environ['SEAT_ARGV_LOG'])\n"
            "with log.open('a') as fh:\n"
            "    fh.write(json.dumps(sys.argv[1:]) + '\\n')\n"
            "if sys.argv[1:2] == ['resume']:\n"
            "    raise SystemExit(0)\n"
            "conn = sqlite3.connect(os.environ['SEAT_LOG_DB'])\n"
            "conn.execute('CREATE TABLE IF NOT EXISTS logs (id INTEGER PRIMARY KEY, thread_id TEXT, feedback_log_body TEXT)')\n"
            "conn.execute(\"INSERT INTO logs (thread_id, feedback_log_body) VALUES ('thread-9', 'hit your usage limit')\")\n"
            "conn.commit()\n"
            "conn.close()\n"
            "try:\n"
            "    time.sleep(30)\n"
            "except KeyboardInterrupt:\n"
            "    raise SystemExit(0)\n"
        )
        fake.chmod(0o755)
        previous = signal.getsignal(signal.SIGINT), signal.getsignal(signal.SIGTERM)
        os.environ["CODEX_BIN"] = str(fake)
        os.environ["SEAT_ARGV_LOG"] = str(argv_log)
        os.environ["SEAT_LOG_DB"] = str(db)
        try:
            with self.assertRaises(SystemExit) as caught:
                cc_seat.cmd_wrap("codex", [])
        finally:
            signal.signal(signal.SIGINT, previous[0])
            signal.signal(signal.SIGTERM, previous[1])
        self.assertEqual(caught.exception.code, 0)
        launched = [json.loads(line) for line in argv_log.read_text().splitlines()]
        self.assertEqual(launched, [[], ["resume", "thread-9"]])
        live = json.loads(auth.read_text())
        self.assertEqual(live["tokens"]["refresh_token"], "ca")
        left = json.loads((codex / "accounts" / "slots" / "b@example.com" / "auth.json").read_text())
        self.assertEqual(left["tokens"]["refresh_token"], "cb-live")

    def test_grok_limit_uses_session_id_field_and_resumes(self) -> None:
        grok = self.home / ".grok"
        grok.mkdir()
        auth = grok / "auth.json"

        def write(email: str, token: str) -> None:
            auth.write_bytes(json.dumps({"acct": {"email": email, "refresh_token": token}}).encode())

        write("a@example.com", "ga")
        cc_seat.cmd_add("grok")
        write("b@example.com", "gb")
        cc_seat.cmd_add("grok")
        write("b@example.com", "gb-live")
        argv_log = self.home / "argv.log"
        fake = self.home / "fake-grok"
        fake.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, subprocess, sys, time\n"
            "from pathlib import Path\n"
            "log = Path(os.environ['SEAT_ARGV_LOG'])\n"
            "with log.open('a') as fh:\n"
            "    fh.write(json.dumps(sys.argv[1:]) + '\\n')\n"
            "if '--resume' in sys.argv:\n"
            "    raise SystemExit(0)\n"
            "payload = json.dumps({'sessionId': 'grok-sid', 'error': 'rate_limit'})\n"
            "subprocess.run(\n"
            "    [sys.executable, os.environ['SEAT_TOOL'], 'hook', '--harness', 'grok'],\n"
            "    input=payload.encode(),\n"
            ")\n"
            "try:\n"
            "    time.sleep(30)\n"
            "except KeyboardInterrupt:\n"
            "    raise SystemExit(0)\n"
        )
        fake.chmod(0o755)
        previous = signal.getsignal(signal.SIGINT), signal.getsignal(signal.SIGTERM)
        os.environ["GROK_BIN"] = str(fake)
        os.environ["SEAT_ARGV_LOG"] = str(argv_log)
        os.environ["SEAT_TOOL"] = str(ROOT / "cc_seat.py")
        try:
            with self.assertRaises(SystemExit) as caught:
                cc_seat.cmd_wrap("grok", [])
        finally:
            signal.signal(signal.SIGINT, previous[0])
            signal.signal(signal.SIGTERM, previous[1])
        self.assertEqual(caught.exception.code, 0)
        launched = [json.loads(line) for line in argv_log.read_text().splitlines()]
        self.assertEqual(launched, [[], ["--resume", "grok-sid"]])
        live = json.loads(auth.read_text())
        self.assertEqual(live["acct"]["refresh_token"], "ga")
        left = json.loads((grok / "accounts" / "slots" / "b@example.com" / "auth.json").read_text())
        self.assertEqual(left["acct"]["refresh_token"], "gb-live")


if __name__ == "__main__":
    unittest.main()
