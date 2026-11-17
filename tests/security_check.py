"""Security checks for the dashboard.

Run from the project root:

    python tests/security_check.py

Starts throwaway servers (with and without auth) on localhost and probes them
the way an attacker would: unauthenticated access, brute force, CSRF, path
traversal, SSRF, oversized bodies, host-header tricks and security headers.
"""
from __future__ import annotations

import http.client
import json
import os
import shutil
import ssl
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from ytclipper.config import Config  # noqa: E402
from ytclipper.engine import Engine  # noqa: E402
from ytclipper.security import Auth  # noqa: E402
from ytclipper.util import find_exe, free_port  # noqa: E402
from ytclipper.web import serve  # noqa: E402

STATIC = os.path.join(ROOT, "static")
PASSWORD = "correct-horse-battery"

passed = 0
failed: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    global passed
    if condition:
        passed += 1
        print(f"  ok   {name}")
    else:
        failed.append(f"{name} :: {detail}")
        print(f"  FAIL {name} {detail}")


def request(port, method, path, body=None, headers=None, cookie=None, raw=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    hdrs = dict(headers or {})
    if cookie:
        hdrs["Cookie"] = cookie
    payload = None
    if raw is not None:
        payload = raw
        hdrs.setdefault("Content-Type", "application/json")
    elif body is not None:
        payload = json.dumps(body).encode()
        hdrs["Content-Type"] = "application/json"
    conn.request(method, path, body=payload, headers=hdrs)
    response = conn.getresponse()
    data = response.read()
    status = response.status
    result_headers = {k.lower(): v for k, v in response.getheaders()}
    conn.close()
    return status, result_headers, data


def make_engine(work: str) -> Engine:
    cfg = Config(
        url="",
        out_dir=os.path.join(work, "clips"),
        work_dir=os.path.join(work, "state"),
        auto=False,
        chat=False,
        captions=False,
        preview=False,
        hotkey_enabled=False,
        ffmpeg=find_exe("ffmpeg") or "",
        ffprobe=find_exe("ffprobe") or "",
    )
    engine = Engine(cfg)
    engine.start()
    return engine


def main() -> int:
    work = tempfile.mkdtemp(prefix="ytclip-sec-")
    engine = make_engine(os.path.join(work, "auth"))
    auth = Auth(PASSWORD)
    port = free_port("127.0.0.1", 8811)
    server = serve(engine, "127.0.0.1", port, STATIC, auth=auth)
    print(f"\nauth server on 127.0.0.1:{port}")

    try:
        # --- unauthenticated access
        status, headers, _ = request(port, "GET", "/")
        check("GET / redirects to login", status == 303 and headers.get("location") == "/login",
              f"{status} {headers.get('location')}")
        status, _, _ = request(port, "GET", "/api/state")
        check("GET /api/state without a session is 401", status == 401, str(status))
        status, _, _ = request(port, "POST", "/api/clip", body={"before": 5})
        check("POST /api/clip without a session is 401", status == 401, str(status))
        status, _, _ = request(port, "GET", "/login")
        check("login page is reachable", status == 200, str(status))
        status, headers, _ = request(port, "GET", "/static/app.js")
        check("static assets are public", status == 200, str(status))
        check("static assets carry a CSP", "default-src 'self'" in headers.get("content-security-policy", ""),
              headers.get("content-security-policy", "missing"))

        # --- login
        status, _, _ = request(port, "POST", "/api/login", body={"password": "nope"})
        check("wrong password is 401", status == 401, str(status))
        status, headers, body = request(port, "POST", "/api/login", body={"password": PASSWORD})
        cookie_header = headers.get("set-cookie", "")
        check("correct password logs in", status == 200, str(status))
        check("session cookie is HttpOnly", "HttpOnly" in cookie_header, cookie_header)
        check("session cookie is SameSite=Strict", "SameSite=Strict" in cookie_header, cookie_header)
        cookie = cookie_header.split(";")[0]

        status, headers, body = request(port, "GET", "/api/state", cookie=cookie)
        check("authenticated state works", status == 200, str(status))
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            payload = {}
        check("state reports auth enabled", payload.get("auth", {}).get("enabled") is True, body[:80])
        check("state has no-store", headers.get("cache-control") == "no-store", headers.get("cache-control", ""))
        check("X-Frame-Options is DENY", headers.get("x-frame-options") == "DENY")
        check("nosniff is set", headers.get("x-content-type-options") == "nosniff")

        # --- CSRF / host header
        status, _, _ = request(port, "POST", "/api/clip", body={"before": 5}, cookie=cookie,
                               headers={"Origin": "http://evil.example"})
        check("cross-origin POST is 403", status == 403, str(status))
        status, _, _ = request(port, "GET", "/api/state", cookie=cookie,
                               headers={"Host": "evil.example"})
        check("foreign Host header is 421", status == 421, str(status))

        # --- path traversal
        status, _, _ = request(port, "GET", "/clips/..%2f..%2fwin.ini", cookie=cookie)
        check("clip path traversal is blocked", status in (403, 404), str(status))
        status, _, _ = request(port, "GET", "/static/..%2f..%2fytclipper%2fweb.py")
        check("static path traversal is blocked", status == 403, str(status))

        # --- SSRF
        for bad in (
            "http://127.0.0.1:8899/",
            "http://169.254.169.254/latest/meta-data/",
            "http://10.0.0.5/stream.m3u8",
            "file:///C:/Windows/win.ini",
            "gopher://example.com/",
        ):
            status, _, body = request(port, "POST", "/api/stream", body={"url": bad}, cookie=cookie)
            try:
                ok = json.loads(body).get("ok")
            except json.JSONDecodeError:
                ok = None
            check(f"SSRF rejected: {bad[:42]}", status == 200 and ok is False, f"{status} {body[:80]}")

        # --- oversized body
        try:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            conn.putrequest("POST", "/api/clip")
            conn.putheader("Cookie", cookie)
            conn.putheader("Content-Type", "application/json")
            conn.putheader("Content-Length", "200000")
            conn.endheaders()
            try:
                conn.send(b"x" * 200000)
            except Exception:
                pass
            response = conn.getresponse()
            big_status = response.status
            conn.close()
        except Exception as exc:
            big_status = f"error {exc}"
        check("oversized body is 413", big_status == 413, str(big_status))

        # --- logout
        status, headers, _ = request(port, "POST", "/api/logout", cookie=cookie)
        check("logout succeeds", status == 200, str(status))
        status, _, _ = request(port, "GET", "/api/state", cookie=cookie)
        check("old session is dead", status == 401, str(status))

        # --- brute force
        auth.clear_failures("127.0.0.1")
        statuses = []
        for _ in range(10):
            status, _, _ = request(port, "POST", "/api/login", body={"password": "wrong"})
            statuses.append(status)
        check("brute force gets rate limited", 429 in statuses, str(statuses))

        # --- HTTPS + Secure cookie (needs openssl for a throwaway cert)
        auth.clear_failures("127.0.0.1")
        openssl = shutil.which("openssl")
        if openssl:
            certdir = os.path.join(work, "tls")
            os.makedirs(certdir, exist_ok=True)
            cert = os.path.join(certdir, "cert.pem")
            key = os.path.join(certdir, "key.pem")
            subprocess.run(
                [openssl, "req", "-x509", "-newkey", "rsa:2048", "-keyout", key,
                 "-out", cert, "-days", "1", "-nodes", "-subj", "/CN=localhost"],
                capture_output=True,
                timeout=120,
            )
            if os.path.isfile(cert) and os.path.isfile(key):
                engine3 = make_engine(os.path.join(work, "tls-engine"))
                port3 = free_port("127.0.0.1", 8831)
                server3 = serve(engine3, "127.0.0.1", port3, STATIC, auth=auth, tls=(cert, key))
                try:
                    context = ssl._create_unverified_context()
                    conn = http.client.HTTPSConnection("127.0.0.1", port3, context=context, timeout=10)
                    conn.request("GET", "/api/state")
                    resp = conn.getresponse()
                    resp.read()
                    check("HTTPS serves the API", resp.status == 401, str(resp.status))
                    conn.close()

                    conn = http.client.HTTPSConnection("127.0.0.1", port3, context=context, timeout=10)
                    conn.request("POST", "/api/login", body=json.dumps({"password": PASSWORD}),
                                 headers={"Content-Type": "application/json"})
                    resp = conn.getresponse()
                    resp.read()
                    secure_cookie = resp.getheader("Set-Cookie") or ""
                    conn.close()
                    check("HTTPS login works", resp.status == 200, str(resp.status))
                    check("TLS cookie is Secure", "Secure" in secure_cookie, secure_cookie)
                finally:
                    server3.shutdown()
                    engine3.stop()

        # --- local mode stays open (no password on loopback)
        work2 = os.path.join(work, "open")
        engine2 = make_engine(work2)
        port2 = free_port("127.0.0.1", 8821)
        server2 = serve(engine2, "127.0.0.1", port2, STATIC, auth=None)
        try:
            status, _, _ = request(port2, "GET", "/")
            check("local mode serves the dashboard without login", status == 200, str(status))
            status, _, _ = request(port2, "GET", "/api/state")
            check("local mode API is open", status == 200, str(status))
            status, _, body = request(port2, "POST", "/api/stream", body={"url": "http://127.0.0.1:8899/x.m3u8"})
            check("SSRF guard also applies locally", status == 200 and b'"ok": false' in body, body[:80])
            status, _, _ = request(port2, "GET", "/api/state", headers={"Host": "evil.example"})
            check("local mode rejects foreign Host too", status == 421, str(status))
        finally:
            server2.shutdown()
            engine2.stop()

        # --- public bind without a password must refuse to start
        result = subprocess.run(
            [sys.executable, os.path.join(ROOT, "clipper.py"), "--host", "0.0.0.0", "--no-open"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=90,
        )
        check(
            "public bind without a password is refused",
            result.returncode == 2 and "refusing" in result.stdout.lower(),
            f"{result.returncode} {result.stdout[:120]}",
        )

    finally:
        server.shutdown()
        engine.stop()
        shutil.rmtree(work, ignore_errors=True)

    print()
    if failed:
        print(f"{len(failed)} check(s) failed:")
        for item in failed:
            print(f"  - {item}")
        return 1
    print(f"all {passed} checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
