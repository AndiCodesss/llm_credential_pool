#!/usr/bin/env python3
"""
CLIProxyAPI account quota dashboard.

A tiny, dependency-free (stdlib only) localhost page showing every logged-in
Codex/ChatGPT account with progress bars for the 5-hour and weekly limits,
a usage sparkline, the broker's routing settings, and per-account actions
(add / disable / enable / remove).

Data sources (no management key required):
  * account list + OAuth tokens : the broker's own auth files in AUTH_DIR
  * 5h / weekly usage            : GET https://chatgpt.com/backend-api/codex/usage
                                   (a zero-cost read - it does NOT spend a message)
  * broker settings              : parsed from config.yaml

Account actions edit / move the broker's own auth files; the broker hot-reloads them.

Open: http://127.0.0.1:8788
"""

import glob
import json
import os
import re
import subprocess
import threading
import time
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from fnmatch import fnmatch
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

# ---------------------------------------------------------------- config ----
AUTH_DIR  = os.environ.get("CLIPROXY_AUTH_DIR", os.path.join(os.path.expanduser("~"), ".cli-proxy-api"))
CONFIG_PATH = os.environ.get("CLIPROXY_CONFIG", os.path.join(AUTH_DIR, "config.yaml"))
CODEX_GLOB = "codex-*.json"
AUTH_GLOB = "*.json"
USAGE_URL = "https://chatgpt.com/backend-api/codex/usage"
DASH_HOST = os.environ.get("DASH_HOST", "127.0.0.1")
DASH_PORT = int(os.environ.get("DASH_PORT", "8788"))
REFRESH_SECONDS = int(os.environ.get("DASH_REFRESH", "60"))
HISTORY_LEN = 60
HTTP_TIMEOUT = 12
UA = "codex_cli_rs/0.114.0 (Windows 11; x86_64) cli"
CLAUDE_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
CLAUDE_UA = "claude-code/1.0.0 (external, cli)"
CLAUDE_USAGE_TTL = int(os.environ.get("DASH_CLAUDE_TTL", "300"))  # Anthropic rate-limits this hard

APP_DIR = os.path.dirname(os.path.abspath(__file__))
def _find_broker_exe():
    env = os.environ.get("CLIPROXY_EXE")
    if env:
        return env
    for p in (os.path.join(os.environ.get("LOCALAPPDATA", ""), "CLIProxyAPI", "app", "cli-proxy-api.exe"),
              os.path.join(APP_DIR, "cli-proxy-api.exe")):
        if p and os.path.exists(p):
            return p
    return os.path.join(APP_DIR, "cli-proxy-api.exe")


BROKER_EXE = _find_broker_exe()
LOGIN_LOG = os.path.join(APP_DIR, "add-account.log")
TRASH_DIR = os.path.join(AUTH_DIR, "removed-accounts")
LOGIN_FLAGS = {"codex": "-codex-login", "claude": "-claude-login",
               "gemini": "-login", "qwen": "-qwen-login", "xai": "-xai-login"}

# --- cross-model fallback gateway (served on the same port as the dashboard) ---
BROKER_BASE = os.environ.get("CLIPROXY_UPSTREAM", "http://127.0.0.1:8317").rstrip("/")
CHAINS_FILE = os.path.join(APP_DIR, "fallback-chains.json")
PROXY_TIMEOUT = int(os.environ.get("DASH_PROXY_TIMEOUT", "300"))
DEFAULT_CHAINS = {"auto": ["gpt-5.5", "claude-sonnet-4-6"]}
FALLBACK_STATUSES = set(int(x) for x in
    os.environ.get("FALLBACK_STATUSES", "408,409,429,500,502,503,504,529").split(",") if x.strip())

_snapshot = {"ok": False, "error": "starting up...", "accounts": [], "ts": 0}
_history = {}
_claude_cache = {}                                 # file -> {data, ts} (slow-polled, 429-prone)
_lock = threading.Lock()


# ------------------------------------------------------------- settings -----
def read_settings():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
            txt = fh.read()
    except Exception:                                          # noqa: BLE001
        return None

    def g(pat, default=None):
        m = re.search(pat, txt, re.M)
        return m.group(1) if m else default

    host = g(r'^host:\s*"?([^"\n]*)"?') or "*"
    port = g(r'^port:\s*(\d+)') or "?"
    return [
        {"label": "Bind", "type": "static", "value": f"{host}:{port}",
         "explain": "Localhost only â€” not exposed to the network (read-only; a change needs a restart)."},
        {"label": "Routing", "key": "strategy", "type": "enum",
         "value": g(r'strategy:\s*"?([a-z-]+)"?', "round-robin"),
         "options": ["round-robin", "fill-first"],
         "explain": "round-robin spreads requests evenly across accounts; fill-first drains one before the next."},
        {"label": "Session affinity", "key": "session-affinity", "type": "bool",
         "value": g(r'session-affinity:\s*(true|false)', "false"),
         "explain": "On: a conversation sticks to one account (keeps prompt cache + stable identity). "
                    "Off: any account per request."},
        {"label": "Switch on quota", "key": "switch-project", "type": "bool",
         "value": g(r'switch-project:\s*(true|false)', "true"),
         "explain": "On a limit, automatically fail over to another account."},
        {"label": "Preview fallback", "key": "switch-preview-model", "type": "bool",
         "value": g(r'switch-preview-model:\s*(true|false)', "false"),
         "explain": "On: may downgrade to a cheaper/preview model on quota errors. Off: never."},
        {"label": "Spend credits", "key": "antigravity-credits", "type": "bool",
         "value": g(r'antigravity-credits:\s*(true|false)', "false"),
         "explain": "On: may spend paid credits as a last resort. Off: never."},
        {"label": "Request retries", "key": "request-retry", "type": "int",
         "value": g(r'request-retry:\s*(\d+)', "0"),
         "explain": "Retry a failed request this many times (403/408/5xx)."},
        {"label": "Failover breadth", "key": "max-retry-credentials", "type": "int",
         "value": g(r'max-retry-credentials:\s*(\d+)', "0"),
         "explain": "Accounts to try on a hard failure (0 = all)."},
    ]


# ---------------------------------------------------------------- probing ---
def codex_usage(token, account_id):
    req = urllib.request.Request(USAGE_URL, headers={
        "Authorization": "Bearer " + token,
        "chatgpt-account-id": account_id or "",
        "originator": "codex_cli_rs",
        "OpenAI-Beta": "responses=experimental",
        "User-Agent": UA,
    })
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8"))


def window_label(secs):
    if secs is None:
        return ""
    if secs <= 6 * 3600:
        return "5h"
    if 6 * 86400 <= secs <= 8 * 86400:
        return "weekly"
    d = round(secs / 86400)
    return f"{d}d" if d >= 1 else f"{round(secs/3600)}h"


def make_window(w):
    if not isinstance(w, dict):
        return None
    return {"used": w.get("used_percent"), "reset_at": w.get("reset_at"),
            "reset_after": w.get("reset_after_seconds"),
            "label": window_label(w.get("limit_window_seconds"))}


def claude_usage(token):
    req = urllib.request.Request(CLAUDE_USAGE_URL, headers={
        "Authorization": "Bearer " + token,
        "anthropic-version": "2023-06-01",
        "anthropic-beta": "oauth-2025-04-20,claude-code-20250219",
        "User-Agent": CLAUDE_UA,
    })
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8"))


def make_claude_window(w, label):
    if not isinstance(w, dict):
        return None
    util = w.get("utilization")
    reset_at = reset_after = None
    ra = w.get("resets_at")
    if ra:
        try:
            reset_at = int(datetime.fromisoformat(ra).timestamp())
            reset_after = max(0, reset_at - int(time.time()))
        except Exception:                                      # noqa: BLE001
            pass
    return {"used": round(util) if isinstance(util, (int, float)) else None,
            "reset_at": reset_at, "reset_after": reset_after, "label": label}


def probe_claude(name, token, base):
    """Claude usage via /api/oauth/usage â€” polled at most every CLAUDE_USAGE_TTL
    seconds and cached, because Anthropic rate-limits this endpoint hard."""
    now = time.time()
    cached = _claude_cache.get(name)
    data, stale = None, False
    if cached and now - cached["ts"] < CLAUDE_USAGE_TTL:
        data = cached["data"]
    else:
        try:
            data = claude_usage(token)
            _claude_cache[name] = {"data": data, "ts": now}
        except urllib.error.HTTPError as e:
            if cached:
                data, stale = cached["data"], True            # keep last-known on 429/etc.
            elif e.code == 401:
                return {**base, "state": "autherr", "detail": "token expired / unauthorized"}
            elif e.code == 429:
                return {**base, "state": "ok", "tracked": False,
                        "detail": "Claude usage rate-limited by Anthropic â€” retrying soon"}
            else:
                return {**base, "state": "ok", "tracked": False, "detail": f"usage HTTP {e.code}"}
        except Exception as e:                                 # noqa: BLE001
            if cached:
                data, stale = cached["data"], True
            else:
                return {**base, "state": "ok", "tracked": False, "detail": str(e)[:80]}

    fh = make_claude_window(data.get("five_hour"), "5h")
    sd = make_claude_window(data.get("seven_day"), "weekly")
    reached = ((fh and fh["used"] is not None and fh["used"] >= 100) or
               (sd and sd["used"] is not None and sd["used"] >= 100))
    return {**base, "tracked": True, "stale": stale,
            "state": "limited" if reached else "ok",
            "primary": fh, "secondary": sd}


def probe_file(path):
    name = os.path.basename(path)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            d = json.load(fh)
    except Exception as e:                                     # noqa: BLE001
        return {"email": name, "file": name, "provider": "?", "plan": "?",
                "state": "error", "detail": f"unreadable token file: {e}"}

    provider = (d.get("type") or name.split("-", 1)[0]).lower()
    email = d.get("email") or name
    base = {"email": email, "file": name, "provider": provider, "plan": provider}
    if d.get("disabled"):
        return {**base, "state": "disabled"}
    token = d.get("access_token")
    if not token:
        return {**base, "state": "error", "detail": "no access token in file"}

    if provider == "claude":
        return probe_claude(name, token, base)
    # Other providers don't expose a zero-cost usage endpoint we chart.
    if provider != "codex":
        return {**base, "state": "ok", "tracked": False,
                "detail": f"{provider} account â€” logged in (usage not tracked here)"}

    try:
        u = codex_usage(token, d.get("account_id"))
    except urllib.error.HTTPError as e:
        return {**base, "state": "autherr",
                "detail": "token expired / unauthorized" if e.code == 401 else f"HTTP {e.code}"}
    except Exception as e:                                     # noqa: BLE001
        return {**base, "state": "unreach", "detail": str(e)[:80]}

    rl = u.get("rate_limit") or {}
    reached = bool(rl.get("limit_reached"))
    return {**base, "tracked": True,
            "email": u.get("email") or email,
            "plan": u.get("plan_type") or "codex",
            "state": "limited" if reached else "ok",
            "primary": make_window(rl.get("primary_window")),
            "secondary": make_window(rl.get("secondary_window"))}


def refresh_once():
    files = [p for p in sorted(glob.glob(os.path.join(AUTH_DIR, AUTH_GLOB)))
             if not os.path.basename(p).lower().startswith("config")]
    if not files:
        return {"ok": False, "ts": time.time(), "settings": read_settings(),
                "error": f"no account json files in {AUTH_DIR}", "accounts": []}
    with ThreadPoolExecutor(max_workers=8) as ex:
        accounts = list(ex.map(probe_file, files))

    for a in accounts:
        used = (a.get("primary") or {}).get("used")
        h = _history.setdefault(a["file"], [])      # key by file: emails can collide across providers
        h.append(used)
        del h[:-HISTORY_LEN]
        a["spark"] = list(h)

    rank = {"limited": 0, "autherr": 1, "unreach": 1, "error": 1, "ok": 2, "disabled": 3}

    def worst(a):
        return max((a.get("primary") or {}).get("used") or 0,
                   (a.get("secondary") or {}).get("used") or 0)

    accounts.sort(key=lambda a: (rank.get(a["state"], 5), -worst(a), a["email"]))
    summary = {"ok": sum(a["state"] == "ok" for a in accounts),
               "limited": sum(a["state"] == "limited" for a in accounts),
               "other": sum(a["state"] in ("disabled", "autherr", "unreach", "error")
                            for a in accounts),
               "total": len(accounts)}
    return {"ok": True, "ts": time.time(), "summary": summary,
            "settings": read_settings(), "accounts": accounts}


def refresher():
    global _snapshot
    while True:
        try:
            snap = refresh_once()
        except Exception as e:                                 # noqa: BLE001
            snap = {"ok": False, "ts": time.time(), "error": str(e), "accounts": []}
        with _lock:
            _snapshot = snap
        time.sleep(REFRESH_SECONDS)


def force_refresh():
    global _snapshot
    snap = refresh_once()
    with _lock:
        _snapshot = snap


# --------------------------------------------------------- account actions --
def resolve_file(file):
    """Validate a client-supplied file name and return its absolute path."""
    base = os.path.basename(file or "")
    if (not base or base != file or not fnmatch(base, AUTH_GLOB)
            or base.lower().startswith("config")):
        raise ValueError("invalid account file")
    path = os.path.join(AUTH_DIR, base)
    if not os.path.isfile(path):
        raise FileNotFoundError("account file not found")
    return path


def write_json_atomic(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:               # utf-8, no BOM
        json.dump(obj, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def set_disabled(file, value):
    path = resolve_file(file)
    with open(path, "r", encoding="utf-8") as fh:
        d = json.load(fh)
    d["disabled"] = bool(value)
    write_json_atomic(path, d)


def remove_account(file, confirm):
    path = resolve_file(file)
    with open(path, "r", encoding="utf-8") as fh:
        d = json.load(fh)
    email = d.get("email") or ""
    if (confirm or "").strip() != email:
        raise ValueError("confirmation text does not match the account email")
    os.makedirs(TRASH_DIR, exist_ok=True)
    dest = os.path.join(TRASH_DIR, f"{os.path.basename(path)}.{int(time.time())}.removed")
    os.replace(path, dest)
    return dest


EDITABLE = {
    "strategy": {"type": "enum", "options": ["round-robin", "fill-first"],
                 "re": r'(strategy:\s*)"?[a-z\-]+"?', "quote": True},
    "session-affinity": {"type": "bool", "re": r'(session-affinity:\s*)(?:true|false)'},
    "switch-project": {"type": "bool", "re": r'(switch-project:\s*)(?:true|false)'},
    "switch-preview-model": {"type": "bool", "re": r'(switch-preview-model:\s*)(?:true|false)'},
    "antigravity-credits": {"type": "bool", "re": r'(antigravity-credits:\s*)(?:true|false)'},
    "request-retry": {"type": "int", "re": r'(request-retry:\s*)\d+'},
    "max-retry-credentials": {"type": "int", "re": r'(max-retry-credentials:\s*)\d+'},
}


def set_setting(key, value):
    """Edit one allow-listed value in config.yaml; the broker hot-reloads it."""
    spec = EDITABLE.get(key)
    if not spec:
        raise ValueError("setting is not editable")
    if spec["type"] == "bool":
        if value not in ("true", "false"):
            raise ValueError("value must be true or false")
        newval = value
    elif spec["type"] == "enum":
        if value not in spec["options"]:
            raise ValueError("invalid option")
        newval = f'"{value}"' if spec.get("quote") else value
    else:  # int
        if not re.fullmatch(r"\d+", value or ""):
            raise ValueError("value must be a whole number")
        newval = value
    with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
        txt = fh.read()
    new, n = re.subn(spec["re"], lambda m: m.group(1) + newval, txt, count=1)
    if n == 0:
        raise ValueError(f"'{key}' not found in config.yaml")
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:                # utf-8, no BOM
        fh.write(new)
    os.replace(tmp, CONFIG_PATH)


def broker_forward(path, method, headers, body):
    req = urllib.request.Request(BROKER_BASE + path, data=body, method=method)
    for k, v in headers.items():
        req.add_header(k, v)
    return urllib.request.urlopen(req, timeout=PROXY_TIMEOUT)


def load_chains():
    try:
        with open(CHAINS_FILE, "r", encoding="utf-8") as fh:
            d = json.load(fh)
        if isinstance(d, dict) and d:
            return {str(k): [str(m) for m in v] for k, v in d.items() if isinstance(v, list)}
    except Exception:                                          # noqa: BLE001
        pass
    return dict(DEFAULT_CHAINS)


def save_chains(d):
    clean = {str(k).strip(): [str(m).strip() for m in v if str(m).strip()]
             for k, v in (d or {}).items() if str(k).strip() and isinstance(v, list)}
    tmp = CHAINS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(clean, fh, indent=2)
    os.replace(tmp, CHAINS_FILE)
    return clean


def start_login(provider="codex"):
    flag = LOGIN_FLAGS.get(provider, "-codex-login")
    if not os.path.exists(BROKER_EXE):
        raise FileNotFoundError(f"broker exe not found: {BROKER_EXE}")
    out = open(LOGIN_LOG, "a", encoding="utf-8")
    flags = 0x00000008 | 0x00000200   # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    subprocess.Popen([BROKER_EXE, flag, "-config", CONFIG_PATH],
                     cwd=APP_DIR, stdout=out, stderr=out, stdin=subprocess.DEVNULL,
                     creationflags=flags, close_fds=True)


# ---------------------------------------------------------------- page ------
PAGE = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Codex accounts</title>
<style>
  :root{--bg:#0d1117;--card:#161b22;--line:#21262d;--txt:#e6edf3;--dim:#8b949e;
        --green:#3fb950;--amber:#d29922;--red:#f85149;--grey:#6e7681;--track:#21262d}
  *{box-sizing:border-box}
  html,body{height:100%}
  body{margin:0;background:var(--bg);color:var(--txt);overflow:hidden;
       font:13px/1.4 ui-sans-serif,-apple-system,Segoe UI,Roboto,Helvetica,Arial}
  .wrap{height:100%;display:flex;flex-direction:column;padding:13px 15px;gap:9px}
  header{display:flex;align-items:center;gap:8px;flex-wrap:wrap;flex:0 0 auto}
  h1{font-size:15px;margin:0;font-weight:600}
  .prov{font-size:12px;background:#0d1117;color:var(--txt);border:1px solid var(--line);
        border-radius:7px;padding:4px 6px}
  .addbtn{font-size:12px;font-weight:600;color:#fff;background:#1f6feb;border:none;
          padding:5px 11px;border-radius:7px;cursor:pointer}
  .addbtn:hover{background:#388bfd}.addbtn:disabled{opacity:.55;cursor:default}
  .pills{display:flex;gap:6px;margin-left:auto;flex-wrap:wrap}
  .pill{font-size:11px;padding:2px 8px;border-radius:999px;border:1px solid var(--line);color:var(--dim)}
  .pill b{color:var(--txt)} .pill.green b{color:var(--green)} .pill.red b{color:var(--red)}
  .meta{color:var(--grey);font-size:11px}
  .toast{position:fixed;left:50%;top:14px;transform:translateX(-50%);max-width:540px;
         background:#161b22;border:1px solid var(--line);color:var(--txt);padding:9px 14px;
         border-radius:8px;font-size:12px;box-shadow:0 6px 24px rgba(0,0,0,.5);
         opacity:0;pointer-events:none;transition:opacity .25s;z-index:9}
  .toast.show{opacity:1}.toast.err{border-color:#3d1d22;color:#ffb4ab}
  .main{flex:1 1 auto;display:flex;gap:11px;min-height:0}
  .grid{flex:1 1 auto;display:grid;gap:9px;align-content:start;overflow-y:auto;min-height:0;
        grid-template-columns:repeat(auto-fit,minmax(330px,1fr))}
  .card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:9px 11px}
  .top{display:flex;align-items:center;gap:7px}
  .dot{width:8px;height:8px;border-radius:50%;flex:0 0 auto}
  .dot.ok{background:var(--green)}.dot.limited{background:var(--red)}
  .dot.disabled{background:var(--grey)}.dot.autherr,.dot.unreach,.dot.error{background:var(--amber)}
  .email{font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .badge{font-size:9.5px;text-transform:uppercase;letter-spacing:.4px;padding:1px 6px;
         border-radius:5px;border:1px solid var(--line);color:var(--dim);flex:0 0 auto}
  .badge.team{color:#a371f7}.badge.plus{color:#58a6ff}
  .st{margin-left:auto;font-size:11px;font-weight:600;flex:0 0 auto}
  .st.ok{color:var(--green)}.st.limited{color:var(--red)}.st.disabled{color:var(--grey)}
  .st.autherr,.st.unreach,.st.error{color:var(--amber)}
  .kebab{flex:0 0 auto;background:none;border:none;color:var(--dim);font-size:16px;line-height:1;
         cursor:pointer;padding:0 3px;border-radius:5px}
  .kebab:hover{color:var(--txt);background:var(--track)}
  .bars{margin-top:7px;display:flex;flex-direction:column;gap:6px}
  .bar{display:grid;grid-template-columns:34px 1fr auto;align-items:center;gap:8px}
  .bar .lbl{font-size:10.5px;color:var(--dim)}
  .track{height:9px;background:var(--track);border-radius:5px;overflow:hidden}
  .fill{height:100%;border-radius:5px;transition:width .4s}
  .fill.green{background:var(--green)}.fill.amber{background:var(--amber)}.fill.red{background:var(--red)}
  .val{font-size:11px;font-variant-numeric:tabular-nums;white-space:nowrap;color:var(--dim)}
  .val b{color:var(--txt)}
  .trend{margin-top:7px;display:grid;grid-template-columns:34px 1fr;align-items:center;gap:8px}
  .trend .lbl{font-size:10px;color:var(--grey)}
  .spark{width:100%;height:20px;display:block}.spark-na{font-size:10px;color:var(--grey)}
  .note{margin-top:6px;font-size:11px;color:var(--dim)}
  .actions,.confirm{margin-top:8px;border-top:1px solid var(--line);padding-top:8px;
                    display:flex;gap:7px;align-items:center;flex-wrap:wrap}
  .actions[hidden],.confirm[hidden]{display:none}
  .actions button,.confirm button{font-size:11px;font-weight:600;border:1px solid var(--line);
     background:#0d1117;color:var(--txt);border-radius:6px;padding:4px 9px;cursor:pointer}
  .act-remove,.act-remove2{color:#ffb4ab;border-color:#3d1d22}
  .act-remove2:disabled{opacity:.45;cursor:default}
  .cmsg{font-size:11px;color:var(--dim);width:100%}
  .crow{display:flex;gap:7px;width:100%;align-items:center}
  .cinput{flex:1;min-width:120px;font-size:12px;background:#0d1117;color:var(--txt);
          border:1px solid var(--line);border-radius:6px;padding:4px 8px}
  .side{flex:0 0 250px;overflow-y:auto;min-height:0;background:var(--card);
        border:1px solid var(--line);border-radius:10px;padding:10px 12px}
  .side h2{font-size:11px;text-transform:uppercase;letter-spacing:.5px;color:var(--dim);
           margin:0 0 8px;font-weight:600}
  .set{padding:6px 0;border-top:1px solid var(--line)}.set:first-of-type{border-top:none}
  .srow{display:flex;justify-content:space-between;gap:8px;align-items:baseline}
  .slbl{font-size:12px;font-weight:600}
  .sval{font-size:11.5px;font-variant-numeric:tabular-nums;color:#58a6ff;white-space:nowrap}
  .sexp{font-size:10.5px;color:var(--dim);margin-top:2px;line-height:1.35}
  .sw{position:relative;display:inline-block;width:30px;height:17px;flex:0 0 auto}
  .sw input{opacity:0;width:0;height:0}
  .slider{position:absolute;inset:0;background:#30363d;border-radius:999px;transition:.2s;cursor:pointer}
  .slider:before{content:"";position:absolute;height:13px;width:13px;left:2px;top:2px;background:#8b949e;border-radius:50%;transition:.2s}
  .sw input:checked + .slider{background:#1f6feb}
  .sw input:checked + .slider:before{transform:translateX(13px);background:#fff}
  .sselect,.snum{background:#0d1117;color:var(--txt);border:1px solid var(--line);border-radius:6px;font-size:11px;padding:2px 5px}
  .snum{width:52px}
  .side-h2b{font-size:11px;text-transform:uppercase;letter-spacing:.5px;color:var(--dim);
            margin:14px 0 8px;font-weight:600;border-top:1px solid var(--line);padding-top:12px}
  .chrow{display:flex;gap:5px;margin-bottom:5px;align-items:center}
  .chrow input{background:#0d1117;color:var(--txt);border:1px solid var(--line);border-radius:6px;font-size:11px;padding:3px 6px;min-width:0}
  .chrow .ca{width:36%}.chrow .cm{flex:1}
  .chrow .cx{background:none;border:none;color:var(--dim);cursor:pointer;font-size:14px;padding:0 3px;flex:0 0 auto}
  .chbtns{display:flex;gap:6px;margin-top:6px}
  .chbtns button{font-size:11px;font-weight:600;border:1px solid var(--line);background:#0d1117;color:var(--txt);border-radius:6px;padding:4px 9px;cursor:pointer}
  #chSave{background:#1f6feb;border:none;color:#fff}
  .err{background:#1b1216;border:1px solid #3d1d22;color:#ffb4ab;padding:14px;border-radius:10px}
  footer{flex:0 0 auto;color:var(--grey);font-size:10.5px;text-align:center}
  @media(max-width:760px){.main{flex-direction:column}.side{flex:0 0 auto}}
</style></head>
<body>
<div class="wrap">
  <header>
    <h1>Accounts</h1>
    <select id="provider" class="prov" title="provider to log in">
      <option value="codex">Codex / ChatGPT</option>
      <option value="claude">Claude</option>
      <option value="gemini">Gemini</option>
      <option value="xai">xAI / Grok</option>
      <option value="qwen">Qwen</option>
    </select>
    <button id="addbtn" class="addbtn">ï¼‹ Add account</button>
    <div class="pills" id="pills"></div>
    <div class="meta" id="meta"></div>
  </header>
  <div id="toast" class="toast"></div>
  <div class="main">
    <div class="grid" id="grid"></div>
    <aside class="side">
      <h2>Broker settings</h2><div id="settings"></div>
      <h2 class="side-h2b">Fallback chains</h2><div id="chains"></div>
    </aside>
  </div>
  <footer>bars = current 5h &amp; weekly usage Â· sparkline = 5h-limit usage over the last hour Â· refreshes every __REFRESH__s</footer>
</div>
<script>
const REFRESH = __REFRESH__ * 1000;
let last = null;
window.panelOpen = false;

function esc(s){ return (s==null?"":String(s)).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c])); }
function cd(sec){
  if(sec==null) return "";
  if(sec<0) sec=0;
  const d=Math.floor(sec/86400); sec-=d*86400;
  const h=Math.floor(sec/3600);  sec-=h*3600;
  const m=Math.floor(sec/60);
  if(d>0) return `${d}d ${h}h`;
  if(h>0) return `${h}h ${m}m`;
  return `${m}m`;
}
function color(p){ return p>=90?"red":p>=70?"amber":"green"; }
function stroke(p){ return p>=90?"#f85149":p>=70?"#d29922":"#3fb950"; }
function bar(label,w){
  if(!w||w.used==null)
    return `<div class="bar"><span class="lbl">${label}</span><div class="track"></div><span class="val">n/a</span></div>`;
  const reset = w.reset_at ? `<span data-at="${w.reset_at}">${cd(w.reset_after)}</span>` : "";
  return `<div class="bar"><span class="lbl">${w.label||label}</span>
    <div class="track"><div class="fill ${color(w.used)}" style="width:${w.used}%"></div></div>
    <span class="val"><b>${w.used}%</b>${reset?` Â· â†» ${reset}`:""}</span></div>`;
}
function sparkline(arr){
  const pts=(arr||[]).filter(v=>v!=null);
  if(pts.length<2) return `<span class="spark-na">collecting trendâ€¦</span>`;
  const W=100,H=20, step=W/(pts.length-1);
  const line=pts.map((v,i)=>`${(i*step).toFixed(1)},${(H-(v/100)*H).toFixed(1)}`).join(" ");
  const c=stroke(pts[pts.length-1]);
  return `<svg class="spark" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">
    <polygon points="0,${H} ${line} ${W},${H}" fill="${c}" opacity="0.12"/>
    <polyline points="${line}" fill="none" stroke="${c}" stroke-width="1.5" vector-effect="non-scaling-stroke"/></svg>`;
}
function stateLabel(s){
  return {ok:"OK",limited:"Limited",disabled:"Disabled",autherr:"Auth err",unreach:"Offline",error:"Error"}[s]||s;
}
function settingControl(s){
  if(s.type==="bool")
    return `<label class="sw"><input type="checkbox" data-key="${esc(s.key)}" ${s.value==="true"?"checked":""}><span class="slider"></span></label>`;
  if(s.type==="enum")
    return `<select class="sselect" data-key="${esc(s.key)}">`+
      s.options.map(o=>`<option ${o===s.value?"selected":""}>${esc(o)}</option>`).join("")+`</select>`;
  if(s.type==="int")
    return `<input class="snum" type="number" min="0" step="1" data-key="${esc(s.key)}" value="${esc(s.value)}">`;
  return `<span class="sval">${esc(s.value)}</span>`;
}
function renderSettings(list){
  const el=document.getElementById("settings");
  if(!list){ el.innerHTML=`<div class="sexp">config.yaml not found.</div>`; return; }
  el.innerHTML=list.map(s=>`<div class="set">
    <div class="srow"><span class="slbl">${esc(s.label)}</span>${settingControl(s)}</div>
    <div class="sexp">${esc(s.explain)}</div></div>`).join("");
}
function render(d){
  const grid=document.getElementById("grid"), pills=document.getElementById("pills"), meta=document.getElementById("meta");
  meta.textContent="updated "+new Date().toLocaleTimeString();
  renderSettings(d.settings);
  if(!d.ok){ pills.innerHTML=""; grid.innerHTML=`<div class="err">âš  ${esc(d.error||"no data")}</div>`; return; }
  const s=d.summary;
  pills.innerHTML=`<span class="pill green"><b>${s.ok}</b> ok</span>`+
    `<span class="pill red"><b>${s.limited}</b> limited</span>`+
    (s.other?`<span class="pill"><b>${s.other}</b> other</span>`:"")+
    `<span class="pill"><b>${s.total}</b> total</span>`;
  grid.innerHTML=d.accounts.map(a=>{
    const plan=(a.plan||"").toLowerCase();
    const enable = a.state==="disabled";
    let inner;
    if((a.state==="ok"||a.state==="limited") && a.tracked){
      inner=`<div class="bars">${bar("5h",a.primary)}${bar("weekly",a.secondary)}</div>
        <div class="trend"><span class="lbl" title="5-hour usage over the last hour">5h</span>${sparkline(a.spark)}</div>`;
    }else{ inner=`<div class="note">${esc(a.detail||stateLabel(a.state))}</div>`; }
    return `<div class="card" data-file="${esc(a.file)}" data-email="${esc(a.email)}">
      <div class="top">
        <span class="dot ${a.state}"></span>
        <span class="email">${esc(a.email)}</span>
        <span class="badge ${plan}">${esc(a.plan)}</span>
        <span class="st ${a.state}">${stateLabel(a.state)}</span>
        <button class="kebab" title="account actions">â‹¯</button>
      </div>${inner}
      <div class="actions" hidden>
        <button class="act-toggle" data-enable="${enable?1:0}">${enable?"Enable":"Disable"}</button>
        <button class="act-remove">Removeâ€¦</button>
      </div>
      <div class="confirm" hidden>
        <div class="cmsg">To remove, type the account email <b>${esc(a.email)}</b> exactly:</div>
        <div class="crow"><input class="cinput" autocomplete="off" spellcheck="false" placeholder="${esc(a.email)}">
          <button class="act-remove2" disabled>Remove</button>
          <button class="act-cancel">Cancel</button></div>
      </div>
    </div>`;
  }).join("");
  last=d;
}
function tick(){
  if(!last||!last.ok) return;
  const now=Date.now()/1000;
  document.querySelectorAll("[data-at]").forEach(el=>{
    const at=parseFloat(el.dataset.at); if(at) el.textContent=cd(at-now);
  });
}
async function load(){
  if(window.panelOpen) return;                  // don't wipe an open action menu
  try{ const r=await fetch("/api/status",{cache:"no-store"}); render(await r.json()); }
  catch(e){ render({ok:false,error:"cannot reach dashboard server: "+e}); }
}
function toast(msg,err){
  const t=document.getElementById("toast");
  t.textContent=msg; t.className="toast show"+(err?" err":"");
  clearTimeout(window._tt); window._tt=setTimeout(()=>{t.className="toast";},7000);
}
function closePanels(){
  document.querySelectorAll(".actions,.confirm").forEach(el=>el.setAttribute("hidden",""));
  window.panelOpen=false;
}
async function mutate(action, params, btn){
  if(btn) btn.disabled=true;
  const qs=new URLSearchParams(params).toString();
  try{
    const r=await fetch(`/api/account/${action}?${qs}`,{method:"POST"});
    const j=await r.json();
    if(j.ok){ toast(j.msg||"done"); window.panelOpen=false; await load(); }
    else{ toast(j.error||(action+" failed"), true); if(btn) btn.disabled=false; }
  }catch(err){ toast("Request failed: "+err, true); if(btn) btn.disabled=false; }
}

document.getElementById("addbtn").addEventListener("click", async (e)=>{
  const b=e.target, provider=document.getElementById("provider").value;
  b.disabled=true; toast("Starting "+provider+" loginâ€¦");
  try{
    const r=await fetch("/api/add-account?provider="+encodeURIComponent(provider),{method:"POST"});
    const j=await r.json();
    toast(j.ok? j.msg : ("Could not start login: "+(j.error||"unknown")), !j.ok);
  }catch(err){ toast("Request failed: "+err,true); }
  setTimeout(()=>{ b.disabled=false; }, 8000);
});

const grid=document.getElementById("grid");
grid.addEventListener("click", async (e)=>{
  const card=e.target.closest(".card"); if(!card) return;
  const file=card.dataset.file, email=card.dataset.email;
  if(e.target.classList.contains("kebab")){
    const a=card.querySelector(".actions"); const wasHidden=a.hasAttribute("hidden");
    closePanels(); if(wasHidden){ a.removeAttribute("hidden"); window.panelOpen=true; }
  }else if(e.target.classList.contains("act-toggle")){
    await mutate(e.target.dataset.enable==="1"?"enable":"disable", {file}, e.target);
  }else if(e.target.classList.contains("act-remove")){
    card.querySelector(".actions").setAttribute("hidden","");
    const c=card.querySelector(".confirm"); c.removeAttribute("hidden");
    window.panelOpen=true; c.querySelector(".cinput").focus();
  }else if(e.target.classList.contains("act-cancel")){
    closePanels();
  }else if(e.target.classList.contains("act-remove2")){
    if(!e.target.disabled) await mutate("remove", {file, confirm: email}, e.target);
  }
});
grid.addEventListener("input",(e)=>{
  if(e.target.classList.contains("cinput")){
    const card=e.target.closest(".card");
    card.querySelector(".act-remove2").disabled = e.target.value.trim() !== card.dataset.email;
  }
});

document.getElementById("settings").addEventListener("change", async (e)=>{
  const key=e.target.dataset.key; if(!key) return;
  const value = e.target.type==="checkbox" ? (e.target.checked?"true":"false") : String(e.target.value);
  try{
    const r=await fetch(`/api/setting?key=${encodeURIComponent(key)}&value=${encodeURIComponent(value)}`,{method:"POST"});
    const j=await r.json();
    toast(j.ok? `${key} â†’ ${value}` : (j.error||"save failed"), !j.ok);
    await load();
  }catch(err){ toast("Request failed: "+err, true); }
});

function chainRow(alias,models){
  return `<div class="chrow"><input class="ca" value="${esc(alias)}" placeholder="alias">`+
    `<input class="cm" value="${esc((models||[]).join(', '))}" placeholder="model-a, model-b">`+
    `<button class="cx" title="remove">Ã—</button></div>`;
}
function renderChains(chains){
  const el=document.getElementById("chains");
  el.innerHTML=Object.entries(chains||{}).map(([a,m])=>chainRow(a,m)).join("")
    +`<div class="chbtns"><button id="chAdd">+ chain</button><button id="chSave">Save</button></div>`
    +`<div class="sexp">A request to an alias tries each model leftâ†’right, falling to the next on a rate-limit. Point your client's base URL at this server's <b>/v1</b> and use an alias (or any model) as the model name.</div>`;
  el.onclick=(e)=>{ if(e.target.classList.contains("cx")) e.target.closest(".chrow").remove(); };
  document.getElementById("chAdd").onclick=()=>el.querySelector(".chbtns").insertAdjacentHTML("beforebegin",chainRow("",[]));
  document.getElementById("chSave").onclick=saveChains;
}
async function loadChains(){
  try{ const r=await fetch("/api/chains",{cache:"no-store"}); renderChains((await r.json()).chains); }
  catch(e){ document.getElementById("chains").innerHTML=`<div class="sexp">chains unavailable</div>`; }
}
async function saveChains(){
  const obj={};
  document.querySelectorAll("#chains .chrow").forEach(r=>{
    const a=r.querySelector(".ca").value.trim();
    const m=r.querySelector(".cm").value.split(",").map(s=>s.trim()).filter(Boolean);
    if(a&&m.length) obj[a]=m;
  });
  try{
    const r=await fetch("/api/chains",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({chains:obj})});
    const j=await r.json();
    if(j.ok){ toast("Fallback chains saved"); renderChains(j.chains); } else toast(j.error||"save failed",true);
  }catch(e){ toast("save failed: "+e,true); }
}
load(); loadChains(); setInterval(load,REFRESH); setInterval(tick,1000);
</script>
</body></html>
"""


# ---------------------------------------------------------------- server ----
class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj):
        self._send(200, json.dumps(obj), "application/json")

    # ---- inference proxy (cross-model fallback) ----
    def _proxy_headers(self):
        h = {"Content-Type": "application/json"}
        if self.headers.get("Authorization"):
            h["Authorization"] = self.headers["Authorization"]
        return h

    def _relay(self, resp, status=None):
        body = resp.read()
        self.send_response(status or getattr(resp, "status", 200))
        self.send_header("Content-Type", resp.headers.get("Content-Type", "application/json"))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _stream(self, resp):
        self.send_response(200)
        self.send_header("Content-Type", resp.headers.get("Content-Type", "text/event-stream"))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        while True:
            chunk = resp.read(2048)
            if not chunk:
                break
            self.wfile.write(chunk)
            self.wfile.flush()

    def _proxy_error(self, code, msg):
        self._send(code, json.dumps({"error": {"message": msg, "type": "proxy_error"}}),
                   "application/json")

    def _passthrough_v1(self, method, body):
        try:
            self._relay(broker_forward(self.path, method, self._proxy_headers(), body or None))
        except urllib.error.HTTPError as e:
            self._relay(e, e.code)
        except Exception as e:                                 # noqa: BLE001
            self._proxy_error(502, f"upstream error: {e}")

    def _v1_models(self):
        try:
            data = json.loads(broker_forward("/v1/models", "GET", self._proxy_headers(), None).read())
        except urllib.error.HTTPError as e:
            return self._relay(e, e.code)
        except Exception as e:                                 # noqa: BLE001
            return self._proxy_error(502, f"upstream error: {e}")
        for name in load_chains():
            data.setdefault("data", []).append({"id": name, "object": "model", "owned_by": "fallback"})
        self._json(data)

    def _chat_fallback(self, raw):
        try:
            payload = json.loads(raw or b"{}")
        except ValueError:
            return self._passthrough_v1("POST", raw)
        chain = load_chains().get(payload.get("model"), [payload.get("model")])
        stream = bool(payload.get("stream"))
        last = None
        for i, cand in enumerate(chain):
            payload["model"] = cand
            data = json.dumps(payload).encode("utf-8")
            try:
                resp = broker_forward("/v1/chat/completions", "POST", self._proxy_headers(), data)
            except urllib.error.HTTPError as e:
                last = e
                if e.code in FALLBACK_STATUSES and i < len(chain) - 1:
                    continue                                   # this model is down â€” try the next
                return self._relay(e, e.code)
            except Exception as e:                             # noqa: BLE001
                last = e
                if i < len(chain) - 1:
                    continue
                return self._proxy_error(502, f"upstream error: {e}")
            return self._stream(resp) if stream else self._relay(resp)
        if isinstance(last, urllib.error.HTTPError):
            self._relay(last, last.code)
        else:
            self._proxy_error(502, "all models in the chain failed")

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            self._send(200, PAGE.replace("__REFRESH__", str(REFRESH_SECONDS)),
                       "text/html; charset=utf-8")
        elif path == "/api/status":
            with _lock:
                snap = dict(_snapshot)
            self._json(snap)
        elif path == "/api/chains":
            self._json({"chains": load_chains()})
        elif path == "/v1/models":
            self._v1_models()
        elif path.startswith("/v1/"):
            self._passthrough_v1("GET", b"")
        else:
            self._send(404, "not found", "text/plain")

    def do_POST(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b""

        def one(k, d=None):
            v = q.get(k)
            return v[0] if v else d

        # inference proxy surface (cross-model fallback) â€” same port as the dashboard
        if u.path == "/v1/chat/completions":
            return self._chat_fallback(raw)
        if u.path.startswith("/v1/"):
            return self._passthrough_v1("POST", raw)

        try:
            if u.path == "/api/chains":
                body = json.loads(raw or b"{}")
                self._json({"ok": True, "chains": save_chains(body.get("chains", body))})
            elif u.path == "/api/add-account":
                provider = (one("provider", "codex")).lower()
                if one("dry"):
                    self._json({"ok": os.path.exists(BROKER_EXE), "dry": True, "exe": BROKER_EXE})
                    return
                start_login(provider)
                self._json({"ok": True, "msg": f"Opening the {provider} login in your browser. "
                                                "Finish there; the new account shows up within a minute."})
            elif u.path in ("/api/account/disable", "/api/account/enable"):
                disable = u.path.endswith("disable")
                set_disabled(one("file"), disable)
                force_refresh()
                self._json({"ok": True, "msg": ("Disabled" if disable else "Enabled") + " account."})
            elif u.path == "/api/account/remove":
                remove_account(one("file"), one("confirm", ""))
                force_refresh()
                self._json({"ok": True, "msg": "Account removed (moved to removed-accounts\\)."})
            elif u.path == "/api/setting":
                set_setting(one("key"), one("value", ""))
                force_refresh()
                self._json({"ok": True, "msg": "Setting saved â€” broker reloads it live."})
            else:
                self._send(404, "not found", "text/plain")
        except Exception as e:                                 # noqa: BLE001
            self._json({"ok": False, "error": str(e)})

    def log_message(self, *a):
        pass


def main():
    global _snapshot
    print(f"CLIProxy quota dashboard  ->  http://{DASH_HOST}:{DASH_PORT}")
    print(f"  auth dir : {AUTH_DIR}")
    print(f"  refresh  : every {REFRESH_SECONDS}s   (Ctrl+C to stop)")
    _snapshot = refresh_once()
    threading.Thread(target=refresher, daemon=True).start()
    ThreadingHTTPServer((DASH_HOST, DASH_PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
