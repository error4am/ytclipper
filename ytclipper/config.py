"""Runtime configuration for one clipper session."""
from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass
class Config:
    url: str = ""
    out_dir: str = "clips"
    work_dir: str = ".clipper"
    before: float = 18.0
    after: float = 12.0
    seg_time: float = 4.0
    buffer_seconds: float = 420.0
    quality: int = 1080
    auto: bool = True
    chat: bool = True
    captions: bool = True
    caption_model: str = "base"
    caption_language: str | None = None
    caption_device: str = "cpu"
    caption_compute: str = "int8"
    caption_box: bool = False
    caption_mode: str = "burn"  # burn | srt
    db_margin: float = 8.5
    floor_db: float = -34.0
    cooldown: float = 45.0
    max_per_hour: int = 40
    direct: bool = False
    cookies_from_browser: str | None = None
    cookies_file: str | None = None
    extra_header: str | None = None
    refresh: float = 3600.0
    host: str = "127.0.0.1"
    port: int = 8765
    open_browser: bool = True
    encoder: str = "auto"
    hotkey: str = "F8"
    hotkey_enabled: bool = True
    vertical_mode: str = "blur"
    vertical_auto: bool = False
    preview: bool = True
    models_dir: str = ""
    voice: str = "kokoro"
    voice_name: str = "af_heart"
    voice_speed: float = 1.0
    ffmpeg: str = ""
    ffprobe: str = ""

    # derived
    buffer_dir: str = field(init=False, default="")
    clips_dir: str = field(init=False, default="")

    def __post_init__(self) -> None:
        root = os.path.abspath(self.work_dir)
        self.buffer_dir = os.path.join(root, "segments")
        self.out_dir = os.path.abspath(self.out_dir)
        self.clips_dir = self.out_dir
        os.makedirs(self.buffer_dir, exist_ok=True)
        os.makedirs(self.clips_dir, exist_ok=True)

        minimum = self.before + self.after + 90.0
        if self.buffer_seconds < minimum:
            self.buffer_seconds = minimum
