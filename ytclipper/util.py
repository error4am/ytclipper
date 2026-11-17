"""Shared helpers: logging, subprocess plumbing, ffprobe wrappers."""
from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from collections import deque

IS_WINDOWS = os.name == "nt"
CREATE_NO_WINDOW = 0x08000000 if IS_WINDOWS else 0


def _make_stdio_safe() -> None:
    """Never let an emoji in a stream title kill the process."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            pass


_make_stdio_safe()


# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #
class LogBus:
    """A bounded in-memory log the web UI can poll, mirrored to stdout."""

    def __init__(self, capacity: int = 600) -> None:
        self._items: deque[dict] = deque(maxlen=capacity)
        self._lock = threading.Lock()

    def log(self, message: str, level: str = "info") -> None:
        rec = {
            "t": time.time(),
            "clock": time.strftime("%H:%M:%S"),
            "level": level,
            "msg": message,
        }
        with self._lock:
            self._items.append(rec)
        prefix = {"warn": " ~ ", "error": " ! ", "clip": " > "}.get(level, "   ")
        try:
            print(f"[{rec['clock']}]{prefix}{message}", flush=True)
        except Exception:
            pass

    def info(self, message: str) -> None:
        self.log(message, "info")

    def warn(self, message: str) -> None:
        self.log(message, "warn")

    def error(self, message: str) -> None:
        self.log(message, "error")

    def clip(self, message: str) -> None:
        self.log(message, "clip")

    def tail(self, count: int = 150) -> list[dict]:
        with self._lock:
            return list(self._items)[-count:]


# --------------------------------------------------------------------------- #
# Processes
# --------------------------------------------------------------------------- #
def find_exe(name: str, *fallbacks: str) -> str | None:
    path = shutil.which(name)
    if path:
        return path
    for candidate in fallbacks:
        if candidate and os.path.isfile(candidate):
            return candidate
    return None


def yt_dlp_prefix() -> list[str] | None:
    exe = shutil.which("yt-dlp")
    if exe:
        return [exe]
    try:
        import importlib.util

        if importlib.util.find_spec("yt_dlp") is not None:
            return [sys.executable, "-m", "yt_dlp"]
    except Exception:
        pass
    return None


def run(cmd: list[str], timeout: float | None = 120, cwd: str | None = None) -> tuple[int, str, str]:
    """Run a command, capture output, never raise."""
    try:
        proc = subprocess.run(
            cmd,
            cwd=cwd,
            timeout=timeout,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=CREATE_NO_WINDOW,
        )
        out = proc.stdout.decode("utf-8", "replace")
        err = proc.stderr.decode("utf-8", "replace")
        return proc.returncode, out, err
    except subprocess.TimeoutExpired:
        return 124, "", f"timed out after {timeout}s: {' '.join(cmd[:3])}"
    except FileNotFoundError as exc:
        return 127, "", str(exc)
    except Exception as exc:  # pragma: no cover - defensive
        return 1, "", str(exc)


def run_cancellable(
    cmd: list[str],
    timeout: float | None = 120,
    cancel: threading.Event | None = None,
) -> tuple[int, str, str]:
    """Like run(), but aborts the child as soon as `cancel` is set."""
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=CREATE_NO_WINDOW,
        )
    except FileNotFoundError as exc:
        return 127, "", str(exc)
    except Exception as exc:
        return 1, "", str(exc)

    started = time.time()
    while True:
        if cancel is not None and cancel.is_set():
            try:
                proc.kill()
            except Exception:
                pass
            try:
                proc.communicate(timeout=5)
            except Exception:
                pass
            return 499, "", "cancelled"
        if timeout and time.time() - started > timeout:
            try:
                proc.kill()
            except Exception:
                pass
            try:
                proc.communicate(timeout=5)
            except Exception:
                pass
            return 124, "", f"timed out after {timeout}s"
        try:
            out, err = proc.communicate(timeout=0.2)
            return (
                proc.returncode,
                out.decode("utf-8", "replace"),
                err.decode("utf-8", "replace"),
            )
        except subprocess.TimeoutExpired:
            continue


def ffprobe_duration(ffprobe: str, path: str) -> float:
    if not os.path.isfile(path):
        return 0.0
    code, out, _ = run(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=nw=1:nk=1",
            path,
        ],
        timeout=20,
    )
    if code != 0:
        return 0.0
    try:
        return max(0.0, float(out.strip()))
    except ValueError:
        return 0.0


def ffprobe_size(ffprobe: str, path: str) -> tuple[int, int]:
    """Video width/height of the first video stream, or (0, 0)."""
    if not os.path.isfile(path):
        return 0, 0
    code, out, _ = run(
        [
            ffprobe,
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height",
            "-of",
            "csv=s=x:p=0",
            path,
        ],
        timeout=20,
    )
    if code != 0:
        return 0, 0
    try:
        width, height = out.strip().split("x")[:2]
        return int(width), int(height)
    except (ValueError, IndexError):
        return 0, 0


def tail_text(text: str, lines: int = 6, width: int = 220) -> str:
    picked = [ln.strip() for ln in (text or "").splitlines() if ln.strip()][-lines:]
    joined = " | ".join(picked)
    return joined[-width:]


# --------------------------------------------------------------------------- #
# Formatting
# --------------------------------------------------------------------------- #
def human_time(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def human_size(num: int) -> str:
    value = float(num)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f}{unit}" if unit == "B" else f"{value:.1f}{unit}"
        value /= 1024
    return f"{value:.1f}GB"


def slug(text: str, limit: int = 28) -> str:
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text or "").strip("-").lower()
    return (text[:limit] or "clip").strip("-")


def local_stamp(ts: float | None = None) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts if ts is not None else time.time()))


def free_port(host: str, port: int) -> int:
    """Return the port, or the next free one if it is taken."""
    for candidate in range(port, port + 40):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind((host, candidate))
                return candidate
            except OSError:
                continue
    return port


def read_json(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except Exception:
        return {}


def write_json(path: str, data: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2)
    os.replace(tmp, path)
