# llm_credential_pool

A tiny, dependency-free dashboard for [CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI)
that puts all your pooled **ChatGPT / Codex** accounts on one page: how much of each
account's **5-hour** and **weekly** limit is used, when it resets, and which accounts have
headroom right now.

CLIProxyAPI lets local AI tools share a pool of ChatGPT subscription logins behind one
endpoint and rotates between them. This repo is a small self-hosted toolkit on top of it:

- **`quota-dashboard.py`** — the dashboard described below.
- **cross-model fallback** — built into the dashboard (same port): map a virtual model to a list; auto-falls-through on a rate-limit.
- **`broker-keepalive/`** — Windows scripts that keep the broker itself running 24/7.

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
- One file, Python standard library only — no installs. Refreshes at **zero quota cost** —
  Codex via `GET /backend-api/codex/usage`, Claude via `GET /api/oauth/usage` (neither spends
  a message; Claude is polled every ~5 min because Anthropic rate-limits it). Other providers
  show as logged-in.

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

## Cross-model fallback (built into the dashboard)

CLIProxyAPI pools accounts *per model*, but can't fall back across *different* models for
OAuth subscription accounts. The dashboard adds that on **the same port** (no extra
service): a virtual model maps to an ordered list of real models, and a request to it tries
each in turn, moving to the next on a rate-limit / server error.

- Point your client's base URL at **`http://127.0.0.1:8788/v1`** and use a chain model
  (default `auto` = `gpt-5.5`, then `claude-sonnet-4-6`).
- Edit chains live in the dashboard's **Fallback chains** panel (saved to
  `fallback-chains.json`). To make a *real* model fall back too, add e.g.
  `gpt-5.5 → gpt-5.5, claude-sonnet-4-6`.
- Your API key passes straight through (no secrets stored); everything else is forwarded to
  the broker. Override with `FALLBACK_CHAINS` (JSON) / `CLIPROXY_UPSTREAM` if you like.

> The broker only fails over across accounts of the **same** model, by design — so
> cross-model fallback belongs here, in front of it.

## Keep the broker alive (`broker-keepalive/`, Windows)

Scripts that keep CLIProxyAPI itself running hidden 24/7: a self-restarting wrapper
(`run-broker.cmd`), hidden launchers, and a 5-minute watchdog. Install once:

```powershell
powershell -ExecutionPolicy Bypass -File broker-keepalive\setup-cliproxyapi-tasks.ps1
```

Registers `CLIProxyAPI` (start at logon) + `CLIProxyAPI-Watchdog` (restart if it dies).
Drop a `STOP` file in `~/.cli-proxy-api` to stop it. Assumes a standard CLIProxyAPI install
at `%LOCALAPPDATA%\CLIProxyAPI\app`.

## License

[MIT](LICENSE)
