"""Serve fixed dashboard assets and the collector's read-only snapshot."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import time
from urllib.parse import urlsplit


STATIC = Path(__file__).parent / "static"
METRICS = Path(os.environ.get("METRICS_FILE", "/data/metrics.json"))
ASSETS = {"/": ("index.html", "text/html; charset=utf-8"),
          "/app.js": ("app.js", "text/javascript; charset=utf-8"),
          "/style.css": ("style.css", "text/css; charset=utf-8"),
          "/favicon.svg": ("favicon.svg", "image/svg+xml")}


class Handler(BaseHTTPRequestHandler):
    server_version = "HostDashboard"

    def do_GET(self):
        path = urlsplit(self.path).path
        if path in ("/api/metrics", "/health"):
            try:
                payload = METRICS.read_bytes()
                data = json.loads(payload)
                age = time.time() - data["timestamp"]
                healthy = -5 <= age < 10
            except (OSError, ValueError, KeyError, TypeError):
                payload = b'{"error":"Waiting for host metrics"}'
                healthy = False
            if path == "/health":
                payload = json.dumps({"status": "ok" if healthy else "stale"}).encode()
            return self.respond(200 if healthy else 503, payload, "application/json")
        if path in ASSETS:
            filename, content_type = ASSETS[path]
            return self.respond(200, (STATIC / filename).read_bytes(), content_type)
        self.respond(404, b"Not found", "text/plain")

    def respond(self, status, body, content_type):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, format, *args):
        if args and str(args[1]) not in ("200",):
            super().log_message(format, *args)


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
