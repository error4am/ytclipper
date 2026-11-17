"""Pick the fastest H.264 encoder that actually works on this machine.

ffmpeg may be built with nvenc/qsv/amf even when the hardware or driver cannot
run it, so each candidate is proven with a tiny real encode before we trust it.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass

from .util import CREATE_NO_WINDOW, LogBus


@dataclass(frozen=True)
class Encoder:
    key: str
    label: str
    args: tuple[str, ...]
    hardware: bool


ENCODERS: dict[str, Encoder] = {
    "nvenc": Encoder(
        "nvenc",
        "h264_nvenc (NVIDIA GPU)",
        ("-c:v", "h264_nvenc", "-preset", "p4", "-tune", "hq", "-rc", "vbr",
         "-cq", "21", "-b:v", "0"),
        True,
    ),
    "qsv": Encoder(
        "qsv",
        "h264_qsv (Intel Quick Sync)",
        ("-c:v", "h264_qsv", "-preset", "veryfast", "-global_quality", "22"),
        True,
    ),
    "amf": Encoder(
        "amf",
        "h264_amf (AMD GPU)",
        ("-c:v", "h264_amf", "-quality", "balanced", "-rc", "cqp",
         "-qp_i", "21", "-qp_p", "23"),
        True,
    ),
    "x264": Encoder(
        "x264",
        "libx264 (CPU)",
        ("-c:v", "libx264", "-preset", "veryfast", "-crf", "20"),
        False,
    ),
}

AUTO_ORDER = ("nvenc", "qsv", "amf")

X264 = ENCODERS["x264"]


def _works(ffmpeg: str, encoder: Encoder) -> bool:
    cmd = [
        ffmpeg,
        "-v",
        "error",
        "-f",
        "lavfi",
        "-i",
        "color=c=black:s=640x360:r=25:d=0.4",
        "-frames:v",
        "8",
        *encoder.args,
        "-f",
        "null",
        "-",
    ]
    try:
        return (
            subprocess.run(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=30,
                creationflags=CREATE_NO_WINDOW,
            ).returncode
            == 0
        )
    except Exception:
        return False


def detect(ffmpeg: str, preference: str = "auto", bus: LogBus | None = None) -> Encoder:
    preference = (preference or "auto").lower()

    if preference in ENCODERS and preference != "auto":
        encoder = ENCODERS[preference]
        if encoder.key == "x264" or _works(ffmpeg, encoder):
            if bus:
                bus.info(f"encoder: {encoder.label}")
            return encoder
        if bus:
            bus.warn(f"{encoder.label} is not usable here - falling back to CPU")

    for key in AUTO_ORDER:
        encoder = ENCODERS[key]
        if _works(ffmpeg, encoder):
            if bus:
                where = "hardware" if encoder.hardware else "CPU"
                bus.info(f"encoder: {encoder.label} [{where}]")
            return encoder

    if bus:
        bus.info(f"encoder: {X264.label}")
    return X264
