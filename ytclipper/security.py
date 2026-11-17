"""Authentication, request guards and SSRF protection for the dashboard.

The dashboard can start processes and delete files, so it is only safe to
expose when it is password protected. Local (loopback) use stays frictionless.
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import os
import secrets
import socket
import threading
import time
from urllib.parse import urlsplit

SESSION_TTL = 12 * 3600
MAX_FAILURES = 8
FAILURE_WINDOW = 600
MAX_BODY_BYTES = 64 * 1024
COOKIE_NAME = "ytc_session"


def is_loopback(host: str) -> bool:
    value = (host or "").strip().lower().strip("[]")
    if value in ("localhost", "::1"):
        return True
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


def _is_blocked_address(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def validate_stream_url(url: str) -> str | None:
    """Reject schemes and hosts that could reach the local network."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return "that does not look like a URL"

    if parts.scheme not in ("http", "https"):
        return "only http(s) stream URLs are allowed"

    host = (parts.hostname or "").strip().lower()
    if not host:
        return "that URL has no host"
    if host == "localhost" or host.endswith(".local") or host.endswith(".internal"):
        return "local addresses are not allowed"
    if host.startswith("0.") or host == "0.0.0.0":
        return "local addresses are not allowed"

    port = parts.port or (443 if parts.scheme == "https" else 80)
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        # unresolvable is not an SSRF risk; yt-dlp will report the real error
        return None
    except Exception:
        return None

    for info in infos:
        address = str(info[4][0]).split("%")[0]
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            continue
        if _is_blocked_address(ip):
            return "that host points at a private or local address"
    return None


class Auth:
    """Password login with in-memory sessions and a small brute-force lockout."""

    def __init__(self, password: str | None, secure_cookie: bool = False) -> None:
        self.enabled = bool(password)
        self.secure_cookie = secure_cookie
        self._salt = secrets.token_bytes(16)
        self._hash = self._derive(password) if password else b""
        self._sessions: dict[str, float] = {}
        self._failures: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    # -- password ----------------------------------------------------------- #
    def _derive(self, password: str) -> bytes:
        return hashlib.pbkdf2_hmac(
            "sha256", (password or "").encode("utf-8"), self._salt, 200_000
        )

    def verify(self, password: str) -> bool:
        if not self.enabled:
            return True
        return hmac.compare_digest(self._derive(password or ""), self._hash)

    # -- sessions ----------------------------------------------------------- #
    def issue(self) -> str:
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._sessions[token] = time.time() + SESSION_TTL
        return token

    def valid(self, token: str | None) -> bool:
        if not self.enabled:
            return True
        if not token:
            return False
        now = time.time()
        with self._lock:
            expiry = self._sessions.get(token)
            if expiry is None:
                return False
            if expiry < now:
                self._sessions.pop(token, None)
                return False
            return True

    def revoke(self, token: str | None) -> None:
        if not token:
            return
        with self._lock:
            self._sessions.pop(token, None)

    # -- brute force -------------------------------------------------------- #
    def blocked(self, ip: str) -> bool:
        now = time.time()
        with self._lock:
            recent = [t for t in self._failures.get(ip, []) if now - t < FAILURE_WINDOW]
            self._failures[ip] = recent
            return len(recent) >= MAX_FAILURES

    def note_failure(self, ip: str) -> None:
        with self._lock:
            self._failures.setdefault(ip, []).append(time.time())

    def clear_failures(self, ip: str) -> None:
        with self._lock:
            self._failures.pop(ip, None)


def generate_password() -> str:
    return secrets.token_urlsafe(12)


def session_cookie(token: str, secure: bool) -> str:
    parts = [
        f"{COOKIE_NAME}={token}",
        "Path=/",
        "HttpOnly",
        "SameSite=Strict",
        f"Max-Age={SESSION_TTL}",
    ]
    if secure:
        parts.append("Secure")
    return "; ".join(parts)


def clear_cookie(secure: bool) -> str:
    parts = [f"{COOKIE_NAME}=", "Path=/", "HttpOnly", "SameSite=Strict", "Max-Age=0"]
    if secure:
        parts.append("Secure")
    return "; ".join(parts)


def parse_cookies(header: str | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for chunk in (header or "").split(";"):
        if "=" not in chunk:
            continue
        key, _, value = chunk.partition("=")
        out[key.strip()] = value.strip()
    return out
