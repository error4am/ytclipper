"""Tiny local web UI + JSON API for the clipper.

Hardened for non-local binding: password login, session cookies, host checks,
CSRF/origin checks, capped request bodies, strict security headers.
"""
from __future__ import annotations

import ipaddress
import json
import mimetypes
import os
import ssl
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .engine import Engine
from .security import (
    MAX_BODY_BYTES,
    COOKIE_NAME,
    Auth,
    clear_cookie,
    parse_cookies,
    session_cookie,
)

CSP = (
    "default-src 'self'; img-src 'self' data:; media-src 'self' blob:; "
    "script-src 'self'; style-src 'self'; connect-src 'self'; "
    "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "ytclipper"
    sys_version = ""
    timeout = 20  # drop idle/slow connections instead of holding threads

    engine: Engine = None  # type: ignore[assignment]
    static_dir: str = ""
    auth: Auth = None  # type: ignore[assignment]
    allowed_hosts: set[str] = set()
    secure_cookie = False

    # -- guards ------------------------------------------------------------- #
    def log_message(self, *_args) -> None:  # noqa: D401
        pass

    def _token(self) -> str | None:
        return parse_cookies(self.headers.get("Cookie")).get(COOKIE_NAME)

    def _authed(self) -> bool:
        if self.auth is None or not self.auth.enabled:
            return True
        return self.auth.valid(self._token())

    def _host_ok(self) -> bool:
        # always on: a hostile page can DNS-rebind its own domain to 127.0.0.1
        header = self.headers.get("Host") or ""
        host = (urllib.parse.urlsplit("//" + header).hostname or "").strip("[]").lower()
        if not host:
            return False
        try:
            ipaddress.ip_address(host)
            return True  # IP literals cannot be DNS-rebound
        except ValueError:
            pass
        return host in self.allowed_hosts

    def _origin_ok(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return True
        host = (self.headers.get("Host") or "").lower()
        try:
            return urllib.parse.urlsplit(origin).netloc.lower() == host
        except ValueError:
            return False

    # -- responses ---------------------------------------------------------- #
    def _send_headers(
        self,
        status: int,
        ctype: str,
        length: int,
        cache: str = "no-store",
        extra: dict[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        self.send_header("Cross-Origin-Opener-Policy", "same-origin")
        self.send_header("Content-Security-Policy", CSP)
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()

    def _json(self, payload: dict, status: int = 200, extra: dict[str, str] | None = None) -> None:
        body = json.dumps(payload).encode("utf-8")
        try:
            self._send_headers(
                status, "application/json; charset=utf-8", len(body), extra=extra
            )
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _redirect(self, location: str, status: int = 303) -> None:
        try:
            self._send_headers(status, "text/plain; charset=utf-8", 0, extra={"Location": location})
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _body(self) -> dict | None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length > MAX_BODY_BYTES:
            self.close_connection = True
            self._json({"ok": False, "error": "request too large"}, 413)
            return None
        raw = self.rfile.read(length) if length else b""
        try:
            data = json.loads(raw or b"{}")
            return data if isinstance(data, dict) else {}
        except json.JSONDecodeError:
            self.close_connection = True
            self._json({"ok": False, "error": "invalid json"}, 400)
            return None

    def _static(self, relative: str) -> None:
        root = os.path.abspath(self.static_dir)
        path = os.path.normpath(os.path.join(root, relative))
        try:
            inside = os.path.commonpath([root, path]) == root
        except ValueError:
            inside = False
        if not inside:
            return self._json({"ok": False, "error": "forbidden"}, 403)
        if not os.path.isfile(path):
            return self._json({"ok": False, "error": "not found"}, 404)
        ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript",):
            ctype += "; charset=utf-8"
        self._file(path, ctype, cache="no-cache")

    def _file(self, path: str, ctype: str, cache: str = "no-store") -> None:
        if not os.path.isfile(path):
            return self._json({"ok": False, "error": "not found"}, 404)
        try:
            size = os.path.getsize(path)
        except OSError:
            return self._json({"ok": False, "error": "unreadable"}, 404)
        start, end, status = 0, max(0, size - 1), 200
        header = self.headers.get("Range")
        if header and header.startswith("bytes="):
            spec = header[6:].split(",")[0].strip()
            if "-" in spec:
                left, right = spec.split("-", 1)
                try:
                    if left:
                        start = int(left)
                        end = int(right) if right else size - 1
                    elif right:
                        start = max(0, size - int(right))
                        end = size - 1
                    start = max(0, min(start, size - 1))
                    end = max(start, min(end, size - 1))
                    status = 206
                except ValueError:
                    start, end, status = 0, size - 1, 200
        length = end - start + 1
        extra = {"Accept-Ranges": "bytes"}
        if status == 206:
            extra["Content-Range"] = f"bytes {start}-{end}/{size}"
        try:
            self._send_headers(status, ctype, length, cache=cache, extra=extra)
            with open(path, "rb") as handle:
                handle.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = handle.read(min(256 * 1024, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _safe_name(self, raw: str) -> str:
        return os.path.basename(urllib.parse.unquote(raw))

    # -- GET ---------------------------------------------------------------- #
    def do_GET(self) -> None:  # noqa: N802
        if not self._host_ok():
            return self._json({"ok": False, "error": "invalid host header"}, 421)

        path = urllib.parse.unquote(urllib.parse.urlsplit(self.path).path)

        if path in ("/login", "/login.html"):
            if self._authed():
                return self._redirect("/")
            return self._static("login.html")
        if path.startswith("/static/"):
            return self._static(path[len("/static/") :])
        if path == "/favicon.ico":
            return self._json({"ok": False}, 404)

        if not self._authed():
            if path.startswith("/api/"):
                return self._json({"ok": False, "error": "login required"}, 401)
            return self._redirect("/login")

        if path in ("/", "/index.html"):
            return self._static("index.html")
        if path == "/api/state":
            try:
                payload = self.engine.snapshot()
                payload["auth"] = {
                    "enabled": bool(self.auth is not None and self.auth.enabled)
                }
                return self._json(payload)
            except Exception as exc:  # pragma: no cover - defensive
                return self._json({"ok": False, "error": str(exc)}, 500)
        if path.startswith("/clips/"):
            name = self._safe_name(path[len("/clips/") :])
            return self._file(os.path.join(self.engine.cfg.clips_dir, name), "video/mp4")
        if path.startswith("/thumbs/"):
            name = self._safe_name(path[len("/thumbs/") :])
            return self._file(
                os.path.join(self.engine.cfg.clips_dir, name), "image/jpeg", cache="no-cache"
            )
        if path.startswith("/subs/"):
            name = self._safe_name(path[len("/subs/") :])
            return self._file(
                os.path.join(self.engine.cfg.clips_dir, name),
                "text/plain; charset=utf-8",
                cache="no-cache",
            )
        if path.startswith("/preview/"):
            name = self._safe_name(path[len("/preview/") :])
            ctype = (
                "application/vnd.apple.mpegurl"
                if name.endswith(".m3u8")
                else "video/mp2t"
            )
            return self._file(
                os.path.join(self.engine.preview.directory, name), ctype, cache="no-store"
            )
        return self._json({"ok": False, "error": "not found"}, 404)

    # -- POST --------------------------------------------------------------- #
    def do_POST(self) -> None:  # noqa: N802
        if not self._host_ok():
            return self._json({"ok": False, "error": "invalid host header"}, 421)

        path = urllib.parse.urlsplit(self.path).path

        if path == "/api/login":
            return self._login()
        if path == "/api/logout":
            return self._logout()

        if not self._authed():
            return self._json({"ok": False, "error": "login required"}, 401)
        if not self._origin_ok():
            return self._json({"ok": False, "error": "cross-origin request rejected"}, 403)

        data = self._body()
        if data is None:
            return

        if path == "/api/clip":
            before = _as_float(data.get("before"), self.engine.cfg.before)
            after = _as_float(data.get("after"), self.engine.cfg.after)
            return self._json(self.engine.manual_clip(before, after))

        if path == "/api/auto":
            self.engine.set_auto(bool(data.get("enabled")))
            return self._json({"ok": True, "auto": self.engine.auto})

        if path == "/api/captions":
            self.engine.set_captions(bool(data.get("enabled")))
            return self._json({"ok": True, "captions": self.engine.captioner.snapshot()})

        if path == "/api/stream":
            return self._json(
                self.engine.start_stream(
                    str(data.get("url") or ""),
                    bool(data.get("direct")),
                    str(data.get("cookies") or ""),
                )
            )

        if path == "/api/stream/stop":
            return self._json(self.engine.stop_stream())

        if path == "/api/demo":
            return self._json(self.engine.start_demo())

        if path == "/api/vertical":
            return self._json(
                self.engine.export_vertical(
                    str(data.get("name") or ""), str(data.get("mode") or "")
                )
            )

        if path == "/api/delete":
            name = self._safe_name(str(data.get("name") or ""))
            if not name.endswith(".mp4"):
                return self._json({"ok": False, "error": "bad name"}, 400)
            from .clips import delete_clip

            return self._json({"ok": delete_clip(self.engine.cfg.clips_dir, name)})

        if path == "/api/reveal":
            # opening a file manager on the host only makes sense locally
            if not (self.client_address[0] in ("127.0.0.1", "::1")):
                return self._json({"ok": False, "error": "only available locally"}, 403)
            folder = self.engine.cfg.clips_dir
            try:
                if sys.platform == "win32":
                    os.startfile(folder)  # type: ignore[attr-defined]
                elif sys.platform == "darwin":
                    os.system(f'open "{folder}"')
                else:
                    os.system(f'xdg-open "{folder}"')
                return self._json({"ok": True})
            except Exception as exc:
                return self._json({"ok": False, "error": str(exc)}, 500)

        return self._json({"ok": False, "error": "not found"}, 404)

    # -- auth --------------------------------------------------------------- #
    def _login(self) -> None:
        ip = self.client_address[0]
        if self.auth is None or not self.auth.enabled:
            return self._json({"ok": True, "auth": False})
        if self.auth.blocked(ip):
            return self._json(
                {"ok": False, "error": "too many attempts - wait a few minutes"}, 429
            )
        data = self._body()
        if data is None:
            return
        if not self.auth.verify(str(data.get("password") or "")):
            self.auth.note_failure(ip)
            return self._json({"ok": False, "error": "wrong password"}, 401)
        self.auth.clear_failures(ip)
        token = self.auth.issue()
        return self._json(
            {"ok": True}, extra={"Set-Cookie": session_cookie(token, self.secure_cookie)}
        )

    def _logout(self) -> None:
        if self.auth is not None:
            self.auth.revoke(self._token())
        return self._json(
            {"ok": True}, extra={"Set-Cookie": clear_cookie(self.secure_cookie)}
        )


def _as_float(value, fallback: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


class QuietServer(ThreadingHTTPServer):
    """Keep-alive sockets closing mid-request is normal, not an error."""

    daemon_threads = True

    def handle_error(self, request, client_address) -> None:
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)):
            return
        super().handle_error(request, client_address)


def serve(
    engine: Engine,
    host: str,
    port: int,
    static_dir: str,
    auth: Auth | None = None,
    allowed_hosts: set[str] | None = None,
    tls: tuple[str, str] | None = None,
) -> QuietServer:
    handler = type(
        "BoundHandler",
        (Handler,),
        {
            "engine": engine,
            "static_dir": static_dir,
            "auth": auth,
            "allowed_hosts": {h.lower() for h in (allowed_hosts or set())}
            | {"localhost", "127.0.0.1", "::1"},
            "secure_cookie": bool(tls),
        },
    )
    httpd = QuietServer((host, port), handler)
    if tls:
        cert, key = tls
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key)
        httpd.socket = context.wrap_socket(httpd.socket, server_side=True)
    thread = threading.Thread(target=httpd.serve_forever, name="web", daemon=True)
    thread.start()
    return httpd
