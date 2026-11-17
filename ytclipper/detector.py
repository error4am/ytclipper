"""Find the loud / hyped moments worth clipping.

Two independent signals:
  * audio  - short-term loudness jumping well above its rolling baseline
  * chat   - live chat message rate spiking above its rolling baseline
Both run on the same stream timeline the recorder produces, so a detection
offset maps directly onto segment positions.
"""
from __future__ import annotations

import array
import math
import statistics
import subprocess
import threading
import time
from collections import deque

from .buffer import Segment
from .util import CREATE_NO_WINDOW, LogBus


class AudioHypeDetector:
    """Compares 0.5s loudness windows against the recent median."""

    def __init__(
        self,
        ffmpeg: str,
        margin_db: float = 8.5,
        floor_db: float = -34.0,
        window: float = 0.5,
        baseline_seconds: float = 90.0,
        min_history_windows: int = 24,
        rate: int = 8000,
    ) -> None:
        self.ffmpeg = ffmpeg
        self.margin_db = float(margin_db)
        self.floor_db = float(floor_db)
        self.window = float(window)
        self.rate = int(rate)
        self.min_history = int(min_history_windows)
        history_len = max(self.min_history + 4, int(baseline_seconds / self.window))
        self.history: deque[float] = deque(maxlen=history_len)
        self.last_db = -90.0
        self.last_base = -90.0
        self.last_delta = 0.0

    def _pcm(self, path: str) -> bytes:
        try:
            proc = subprocess.run(
                [
                    self.ffmpeg,
                    "-v",
                    "error",
                    "-nostdin",
                    "-i",
                    path,
                    "-vn",
                    "-ac",
                    "1",
                    "-ar",
                    str(self.rate),
                    "-f",
                    "s16le",
                    "-",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=30,
                creationflags=CREATE_NO_WINDOW,
            )
            return proc.stdout or b""
        except Exception:
            return b""

    def analyze(self, seg: Segment) -> tuple[float, float, float, float] | None:
        """Return (timeline_time, db, delta, baseline) for the strongest hit."""
        raw = self._pcm(seg.path)
        if len(raw) < 512:
            return None
        samples = array.array("h")
        samples.frombytes(raw[: len(raw) - (len(raw) % 2)])
        if not samples:
            return None

        size = max(1, int(self.rate * self.window))
        best: tuple[float, float, float, float] | None = None
        for index in range(len(samples) // size):
            chunk = samples[index * size : (index + 1) * size]
            acc = 0
            for value in chunk:
                acc += value * value
            rms = math.sqrt(acc / len(chunk)) / 32768.0
            db = 20.0 * math.log10(max(rms, 1e-7))

            if len(self.history) >= self.min_history:
                base = statistics.median(self.history)
                delta = db - base
                if db >= self.floor_db and delta >= self.margin_db:
                    if best is None or delta > best[2]:
                        moment = seg.start + index * self.window + self.window / 2.0
                        best = (moment, db, delta, base)
            self.history.append(db)
            self.last_db = db
            if self.history:
                self.last_base = statistics.median(self.history)
                self.last_delta = db - self.last_base
        return best

    def snapshot(self) -> dict:
        return {
            "last_db": round(self.last_db, 1),
            "baseline_db": round(self.last_base, 1),
            "delta_db": round(self.last_delta, 1),
            "margin_db": self.margin_db,
            "floor_db": self.floor_db,
            "warm": len(self.history) >= self.min_history,
        }


class ChatHypeDetector(threading.Thread):
    """Optional: watches live chat and fires when the message rate spikes."""

    def __init__(
        self,
        watch_url: str,
        on_hype,
        bus: LogBus,
        bucket_seconds: float = 5.0,
        min_messages: int = 9,
        ratio: float = 3.0,
    ) -> None:
        super().__init__(name="chat-detector", daemon=True)
        self.watch_url = watch_url
        self.on_hype = on_hype
        self.bus = bus
        self.bucket = float(bucket_seconds)
        self.min_messages = int(min_messages)
        self.ratio = float(ratio)
        self.available = False
        self.messages = 0
        self.last_rate = 0.0
        self.baseline = 0.0
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        try:
            from chat_downloader import ChatDownloader  # type: ignore
        except Exception:
            self.bus.warn(
                "live chat detection is off (optional: python -m pip install chat-downloader)"
            )
            return

        self.available = True
        self.bus.info("live chat detection on")

        while not self._stop.is_set():
            try:
                chat = ChatDownloader().get_chat(
                    self.watch_url, message_groups=["messages"]
                )
                self._consume(chat)
            except Exception as exc:
                if self._stop.is_set():
                    return
                self.bus.warn(f"chat connection lost: {exc} (retrying in 15s)")
                if self._stop.wait(15):
                    return

    def _consume(self, chat) -> None:
        counts: deque[int] = deque(maxlen=24)  # ~2 minutes of buckets
        bucket_start = time.time()
        count = 0
        for message in chat:
            if self._stop.is_set():
                return
            if not isinstance(message, dict):
                continue
            text = message.get("message") or ""
            if not text:
                continue
            self.messages += 1
            count += 1
            now = time.time()
            if now - bucket_start >= self.bucket:
                self._close_bucket(counts, count)
                count = 0
                bucket_start = now
            self.last_rate = count / max(0.001, now - bucket_start)

    def _close_bucket(self, counts: deque[int], count: int) -> None:
        if len(counts) >= 6:
            base = statistics.median(counts) or 1.0
            self.baseline = base
            if count >= max(self.min_messages, self.ratio * base):
                self.bus.clip(f"chat spike: {count} msgs / {self.bucket:.0f}s (base {base:.0f})")
                try:
                    self.on_hype(f"chat spike {count} msgs/{self.bucket:.0f}s")
                except Exception:
                    pass
        counts.append(count)

    def snapshot(self) -> dict:
        return {
            "available": self.available,
            "messages": self.messages,
            "rate": round(self.last_rate, 1),
            "baseline": round(self.baseline, 1),
            "min_messages": self.min_messages,
        }
