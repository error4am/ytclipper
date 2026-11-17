"""Low-latency preview of the live stream for the dashboard.

The browser cannot play the upstream directly (cross-origin, expiring URLs,
separate video/audio tracks), so we remux the stream into local 1-second HLS
segments with ffmpeg and serve them from our own HTTP server. `-c copy` is used
when the source is H.264/AAC; otherwise a small transcode keeps it playable.
"""
from __future__ import annotations

import glob
import os
import shutil
import subprocess
import threading
import time

from .resolve import StreamInfo
from .util import CREATE_NO_WINDOW, LogBus, tail_text

PLAYLIST = "index.m3u8"


class PreviewStream:
    def __init__(
        self, directory: str, ffmpeg: str, bus: LogBus, enabled: bool = True
    ) -> None:
        self.directory = directory
        self.ffmpeg = ffmpeg
        self.bus = bus
        self.enabled = enabled
        self.proc: subprocess.Popen | None = None
        self.mode = "copy"
        self.error = ""
        self.token = ""
        self._url = ""
        self._lock = threading.Lock()
        self._generation = 0
        os.makedirs(directory, exist_ok=True)

    # -- public ------------------------------------------------------------- #
    def start(self, info: StreamInfo) -> None:
        if not self.enabled or not info.inputs:
            return
        url = info.inputs[0]["url"]
        with self._lock:
            if (
                self.proc is not None
                and self.proc.poll() is None
                and url == self._url
            ):
                return
            self._generation += 1
            generation = self._generation
            self.token = str(int(time.time() * 1000))

        self._stop_proc()
        self._reset_dir()
        self._url = url
        self.error = ""
        mode = "copy" if self._can_copy(info) else "transcode"
        self.mode = mode
        self._spawn(info, mode)
        threading.Thread(
            target=self._watch,
            args=(generation, info, mode),
            name="preview-watch",
            daemon=True,
        ).start()

    def stop(self) -> None:
        with self._lock:
            self._generation += 1
            self._url = ""
        self._stop_proc()

    def snapshot(self) -> dict:
        ready = self.proc is not None and self.proc.poll() is None
        playlist = os.path.join(self.directory, PLAYLIST)
        has_playlist = os.path.isfile(playlist) and os.path.getsize(playlist) > 0
        return {
            "enabled": self.enabled,
            "ready": bool(ready and has_playlist),
            "mode": self.mode,
            "error": self.error,
            "token": self.token,
        }

    # -- internals ---------------------------------------------------------- #
    @staticmethod
    def _can_copy(info: StreamInfo) -> bool:
        def compatible(codec: str | None, prefixes: tuple[str, ...]) -> bool:
            value = (codec or "").lower()
            return value in ("", "none") or value.startswith(prefixes)

        for item in info.inputs:
            if not compatible(item.get("vcodec"), ("avc", "h264")):
                return False
            if not compatible(item.get("acodec"), ("aac", "mp4a")):
                return False
        return True

    def _build(self, info: StreamInfo, mode: str) -> list[str]:
        cmd = [
            self.ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-y",
            "-fflags",
            "+genpts",
        ]
        inputs = info.inputs
        realtime = info.live_status in ("is_live", "post_live")
        for item in inputs:
            if item.get("ua"):
                cmd += ["-user_agent", item["ua"]]
            if not realtime:
                cmd += ["-re"]
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
            cmd += ["-map", f"{audio}:a?"] if audio is not None else ["-map", "0:a?"]

        if mode == "copy":
            cmd += ["-c", "copy"]
        else:
            cmd += [
                "-vf",
                "scale=-2:540",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-tune",
                "zerolatency",
                "-g",
                "25",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-b:a",
                "96k",
                "-ar",
                "44100",
            ]

        cmd += [
            "-f",
            "hls",
            "-hls_time",
            "1",
            "-hls_list_size",
            "4",
            "-hls_flags",
            "delete_segments+omit_endlist",
            "-hls_segment_filename",
            os.path.join(self.directory, "seg%05d.ts"),
            os.path.join(self.directory, PLAYLIST),
        ]
        return cmd

    def _spawn(self, info: StreamInfo, mode: str) -> None:
        try:
            self.proc = subprocess.Popen(
                self._build(info, mode),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                creationflags=CREATE_NO_WINDOW,
            )
        except Exception as exc:
            self.error = str(exc)
            self.bus.warn(f"preview could not start: {exc}")

    def _watch(self, generation: int, info: StreamInfo, mode: str) -> None:
        time.sleep(6)
        with self._lock:
            if generation != self._generation:
                return
            proc = self.proc
        if proc is None or proc.poll() is None:
            return

        with self._lock:
            if generation != self._generation:
                return

        # a normal video simply finishes - that is not a failure
        if (
            info.live_status not in ("is_live", "post_live")
            and proc.returncode == 0
        ):
            return

        detail = ""
        try:
            detail = tail_text((proc.stderr.read() or b"").decode("utf-8", "replace"))
        except Exception:
            pass

        if mode == "copy":
            self.bus.warn(
                f"preview copy failed ({detail or 'ffmpeg exited'}) - "
                "retrying with a small transcode"
            )
            with self._lock:
                if generation != self._generation:
                    return
            self._reset_dir()
            self.mode = "transcode"
            self._spawn(info, "transcode")
            threading.Thread(
                target=self._watch,
                args=(generation, info, "transcode"),
                name="preview-watch",
                daemon=True,
            ).start()
            return

        self.error = detail or "preview stopped"
        self.bus.warn(f"preview unavailable: {self.error}")

    def _reset_dir(self) -> None:
        try:
            shutil.rmtree(self.directory, ignore_errors=True)
        except Exception:
            pass
        os.makedirs(self.directory, exist_ok=True)
        for path in glob.glob(os.path.join(self.directory, "*")):
            try:
                os.remove(path)
            except OSError:
                pass

    def _stop_proc(self) -> None:
        proc = self.proc
        self.proc = None
        if proc is None:
            return
        if proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
