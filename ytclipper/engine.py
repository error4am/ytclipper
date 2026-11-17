"""Wires the recorder, buffer, detectors and clip worker together."""
from __future__ import annotations

import json
import os
import threading
import time
from collections import deque

from .buffer import SegmentBuffer
from .captions import Captioner
from .clips import ClipJob, ClipWorker, list_clips
from .config import Config
from .detector import AudioHypeDetector, ChatHypeDetector
from .encode import detect
from .hotkey import GlobalHotkey
from .preview import PreviewStream
from .recorder import Recorder
from .security import validate_stream_url
from .util import LogBus


class Engine:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.bus = LogBus()
        self.started_at = time.time()
        self.auto = cfg.auto

        self.buffer = SegmentBuffer(
            directory=cfg.buffer_dir,
            bus=self.bus,
            ffprobe=cfg.ffprobe,
            keep_seconds=cfg.buffer_seconds,
            seg_time=cfg.seg_time,
        )
        self.recorder = Recorder(
            url="",
            buffer_dir=cfg.buffer_dir,
            bus=self.bus,
            ffmpeg=cfg.ffmpeg,
            seg_time=cfg.seg_time,
            refresh_seconds=cfg.refresh,
            quality=cfg.quality,
            direct=cfg.direct,
            cookies_from_browser=cfg.cookies_from_browser,
            cookies_file=cfg.cookies_file,
            extra_header=cfg.extra_header,
        )
        self.captioner = Captioner(
            bus=self.bus,
            ffmpeg=cfg.ffmpeg,
            model_name=cfg.caption_model,
            device=cfg.caption_device,
            compute_type=cfg.caption_compute,
            language=cfg.caption_language,
            box=cfg.caption_box,
            enabled=cfg.captions,
            mode=cfg.caption_mode,
        )
        self.encoder = detect(cfg.ffmpeg, cfg.encoder, self.bus)
        self.worker = ClipWorker(
            buffer=self.buffer,
            clips_dir=cfg.clips_dir,
            bus=self.bus,
            ffmpeg=cfg.ffmpeg,
            ffprobe=cfg.ffprobe,
            stream_title=lambda: self.recorder.title or cfg.url,
            captioner=self.captioner,
            encoder=self.encoder,
            vertical_mode=cfg.vertical_mode,
            vertical_auto=cfg.vertical_auto,
        )
        self.hotkey: GlobalHotkey | None = None
        self.preview = PreviewStream(
            os.path.join(cfg.work_dir, "preview"),
            cfg.ffmpeg,
            self.bus,
            enabled=cfg.preview,
        )
        self.recorder.on_stream = self._on_stream_resolved
        self.audio = AudioHypeDetector(
            ffmpeg=cfg.ffmpeg,
            margin_db=cfg.db_margin,
            floor_db=cfg.floor_db,
        )
        self.chat: ChatHypeDetector | None = None
        self._demo = None
        self._last_url = ""
        self._last_cookies = cfg.cookies_from_browser or ""
        self._last_url_path = os.path.join(cfg.work_dir, "last_url.txt")
        try:
            with open(self._last_url_path, "r", encoding="utf-8") as handle:
                raw = handle.read().strip()[:4000]
            if raw.startswith("{"):
                data = json.loads(raw)
                self._last_url = str(data.get("url") or "")[:2000]
                self._last_cookies = str(data.get("cookies") or "")
            else:
                self._last_url = raw[:2000]
        except (OSError, ValueError):
            pass

        self._analyzed: set[str] = set()
        self._auto_times: deque[float] = deque()
        self._last_auto = 0.0
        self._last_manual = 0.0
        self._triggers = 0
        self._stop = threading.Event()
        self._loop_thread: threading.Thread | None = None

    # -- lifecycle ---------------------------------------------------------- #
    def start(self) -> None:
        self.buffer.reset()
        self.worker.start()
        self.recorder.start()

        self._loop_thread = threading.Thread(
            target=self._analysis_loop, name="analysis", daemon=True
        )
        self._loop_thread.start()

        if self.cfg.hotkey_enabled:
            self.hotkey = GlobalHotkey(self.cfg.hotkey, self._hotkey_clip, self.bus)
            self.hotkey.start()

        if self.cfg.url:
            self.start_stream(self.cfg.url, self.cfg.direct, trusted=True)
        else:
            self.bus.info("no stream yet - paste a URL in the dashboard")

        self.bus.info(
            f"clipper running - clips land in {self.cfg.clips_dir}"
        )
        if self.auto:
            self.bus.info(
                f"auto clipping on: {self.cfg.db_margin:.0f}dB above baseline, "
                f"{self.cfg.cooldown:.0f}s cooldown"
            )
        if self.captioner.enabled:
            if self.captioner.installed:
                self.captioner.warm()
                self.bus.info(
                    f"captions on ({self.cfg.caption_mode}): "
                    f"whisper '{self.cfg.caption_model}', language "
                    f"{self.cfg.caption_language or 'auto'}"
                )
            else:
                self.bus.warn(
                    "captions are on but faster-whisper is missing "
                    "(python -m pip install faster-whisper)"
                )

    def stop(self) -> None:
        self._stop.set()
        if self.chat:
            self.chat.stop()
        if self.hotkey:
            self.hotkey.stop()
        self.preview.stop()
        if self._demo is not None:
            self._demo.stop()
            self._demo = None
        self.recorder.stop()
        self.worker.stop()
        self.bus.info("stopped")

    # -- stream control ----------------------------------------------------- #
    def start_stream(
        self,
        url: str,
        direct: bool = False,
        cookies: str | None = None,
        trusted: bool = False,
    ) -> dict:
        url = (url or "").strip()
        if not url:
            return {"ok": False, "error": "paste a stream URL first"}
        if not url.lower().startswith(("http://", "https://")):
            return {"ok": False, "error": "that does not look like an http(s) URL"}
        if not trusted:
            problem = validate_stream_url(url)
            if problem:
                self.bus.warn(f"rejected stream URL: {problem}")
                return {"ok": False, "error": problem}

        cookies = (cookies or "").strip().lower()
        if cookies:
            self.bus.info(
                f"using {cookies} cookies for this stream "
                "(close the browser if the lookup fails)"
            )

        if self.chat is not None:
            self.chat.stop()
            self.chat = None
        if self._demo is not None and url != getattr(self._demo, "url", ""):
            self._demo.stop()
            self._demo = None
        if not self.worker.busy:
            self.buffer.reset()
        else:
            self.bus.warn("a clip is still encoding - the old buffer stays for now")

        self.cfg.url = url
        self.cfg.direct = direct
        self.cfg.cookies_from_browser = cookies or None
        self.recorder.set_url(url, direct, cookies_from_browser=cookies)

        if self.cfg.chat and not direct:
            self.chat = ChatHypeDetector(
                watch_url=url, on_hype=self._chat_hype, bus=self.bus
            )
            self.chat.start()

        self._last_url = url
        self._last_cookies = cookies or ""
        try:
            with open(self._last_url_path, "w", encoding="utf-8") as handle:
                json.dump({"url": url, "cookies": self._last_cookies}, handle)
        except OSError:
            pass
        self.bus.info(f"watching {url}")
        return {"ok": True, "url": url, "cookies": self._last_cookies}

    def stop_stream(self) -> dict:
        if self.chat is not None:
            self.chat.stop()
            self.chat = None
        self.preview.stop()
        self.recorder.stop_stream()
        if self._demo is not None:
            self._demo.stop()
            self._demo = None
        if not self.worker.busy:
            self.buffer.reset()
        self.bus.info("stream stopped - paste another URL to keep clipping")
        return {"ok": True}

    def start_demo(self) -> dict:
        if self._demo is not None:
            return {"ok": False, "error": "the demo is already running"}

        def run() -> None:
            try:
                from .demo import DemoStream

                demo = DemoStream(
                    os.path.join(self.cfg.work_dir, "demo"),
                    self.cfg.ffmpeg,
                    self.bus,
                    voice_provider=self.cfg.voice,
                    voice_name=self.cfg.voice_name,
                    voice_speed=self.cfg.voice_speed,
                    model_dir=(
                        os.path.join(self.cfg.models_dir, "kokoro")
                        if self.cfg.models_dir
                        else ""
                    ),
                )
                self._demo = demo
                url = demo.start()
                self.start_stream(url, direct=True, trusted=True)
            except Exception as exc:
                self.bus.error(f"demo failed: {exc}")
                self._demo = None

        if not self.recorder.snapshot()["idle"]:
            self.stop_stream()
        threading.Thread(target=run, name="demo", daemon=True).start()
        return {"ok": True, "starting": True}

    # -- triggers ----------------------------------------------------------- #
    def _analysis_loop(self) -> None:
        while not self._stop.is_set():
            try:
                segments = self.buffer.refresh()
                for segment in segments:
                    if segment.name in self._analyzed:
                        continue
                    self._analyzed.add(segment.name)
                    if not self.auto:
                        continue
                    try:
                        hit = self.audio.analyze(segment)
                    except Exception as exc:  # decoding is best-effort
                        self.bus.warn(f"audio analysis failed: {exc}")
                        continue
                    if hit:
                        moment, db, delta, base = hit
                        self._auto_trigger(
                            moment,
                            "audio",
                            f"loud moment +{delta:.0f}dB ({db:.0f}dB vs {base:.0f}dB)",
                        )
                self.buffer.prune()
                if len(self._analyzed) > 4000:
                    keep = {s.name for s in segments}
                    self._analyzed &= keep
            except Exception as exc:
                self.bus.error(f"analysis loop error: {exc}")
            self._stop.wait(1.0)

    def _chat_hype(self, detail: str) -> None:
        if not self.auto:
            return
        moment = self.buffer.now() - 2.0
        if moment > 0:
            self._auto_trigger(moment, "chat", detail)

    def _on_stream_resolved(self, info) -> None:
        """Called by the recorder once a stream URL is resolved."""
        self.preview.start(info)

    def _auto_trigger(self, moment: float, category: str, detail: str) -> bool:
        now = time.time()
        while self._auto_times and now - self._auto_times[0] > 3600:
            self._auto_times.popleft()
        if len(self._auto_times) >= self.cfg.max_per_hour:
            self.bus.warn(
                f"auto clip limit reached ({self.cfg.max_per_hour}/hour) - skipping"
            )
            return False
        if now - self._last_auto < self.cfg.cooldown:
            return False
        self._last_auto = now
        self._auto_times.append(now)
        self._queue(moment, category, detail)
        return True

    def manual_clip(self, before: float | None = None, after: float | None = None) -> dict:
        moment = self.buffer.now()
        if moment <= 0:
            return {"ok": False, "error": "no video recorded yet - give it a few seconds"}
        now = time.time()
        if now - self._last_manual < 3.0:
            return {"ok": False, "error": "slow down a moment"}
        self._last_manual = now
        before = self.cfg.before if before is None else max(1.0, min(120.0, before))
        after = self.cfg.after if after is None else max(0.0, min(120.0, after))
        self._queue(moment, "manual", "button press", before=before, after=after)
        return {"ok": True, "ready_in": round(after, 1), "length": round(before + after, 1)}

    def _queue(
        self,
        moment: float,
        category: str,
        detail: str,
        before: float | None = None,
        after: float | None = None,
    ) -> None:
        before = self.cfg.before if before is None else before
        after = self.cfg.after if after is None else after
        self.worker.submit(
            ClipJob(
                t0=moment - before,
                t1=moment + after,
                reason=category,
                category=category,
                detail=detail,
            )
        )
        self._triggers += 1

    def set_auto(self, enabled: bool) -> None:
        if enabled == self.auto:
            return
        self.auto = enabled
        self.bus.info(f"auto clipping {'enabled' if enabled else 'disabled'}")

    def set_captions(self, enabled: bool) -> None:
        if enabled == self.captioner.enabled:
            return
        self.captioner.enabled = enabled
        self.bus.info(f"captions {'enabled' if enabled else 'disabled'}")
        if enabled:
            if self.captioner.installed:
                self.captioner.warm()
            else:
                self.bus.warn(
                    "captions need faster-whisper: python -m pip install faster-whisper"
                )

    # -- hotkey / 9:16 ------------------------------------------------------ #
    def _hotkey_clip(self) -> None:
        self.bus.info(f"hotkey {self.cfg.hotkey.upper()} pressed")
        result = self.manual_clip()
        if not result.get("ok"):
            self.bus.warn(f"hotkey: {result.get('error')}")

    def export_vertical(self, name: str, mode: str = "") -> dict:
        name = os.path.basename(str(name or ""))
        if not name.endswith(".mp4"):
            return {"ok": False, "error": "bad clip name"}
        if not os.path.isfile(os.path.join(self.cfg.clips_dir, name)):
            return {"ok": False, "error": "clip not found"}
        self.worker.submit_vertical(name, mode)
        return {"ok": True, "mode": mode or self.cfg.vertical_mode}

    # -- state for the UI --------------------------------------------------- #
    def snapshot(self) -> dict:
        now = self.buffer.now()
        return {
            "ok": True,
            "server_time": time.time(),
            "uptime": time.time() - self.started_at,
            "timeline": now,
            "stream": {
                "title": (
                    "no stream"
                    if self.recorder.state == "idle"
                    else self.recorder.title or "waiting for stream…"
                ),
                "url": self.cfg.url,
                "live": self.recorder.state == "recording",
                "state": self.recorder.state,
                "last_url": self._last_url,
                "last_cookies": self._last_cookies,
                "cookies": self.cfg.cookies_from_browser or "",
                "demo": self._demo is not None,
            },
            "recorder": self.recorder.snapshot(),
            "buffer": {
                "seconds": round(self.buffer.span(), 1),
                "segments": len(self.buffer.segments),
                "capacity": self.cfg.buffer_seconds,
                "dir": self.cfg.buffer_dir,
            },
            "detector": {
                "auto": self.auto,
                "audio": self.audio.snapshot(),
                "chat": self.chat.snapshot() if self.chat else {"available": False},
                "triggers": self._triggers,
                "cooldown": self.cfg.cooldown,
                "last_auto": self._last_auto,
                "worker_busy": self.worker.busy,
                "queued": self.worker.jobs.qsize(),
            },
            "captions": self.captioner.snapshot(),
            "preview": {**self.preview.snapshot(), "url": "/preview/index.m3u8"},
            "build": {
                "encoder": self.encoder.key,
                "encoder_label": self.encoder.label,
                "hardware": self.encoder.hardware,
                "hotkey": self.cfg.hotkey.upper() if self.cfg.hotkey_enabled else "",
                "hotkey_active": bool(self.hotkey and self.hotkey.registered),
                "vertical": self.cfg.vertical_mode,
                "vertical_auto": self.cfg.vertical_auto,
                "clips_made": self.worker.completed,
                "verticals_made": self.worker.verticals,
            },
            "defaults": {"before": self.cfg.before, "after": self.cfg.after},
            "clips": list_clips(self.cfg.clips_dir),
            "log": self.bus.tail(140),
            "clips_dir": self.cfg.clips_dir,
        }
