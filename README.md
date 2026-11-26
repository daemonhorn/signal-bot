# signal-bot

Message Claude Code from your phone over Signal. A small Python bridge listens
on a Signal account. When a **whitelisted** sender sends a message that **starts
with `@claude`**, the bridge runs it through `claude -p` on this machine and
sends the answer back.

```
@claude how full is the root filesystem?
@claude summarise ~/notes/todo.md
@claude /new
```

## How it works

```
 Phone ──Signal──▶ signal-cli (Docker: bbernhard/signal-cli-rest-api, json-rpc mode)
                     │  websocket  ws://127.0.0.1:8080/v1/receive/<account>
                     ▼                                   ▲  POST /v2/send
               signal-bot (Python, systemd, runs as you) │
                     │  whitelist ✓  trigger "@claude" ✓ │
                     ▼                                   │
               claude -p --output-format json --resume <session> ──┘
                    (cwd = workdir, --allowedTools from config, --permission-mode dontAsk)
```

- **signal-cli** runs in Docker. Its REST API is bound to `127.0.0.1` only, and
  the account keys live in `./signal-cli-config/`.
- **signal-bot** (`signal_bot/`) runs as a systemd service under your user, so it
  uses your Claude Code login. Each Signal user gets their own Claude session,
  which persists across messages and restarts (`state/sessions.json`).
  Requests from one user run one at a time and queue. At most
  `max_concurrent` requests run across all users.
- While Claude works, the bot reacts 👀 and shows the typing indicator. Long
  answers are split into several messages.

## Setup

See **[setup.md](setup.md)**. In short:

```bash
git clone git@github.com:daemonhorn/signal-bot.git && cd signal-bot
./setup.sh
```

The script installs the packages, starts signal-cli, and links to an existing
Signal account or registers a new number. It then asks for the whitelist and
tool permissions, and installs the `signal-bot` systemd service.

## Commands

Every message must start with the trigger (default `@claude`). The match ignores
case, and `@claude:` or `@claude,` also work. Messages without the trigger are
ignored silently.

| Message | Effect |
|---|---|
| `@claude <anything>` | Send it to Claude, continuing your current conversation |
| `@claude /new` | Forget the conversation and start fresh |
| `@claude /status` | Show your session id, whether a request is running or queued, and the workdir |
| `@claude /cancel` | Kill your running request |
| `@claude` or `@claude /help` | Show help |

## Security model

Read this before you widen the permissions. Anyone on the whitelist can run
Claude Code **as your Unix user** on this machine, within the tools you allow.

- **Whitelist** (`[access].allowed`). Only listed phone numbers or Signal UUIDs
  are served. Everyone else is ignored and logged. The bot doesn't reply to
  them unless you set `reply_to_unknown = true`.
- **Trigger prefix.** The bot acts only on messages that start with `@claude`. In
  linked-device mode it can see all your 1:1 chats, so this keeps it from
  answering ordinary messages.
- **Tool allowlist.** Claude runs with `--permission-mode dontAsk` and
  `--allowedTools <your list>`, so any tool not on the list is denied instead of
  prompting. The default is read-only: `Read, Glob, Grep, WebSearch, WebFetch`. A
  denied tool shows up at the end of the reply as `[blocked tools: …]`.
  - Adding `Edit`/`Write` lets whitelisted users change files.
  - Adding plain `Bash` lets them run **any command**. Scope it instead, for
    example `Bash(git status:*)` or `Bash(df:*)`.
  - The bot refuses to start with `bypassPermissions` or
    `--dangerously-skip-permissions`.
- **Workdir.** Claude starts in `[claude].workdir`. That sets where it starts,
  not a hard sandbox: read tools can still read any file your user can read.
- **signal-cli API.** It is reachable only from localhost. Anything that can
  reach it can read and send messages as the bot's account.
- **Secrets.** `config.toml` (mode 600), `signal-cli-config/` (mode 700, holds the
  account keys) and `state/` are gitignored.
- The service runs with `NoNewPrivileges=true`, so Claude cannot `sudo` even if
  Bash is allowed.

## Configuration

`config.toml` is written by `setup.sh`. Every option is documented in
[`config.example.toml`](config.example.toml). Restart the service after editing:
`sudo systemctl restart signal-bot`.

| Key | Default | Meaning |
|---|---|---|
| `signal.account` | | Bot's number (your number in linked mode) |
| `access.allowed` | | Whitelisted numbers or `uuid:` ids |
| `access.reply_to_unknown` | `false` | Tell non-whitelisted senders they are not authorised |
| `bot.trigger` | `@claude` | Required message prefix |
| `bot.max_concurrent` | `2` | Claude processes running at once, across all users |
| `bot.chunk_size` | `1800` | Characters per outgoing Signal message |
| `claude.binary` | | Absolute path to `claude` |
| `claude.workdir` | | Claude's working directory |
| `claude.allowed_tools` | read-only set | Permission rules passed to `--allowedTools` |
| `claude.model` | `""` | Model override (`--model`) |
| `claude.timeout_seconds` | `600` | Request timeout; on expiry the process group is killed |
| `claude.max_budget_usd` | `0` | Per-request spend cap (`--max-budget-usd`); 0 = none |
| `claude.append_system_prompt` | phone-friendly hint | Extra system prompt |
| `claude.extra_args` | `[]` | Extra CLI flags, e.g. `["--add-dir", "/srv/data"]` |

## Operations

```bash
journalctl -u signal-bot -f                  # bridge logs (ignored senders show up here)
docker compose logs -f signal-cli            # signal-cli logs
sudo systemctl restart signal-bot            # after editing config.toml
./setup.sh --reconfigure                     # re-ask whitelist, trigger, tools
.venv/bin/signal-bot --config config.toml --check   # validate config and reach the API
```

**Updating signal-cli.** Signal changes its protocol from time to time, and an
outdated signal-cli eventually stops working. Bump the tag in
`docker-compose.yml` (or set `SIGNAL_CLI_TAG` in `.env`), then run:

```bash
docker compose pull && docker compose up -d && sudo systemctl restart signal-bot
```

**Updating the bot:** `git pull && .venv/bin/pip install -e . && sudo systemctl restart signal-bot`

**Uninstall:** `./setup.sh --uninstall`

## Troubleshooting

| Symptom | Check |
|---|---|
| No reply at all | Does the message start with `@claude`? Is the sender whitelisted? Run `journalctl -u signal-bot -f` and look for "ignoring message from non-whitelisted sender". |
| Sender is ignored though their number is listed | They may hide their number. Copy the `uuid:…` from the log line into `allowed`. |
| `signal-cli REST API not reachable` | `docker compose ps`; `docker compose up -d` |
| `must run with MODE=json-rpc` | The container env is wrong; use the provided `docker-compose.yml`. |
| `Claude Code is not logged in` / auth errors in replies | Run `claude auth status` as the service user; log in again with `claude auth login`. |
| Replies say `[blocked tools: Bash]` | Working as designed. Add a scoped rule to `allowed_tools` if you want it. |
| Linked device stopped receiving | It may have been unlinked on the phone, or signal-cli is outdated. Update the image, then re-run `./setup.sh`. |

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/pytest -q          # uses a fake `claude` binary; no network or Signal needed
shellcheck setup.sh
```

Layout: `signal_bot/config.py` (TOML → dataclasses), `signal_client.py`
(websocket receive, send, typing, reactions), `claude_runner.py`
(`claude -p` subprocess), `bot.py` (whitelist, trigger, commands, queueing,
chunking), `sessions.py` (per-user session ids).
