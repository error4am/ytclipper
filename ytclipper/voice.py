"""Text-to-speech for the demo stream.

Kokoro is a small local neural TTS (82M params, onnxruntime) that sounds much
more natural than the built-in Windows voice. It needs two model files; if they
are missing or it fails, we fall back to SAPI, then to a plain tone.
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import wave

from .util import CREATE_NO_WINDOW, LogBus

MODEL_FILE = "kokoro-v1.0.onnx"
VOICES_FILE = "voices-v1.0.bin"
MODEL_MIN_BYTES = 150_000_000
VOICES_MIN_BYTES = 20_000_000
MODEL_URL = (
    "https://github.com/thewh1teagle/kokoro-onnx/releases/download/"
    "model-files-v1.0/kokoro-v1.0.onnx"
)
VOICES_URL = (
    "https://github.com/thewh1teagle/kokoro-onnx/releases/download/"
    "model-files-v1.0/voices-v1.0.bin"
)

# A few of the nicer built-in Kokoro voices.
VOICE_CHOICES = {
    "af_heart": "female, warm and clear (default)",
    "af_bella": "female, energetic",
    "af_nicole": "female, soft",
    "am_michael": "male, calm and professional",
    "am_adam": "male, deep",
    "bf_emma": "British female",
    "bm_george": "British male",
}


def _downloaded(model_dir: str) -> bool:
    for name, minimum in ((MODEL_FILE, MODEL_MIN_BYTES), (VOICES_FILE, VOICES_MIN_BYTES)):
        path = os.path.join(model_dir, name)
        try:
            if os.path.getsize(path) < minimum:
                return False
        except OSError:
            return False
    return True


def _download(model_dir: str, bus: LogBus) -> bool:
    """Fetch the Kokoro model files once (about 340 MB total)."""
    try:
        from urllib.request import urlretrieve
    except Exception:
        return False
    os.makedirs(model_dir, exist_ok=True)
    pairs = [
        (MODEL_FILE, MODEL_URL, MODEL_MIN_BYTES),
        (VOICES_FILE, VOICES_URL, VOICES_MIN_BYTES),
    ]
    for name, url, minimum in pairs:
        target = os.path.join(model_dir, name)
        try:
            if os.path.getsize(target) >= minimum:
                continue
        except OSError:
            pass
        bus.info(f"downloading kokoro {name} (one time, please wait)…")
        shown = [-10]

        def report(blocks: int, block_size: int, total: int) -> None:
            if total <= 0:
                return
            pct = int(min(100, blocks * block_size * 100 / total))
            if pct >= shown[0] + 10:
                shown[0] = pct - (pct % 10)
                bus.info(f"  {name}: {pct}%")

        try:
            urlretrieve(url, target + ".part", reporthook=report)
            os.replace(target + ".part", target)
        except Exception as exc:
            bus.warn(f"could not download {name}: {exc}")
            for leftover in (target + ".part",):
                try:
                    os.remove(leftover)
                except OSError:
                    pass
            return False
    return _downloaded(model_dir)


def _write_wav(path: str, samples, rate: int) -> None:
    import numpy as np

    audio = np.asarray(samples, dtype=np.float32).reshape(-1)
    peak = float(np.max(np.abs(audio))) if audio.size else 0.0
    if peak > 1.0:
        audio = audio / peak
    pcm = (audio * 32767.0).astype("<i2")
    with wave.open(path, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(int(rate))
        handle.writeframes(pcm.tobytes())


def _kokoro(
    text: str,
    out_dir: str,
    model_dir: str,
    voice: str,
    speed: float,
    bus: LogBus,
) -> str | None:
    if importlib.util.find_spec("kokoro_onnx") is None:
        bus.warn("kokoro-onnx is not installed (python -m pip install kokoro-onnx)")
        return None
    if not _downloaded(model_dir) and not _download(model_dir, bus):
        return None
    try:
        from kokoro_onnx import Kokoro

        engine = Kokoro(
            os.path.join(model_dir, MODEL_FILE), os.path.join(model_dir, VOICES_FILE)
        )
        samples, rate = engine.create(text, voice=voice, speed=speed, lang="en-us")
        path = os.path.join(out_dir, "voice.wav")
        _write_wav(path, samples, rate)
        seconds = len(samples) / float(rate)
        bus.info(f"demo voice: kokoro '{voice}' ({seconds:.1f}s of speech)")
        return path
    except Exception as exc:
        bus.warn(f"kokoro voice failed: {exc}")
        return None


def _sapi(text: str, out_dir: str, bus: LogBus) -> str | None:
    if os.name != "nt":
        return None
    path = os.path.join(out_dir, "voice.wav")
    script = os.path.join(out_dir, "speak.ps1")
    body = (
        "Add-Type -AssemblyName System.Speech\n"
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer\n"
        "$s.Rate = 0\n"
        f"$s.SetOutputToWaveFile('{path}')\n"
        f"$s.Speak('{text}')\n"
        "$s.Dispose()\n"
    )
    try:
        with open(script, "w", encoding="utf-8") as handle:
            handle.write(body)
        code = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=120,
            creationflags=CREATE_NO_WINDOW,
        ).returncode
        if code == 0 and os.path.isfile(path) and os.path.getsize(path) > 20000:
            bus.info("demo voice: windows speech")
            return path
    except Exception:
        pass
    return None


def _tone(out_dir: str, ffmpeg: str) -> str:
    path = os.path.join(out_dir, "tone.wav")
    subprocess.run(
        [
            ffmpeg,
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=330:sample_rate=44100:duration=8",
            "-c:a",
            "pcm_s16le",
            path,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=60,
        creationflags=CREATE_NO_WINDOW,
    )
    return path


def make_speech(
    text: str,
    out_dir: str,
    ffmpeg: str,
    bus: LogBus,
    model_dir: str,
    provider: str = "kokoro",
    voice: str = "af_heart",
    speed: float = 1.0,
) -> tuple[str, bool]:
    """Return (audio_path, spoken). spoken=False means it fell back to a tone."""
    os.makedirs(out_dir, exist_ok=True)
    if provider == "kokoro":
        path = _kokoro(text, out_dir, model_dir, voice, speed, bus)
        if path:
            return path, True
        bus.warn("kokoro unavailable - falling back to the built-in voice")
    if provider in ("kokoro", "sapi"):
        path = _sapi(text, out_dir, bus)
        if path:
            return path, True
    bus.warn("no speech voice available - using a tone (captions will be empty)")
    return _tone(out_dir, ffmpeg), False
