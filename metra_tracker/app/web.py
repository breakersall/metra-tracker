"""Tiny web server for the Home Assistant ingress panel: the live map page + a JSON snapshot."""
import json
import logging
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

log = logging.getLogger(__name__)

STATIC = os.path.join(os.path.dirname(__file__), "static")
INGRESS_PROXY = "172.30.32.2"  # Supervisor's ingress proxy; nothing else may talk to this server


class Web:
    def __init__(self, port, snapshot):
        self.port = port
        self.snapshot = snapshot  # callable -> dict
        self.open = os.environ.get("METRA_WEB_OPEN") == "1"  # local development only

    def start(self):
        web = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                log.debug("web: " + fmt, *args)

            def _send(self, code, body, ctype):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if not web.open and self.client_address[0] not in (INGRESS_PROXY, "127.0.0.1", "::1"):
                    return self._send(403, b"forbidden", "text/plain")
                path = self.path.split("?")[0].rstrip("/") or "/"
                if path in ("/", "/index.html"):
                    with open(os.path.join(STATIC, "index.html"), "rb") as fh:
                        return self._send(200, fh.read(), "text/html; charset=utf-8")
                if path == "/api/state":
                    body = json.dumps(web.snapshot(), default=str).encode()
                    return self._send(200, body, "application/json")
                return self._send(404, b"not found", "text/plain")

        server = ThreadingHTTPServer(("0.0.0.0", self.port), Handler)
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, name="web", daemon=True).start()
        log.info("Map page listening on :%d", self.port)
        return server
