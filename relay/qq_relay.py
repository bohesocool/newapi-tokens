#!/usr/bin/env python3
"""QQ Bot relay — receives POST, forwards text to QQ via hermes send."""
import os, json, subprocess, threading
from http.server import HTTPServer, BaseHTTPRequestHandler

LISTEN_PORT = 9219
QQ_TARGET = "qqbot"

def send_qq(text):
    """Send message to QQ via hermes CLI."""
    try:
        result = subprocess.run(
            ["hermes", "send", "--to", QQ_TARGET, "-q", "--text", text or "无内容"],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0:
            print(f"[qq-relay] sent OK", flush=True)
            return True
        else:
            print(f"[qq-relay] hermes send failed rc={result.returncode}: {result.stderr.strip()}", flush=True)
            return False
    except Exception as e:
        print(f"[qq-relay] error: {e}", flush=True)
        return False

class QQRelayHandler(BaseHTTPRequestHandler):
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

        ok = send_qq(text)
        self.send_response(200 if ok else 502)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        if ok:
            self.wfile.write(b'{"ok":true}')
        else:
            self.wfile.write(b'{"ok":false,"error":"qq send failed"}')

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"status":"ok","service":"qq-relay"}')

    def log_message(self, format, *args):
        print(f"[qq-relay] {format % args}", flush=True)

if __name__ == "__main__":
    server = HTTPServer(("127.0.0.1", LISTEN_PORT), QQRelayHandler)
    print(f"[qq-relay] listening on 127.0.0.1:{LISTEN_PORT}", flush=True)
    server.serve_forever()