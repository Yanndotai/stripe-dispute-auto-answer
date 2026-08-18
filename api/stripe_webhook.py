"""
Vercel serverless entry point. Thin wrapper around webhook_core.process().
"""
import os
import sys
import json
from http.server import BaseHTTPRequestHandler

# Make the project root importable (dispute_brain, webhook_core, assets/).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import webhook_core


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
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
