# cc-seat

Switch a saved login when Grok or Codex hits a usage limit, then resume the same session.

This machine has one Claude login. `cc-seat` still starts Claude. A Claude limit does not switch. Grok and Codex each keep two logins. Use `grok-seat` and `codex-seat`.

The switch is the one in [cux](https://github.com/inulute/cux), [grok-accounts](https://github.com/puppe1990/grok-accounts), and [codex-account](https://github.com/frndchagas/codex-account):

1. Copy the live login back into the seat that owns it. Refresh tokens rotate, so the copy you made earlier is not enough.
2. Write the other seat into the live file.
3. Start the harness again on the same session id.

Do not log out. Logout revokes the token you just saved.

## Use

```bash
python3 cc_seat.py install
```

That links `cc-seat`, `grok-seat`, and `codex-seat` into `~/.local/bin`. It adds a `StopFailure` / `rate_limit` hook for Claude and Grok. It sets `cli_auth_credentials_store = "file"` in `~/.codex/config.toml` so Codex reads `auth.json`.

Save each Grok login and each Codex login. Log the second account in without logging the first one out.

```bash
grok
grok-seat add
grok
grok-seat add

codex login
codex-seat add
codex login
codex-seat add

cc-seat list
```

Run the wrapper instead of the bare binary:

```bash
grok-seat
codex-seat
```

`grok-seat swap` and `codex-seat swap` switch now. `cc-seat list` shows the saved seats. A `*` marks the active one.

## Where the files go

```
~/.claude/accounts/slots/<email>/credentials.json
~/.claude/accounts/slots/<email>/oauth.json
~/.grok/accounts/slots/<email>/auth.json
~/.codex/accounts/slots/<email>/auth.json
```

Files are mode `0600`. Directories are mode `0700`. Codex `.credentials.json` is a Sentry file. This tool does not copy it.

## What each harness watches

Grok runs a hook inside the wrapped process. The hook writes `signal-<pid>.json`. The wrapper sees that file, stops the process, refreshes the seat, and starts again with `--resume <session id>`. The same hook exists for Claude and switches only when a second Claude login is saved.

Codex has no such hook. The wrapper reads new rows from `~/.codex/logs_2.sqlite` by log id. It does not scan the whole database. A row that says the usage limit was hit starts the same switch. The next process is `codex resume <thread id>`.

A second limit within 20 seconds stops the loop.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

Tests use `CC_SEAT_HOME`. They do not read your real logins.
