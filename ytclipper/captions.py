"""Automatic captions for clips.

Speech is transcribed locally with faster-whisper (no API key, no upload),
turned into short readable cues, and burned into the clip while it is encoded.
An .srt file is always written next to the clip so it can be edited or reused.
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import threading
import time
from dataclasses import dataclass

from .util import CREATE_NO_WINDOW, LogBus


@dataclass
class Cue:
    start: float
    end: float
    text: str


def build_cues(raw, max_chars: int = 42, max_lines: int = 2) -> list[Cue]:
    """Split whisper segments into short, evenly timed caption cues."""
    cues: list[Cue] = []
    limit = max(12, max_chars * max_lines)

    for start, end, text in raw:
        text = " ".join((text or "").split())
        if not text or end <= start:
            continue

        words = text.split()
        chunks: list[list[str]] = []
        current: list[str] = []
        for word in words:
            candidate = " ".join(current + [word])
            if current and len(candidate) > limit:
                chunks.append(current)
                current = [word]
            else:
                current.append(word)
        if current:
            chunks.append(current)

        weights = [max(1, len(" ".join(chunk))) for chunk in chunks]
        total = sum(weights)
        span = max(0.4, end - start)
        cursor = 0.0
        for chunk, weight in zip(chunks, weights):
            share = weight / total
            cue_start = start + span * cursor
            cue_end = start + span * (cursor + share)
            cursor += share
            if cue_end - cue_start >= 0.25:
                cues.append(Cue(cue_start, cue_end, " ".join(chunk)))

    # Guarantee a little breathing room and monotonic order.
    cleaned: list[Cue] = []
    for cue in cues:
        if cleaned and cue.start < cleaned[-1].end:
            cleaned[-1].end = max(cleaned[-1].start + 0.2, cue.start - 0.02)
        if cue.end - cue.start >= 0.2:
            cleaned.append(cue)
    return cleaned


def _stamp_srt(seconds: float) -> str:
    seconds = max(0.0, seconds)
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    millis = int(round((seconds - int(seconds)) * 1000))
    if millis == 1000:
        millis = 0
        secs += 1
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def to_srt(cues: list[Cue]) -> str:
    lines: list[str] = []
    for index, cue in enumerate(cues, start=1):
        lines.append(str(index))
        lines.append(f"{_stamp_srt(cue.start)} --> {_stamp_srt(cue.end)}")
        lines.append(cue.text)
        lines.append("")
    return "\n".join(lines)


def _stamp_ass(seconds: float) -> str:
    seconds = max(0.0, seconds)
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = seconds % 60
    return f"{hours}:{minutes:02d}:{secs:05.2f}"


def _wrap(text: str, max_chars: int) -> str:
    lines: list[str] = []
    current = ""
    for word in text.split():
        candidate = f"{current} {word}".strip()
        if current and len(candidate) > max_chars:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return "\\N".join(lines)


def to_ass(cues: list[Cue], width: int, height: int, box: bool = False) -> str:
    width = width if width >= 320 else 1280
    height = height if height >= 240 else 720
    font_size = max(16, round(height * 0.047))
    margin_v = round(height * 0.06)
    margin_lr = round(width * 0.07)
    outline = max(1.4, round(height * 0.0026, 1))
    shadow = max(1.0, round(height * 0.0012, 1))
    border_style = 3 if box else 1
    outline_colour = "&H99000000" if box else "&H00000000"
    back_colour = "&H99000000" if box else "&H80000000"
    if box:
        outline = max(2, round(height * 0.007))
        shadow = 0

    header = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {width}",
        f"PlayResY: {height}",
        "WrapStyle: 0",
        "ScaledBorderAndShadow: yes",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: Cap,Arial,{font_size},&H00FFFFFF,&H000000FF,{outline_colour},"
        f"{back_colour},1,0,0,0,100,100,0,0,{border_style},{outline},{shadow},"
        f"2,{margin_lr},{margin_lr},{margin_v},1",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, "
        "Effect, Text",
    ]

    events = []
    for cue in cues:
        text = (
            cue.text.replace("\\", "/")
            .replace("{", "(")
            .replace("}", ")")
            .replace("\n", " ")
        )
        body = _wrap(text, 42)
        events.append(
            f"Dialogue: 0,{_stamp_ass(cue.start)},{_stamp_ass(cue.end)},"
            f"Cap,,0,0,0,,{body}"
        )
    return "\n".join(header + events) + "\n"


class Captioner:
    """Lazily loads one whisper model and transcribes clip audio on demand."""

    def __init__(
        self,
        bus: LogBus,
        ffmpeg: str,
        model_name: str = "base",
        device: str = "cpu",
        compute_type: str = "int8",
        language: str | None = None,
        box: bool = False,
        enabled: bool = True,
        mode: str = "burn",
    ) -> None:
        self.bus = bus
        self.ffmpeg = ffmpeg
        self.model_name = model_name
        self.device = device
        self.compute_type = compute_type
        self.language = language
        self.box = box
        self.enabled = enabled
        self.mode = mode if mode in ("burn", "srt") else "burn"

        self.installed = importlib.util.find_spec("faster_whisper") is not None
        self.status = "missing" if not self.installed else "idle"
        self.last_error = ""
        self.last_language: str | None = None
        self.truncated = False

        self._model = None
        self._lock = threading.Lock()
        self._warned = False

    # -- model -------------------------------------------------------------- #
    def warm(self) -> None:
        if not self.installed or not self.enabled:
            return
        threading.Thread(target=self._load, name="caption-warm", daemon=True).start()

    def _load(self):
        with self._lock:
            if self._model is not None:
                return self._model
            if not self.installed:
                self.status = "missing"
                return None
            self.status = "loading"
            started = time.time()
            try:
                from faster_whisper import WhisperModel

                self._model = WhisperModel(
                    self.model_name,
                    device=self.device,
                    compute_type=self.compute_type,
                )
                self.status = "ready"
                self.bus.info(
                    f"caption model '{self.model_name}' ready "
                    f"({time.time() - started:.0f}s, {self.device}/{self.compute_type})"
                )
            except Exception as exc:
                self.status = "error"
                self.last_error = str(exc)
                self.bus.error(f"caption model failed to load: {exc}")
                self._model = None
            return self._model

    # -- audio -------------------------------------------------------------- #
    def _decode(self, path: str, offset: float, duration: float):
        try:
            import numpy as np
        except Exception:
            return None
        try:
            proc = subprocess.run(
                [
                    self.ffmpeg,
                    "-v",
                    "error",
                    "-nostdin",
                    "-i",
                    path,
                    "-ss",
                    f"{max(0.0, offset):.3f}",
                    "-t",
                    f"{max(0.2, duration):.3f}",
                    "-vn",
                    "-ac",
                    "1",
                    "-ar",
                    "16000",
                    "-f",
                    "f32le",
                    "-",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=180,
                creationflags=CREATE_NO_WINDOW,
            )
            data = proc.stdout or b""
            if len(data) < 3200:
                return None
            return np.frombuffer(data, dtype=np.float32)
        except Exception as exc:
            self.last_error = str(exc)
            return None

    # -- transcription ------------------------------------------------------ #
    def transcribe(self, path: str, offset: float, duration: float) -> list[Cue]:
        if not self.enabled:
            return []
        if not self.installed:
            if not self._warned:
                self._warned = True
                self.bus.warn(
                    "captions are on but faster-whisper is not installed "
                    "(python -m pip install faster-whisper) - clips will be uncaptioned"
                )
            return []

        audio = self._decode(path, offset, duration)
        if audio is None or audio.size < 1600:
            return []

        model = self._load()
        if model is None:
            return []

        started = time.time()
        try:
            segments, info = model.transcribe(
                audio,
                language=self.language,
                beam_size=1,
                vad_filter=True,
                condition_on_previous_text=False,
                word_timestamps=False,
            )
            raw = [(s.start, s.end, s.text) for s in segments]
            self.last_language = getattr(info, "language", None)
        except Exception as exc:
            self.last_error = str(exc)
            self.bus.warn(f"transcription failed: {exc}")
            return []

        cues = build_cues(raw)
        if cues:
            self.bus.info(
                f"transcribed {duration:.0f}s -> {len(cues)} caption cues "
                f"({time.time() - started:.1f}s, {self.last_language or 'auto'})"
            )
        return cues

    def snapshot(self) -> dict:
        return {
            "enabled": self.enabled,
            "available": self.installed,
            "status": self.status,
            "model": self.model_name,
            "language": self.language or "auto",
            "box": self.box,
            "mode": self.mode,
        }
