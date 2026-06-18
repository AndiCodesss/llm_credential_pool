#!/usr/bin/env python3
"""
fallback-proxy.py — a tiny cross-model fallback gateway in front of CLIProxyAPI.

CLIProxyAPI pools multiple accounts PER MODEL, but for OAuth subscription
accounts it cannot fall back across DIFFERENT models. This shim adds that:
define a virtual model whose value is an ordered list of real models; a request
to it tries each in turn, moving to the next on a rate-limit / server error.

Point your client at this proxy instead of the broker, and use a chain model
(e.g. "auto"). Everything else is transparently forwarded to CLIProxyAPI.

No secrets are stored — the client's Authorization header is passed straight
through to CLIProxyAPI, so you still send your normal broker API key.

Config (all optional, via environment variables):
  CLIPROXY_UPSTREAM   default http://127.0.0.1:8317   (the broker)
  FALLBACK_HOST       default 127.0.0.1
  FALLBACK_PORT       default 8789
  FALLBACK_CHAINS     JSON, e.g. {"auto":["gpt-5.5","claude-sonnet-4-6"]}
  FALLBACK_STATUSES   CSV of upstream status codes that trigger fallback
"""

import json
import os
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UPSTREAM = os.environ.get("CLIPROXY_UPSTREAM", "http://127.0.0.1:8317").rstrip("/")
LISTEN_HOST = os.environ.get("FALLBACK_HOST", "127.0.0.1")
LISTEN_PORT = int(os.environ.get("FALLBACK_PORT", "8789"))
TIMEOUT = int(os.environ.get("FALLBACK_TIMEOUT", "300"))

# Virtual model -> ordered list of real models to try (first available wins).
# Edit to taste, or override with the FALLBACK_CHAINS env var (JSON).
DEFAULT_CHAINS = {
    "auto":      ["gpt-5.5", "claude-sonnet-4-6"],
    "auto-opus": ["gpt-5.5", "claude-opus-4-8"],
}
CHAINS = json.loads(os.environ["FALLBACK_CHAINS"]) if os.environ.get("FALLBACK_CHAINS") else DEFAULT_CHAINS

# Upstream statuses meaning "this model is unavailable — try the next one".
# A plain 4xx like 400/404 is NOT retried (that's a bad request, not a limit).
FALLBACK_STATUSES = set(
    int(x) for x in os.environ.get("FALLBACK_STATUSES", "408,409,429,500,502,503,504,529").split(",") if x.strip()
)


def _forward(path, method, headers, body):
    req = urllib.request.Request(UPSTREAM + path, data=body, method=method)
    for k, v in headers.items():
        req.add_header(k, v)
    return urllib.request.urlopen(req, timeout=TIMEOUT)


class Handler(BaseHTTPRequestHandler):
    # ---- helpers -------------------------------------------------------
    def _client_headers(self):
        h = {"Content-Type": "application/json"}
        if self.headers.get("Authorization"):
            h["Authorization"] = self.headers["Authorization"]
        return h

    def _relay(self, resp, status=None):
        body = resp.read()
        self.send_response(status or getattr(resp, "status", 200))
        self.send_header("Content-Type", resp.headers.get("Content-Type", "application/json"))
        self.send_header("Content-Length", str(len(body)))
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

    def _error(self, code, msg):
        out = json.dumps({"error": {"message": msg, "type": "fallback_proxy_error"}}).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def _passthrough(self, method, body):
        try:
            self._relay(_forward(self.path, method, self._client_headers(), body or None))
        except urllib.error.HTTPError as e:
            self._relay(e, e.code)
        except Exception as e:                                 # noqa: BLE001
            self._error(502, f"upstream error: {e}")

    # ---- routes --------------------------------------------------------
    def do_GET(self):
        if self.path.split("?", 1)[0] == "/v1/models":
            return self._models()
        self._passthrough("GET", b"")

    def _models(self):
        try:
            data = json.loads(_forward("/v1/models", "GET", self._client_headers(), None).read())
        except Exception as e:                                 # noqa: BLE001
            return self._error(502, f"upstream error: {e}")
        for name in CHAINS:
            data.setdefault("data", []).append(
                {"id": name, "object": "model", "owned_by": "fallback-proxy"})
        out = json.dumps(data).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b""
        if self.path.split("?", 1)[0] != "/v1/chat/completions":
            return self._passthrough("POST", raw)
        try:
            payload = json.loads(raw or b"{}")
        except ValueError:
            return self._passthrough("POST", raw)

        chain = CHAINS.get(payload.get("model"), [payload.get("model")])
        stream = bool(payload.get("stream"))
        last = None
        for i, candidate in enumerate(chain):
            payload["model"] = candidate
            data = json.dumps(payload).encode("utf-8")
            try:
                resp = _forward("/v1/chat/completions", "POST", self._client_headers(), data)
            except urllib.error.HTTPError as e:
                last = e
                if e.code in FALLBACK_STATUSES and i < len(chain) - 1:
                    continue                                   # this model is down — try the next
                return self._relay(e, e.code)
            except Exception as e:                             # noqa: BLE001
                last = e
                if i < len(chain) - 1:
                    continue
                return self._error(502, f"upstream error: {e}")
            return self._stream(resp) if stream else self._relay(resp)

        if isinstance(last, urllib.error.HTTPError):
            self._relay(last, last.code)
        else:
            self._error(502, "all models in the chain failed")

    def log_message(self, *a):
        pass


def main():
    print(f"fallback-proxy  ->  {UPSTREAM}   listening on http://{LISTEN_HOST}:{LISTEN_PORT}")
    print(f"  chains   : {CHAINS}")
    print(f"  fallback : on upstream status {sorted(FALLBACK_STATUSES)}")
    ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
