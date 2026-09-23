#!/usr/bin/env python3
"""Seat rotator for Claude Code, Grok, and Codex.

The switch is the one used by cux, grok-accounts, and codex-account:
save the live login back into its own seat, then write the other seat
into the live file. A hook (Claude, Grok) or a new log row (Codex) tells
the wrapper to stop the process and resume. Tokens are never printed.
"""

from __future__ import annotations

import base64
import fcntl
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

VERSION = "0.3.0"
MAX_CLAUDE = 2
MAX_OTHER = 5
SWAP_COOLDOWN_S = 20
STOP_WAIT_S = 8
HOOK_MARKERS = ("cc_seat.py", "cc-seat")
CODEX_LIMIT_PHRASES = (
    "hit your usage limit",
    "hit your rate limit",
    "usage limit exceeded",
    "rate limit exceeded",
)
FILE_STORE_LINE = 'cli_auth_credentials_store = "file"'


def home() -> Path:
    return Path(os.environ.get("CC_SEAT_HOME") or Path.home())


def _die(msg: str, code: int = 1) -> None:
    print(f"cc-seat: {msg}", file=sys.stderr)
    raise SystemExit(code)


def _info(msg: str) -> None:
    print(f"cc-seat: {msg}", file=sys.stderr)


def _private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass


def atomic_bytes(path: Path, data: bytes, mode: int = 0o600) -> None:
    _private_dir(path.parent)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-")
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        os.chmod(path, mode)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def atomic_json(path: Path, data: object, mode: int = 0o600) -> None:
    raw = json.dumps(data, indent=2).encode() + b"\n"
    atomic_bytes(path, raw, mode)


def load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    with path.open() as fh:
        data = json.load(fh)
    return data if isinstance(data, dict) else {}


def _has_token(obj: object) -> bool:
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key in ("accessToken", "refreshToken", "access_token", "refresh_token") and value:
                return True
            if _has_token(value):
                return True
    elif isinstance(obj, list):
        return any(_has_token(item) for item in obj)
    return False


def _jwt_email(token: str) -> str:
    parts = token.split(".")
    if len(parts) < 2:
        return ""
    padded = parts[1] + "=" * ((4 - len(parts[1]) % 4) % 4)
    try:
        claims = json.loads(base64.urlsafe_b64decode(padded.encode()))
    except (json.JSONDecodeError, ValueError):
        return ""
    email = claims.get("email") if isinstance(claims, dict) else ""
    return email if isinstance(email, str) else ""


class Seat:
    name = "base"
    display = "Base"
    max_slots = MAX_OTHER

    def lock(self):
        _private_dir(self.root)
        fh = (self.root / ".lock").open("a+")
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        return fh

    @property
    def root(self) -> Path:
        raise NotImplementedError

    @property
    def slots(self) -> Path:
        return self.root / "slots"

    @property
    def runtime(self) -> Path:
        return self.root / "runtime"

    @property
    def state_path(self) -> Path:
        return self.root / "state.json"

    def load_state(self) -> dict:
        state = load_json(self.state_path)
        state.setdefault("order", [])
        state.setdefault("active", None)
        state.setdefault("cooldowns", {})
        return state

    def save_state(self, state: dict) -> None:
        atomic_json(self.state_path, state)

    def signal_path(self, pid: int) -> Path:
        return self.runtime / f"signal-{pid}.json"

    def live_identity(self) -> str:
        raise NotImplementedError

    def snapshot(self) -> str:
        """Write the live login into the seat that owns it. Return that identity."""
        raise NotImplementedError

    def install(self, identity: str) -> None:
        raise NotImplementedError

    def binary(self) -> str:
        raise NotImplementedError

    def resume_argv(self, argv: list[str], session_id: str) -> list[str]:
        raise NotImplementedError

    def install_hooks(self) -> None:
        return None

    def uninstall_hooks(self) -> None:
        return None

    def read_signal(self, pid: int) -> dict | None:
        path = self.signal_path(pid)
        if not path.exists():
            return None
        try:
            data = load_json(path)
        except (OSError, json.JSONDecodeError):
            return None
        try:
            path.unlink()
        except OSError:
            pass
        return data or None

    def poll_limit(self, cursor: int) -> tuple[int, dict | None]:
        return cursor, None

    def log_cursor(self) -> int:
        return 0

    def other(self, current: str | None) -> str | None:
        order = [item for item in self.load_state().get("order") or [] if item]
        if len(order) < 2:
            return None
        for identity in order:
            if identity != current:
                return identity
        return None

    def add(self, *, quiet: bool = False) -> str | None:
        lock = self.lock()
        try:
            try:
                identity = self.snapshot()
            except Exception as exc:
                if quiet:
                    return None
                _die(str(exc))
            state = self.load_state()
            order = list(state.get("order") or [])
            if identity not in order:
                if len(order) >= self.max_slots:
                    if quiet:
                        return identity
                    _die(f"already have {self.max_slots} seats: {', '.join(order)}")
                order.append(identity)
                _info(f"saved {identity} ({len(order)}/{self.max_slots})")
            else:
                _info(f"refreshed {identity}")
            state["order"] = order
            state["active"] = identity
            self.save_state(state)
            return identity
        finally:
            lock.close()

    def swap(self) -> tuple[str, str]:
        """Save the live login into its own seat, then install the other seat."""
        lock = self.lock()
        try:
            state = self.load_state()
            try:
                current = self.snapshot()
            except Exception as exc:
                current = state.get("active") or ""
                if current:
                    _info(f"could not refresh {current} before switch: {exc}")
                else:
                    raise
            if current and current not in (state.get("order") or []):
                _info(f"live login {current} is not a saved seat; it was not overwritten onto another seat")
            target = self.other(current)
            if not target:
                raise RuntimeError(f"need two seats for {self.display}")
            self.install(target)
            state = self.load_state()
            state["active"] = target
            if current:
                state.setdefault("cooldowns", {})[current] = time.time()
            self.save_state(state)
            return current or "?", target
        finally:
            lock.close()


def _which(env_name: str, program: str) -> str:
    env = os.environ.get(env_name)
    if env:
        return env
    path = shutil.which(program)
    if not path:
        raise RuntimeError(f"{program} not on PATH")
    if Path(path).resolve() == Path(__file__).resolve():
        raise RuntimeError(f"{program} points at cc-seat. Set {env_name} to the real binary.")
    return path


def _strip_flag(argv: list[str], flags: set[str]) -> list[str]:
    out: list[str] = []
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg in flags:
            index += 1
            if index < len(argv) and not argv[index].startswith("-"):
                index += 1
            continue
        if any(arg.startswith(flag + "=") for flag in flags):
            index += 1
            continue
        out.append(arg)
        index += 1
    return out


class ClaudeSeat(Seat):
    name = "claude"
    display = "Claude Code"
    max_slots = MAX_CLAUDE

    @property
    def root(self) -> Path:
        return home() / ".claude" / "accounts"

    @property
    def creds_path(self) -> Path:
        return home() / ".claude" / ".credentials.json"

    @property
    def claude_json(self) -> Path:
        return home() / ".claude.json"

    @property
    def settings_path(self) -> Path:
        return home() / ".claude" / "settings.json"

    def live_identity(self) -> str:
        account = load_json(self.claude_json).get("oauthAccount") or {}
        email = account.get("emailAddress") if isinstance(account, dict) else ""
        if not email:
            raise RuntimeError("no oauthAccount.emailAddress in ~/.claude.json")
        return email

    def snapshot(self) -> str:
        if not self.creds_path.exists():
            raise RuntimeError("missing ~/.claude/.credentials.json — log in first")
        creds = load_json(self.creds_path)
        if not _has_token(creds):
            raise RuntimeError("live Claude credentials have no account token")
        account = load_json(self.claude_json).get("oauthAccount")
        if not isinstance(account, dict) or not account.get("emailAddress"):
            raise RuntimeError("no oauthAccount in ~/.claude.json")
        email = account["emailAddress"]
        slot = self.slots / email
        _private_dir(self.root)
        _private_dir(self.slots)
        _private_dir(slot)
        atomic_json(slot / "credentials.json", creds)
        atomic_json(slot / "oauth.json", account)
        return email

    def install(self, identity: str) -> None:
        slot = self.slots / identity
        creds = load_json(slot / "credentials.json")
        oauth = load_json(slot / "oauth.json")
        if oauth.get("emailAddress") != identity:
            raise RuntimeError(f"seat {identity} does not match its saved login")
        if not _has_token(creds):
            raise RuntimeError(f"seat {identity} has no account token — log in again and run cc-seat add")
        dest = self.claude_json
        merged = load_json(dest) if dest.exists() else {}
        merged["oauthAccount"] = oauth
        mode = dest.stat().st_mode & 0o777 if dest.exists() else 0o600
        atomic_json(self.creds_path, creds)
        atomic_json(dest, merged, mode=mode)

    def binary(self) -> str:
        return _which("CLAUDE_BIN", "claude")

    def resume_argv(self, argv: list[str], session_id: str) -> list[str]:
        kept = _strip_flag(argv, {"--resume", "-r", "--continue", "-c"})
        return ["--resume", session_id, *kept]

    def install_hooks(self) -> None:
        path = self.settings_path
        data = load_json(path) if path.exists() else {}
        hooks = data.setdefault("hooks", {})
        command = f"{sys.executable} {Path(__file__).resolve()}"

        def group(args: str, matcher: str | None = None) -> dict:
            item = {"hooks": [{"type": "command", "command": f"{command} {args}", "timeout": 5}]}
            if matcher:
                item["matcher"] = matcher
            return item

        for event in ("StopFailure", "SessionStart", "Notification"):
            if event in hooks:
                hooks[event] = _strip_hook_groups(hooks[event])
        hooks.setdefault("StopFailure", []).append(group("hook --harness claude", "rate_limit"))
        hooks.setdefault("SessionStart", []).append(group("add --harness claude --quiet", "startup|resume"))
        mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
        atomic_json(path, data, mode=mode)

    def uninstall_hooks(self) -> None:
        _uninstall_settings_hooks(self.settings_path)


class GrokSeat(Seat):
    name = "grok"
    display = "Grok"

    @property
    def root(self) -> Path:
        return home() / ".grok" / "accounts"

    @property
    def auth_path(self) -> Path:
        return home() / ".grok" / "auth.json"

    @property
    def hook_path(self) -> Path:
        return home() / ".grok" / "hooks" / "cc-seat.json"

    def live_identity(self) -> str:
        return _grok_email(load_json(self.auth_path))

    def snapshot(self) -> str:
        if not self.auth_path.exists():
            raise RuntimeError("missing ~/.grok/auth.json — log in first")
        raw = self.auth_path.read_bytes()
        email = _grok_email(json.loads(raw))
        if not email:
            raise RuntimeError("no email in ~/.grok/auth.json")
        if not _has_token(json.loads(raw)):
            raise RuntimeError("live Grok auth has no refresh token")
        slot = self.slots / email
        _private_dir(self.root)
        _private_dir(self.slots)
        _private_dir(slot)
        atomic_bytes(slot / "auth.json", raw)
        return email

    def install(self, identity: str) -> None:
        src = self.slots / identity / "auth.json"
        if not src.exists():
            raise RuntimeError(f"no snapshot for {identity}")
        raw = src.read_bytes()
        email = _grok_email(json.loads(raw))
        if email != identity:
            raise RuntimeError(f"seat {identity} does not match its saved login")
        if not _has_token(json.loads(raw)):
            raise RuntimeError(f"seat {identity} has no refresh token — log in again and run grok-seat add")
        atomic_bytes(self.auth_path, raw)

    def binary(self) -> str:
        return _which("GROK_BIN", "grok")

    def resume_argv(self, argv: list[str], session_id: str) -> list[str]:
        kept = _strip_flag(argv, {"--resume", "-r", "--continue", "-c"})
        return ["--resume", session_id, *kept]

    def install_hooks(self) -> None:
        command = f"{sys.executable} {Path(__file__).resolve()}"
        payload = {
            "hooks": {
                "StopFailure": [
                    {
                        "matcher": "rate_limit",
                        "hooks": [{"type": "command", "command": f"{command} hook --harness grok", "timeout": 5}],
                    }
                ],
                "SessionStart": [
                    {
                        "matcher": "startup|resume",
                        "hooks": [{"type": "command", "command": f"{command} add --harness grok --quiet", "timeout": 5}],
                    }
                ],
            }
        }
        atomic_json(self.hook_path, payload)

    def uninstall_hooks(self) -> None:
        if self.hook_path.exists():
            self.hook_path.unlink()


class CodexSeat(Seat):
    name = "codex"
    display = "Codex"

    @property
    def root(self) -> Path:
        return home() / ".codex" / "accounts"

    @property
    def auth_path(self) -> Path:
        return home() / ".codex" / "auth.json"

    @property
    def config_path(self) -> Path:
        return home() / ".codex" / "config.toml"

    @property
    def log_path(self) -> Path:
        return home() / ".codex" / "logs_2.sqlite"

    def live_identity(self) -> str:
        return _codex_email(load_json(self.auth_path))

    def snapshot(self) -> str:
        if not self.auth_path.exists():
            raise RuntimeError("missing ~/.codex/auth.json — log in first")
        raw = self.auth_path.read_bytes()
        data = json.loads(raw)
        email = _codex_email(data)
        if not email:
            raise RuntimeError("no email in ~/.codex/auth.json")
        if not _has_token(data):
            raise RuntimeError("live Codex auth has no refresh token")
        slot = self.slots / email
        _private_dir(self.root)
        _private_dir(self.slots)
        _private_dir(slot)
        atomic_bytes(slot / "auth.json", raw)
        return email

    def install(self, identity: str) -> None:
        src = self.slots / identity / "auth.json"
        if not src.exists():
            raise RuntimeError(f"no snapshot for {identity}")
        raw = src.read_bytes()
        data = json.loads(raw)
        email = _codex_email(data)
        if email != identity:
            raise RuntimeError(f"seat {identity} does not match its saved login")
        if not _has_token(data):
            raise RuntimeError(f"seat {identity} has no refresh token — log in again and run codex-seat add")
        self.ensure_file_store()
        atomic_bytes(self.auth_path, raw)

    def ensure_file_store(self) -> None:
        text = self.config_path.read_text() if self.config_path.exists() else ""
        lines = text.splitlines()
        replaced = False
        out: list[str] = []
        for line in lines:
            if line.strip().startswith("cli_auth_credentials_store"):
                out.append(FILE_STORE_LINE)
                replaced = True
            else:
                out.append(line)
        if not replaced:
            out.insert(0, FILE_STORE_LINE)
        updated = "\n".join(out).rstrip() + "\n"
        if updated != (text if text.endswith("\n") or text == "" else text + "\n") and updated != text:
            mode = self.config_path.stat().st_mode & 0o777 if self.config_path.exists() else 0o600
            atomic_bytes(self.config_path, updated.encode(), mode=mode)

    def binary(self) -> str:
        return _which("CODEX_BIN", "codex")

    def resume_argv(self, argv: list[str], session_id: str) -> list[str]:
        kept: list[str] = []
        index = 0
        while index < len(argv):
            if argv[index] == "resume":
                index += 1
                if index < len(argv) and not argv[index].startswith("-"):
                    index += 1
                continue
            kept.append(argv[index])
            index += 1
        if session_id:
            return ["resume", session_id, *kept]
        return kept

    def log_cursor(self) -> int:
        if not self.log_path.exists():
            return 0
        try:
            conn = sqlite3.connect(f"file:{self.log_path}?mode=ro", uri=True, timeout=0.2)
            row = conn.execute("SELECT COALESCE(MAX(id), 0) FROM logs").fetchone()
            conn.close()
        except sqlite3.Error:
            return 0
        return int(row[0] if row else 0)

    def poll_limit(self, cursor: int) -> tuple[int, dict | None]:
        if not self.log_path.exists():
            return cursor, None
        try:
            conn = sqlite3.connect(f"file:{self.log_path}?mode=ro", uri=True, timeout=0.2)
            rows = conn.execute(
                "SELECT id, thread_id, feedback_log_body FROM logs WHERE id > ? ORDER BY id ASC LIMIT 40",
                (cursor,),
            ).fetchall()
            conn.close()
        except sqlite3.Error:
            return cursor, None
        hit = None
        for row_id, thread_id, body in rows:
            cursor = int(row_id)
            text = (body or "").lower()
            if any(phrase in text for phrase in CODEX_LIMIT_PHRASES):
                hit = {"session_id": thread_id or "", "error": "rate_limit"}
        return cursor, hit


def _grok_email(data: dict) -> str:
    for value in data.values():
        if isinstance(value, dict) and isinstance(value.get("email"), str):
            return value["email"]
    return ""


def _codex_email(data: dict) -> str:
    tokens = data.get("tokens") or {}
    if not isinstance(tokens, dict):
        return ""
    email = _jwt_email(str(tokens.get("id_token") or ""))
    if email:
        return email
    account_id = tokens.get("account_id")
    return f"codex-{account_id[:8]}" if account_id else ""


SEATS: dict[str, type[Seat]] = {
    "claude": ClaudeSeat,
    "grok": GrokSeat,
    "codex": CodexSeat,
}


def get_seat(name: str | None = None) -> Seat:
    key = (name or "claude").lower()
    if key not in SEATS:
        _die(f"unknown harness {key}")
    return SEATS[key]()


def _strip_hook_groups(groups: list) -> list:
    kept = []
    for group in groups:
        inner = [
            hook
            for hook in (group.get("hooks") or [])
            if not any(marker in str(hook.get("command", "")) for marker in HOOK_MARKERS)
        ]
        if inner:
            kept.append({**group, "hooks": inner})
    return kept


def _uninstall_settings_hooks(path: Path) -> None:
    if not path.exists():
        return
    data = load_json(path)
    hooks = data.get("hooks") or {}
    for name in ("StopFailure", "SessionStart", "Notification"):
        if name not in hooks:
            continue
        kept = _strip_hook_groups(hooks[name])
        if kept:
            hooks[name] = kept
        else:
            del hooks[name]
    mode = path.stat().st_mode & 0o777
    atomic_json(path, data, mode=mode)


def accounts_dir() -> Path:
    return ClaudeSeat().root


def runtime_dir() -> Path:
    return ClaudeSeat().runtime


def load_state(harness_name: str = "claude") -> dict:
    return get_seat(harness_name).load_state()


def with_resume(argv: list[str], session_id: str) -> list[str]:
    return ClaudeSeat().resume_argv(argv, session_id)


def swap_now(current: str | None = None, harness_name: str = "claude") -> tuple[str, str]:
    del current
    return get_seat(harness_name).swap()


def cmd_add(harness_name: str = "claude", *, quiet: bool = False) -> None:
    get_seat(harness_name).add(quiet=quiet)


def cmd_list(harness_name: str | None = None) -> None:
    names = [harness_name] if harness_name else list(SEATS)
    for name in names:
        seat = get_seat(name)
        state = seat.load_state()
        order = state.get("order") or []
        active = state.get("active")
        print(f"\n--- {seat.display} ({len(order)}) ---")
        if not order:
            print(f"  no seats. Log in, then: cc-seat {seat.name} add")
            continue
        now = time.time()
        for index, identity in enumerate(order, 1):
            mark = "*" if identity == active else " "
            stamp = (state.get("cooldowns") or {}).get(identity) or 0
            status = ""
            if stamp and (now - stamp) < 18000:
                mins = int((18000 - (now - stamp)) / 60)
                status = f" [limit hit, ~{mins}m]"
            print(f" {mark} {index} {identity}{status}")


def cmd_swap(harness_name: str = "claude") -> None:
    old, new = get_seat(harness_name).swap()
    _info(f"{old} → {new}")


def write_rate_limit_signal(harness_name: str = "claude") -> None:
    if os.environ.get("CC_SEAT") != "1":
        return
    pid_raw = os.environ.get("CC_SEAT_PID") or ""
    if not pid_raw.isdigit():
        return
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        payload = {}
    if not isinstance(payload, dict):
        return
    error = str(payload.get("error") or "")
    if error and error != "rate_limit":
        return
    session_id = str(payload.get("session_id") or payload.get("sessionId") or "")
    seat = get_seat(harness_name)
    _private_dir(seat.runtime)
    atomic_json(
        seat.signal_path(int(pid_raw)),
        {"session_id": session_id, "error": "rate_limit", "ts": time.time()},
    )


def cmd_install_hooks(harness_name: str | None = None) -> None:
    names = [harness_name] if harness_name else ("claude", "grok")
    for name in names:
        seat = get_seat(name)
        seat.install_hooks()
        _info(f"hooks installed for {seat.display}")


def cmd_uninstall_hooks(harness_name: str | None = None) -> None:
    names = [harness_name] if harness_name else ("claude", "grok")
    for name in names:
        seat = get_seat(name)
        seat.uninstall_hooks()
        _info(f"hooks removed for {seat.display}")


def cmd_install() -> None:
    bin_dir = Path(os.environ.get("CC_SEAT_BIN_DIR") or (Path.home() / ".local" / "bin"))
    bin_dir.mkdir(parents=True, exist_ok=True)
    src = Path(__file__).resolve()
    for name in ("cc-seat", "grok-seat", "codex-seat"):
        dest = bin_dir / name
        if dest.is_symlink() or dest.exists():
            dest.unlink()
        try:
            dest.symlink_to(src)
        except OSError:
            shutil.copy2(src, dest)
            os.chmod(dest, 0o700)
        _info(f"linked {dest}")
    cmd_install_hooks()
    CodexSeat().ensure_file_store()
    _info("Codex will read auth.json from disk")


def cmd_login(harness_name: str = "claude") -> None:
    seat = get_seat(harness_name)
    argv = [seat.binary()] if seat.name == "grok" else [seat.binary(), "login"]
    _info(f"opening login for {seat.display}")
    code = subprocess.call(argv)
    if code != 0:
        _die(f"login exited {code}", code=code)
    cmd_add(harness_name)


def stop_child(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    proc.send_signal(signal.SIGINT)
    try:
        proc.wait(timeout=STOP_WAIT_S)
        return
    except subprocess.TimeoutExpired:
        pass
    proc.terminate()
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def cmd_wrap(harness_name: str, argv: list[str]) -> None:
    seat = get_seat(harness_name)
    try:
        identity = seat.snapshot()
        state = seat.load_state()
        order = list(state.get("order") or [])
        if identity not in order and len(order) < seat.max_slots:
            order.append(identity)
        if identity in order:
            state["order"] = order
            state["active"] = identity
            seat.save_state(state)
        if len(order) < 2:
            _info(f"one seat ({identity}). Log the other account in, then: cc-seat {seat.name} add")
    except Exception as exc:
        _info(f"starting {seat.display} ({exc})")

    binary = seat.binary()
    pid = os.getpid()
    _private_dir(seat.runtime)
    leftover = seat.signal_path(pid)
    if leftover.exists():
        leftover.unlink()

    env = os.environ.copy()
    env["CC_SEAT"] = "1"
    env["CC_SEAT_PID"] = str(pid)
    env["CC_SEAT_HARNESS"] = seat.name
    last_swap = 0.0
    child: subprocess.Popen | None = None

    def forward(signum, _frame):
        if child is not None and child.poll() is None:
            child.send_signal(signum)

    signal.signal(signal.SIGINT, forward)
    signal.signal(signal.SIGTERM, forward)
    _info(f"wrapping {seat.display}")

    while True:
        cursor = seat.log_cursor()
        child = subprocess.Popen([binary, *argv], env=env)
        session_id = None
        while child.poll() is None:
            data = seat.read_signal(pid)
            if data is None:
                cursor, data = seat.poll_limit(cursor)
            if not data:
                time.sleep(0.25)
                continue
            current = seat.load_state().get("active") or ""
            found = str(data.get("session_id") or "")
            if not seat.other(current):
                _info("rate limit with one saved seat — not switching")
            elif seat.name != "codex" and not found:
                _info("rate limit signal had no session id — not switching")
            else:
                session_id = found
                _info(f"rate limit — stopping {seat.display} to switch seats")
                stop_child(child)
                break
            time.sleep(0.25)

        time.sleep(0.4)
        try:
            seat.snapshot()
        except Exception:
            pass
        if session_id is None:
            raise SystemExit(child.returncode or 0)

        now = time.time()
        if now - last_swap < SWAP_COOLDOWN_S:
            _info("the other seat also looks limited — not switching again")
            raise SystemExit(child.returncode or 1)
        old, new = seat.swap()
        last_swap = now
        _info(f"{old} → {new}")
        argv = seat.resume_argv(argv, session_id)


def main() -> None:
    _private_dir(accounts_dir())
    raw = sys.argv[1:]
    prog = Path(sys.argv[0]).name
    selected = "claude"
    if "grok" in prog:
        selected = "grok"
    elif "codex" in prog:
        selected = "codex"

    commands = {
        "login",
        "add",
        "list",
        "swap",
        "hook",
        "install",
        "install-hooks",
        "uninstall-hooks",
        "version",
        "help",
    }
    filtered: list[str] = []
    index = 0
    named = False
    while index < len(raw):
        arg = raw[index]
        if arg in ("--harness", "-H") and index + 1 < len(raw):
            selected = raw[index + 1].lower()
            named = True
            index += 2
            continue
        if arg in SEATS and index == 0:
            selected = arg
            named = True
            index += 1
            continue
        filtered.append(arg)
        index += 1

    if not filtered:
        cmd_wrap(selected, [])
        return
    cmd = filtered[0]
    rest = filtered[1:]
    if cmd not in commands:
        cmd_wrap(selected, filtered)
        return
    if cmd == "login":
        cmd_login(selected)
    elif cmd == "add":
        cmd_add(selected, quiet="--quiet" in rest)
    elif cmd == "list":
        cmd_list(selected if named else None)
    elif cmd == "swap":
        cmd_swap(selected)
    elif cmd == "hook":
        write_rate_limit_signal(selected)
    elif cmd == "install":
        cmd_install()
    elif cmd == "install-hooks":
        cmd_install_hooks(selected if named else None)
    elif cmd == "uninstall-hooks":
        cmd_uninstall_hooks(selected if named else None)
    elif cmd == "version":
        print(VERSION)
    elif cmd == "help":
        print(f"cc-seat {VERSION}")
        print("  cc-seat | grok-seat | codex-seat          run the harness and switch on a limit")
        print("  cc-seat [claude|grok|codex] add           save the live login")
        print("  cc-seat list                              show seats")
        print("  cc-seat [claude|grok|codex] swap          switch now")
        print("  cc-seat install                           link the commands and install hooks")


if __name__ == "__main__":
    main()
