#!/usr/bin/env python3
"""Two-seat Claude Code login swap. Tokens are never printed."""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

MAX_SLOTS = 2
SWAP_COOLDOWN_S = 20
STOP_WAIT_S = 8
VERSION = "0.1.0"

# Commands that belong to this tool (also matches the first local install).
_OUR_HOOK_MARKERS = ("cc-seat", "cc_seat.py", "rate-limit-seat.py", "save-seat.py")


def home() -> Path:
    return Path(os.environ.get("CC_SEAT_HOME") or Path.home())


def accounts_dir() -> Path:
    return home() / ".claude" / "accounts"


def slots_dir() -> Path:
    return accounts_dir() / "slots"


def runtime_dir() -> Path:
    return accounts_dir() / "runtime"


def state_path() -> Path:
    return accounts_dir() / "state.json"


def lock_path() -> Path:
    return accounts_dir() / ".lock"


def creds_path() -> Path:
    return home() / ".claude" / ".credentials.json"


def claude_json_path() -> Path:
    return home() / ".claude.json"


def settings_path() -> Path:
    return home() / ".claude" / "settings.json"


def _die(msg: str, code: int = 1) -> None:
    print(f"cc-seat: {msg}", file=sys.stderr)
    raise SystemExit(code)


def _info(msg: str) -> None:
    print(f"cc-seat: {msg}", file=sys.stderr)


def atomic_write(path: Path, data: object, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-")
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w") as fh:
            json.dump(data, fh, indent=2)
            fh.write("\n")
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


def _load_json(path: Path) -> dict:
    with path.open() as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        _die(f"not an object: {path}")
    return data


def _lock():
    lock_path().parent.mkdir(parents=True, exist_ok=True)
    fh = lock_path().open("a+")
    fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
    return fh


def load_state() -> dict:
    if not state_path().exists():
        return {"order": [], "active": None}
    return _load_json(state_path())


def save_state(state: dict) -> None:
    atomic_write(state_path(), state)


def live_email() -> str:
    path = claude_json_path()
    if not path.exists():
        _die("missing ~/.claude.json")
    account = _load_json(path).get("oauthAccount") or {}
    email = account.get("emailAddress")
    if not email:
        _die("no oauthAccount.emailAddress in ~/.claude.json")
    return email


def _chmod_private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass


def snapshot_live(email: str | None = None) -> str:
    if not creds_path().exists():
        _die("missing ~/.claude/.credentials.json — log in first")
    if not claude_json_path().exists():
        _die("missing ~/.claude.json")
    creds = _load_json(creds_path())
    claude = _load_json(claude_json_path())
    account = claude.get("oauthAccount")
    if not isinstance(account, dict):
        _die("no oauthAccount in ~/.claude.json")
    email = email or account.get("emailAddress")
    if not email:
        _die("oauthAccount has no emailAddress")
    slot = slots_dir() / email
    _chmod_private_dir(accounts_dir())
    _chmod_private_dir(slots_dir())
    _chmod_private_dir(slot)
    atomic_write(slot / "credentials.json", creds)
    atomic_write(slot / "oauth.json", account)
    return email


def install_slot(email: str) -> None:
    slot = slots_dir() / email
    creds_file = slot / "credentials.json"
    oauth_file = slot / "oauth.json"
    if not creds_file.exists() or not oauth_file.exists():
        _die(f"no snapshot for {email}")
    creds = _load_json(creds_file)
    oauth = _load_json(oauth_file)
    dest = claude_json_path()
    claude = _load_json(dest) if dest.exists() else {}
    claude["oauthAccount"] = oauth
    mode = dest.stat().st_mode & 0o777 if dest.exists() else 0o600
    atomic_write(creds_path(), creds)
    atomic_write(dest, claude, mode=mode)


def cmd_add(*, quiet: bool = False) -> None:
    lock = _lock()
    try:
        try:
            email = snapshot_live()
        except SystemExit:
            if quiet:
                return
            raise
        state = load_state()
        order = list(state.get("order") or [])
        added = email not in order
        if added:
            if len(order) >= MAX_SLOTS:
                if quiet:
                    return
                _die(f"already have {MAX_SLOTS} seats: {', '.join(order)}")
            order.append(email)
        state["order"] = order
        state["active"] = email
        save_state(state)
        if added or not quiet:
            _info(f"saved {email} ({len(order)}/{MAX_SLOTS})")
    finally:
        lock.close()


def cmd_list() -> None:
    state = load_state()
    order = state.get("order") or []
    active = state.get("active")
    if not order:
        _info("no seats. Log in, then: cc-seat add")
        return
    for i, email in enumerate(order, 1):
        mark = "*" if email == active else " "
        print(f"{mark} {i} {email}")


def other_email(state: dict, current: str) -> str | None:
    order = [e for e in (state.get("order") or []) if e]
    if len(order) < 2:
        return None
    for email in order:
        if email != current:
            return email
    return None


def swap_now(current: str | None = None) -> tuple[str, str]:
    lock = _lock()
    try:
        state = load_state()
        current = current or state.get("active") or live_email()
        snapshot_live(current)
        target = other_email(state, current)
        if not target:
            _die("need two seats before a swap. cc-seat add the second login.")
        install_slot(target)
        state["active"] = target
        save_state(state)
        return current, target
    finally:
        lock.close()


def cmd_swap() -> None:
    old, new = swap_now()
    _info(f"{old} → {new}")


def script_path() -> Path:
    return Path(__file__).resolve()


def claude_bin() -> str:
    env = os.environ.get("CLAUDE_BIN")
    if env:
        return env
    path = shutil.which("claude")
    if not path:
        _die("claude not on PATH")
    resolved = str(Path(path).resolve())
    self = str(script_path())
    if resolved == self:
        _die("cc-seat would call itself. Set CLAUDE_BIN to the real claude binary.")
    return path


def with_resume(argv: list[str], session_id: str) -> list[str]:
    out: list[str] = []
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in ("--resume", "-r", "--continue", "-c"):
            i += 1
            if i < len(argv) and not argv[i].startswith("-"):
                i += 1
            continue
        if arg.startswith("--resume="):
            i += 1
            continue
        out.append(arg)
        i += 1
    return ["--resume", session_id] + out


def signal_path(pid: int) -> Path:
    return runtime_dir() / f"signal-{pid}.json"


def read_signal(pid: int) -> dict | None:
    path = signal_path(pid)
    if not path.exists():
        return None
    try:
        data = _load_json(path)
    except (OSError, json.JSONDecodeError):
        return None
    try:
        path.unlink()
    except OSError:
        pass
    return data


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


def cmd_wrap(claude_args: list[str]) -> None:
    if not creds_path().exists():
        _die("not logged in")
    lock = _lock()
    try:
        email = snapshot_live()
        state = load_state()
        order = list(state.get("order") or [])
        if email not in order:
            if len(order) >= MAX_SLOTS:
                _info(f"live login {email} is not a saved seat")
            else:
                order.append(email)
        state["order"] = order
        state["active"] = email
        save_state(state)
        if len(order) < 2:
            _info(f"only one seat ({email}). Add the second with: claude login && cc-seat add")
    finally:
        lock.close()

    bin_path = claude_bin()
    pid = os.getpid()
    runtime_dir().mkdir(parents=True, exist_ok=True)
    leftover = signal_path(pid)
    if leftover.exists():
        leftover.unlink()

    env = os.environ.copy()
    env["CC_SEAT"] = "1"
    env["CC_SEAT_PID"] = str(pid)
    argv = list(claude_args)
    last_swap = 0.0
    child: subprocess.Popen | None = None

    def forward(signum, _frame):
        if child is not None and child.poll() is None:
            child.send_signal(signum)

    signal.signal(signal.SIGINT, forward)
    signal.signal(signal.SIGTERM, forward)

    while True:
        child = subprocess.Popen([bin_path] + argv, env=env)
        swap_sid = None
        while child.poll() is None:
            data = read_signal(pid)
            if data:
                state = load_state()
                current = state.get("active") or ""
                if not other_email(state, current):
                    _info("rate limit with only one saved seat — not swapping")
                    continue
                swap_sid = data.get("session_id") or None
                if not swap_sid:
                    _info("rate limit signal had no session_id — not swapping")
                    continue
                _info("rate limit — stopping this turn to swap seats")
                stop_child(child)
                break
            time.sleep(0.15)
        time.sleep(0.4)
        try:
            snapshot_live()
        except SystemExit as exc:
            if child.returncode:
                raise SystemExit(child.returncode) from exc
            _info("could not snapshot live credentials after exit")
        if swap_sid is None:
            raise SystemExit(child.returncode or 0)
        now = time.time()
        if now - last_swap < SWAP_COOLDOWN_S:
            _info("other seat also looks exhausted; not swapping again")
            raise SystemExit(child.returncode or 1)
        try:
            old, new = swap_now()
        except SystemExit:
            _info("swap failed; leaving credentials as they are")
            raise
        last_swap = now
        _info(f"rate limit on {old} → swapped to {new}, resuming")
        argv = with_resume(claude_args, str(swap_sid))


def write_rate_limit_signal() -> None:
    if os.environ.get("CC_SEAT") != "1":
        return
    pid_raw = os.environ.get("CC_SEAT_PID") or ""
    if not pid_raw.isdigit():
        return
    raw = sys.stdin.read()
    session_id = ""
    error = ""
    try:
        payload = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        payload = {}
    if isinstance(payload, dict):
        session_id = str(payload.get("session_id") or "")
        error = str(payload.get("error") or "")
    if error and error != "rate_limit":
        return
    runtime_dir().mkdir(parents=True, exist_ok=True)
    atomic_write(
        signal_path(int(pid_raw)),
        {"session_id": session_id, "error": "rate_limit", "ts": time.time()},
    )


def _our_command(command: str) -> bool:
    return any(mark in command for mark in _OUR_HOOK_MARKERS)


def _strip_our_hooks(groups: list) -> list:
    kept = []
    for group in groups:
        inner = []
        for hook in group.get("hooks") or []:
            if _our_command(str(hook.get("command") or "")):
                continue
            inner.append(hook)
        if inner:
            kept.append({**group, "hooks": inner})
    return kept


def _hook_cmd(args: str) -> dict:
    return {
        "type": "command",
        "command": f"{sys.executable} {script_path()} {args}",
        "timeout": 5,
    }


def cmd_install_hooks() -> None:
    path = settings_path()
    data = _load_json(path) if path.exists() else {}
    hooks = data.setdefault("hooks", {})
    hooks["StopFailure"] = _strip_our_hooks(hooks.get("StopFailure") or [])
    hooks["StopFailure"].append(
        {"matcher": "rate_limit", "hooks": [_hook_cmd("hook")]}
    )
    hooks["SessionStart"] = _strip_our_hooks(hooks.get("SessionStart") or [])
    hooks["SessionStart"].append(
        {"matcher": "startup|resume", "hooks": [_hook_cmd("add --quiet")]}
    )
    hooks["Notification"] = _strip_our_hooks(hooks.get("Notification") or [])
    hooks["Notification"].append(
        {"matcher": "auth_success", "hooks": [_hook_cmd("add --quiet")]}
    )
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
    atomic_write(path, data, mode=mode)
    _info(f"hooks written to {path}")


def cmd_uninstall_hooks() -> None:
    path = settings_path()
    if not path.exists():
        _info("no settings.json")
        return
    data = _load_json(path)
    hooks = data.get("hooks") or {}
    for name in ("StopFailure", "SessionStart", "Notification"):
        if name in hooks:
            hooks[name] = _strip_our_hooks(hooks[name] or [])
            if not hooks[name]:
                del hooks[name]
    atomic_write(path, data, mode=path.stat().st_mode & 0o777)
    _info("removed cc-seat hooks; seat snapshots were left in place")


def cmd_install() -> None:
    dest = Path(os.environ.get("CC_SEAT_BIN") or (Path.home() / ".local" / "bin" / "cc-seat"))
    dest.parent.mkdir(parents=True, exist_ok=True)
    src = script_path()
    if dest.exists() or dest.is_symlink():
        dest.unlink()
    try:
        dest.symlink_to(src)
    except OSError:
        shutil.copy2(src, dest)
        os.chmod(dest, 0o700)
    cmd_install_hooks()
    _info(f"installed {dest} -> {src}")
    _info("start sessions with `cc-seat` after you have two logins saved")


def main() -> None:
    _chmod_private_dir(accounts_dir())
    args = sys.argv[1:]
    commands = {
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
    if not args or args[0] not in commands:
        cmd_wrap(args)
        return
    cmd = args[0]
    if cmd == "add":
        cmd_add(quiet="--quiet" in args)
    elif cmd == "list":
        cmd_list()
    elif cmd == "swap":
        cmd_swap()
    elif cmd == "hook":
        write_rate_limit_signal()
    elif cmd == "install":
        cmd_install()
    elif cmd == "install-hooks":
        cmd_install_hooks()
    elif cmd == "uninstall-hooks":
        cmd_uninstall_hooks()
    elif cmd == "version":
        print(VERSION)
    elif cmd == "help":
        print(__doc__ or "cc-seat")
        print("usage: cc-seat [add|list|swap|install|install-hooks|uninstall-hooks|version] | <claude args>")


if __name__ == "__main__":
    main()
