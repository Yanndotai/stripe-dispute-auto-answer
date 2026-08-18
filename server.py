"""
Standalone webhook server — no web framework, stdlib only.

For any host that isn't Vercel: Docker, Fly.io, Railway, Render, Google Cloud Run,
a plain VPS, etc. Listens on $PORT (default 8080) and accepts POST /webhook.

Run:  python server.py
"""
import os
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import webhook_core


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path.rstrip("/") not in ("", "/webhook"):
            return self._send(404, {"error": "not found"})
        length = int(self.headers.get("content-length", 0))
        payload = self.rfile.read(length)
        sig = self.headers.get("stripe-signature")
        code, body = webhook_core.process(payload, sig)
        self._send(code, body)

    def do_GET(self):
        self._send(200, {"ok": True, "service": "stripe-dispute-answer-automation"})

    def _send(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):  # quiet access logs
        pass


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    print(f"stripe-dispute-answer-automation listening on :{port} (POST /webhook)")
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
