#!/usr/bin/env python3
"""Telegram Webhook Relay — receives POST from NewAPI Monitor, forwards text to Telegram."""
import os, json, urllib.request, urllib.error
from http.server import HTTPServer, BaseHTTPRequestHandler

BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TG_CHAT_ID", "")
LISTEN_PORT = int(os.environ.get("RELAY_PORT", "9218"))

if not BOT_TOKEN or not CHAT_ID:
    raise SystemExit("TG_BOT_TOKEN and TG_CHAT_ID must be set")

def send_telegram(text):
    """Send a message to Telegram via Bot API."""
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = json.dumps({
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }).encode("utf-8")
    req = urllib.request.Request(
        url, data=payload, method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read().decode()
            if resp.status >= 400:
                print(f"[relay] Telegram API error {resp.status}: {body}", flush=True)
                return False
            return True
    except Exception as e:
        print(f"[relay] Telegram send failed: {e}", flush=True)
        return False

class RelayHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error":"invalid json"}')
            return

        text = payload.get("text", "")
        if not text:
            text = json.dumps(payload, ensure_ascii=False, indent=2)

        ok = send_telegram(text)
        self.send_response(200 if ok else 502)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        if ok:
            self.wfile.write(b'{"ok":true}')
        else:
            self.wfile.write(b'{"ok":false,"error":"telegram send failed"}')

    def do_GET(self):
        """Health check endpoint."""
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"status":"ok","service":"tg-relay"}')

    def log_message(self, format, *args):
        print(f"[relay] {format % args}", flush=True)

if __name__ == "__main__":
    server = HTTPServer(("0.0.0.0", LISTEN_PORT), RelayHandler)
    print(f"[relay] Telegram webhook relay listening on :{LISTEN_PORT}", flush=True)
    print(f"[relay] Target chat_id: {CHAT_ID}", flush=True)
    server.serve_forever()