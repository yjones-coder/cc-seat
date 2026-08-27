# cc-seat

Swap two Claude Code logins when a rate limit hits, then resume the same session.

A Stop hook cannot restart Claude. `cc-seat` is a small wrapper plus a `StopFailure` hook with matcher `rate_limit`. The hook writes a signal. The wrapper stops the process, swaps the other saved login, and relaunches with `--resume`.

Not affiliated with Anthropic. Linux and WSL only. macOS Keychain is not supported.

## Why this exists

Claude Code stores one live OAuth login. If you hold two Pro (or Max) seats, you can snapshot each login and rotate when the 5-hour window fills. You keep one `~/.claude` tree: skills, MCP, and session history stay shared.

This is not a proxy and not a third-party harness. Claude Code still talks to Anthropic. The script only copies the files Claude Code already writes on Linux:

- `~/.claude/.credentials.json`
- `~/.claude.json` (`oauthAccount`)

## Install

Python 3.10+. `claude` on your `PATH`.

```bash
git clone https://github.com/yjones-coder/cc-seat.git
cd cc-seat
python3 cc_seat.py install
```

That symlinks `~/.local/bin/cc-seat` and merges hooks into `~/.claude/settings.json`. Other hooks are left in place.

## Setup two seats

Do **not** run `claude logout`. Logout revokes the refresh token.

1. Log in as account A (`claude` or `/login`).
2. `cc-seat add` — or just start a session; `auth_success` and `SessionStart` save the live login.
3. Log in as account B. Same machine, no logout.
4. `cc-seat add` again (or wait for the login hook).
5. `cc-seat list` — two emails, one marked `*`.

After that, start work with **`cc-seat`**, not `claude`:

```bash
cc-seat
cc-seat --resume <id>
```

A third login is ignored.

## Commands

| Command | Effect |
|---|---|
| `cc-seat` | Run Claude. On rate-limit, swap and `--resume`. |
| `cc-seat add` | Snapshot the live login (max 2). |
| `cc-seat list` | Show seats. `*` is active. |
| `cc-seat swap` | Swap files only. No resume. |
| `cc-seat install` | Symlink the CLI and write hooks. |
| `cc-seat install-hooks` | Write hooks only. |
| `cc-seat uninstall-hooks` | Remove our hooks. Snapshots stay. |

Pass any Claude flag through: `cc-seat -r <id>`.

## What a swap looks like

1. The live turn hits `rate_limit`.
2. The hook writes `~/.claude/accounts/runtime/signal-<pid>.json` with the session id.
3. The wrapper snapshots the live tokens (Claude rotates refresh tokens), installs the other seat, relaunches `claude --resume <id>`.
4. The conversation is the same. The last prompt that 429'd is not sent again. Type the next message.

If both seats 429 within 20 seconds, it stops bouncing.

## Storage

```
~/.claude/accounts/          mode 0700
  state.json                 which emails, which is active
  slots/<email>/
    credentials.json         mode 0600
    oauth.json               mode 0600
  runtime/signal-<pid>.json  rate-limit ping, wrapper only
```

Tokens are never printed or logged. The helper that reads a token does not put it in error text.

## Limits

- **CLI only.** The VS Code / Desktop session is not wrapped.
- **Two seats.** No usage API, no drain strategy, no macOS Keychain.
- **Accounts on the same payment method** may share a quota. A swap will not help then.
- A hook inside Claude cannot restart Claude. If you launch `claude` instead of `cc-seat`, a rate limit will not resume.

## Uninstall

```bash
cc-seat uninstall-hooks
rm ~/.local/bin/cc-seat
# optional: rm -rf ~/.claude/accounts
```

## Develop

```bash
python3 -m unittest discover -s tests -v
```

Tests use a temp `CC_SEAT_HOME`. They never touch your real login.

## License

MIT. See [LICENSE](LICENSE).
