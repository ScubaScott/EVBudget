"""Small HTTP layer: a route table, JSON helpers, and a request handler."""

import json
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler
from typing import Any, Callable, Dict, Optional, Set, Tuple

MAX_BODY_BYTES = 64 * 1024 * 1024


class HttpError(Exception):
    def __init__(self, status: int, message: str, **extra: Any):
        super().__init__(message)
        self.status = status
        self.message = message
        self.extra = extra


class Request:
    def __init__(self, method: str, path: str, query: Dict[str, str], headers: Any, raw: bytes):
        self.method = method
        self.path = path
        self.query = query
        self.headers = headers
        self.raw = raw
        self._json: Optional[Dict[str, Any]] = None

    @property
    def content_type(self) -> str:
        return self.headers.get("Content-Type", "") if self.headers else ""

    def json(self) -> Dict[str, Any]:
        if self._json is None:
            if not self.raw:
                self._json = {}
            else:
                try:
                    data = json.loads(self.raw.decode("utf-8"))
                except Exception as exc:
                    raise HttpError(400, f"Malformed JSON: {exc}")
                if not isinstance(data, dict):
                    raise HttpError(400, "JSON body must be an object")
                self._json = data
        return self._json


class Response:
    def __init__(self, body: bytes, content_type: str, status: int = 200, headers: Optional[Dict[str, str]] = None):
        self.body = body
        self.content_type = content_type
        self.status = status
        self.headers = headers or {}


def json_response(payload: Any, status: int = 200) -> Response:
    body = json.dumps(payload, default=str).encode("utf-8")
    return Response(body, "application/json; charset=utf-8", status)


class Router:
    def __init__(self) -> None:
        self._exact: Dict[Tuple[str, str], Callable[[Request], Any]] = {}
        self._prefix: Dict[Tuple[str, str], Callable[[Request], Any]] = {}

    def add(self, method: str, path: str, fn: Callable[[Request], Any]) -> None:
        self._exact[(method.upper(), path)] = fn

    def add_prefix(self, method: str, prefix: str, fn: Callable[[Request], Any]) -> None:
        self._prefix[(method.upper(), prefix)] = fn

    def route(self, method: str, *paths: str):
        def deco(fn: Callable[[Request], Any]):
            for p in paths:
                self.add(method, p, fn)
            return fn
        return deco

    def find(self, method: str, path: str) -> Optional[Callable[[Request], Any]]:
        fn = self._exact.get((method.upper(), path))
        if fn:
            return fn
        for (m, prefix), handler in self._prefix.items():
            if m == method.upper() and path.startswith(prefix):
                return handler
        return None


def _to_response(result: Any) -> Response:
    if isinstance(result, Response):
        return result
    if isinstance(result, tuple) and len(result) == 2 and isinstance(result[0], int):
        return json_response(result[1], result[0])
    return json_response(result)


def make_handler(router: Router, allowed_hosts: Set[str]):
    """Build a request handler class bound to a router.

    Requests whose Host header isn't this machine are refused, which blocks
    DNS-rebinding attacks. No CORS headers are sent, so other websites can't
    read responses.
    """

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):  # noqa: A002
            pass

        def _dispatch(self, method: str) -> None:
            host = (self.headers.get("Host") or "").lower()
            if host not in allowed_hosts:
                self._send(Response(b"Forbidden", "text/plain", 403))
                return
            parsed = urllib.parse.urlparse(self.path)
            query = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}

            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY_BYTES:
                self._send(json_response({"success": False, "error": "Request too large"}, 413))
                return
            raw = self.rfile.read(length) if length > 0 else b""

            fn = router.find(method, parsed.path)
            if fn is None:
                self._send(json_response({"success": False, "error": "Endpoint not found"}, 404))
                return
            try:
                response = _to_response(fn(Request(method, parsed.path, query, self.headers, raw)))
            except HttpError as err:
                response = json_response(dict({"success": False, "error": err.message}, **err.extra), err.status)
            except Exception as exc:  # pragma: no cover - last-resort guard
                traceback.print_exc()
                response = json_response({"success": False, "error": f"Server error: {exc}"}, 500)
            self._send(response)

        def _send(self, response: Response) -> None:
            self.send_response(response.status)
            self.send_header("Content-Type", response.content_type)
            self.send_header("Content-Length", str(len(response.body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            for k, v in response.headers.items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(response.body)

        def do_GET(self) -> None:
            self._dispatch("GET")

        def do_POST(self) -> None:
            self._dispatch("POST")

    return Handler
