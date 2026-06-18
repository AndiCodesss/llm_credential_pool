# llm_credential_pool

A tiny, dependency-free dashboard for [CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI)
that puts all your pooled **ChatGPT / Codex** accounts on one page: how much of each
account's **5-hour** and **weekly** limit is used, when it resets, and which accounts have
headroom right now.

CLIProxyAPI lets local AI tools share a pool of ChatGPT subscription logins behind one
endpoint and rotates between them. This is the missing dashboard on top of it.

## What it's good for

- **Never stall mid-task** — see at a glance which accounts still have capacity.
- **Keep a pool warm** — leave it running on an always-on box or VPS to monitor your
  accounts and keep their tokens refreshed even when you're not using them.
- **Manage accounts from the browser** — add a login, or disable / remove one, without the terminal.
- **Tune the broker live** — flip routing (round-robin / fill-first), session affinity and
  failover from the page; it writes `config.yaml` and the broker reloads instantly.

## Features

- 5h + weekly usage bars, live reset countdowns, and a usage sparkline
- Status per account: ok / limited / disabled / auth-error
- ＋ Add account (Codex / Claude / Gemini / xAI / Qwen) via browser OAuth
- Disable / Enable, and Remove with a type-the-email confirmation (the token file is moved
  to `removed-accounts/`, never hard-deleted)
- Editable broker-settings panel
- One file, Python standard library only — no installs. Refreshes every minute at **zero
  quota cost** (it reads `GET /backend-api/codex/usage`, which doesn't spend a message).

## Quick start

```sh
python quota-dashboard.py      # then open http://127.0.0.1:8788
```

## Configuration (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `CLIPROXY_AUTH_DIR` | `~/.cli-proxy-api` | where the broker stores `codex-*.json` |
| `CLIPROXY_CONFIG` | `<auth dir>/config.yaml` | broker config (for the settings panel) |
| `CLIPROXY_EXE` | auto-detected | path to `cli-proxy-api` (for **Add account**) |
| `DASH_PORT` | `8788` | dashboard port |
| `DASH_REFRESH` | `60` | seconds between refreshes |

## Run hidden at startup (Windows)

```powershell
powershell -ExecutionPolicy Bypass -File setup-autostart.ps1
```

Registers a hidden scheduled task that keeps the dashboard running at logon. Drop a
`DASH_STOP` file next to the script to stop the self-restarting wrapper.

## License

[MIT](LICENSE)
