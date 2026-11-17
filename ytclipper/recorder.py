"""Records the live stream into a rolling set of short .mkv segments."""
from __future__ import annotations

import collections
import os
import subprocess
import threading
import time

from .resolve import NotLiveYet, ResolveError, StreamInfo, direct_info, resolve
from .util import CREATE_NO_WINDOW, LogBus, tail_text


def build_ffmpeg_cmd(
    ffmpeg: str,
    info: StreamInfo,
    pattern: str,
    seg_time: float,
) -> list[str]:
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y"]
    inputs = info.inputs
    realtime = info.live_status in ("is_live", "post_live")

    for item in inputs:
        if item.get("ua"):
            cmd += ["-user_agent", item["ua"]]
        if item.get("extra_header"):
            cmd += ["-headers", item["extra_header"] + "\r\n"]
        if not realtime:
            cmd += ["-re"]  # a VOD should play out at 1x, not be swallowed instantly
        cmd += [
            "-reconnect",
            "1",
            "-reconnect_streamed",
            "1",
            "-reconnect_delay_max",
            "10",
            "-i",
            item["url"],
        ]

    if len(inputs) == 1:
        cmd += ["-map", "0:v?", "-map", "0:a?"]
    else:
        video = next(
            (i for i, it in enumerate(inputs) if it.get("kind") in ("video", "av")), 0
        )
        audio = next(
            (
                i
                for i, it in enumerate(inputs)
                if it.get("kind") in ("audio", "av") and i != video
            ),
            None,
        )
        cmd += ["-map", f"{video}:v?"]
        if audio is not None:
            cmd += ["-map", f"{audio}:a?"]
        else:
            cmd += ["-map", "0:a?"]

    cmd += [
        "-c",
        "copy",
        "-f",
        "segment",
        "-segment_time",
        f"{seg_time:g}",
        "-segment_format",
        "matroska",
        "-reset_timestamps",
        "1",
        "-strftime",
        "1",
        pattern,
    ]
    return cmd


class Recorder:
    """Owns the ffmpeg process that writes segments, and keeps it alive."""

    def __init__(
        self,
        url: str,
        buffer_dir: str,
        bus: LogBus,
        ffmpeg: str,
        seg_time: float = 4.0,
        refresh_seconds: float = 3600.0,
        quality: int = 1080,
        direct: bool = False,
        cookies_from_browser: str | None = None,
        cookies_file: str | None = None,
        extra_header: str | None = None,
        retry_seconds: float = 15.0,
        wait_seconds: float = 45.0,
    ) -> None:
        self.url = url or ""
        self.buffer_dir = buffer_dir
        self.bus = bus
        self.ffmpeg = ffmpeg
        self.seg_time = seg_time
        self.refresh_seconds = refresh_seconds
        self.quality = quality
        self.direct = direct
        self.cookies_from_browser = cookies_from_browser
        self.cookies_file = cookies_file
        self.extra_header = extra_header
        self.retry_seconds = retry_seconds
        self.wait_seconds = wait_seconds

        self.title = ""
        self.state = "starting" if self.url else "idle"  # idle|starting|waiting|recording|ended|error
        self.last_error = ""
        self.sessions = 0
        self.restarts = 0
        self.started_at = time.time()
        self.active_since: float | None = None
        self.info: StreamInfo | None = None

        self._proc: subprocess.Popen | None = None
        self.on_stream = None
        self._stop = threading.Event()
        self._enabled = threading.Event()
        if self.url:
            self._enabled.set()
        self._generation = 0
        self._session_seq = 0
        self._cancel = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    # -- public ------------------------------------------------------------- #
    def start(self) -> None:
        os.makedirs(self.buffer_dir, exist_ok=True)
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="recorder", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._enabled.clear()
        self._kill_proc()
        if self._thread:
            self._thread.join(timeout=12)

    def set_url(
        self,
        url: str,
        direct: bool | None = None,
        cookies_from_browser: str | None = None,
    ) -> None:
        """Start (or switch to) a stream."""
        with self._lock:
            self._generation += 1
            self._cancel.set()
            self._cancel = threading.Event()
            self.url = url
            if direct is not None:
                self.direct = bool(direct)
            if cookies_from_browser is not None:
                self.cookies_from_browser = cookies_from_browser or None
            self.title = ""
            self.last_error = ""
            self.sessions = 0
            self.restarts = 0
        self._kill_proc()
        self.state = "starting"
        self.active_since = None
        self._enabled.set()

    def stop_stream(self) -> None:
        """Stop recording but keep the app and dashboard alive."""
        with self._lock:
            self._generation += 1
            self._cancel.set()
            self._cancel = threading.Event()
            self._enabled.clear()
            self.title = ""
            self.last_error = ""
            self.sessions = 0
        self._kill_proc()
        self.active_since = None
        self.state = "idle"

    def snapshot(self) -> dict:
        active = 0.0
        if self.state == "recording" and self.active_since:
            active = time.time() - self.active_since
        return {
            "state": self.state,
            "title": self.title,
            "error": self.last_error,
            "sessions": self.sessions,
            "restarts": self.restarts,
            "active_seconds": active,
            "url": self.url,
            "idle": not self._enabled.is_set(),
        }

    # -- internals ---------------------------------------------------------- #
    def _resolve(self, url: str | None = None, cancel=None) -> StreamInfo:
        target = url or self.url
        if self.direct:
            return direct_info(target)
        return resolve(
            target,
            quality=self.quality,
            bus=self.bus,
            cookies_from_browser=self.cookies_from_browser,
            cookies_file=self.cookies_file,
            extra_header=self.extra_header,
            cancel=cancel,
        )

    def _loop(self) -> None:
        while not self._stop.is_set():
            if not self._enabled.wait(timeout=0.5):
                continue

            with self._lock:
                url = self.url
                generation = self._generation
                cancel = self._cancel
            if not url:
                self._enabled.clear()
                self.state = "idle"
                continue

            try:
                info = self._resolve(url, cancel)
            except NotLiveYet as exc:
                if generation != self._generation:
                    continue
                self.state = "waiting"
                self.last_error = str(exc)
                self.bus.warn(f"{exc} - checking again in {int(self.wait_seconds)}s")
                if self._stop.wait(self.wait_seconds):
                    break
                continue
            except ResolveError as exc:
                if generation != self._generation or str(exc) == "cancelled":
                    continue
                self.state = "waiting"
                self.last_error = str(exc)
                self.bus.error(
                    f"stream lookup failed: {exc} (retrying in {int(self.retry_seconds)}s)"
                )
                if self._stop.wait(self.retry_seconds):
                    break
                continue

            if generation != self._generation:
                continue

            self.info = info
            self.title = info.title
            if info.ended and self.sessions > 0:
                self.state = "ended"
                self._enabled.clear()
                self.bus.info("the stream has ended - buffer stays available, web UI still up")
                continue

            if generation == self._generation and self.on_stream is not None:
                try:
                    self.on_stream(info)
                except Exception as exc:
                    self.bus.warn(f"stream hook failed: {exc}")

            self._record(info, generation)

            if self._stop.is_set():
                break
            if generation != self._generation:
                continue
            if self.state == "ended":
                self._enabled.clear()
                continue
            if self._stop.wait(self.retry_seconds):
                break

    def _record(self, info: StreamInfo, generation: int) -> None:
        with self._lock:
            self.sessions += 1
            self._session_seq += 1
            session = self._session_seq
        pattern = os.path.join(
            self.buffer_dir, f"seg_s{session:02d}_%Y%m%d_%H%M%S.mkv"
        )
        cmd = build_ffmpeg_cmd(self.ffmpeg, info, pattern, self.seg_time)

        self.bus.info(f"recording '{info.title}' -> {self.buffer_dir}")
        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                creationflags=CREATE_NO_WINDOW,
            )
        except Exception as exc:
            self.state = "error"
            self.last_error = str(exc)
            self.bus.error(f"could not start ffmpeg: {exc}")
            return

        self._proc = proc
        self.state = "recording"
        self.active_since = time.time()
        stderr_lines: collections.deque[str] = collections.deque(maxlen=30)

        def drain() -> None:
            try:
                for raw in proc.stderr:  # type: ignore[union-attr]
                    text = raw.decode("utf-8", "replace").strip()
                    if text:
                        stderr_lines.append(text)
            except Exception:
                pass

        threading.Thread(target=drain, name="ffmpeg-log", daemon=True).start()

        refreshed = False
        started = time.time()
        while not self._stop.is_set() and generation == self._generation:
            if proc.poll() is not None:
                break
            if self.refresh_seconds and time.time() - started > self.refresh_seconds:
                refreshed = True
                self.bus.info("refreshing stream URL (long session)")
                break
            time.sleep(1.0)

        switching = generation != self._generation
        stopping = self._stop.is_set() or switching
        self._kill_proc(proc)

        if stopping:
            self.active_since = None
            if not self._stop.is_set() and not switching:
                self.state = "idle"
            return
        if refreshed:
            self.restarts += 1
            return

        code = proc.returncode
        self.active_since = None
        if code == 0:
            self.state = "ended"
            self.bus.info("ffmpeg finished: the stream is over")
            return

        detail = tail_text("\n".join(stderr_lines))
        self.last_error = detail or f"ffmpeg exited with code {code}"
        self.state = "waiting"
        self.restarts += 1
        self.bus.warn(f"recording stopped (code {code}): {self.last_error}")

    def _kill_proc(self, proc: subprocess.Popen | None = None) -> None:
        target = proc or self._proc
        if target is None:
            return
        if target.poll() is None:
            try:
                target.terminate()
            except Exception:
                pass
            try:
                target.wait(timeout=8)
            except Exception:
                try:
                    target.kill()
                except Exception:
                    pass
        if target is self._proc:
            self._proc = None
