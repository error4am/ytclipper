"""A local synthetic "live stream" so the clipper can be tested offline.

It generates a real HLS stream with ffmpeg: a test pattern plus looping spoken
lines (Windows SAPI when available) and a loud burst every ~16 seconds. That is
enough to exercise recording, auto-detection, clipping, captions and the UI
without touching YouTube.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

from .util import CREATE_NO_WINDOW, LogBus, free_port
from .voice import make_speech

SPEECH = (
    "Hey chat, welcome back to the stream. "
    "Today we are testing the automatic clipper. "
    "That was absolutely insane, I cannot believe it worked. "
    "Clip that moment right now. "
    "Thanks for watching, see you tomorrow."
)


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *_args) -> None:  # noqa: D401
        pass


class DemoStream:
    """Serves a fake live HLS stream on 127.0.0.1."""

    def __init__(
        self,
        root: str,
        ffmpeg: str,
        bus: LogBus,
        voice_provider: str = "kokoro",
        voice_name: str = "af_heart",
        voice_speed: float = 1.0,
        model_dir: str = "",
    ) -> None:
        self.root = root
        self.ffmpeg = ffmpeg
        self.bus = bus
        self.voice_provider = voice_provider
        self.voice_name = voice_name
        self.voice_speed = voice_speed
        self.model_dir = model_dir
        self.proc: subprocess.Popen | None = None
        self.httpd: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None
        self.url = ""
        self.had_voice = False

    # -- public ------------------------------------------------------------- #
    def start(self) -> str:
        shutil.rmtree(self.root, ignore_errors=True)
        os.makedirs(self.root, exist_ok=True)
        voice, self.had_voice = make_speech(
            SPEECH,
            self.root,
            self.ffmpeg,
            self.bus,
            model_dir=self.model_dir,
            provider=self.voice_provider,
            voice=self.voice_name,
            speed=self.voice_speed,
        )

        playlist = os.path.join(self.root, "demo.m3u8")
        segments = os.path.join(self.root, "seg%03d.ts")
        cmd = [
            self.ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-re",
            "-stream_loop",
            "-1",
            "-i",
            voice,
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=1280x720:rate=25",
            "-map",
            "1:v",
            "-map",
            "0:a",
            "-af",
            "volume='1+5*gt(mod(t,16),13.5)':eval=frame",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-tune",
            "zerolatency",
            "-g",
            "50",
            "-pix_fmt",
            "yuv420p",
            "-crf",
            "26",
            "-c:a",
            "aac",
            "-b:a",
            "96k",
            "-ar",
            "44100",
            "-f",
            "hls",
            "-hls_time",
            "2",
            "-hls_list_size",
            "8",
            "-hls_flags",
            "delete_segments",
            "-hls_segment_filename",
            segments,
            "-t",
            "1800",
            playlist,
        ]
        self.proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            creationflags=CREATE_NO_WINDOW,
        )

        port = free_port("127.0.0.1", 8901)
        self.httpd = ThreadingHTTPServer(
            ("127.0.0.1", port), partial(_QuietHandler, directory=self.root)
        )
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(
            target=self.httpd.serve_forever, name="demo-http", daemon=True
        )
        self.thread.start()

        for _ in range(200):
            if self.proc.poll() is not None:
                err = (self.proc.stderr.read() or b"").decode("utf-8", "replace")
                raise RuntimeError(f"demo ffmpeg exited: {err.strip()[:300]}")
            if os.path.isfile(playlist) and os.path.getsize(playlist) > 0:
                break
            time.sleep(0.1)
        else:
            raise RuntimeError("demo stream did not produce a playlist")

        self.url = f"http://127.0.0.1:{port}/demo.m3u8"
        return self.url

    def stop(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=6)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass
        self.proc = None
        if self.httpd is not None:
            try:
                self.httpd.shutdown()
                self.httpd.server_close()
            except Exception:
                pass
            self.httpd = None
        try:
            shutil.rmtree(self.root, ignore_errors=True)
        except Exception:
            pass
