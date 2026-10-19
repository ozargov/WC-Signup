"""Vercel entry point for the Telegram bot (wcbot.py): Telegram POSTs every
message and button tap here.

Open GET /api/telegram?setup=1 once after deploying (and again if the bot
token ever changes) to connect the bot to this address.
"""

import json
import sys
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import wcbot  # noqa: E402


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        # Only Telegram knows the secret it was given at setup.
        if not wcbot.secret_ok(self.headers.get("X-Telegram-Bot-Api-Secret-Token")):
            return self._reply(403, "forbidden")
        try:
            length = int(self.headers.get("Content-Length") or 0)
            update = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return self._reply(400, "bad request")
        wcbot.handle_update(update)
        self._reply(200, "ok")

    def do_GET(self):
        if "setup" in parse_qs(urlsplit(self.path).query, keep_blank_values=True):
            ok, page = wcbot.setup_webhook(f"https://{self.headers.get('Host', '')}/api/telegram")
            return self._reply(200 if ok else 500, page, "text/html; charset=utf-8")
        self._reply(200, "WC Signup bot is running.")

    def _reply(self, status, body, content_type="text/plain; charset=utf-8"):
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):  # Vercel logs requests already
        pass
