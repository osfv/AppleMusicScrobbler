"""Local stand-in for ws.audioscrobbler.com, for tests and manual runs."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse


class FakeLastfm:
    def __init__(self):
        self.requests = []  # (method, params) in arrival order
        self.replies = {}  # method -> list of (status, body), consumed in order
        self.lock = threading.Lock()
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _handle(self, params):
                params = {k: v[0] for k, v in params.items()}
                method = params.get("method", "")
                with fake.lock:
                    fake.requests.append((method, params))
                    queue = fake.replies.get(method) or []
                    status, body = queue.pop(0) if len(queue) > 1 else (queue[0] if queue else (200, None))
                if body is None:
                    body = fake.default(method, params)
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                self._handle(parse_qs(urlparse(self.path).query))

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                self._handle(parse_qs(self.rfile.read(n).decode()))

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def reply(self, method, *responses):
        """Queue (status, body) replies for a method. The last one repeats."""
        self.replies[method] = list(responses)

    def calls(self, method):
        return [p for m, p in self.requests if m == method]

    def default(self, method, params):
        if method == "auth.getToken":
            return {"token": "tok"}
        if method == "auth.getSession":
            return {"session": {"name": "tester", "key": "sk-new", "subscriber": 0}}
        if method == "track.scrobble":
            n = sum(1 for k in params if k.startswith("track["))
            return {"scrobbles": {"@attr": {"accepted": n, "ignored": 0}}}
        if method == "artist.getInfo":
            return {"artist": {"name": params.get("artist")}}
        return {}

    def close(self):
        self.server.shutdown()
        self.server.server_close()
