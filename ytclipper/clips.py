"""Cut finished .mp4 clips out of the buffered segments."""
from __future__ import annotations

import glob
import os
import queue
import threading
import time
from dataclasses import dataclass, field

from .buffer import SegmentBuffer
from .captions import to_ass, to_srt
from .encode import X264, Encoder
from .util import (
    LogBus,
    ffprobe_duration,
    ffprobe_size,
    human_size,
    local_stamp,
    read_json,
    run,
    slug,
    tail_text,
    write_json,
)


@dataclass
class ClipJob:
    t0: float
    t1: float
    reason: str = "manual"
    category: str = "manual"
    detail: str = ""
    created: float = field(default_factory=time.time)


@dataclass
class VerticalJob:
    source: str
    mode: str = "blur"
    created: float = field(default_factory=time.time)


def vertical_filter(mode: str) -> str:
    """ffmpeg filter graph producing a 1080x1920 frame."""
    if mode == "crop":
        return (
            "[0:v]scale=1080:1920:force_original_aspect_ratio=increase,"
            "crop=1080:1920,setsar=1[v]"
        )
    return (
        "[0:v]split=2[bg][fg];"
        "[bg]scale=1080:1920:force_original_aspect_ratio=increase,"
        "crop=1080:1920,gblur=sigma=40,setsar=1[bgb];"
        "[fg]scale=1080:-2,setsar=1[fgs];"
        "[bgb][fgs]overlay=(W-w)/2:(H-h)/2[v]"
    )


def delete_clip(clips_dir: str, name: str) -> bool:
    """Delete a clip, or a vertical, plus its sidecars."""
    name = os.path.basename(name)
    if not name.endswith(".mp4"):
        return False
    removed = False
    stem = os.path.join(clips_dir, name[:-4])
    for suffix in (".mp4", ".json", ".jpg", ".srt"):
        target = stem + suffix
        if os.path.isfile(target):
            try:
                os.remove(target)
                removed = True
            except OSError:
                pass
    # remove any 9:16 renders of this clip
    for sidecar in glob.glob(os.path.join(clips_dir, "*_vertical.json")):
        meta = read_json(sidecar)
        if meta.get("vertical_of") != name:
            continue
        for suffix in (".mp4", ".json", ".jpg"):
            target = sidecar[: -len(".json")] + suffix
            if os.path.isfile(target):
                try:
                    os.remove(target)
                    removed = True
                except OSError:
                    pass
    return removed


class ClipWorker(threading.Thread):
    """Single background worker: waits for the tail of a clip to be recorded,
    then cuts and encodes it."""

    def __init__(
        self,
        buffer: SegmentBuffer,
        clips_dir: str,
        bus: LogBus,
        ffmpeg: str,
        ffprobe: str,
        stream_title=lambda: "",
        captioner=None,
        encoder: Encoder = X264,
        vertical_mode: str = "blur",
        vertical_auto: bool = False,
    ) -> None:
        super().__init__(name="clip-worker", daemon=True)
        self.buffer = buffer
        self.clips_dir = clips_dir
        self.bus = bus
        self.ffmpeg = ffmpeg
        self.ffprobe = ffprobe
        self.stream_title = stream_title
        self.captioner = captioner
        self.encoder = encoder
        self.vertical_mode = vertical_mode if vertical_mode in ("blur", "crop") else "blur"
        self.vertical_auto = vertical_auto
        self.jobs: queue.Queue = queue.Queue()
        self.busy = False
        self.completed = 0
        self.failed = 0
        self.verticals = 0
        self._stop = threading.Event()
        os.makedirs(clips_dir, exist_ok=True)

    def submit(self, job: ClipJob) -> None:
        self.jobs.put(job)
        wait = max(0.0, job.t1 - self.buffer.now())
        self.bus.clip(
            f"queued clip [{job.reason}] {max(0.0, job.t1 - job.t0):.0f}s "
            f"(ready in ~{wait:.0f}s)"
        )

    def submit_vertical(self, name: str, mode: str = "") -> None:
        job = VerticalJob(source=os.path.basename(name), mode=mode or self.vertical_mode)
        self.jobs.put(job)
        self.bus.clip(f"queued 9:16 export for {job.source}")

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                job = self.jobs.get(timeout=0.5)
            except queue.Empty:
                continue
            self.busy = True
            try:
                if isinstance(job, VerticalJob):
                    self._process_vertical(job)
                    self.verticals += 1
                else:
                    self._process(job)
                    self.completed += 1
            except Exception as exc:
                self.failed += 1
                kind = "9:16 export" if isinstance(job, VerticalJob) else "clip"
                self.bus.error(f"{kind} failed: {exc}")
            finally:
                self.busy = False
                self.jobs.task_done()

    # -- internals ---------------------------------------------------------- #
    def _process(self, job: ClipJob) -> None:
        deadline = time.time() + max(30.0, (job.t1 - self.buffer.now()) + 45.0)
        complete = False
        got0, got1 = job.t0, job.t1
        while time.time() < deadline and not self._stop.is_set():
            self.buffer.refresh()
            complete, got0, got1 = self.buffer.covered(job.t0, job.t1)
            if complete:
                break
            time.sleep(0.75)

        if not complete:
            if got1 - got0 < 1.5:
                raise RuntimeError(
                    "buffer did not contain that window (recording may have just started)"
                )
            self.bus.warn(
                f"clip truncated to available buffer ({got1 - got0:.0f}s) - "
                "increase --buffer for longer clips"
            )

        start = max(job.t0, got0 - 0.15)
        end = min(job.t1, got1)
        parts = self.buffer.take(start, end)
        if not parts:
            raise RuntimeError("no segments in the requested window")

        paths = [seg.path for seg, _, _ in parts]
        try:
            self._encode(job, parts, start, end, complete)
        finally:
            self.buffer.unprotect(paths)

    def _encode(self, job, parts, start, end, complete) -> None:
        work = os.path.join(self.clips_dir, ".work")
        os.makedirs(work, exist_ok=True)
        token = f"{int(time.time() * 1000)}-{threading.get_ident()}"
        listfile = os.path.join(work, f"list-{token}.txt")
        part = os.path.join(work, f"part-{token}.mkv")
        temp_mp4 = os.path.join(work, f"out-{token}.mp4")
        temp_jpg = os.path.join(work, f"out-{token}.jpg")

        try:
            with open(listfile, "w", encoding="utf-8") as handle:
                for seg, _inpoint, _dur in parts:
                    safe = seg.path.replace("'", "'\\''")
                    handle.write(f"file '{safe}'\n")

            # 1) stitch the raw segments losslessly
            code, _out, err = run(
                [
                    self.ffmpeg,
                    "-v",
                    "error",
                    "-nostdin",
                    "-y",
                    "-fflags",
                    "+genpts",
                    "-f",
                    "concat",
                    "-safe",
                    "0",
                    "-i",
                    listfile,
                    "-c",
                    "copy",
                    part,
                ],
                timeout=180,
            )
            if code != 0 or not os.path.isfile(part) or os.path.getsize(part) < 1000:
                raise RuntimeError(f"could not join segments: {tail_text(err)}")

            # 2) optional captions: transcribe exactly the clip window
            offset = max(0.0, start - parts[0][0].start)
            duration = max(0.5, end - start)
            cues = []
            ass_name = None
            if self.captioner is not None and self.captioner.enabled:
                try:
                    cues = self.captioner.transcribe(part, offset, duration)
                except Exception as exc:
                    self.bus.warn(f"caption pass failed: {exc}")
                if cues and self.captioner.mode == "burn":
                    width, height = ffprobe_size(self.ffprobe, part)
                    ass_name = f"cap-{token}.ass"
                    with open(
                        os.path.join(work, ass_name), "w", encoding="utf-8"
                    ) as handle:
                        handle.write(to_ass(cues, width, height, self.captioner.box))

            # 3) frame-accurate trim + re-encode so the clip starts exactly on time
            base_vf = "scale=trunc(iw/2)*2:trunc(ih/2)*2"
            with_subs = base_vf + (f",ass={ass_name}" if ass_name else "")
            attempts: list[tuple[Encoder, str, bool, bool]] = []
            if ass_name:
                attempts.append((self.encoder, with_subs, True, True))
            attempts.append((self.encoder, base_vf, False, True))
            if self.encoder.key != "x264":
                if ass_name:
                    attempts.append((X264, with_subs, True, False))
                attempts.append((X264, base_vf, False, False))

            ok = False
            burned = False
            err = ""
            for encoder, vf, wants_subs, gpu in attempts:
                code, _out, err = self._encode_clip(
                    part, offset, duration, vf, encoder.args, temp_mp4, work
                )
                good = (
                    code == 0
                    and os.path.isfile(temp_mp4)
                    and os.path.getsize(temp_mp4) >= 1000
                )
                if good:
                    burned = wants_subs
                    ok = True
                    if encoder.key != self.encoder.key:
                        self.bus.warn(
                            f"{self.encoder.label} failed - encoded with {encoder.label}"
                        )
                    break
                if wants_subs:
                    self.bus.warn(
                        f"burning subtitles failed ({tail_text(err)}) - retrying without them"
                    )
            if not ok:
                raise RuntimeError(f"encode failed: {tail_text(err)}")

            # 4) publish atomically: metadata + thumb + subs first, media last,
            #    so the UI never lists a clip that has no duration or preview.
            media_duration = ffprobe_duration(self.ffprobe, temp_mp4) or duration
            size = os.path.getsize(temp_mp4)
            self._thumbnail(temp_mp4, temp_jpg, media_duration)
            name = self._clip_name(job)
            final_mp4 = os.path.join(self.clips_dir, name)
            if os.path.isfile(temp_jpg):
                os.replace(temp_jpg, final_mp4[:-4] + ".jpg")
            if cues:
                with open(final_mp4[:-4] + ".srt", "w", encoding="utf-8") as handle:
                    handle.write(to_srt(cues))
            write_json(
                final_mp4[:-4] + ".json",
                {
                    "file": name,
                    "reason": job.reason,
                    "category": job.category,
                    "detail": job.detail,
                    "duration": round(media_duration, 2),
                    "size": size,
                    "created": time.time(),
                    "created_local": local_stamp(),
                    "stream": self.stream_title(),
                    "requested_from": round(job.t0, 2),
                    "requested_to": round(job.t1, 2),
                    "complete_window": complete,
                    "subtitles": bool(cues),
                    "subtitles_burned": burned,
                    "subtitle_language": (
                        self.captioner.last_language if self.captioner else None
                    ),
                    "caption_model": self.captioner.model_name if self.captioner else None,
                    "encoder": self.encoder.key,
                },
            )
            os.replace(temp_mp4, final_mp4)
            extra = " +captions" if burned else ""
            self.bus.clip(
                f"saved {name}  {media_duration:.1f}s  {human_size(size)}{extra}"
            )
            if self.vertical_auto:
                self.submit_vertical(name)
        finally:
            for leftover in (listfile, part, temp_mp4, temp_jpg):
                try:
                    os.remove(leftover)
                except OSError:
                    pass
            for stray in glob.glob(os.path.join(work, f"cap-{token}.ass")):
                try:
                    os.remove(stray)
                except OSError:
                    pass

    def _encode_clip(self, part, offset, duration, vf, codec_args, temp_mp4, work):
        return run(
            [
                self.ffmpeg,
                "-v",
                "error",
                "-nostdin",
                "-y",
                "-i",
                part,
                "-ss",
                f"{offset:.3f}",
                "-t",
                f"{duration:.3f}",
                "-map",
                "0:v:0",
                "-map",
                "0:a:0?",
                *codec_args,
                "-pix_fmt",
                "yuv420p",
                "-vf",
                vf,
                "-c:a",
                "aac",
                "-b:a",
                "160k",
                "-ar",
                "48000",
                "-ac",
                "2",
                "-movflags",
                "+faststart",
                "-max_muxing_queue_size",
                "2048",
                temp_mp4,
            ],
            timeout=900,
            cwd=work,
        )

    # -- 9:16 --------------------------------------------------------------- #
    def _process_vertical(self, job: VerticalJob) -> None:
        source = os.path.join(self.clips_dir, job.source)
        if not os.path.isfile(source):
            raise RuntimeError("that clip no longer exists")

        work = os.path.join(self.clips_dir, ".work")
        os.makedirs(work, exist_ok=True)
        token = f"{int(time.time() * 1000)}-{threading.get_ident()}"
        temp_mp4 = os.path.join(work, f"vert-{token}.mp4")
        temp_jpg = os.path.join(work, f"vert-{token}.jpg")
        name = job.source[:-4] + "_vertical.mp4"
        final_mp4 = os.path.join(self.clips_dir, name)
        graph = vertical_filter(job.mode)

        encoders = [self.encoder]
        if self.encoder.key != "x264":
            encoders.append(X264)

        ok = False
        err = ""
        try:
            for encoder in encoders:
                code, _out, err = run(
                    [
                        self.ffmpeg,
                        "-v",
                        "error",
                        "-nostdin",
                        "-y",
                        "-i",
                        source,
                        "-filter_complex",
                        graph,
                        "-map",
                        "[v]",
                        "-map",
                        "0:a:0?",
                        *encoder.args,
                        "-pix_fmt",
                        "yuv420p",
                        "-c:a",
                        "copy",
                        "-movflags",
                        "+faststart",
                        "-max_muxing_queue_size",
                        "2048",
                        temp_mp4,
                    ],
                    timeout=900,
                    cwd=work,
                )
                if (
                    code == 0
                    and os.path.isfile(temp_mp4)
                    and os.path.getsize(temp_mp4) > 1000
                ):
                    ok = True
                    if encoder.key != self.encoder.key:
                        self.bus.warn(
                            f"{self.encoder.label} failed - 9:16 used {encoder.label}"
                        )
                    break
            if not ok:
                raise RuntimeError(f"9:16 encode failed: {tail_text(err)}")

            duration = ffprobe_duration(self.ffprobe, temp_mp4)
            size = os.path.getsize(temp_mp4)
            self._thumbnail(temp_mp4, temp_jpg, duration)
            if os.path.isfile(temp_jpg):
                os.replace(temp_jpg, final_mp4[:-4] + ".jpg")
            write_json(
                final_mp4[:-4] + ".json",
                {
                    "file": name,
                    "vertical_of": job.source,
                    "mode": job.mode,
                    "duration": round(duration, 2),
                    "size": size,
                    "created": time.time(),
                    "created_local": local_stamp(),
                    "stream": self.stream_title(),
                    "encoder": self.encoder.key,
                },
            )
            os.replace(temp_mp4, final_mp4)
            self.bus.clip(
                f"saved {name}  9:16  {duration:.1f}s  {human_size(size)}"
            )
        finally:
            for leftover in (temp_mp4, temp_jpg):
                try:
                    os.remove(leftover)
                except OSError:
                    pass

    def _clip_name(self, job: ClipJob) -> str:
        base = time.strftime("%Y%m%d_%H%M%S") + "_" + slug(job.reason, 24)
        candidate = f"clip_{base}.mp4"
        counter = 2
        while os.path.exists(os.path.join(self.clips_dir, candidate)):
            candidate = f"clip_{base}_{counter}.mp4"
            counter += 1
        return candidate

    def _thumbnail(self, source: str, target: str, duration: float) -> None:
        stamp = min(0.5, max(0.0, duration / 3.0))
        run(
            [
                self.ffmpeg,
                "-v",
                "error",
                "-y",
                "-ss",
                f"{stamp:.2f}",
                "-i",
                source,
                "-frames:v",
                "1",
                "-vf",
                "scale=560:-2",
                target,
            ],
            timeout=60,
        )


def list_clips(clips_dir: str) -> list[dict]:
    if not os.path.isdir(clips_dir):
        return []
    paths = glob.glob(os.path.join(clips_dir, "*.mp4"))
    paths.sort(key=os.path.getmtime, reverse=True)
    hints = {
        "audio": "loud moment",
        "chat": "chat spike",
        "manual": "manual cut",
    }

    clips: list[dict] = []
    verticals: dict[str, dict] = {}

    for path in paths[:400]:
        name = os.path.basename(path)
        meta = read_json(path[:-4] + ".json")
        try:
            size = os.path.getsize(path)
        except OSError:
            size = 0
        thumb = (
            f"/thumbs/{name[:-4]}.jpg"
            if os.path.isfile(path[:-4] + ".jpg")
            else None
        )

        if meta.get("vertical_of"):
            verticals[meta["vertical_of"]] = {
                "name": name,
                "url": f"/clips/{name}",
                "thumb": thumb,
                "size": size,
                "size_human": human_size(size),
                "duration": meta.get("duration"),
                "mode": meta.get("mode", "blur"),
            }
            continue

        clips.append(
            {
                "name": name,
                "url": f"/clips/{name}",
                "thumb": thumb,
                "srt": f"/subs/{name[:-4]}.srt"
                if os.path.isfile(path[:-4] + ".srt")
                else None,
                "subtitles": bool(meta.get("subtitles")),
                "size": size,
                "size_human": human_size(size),
                "duration": meta.get("duration"),
                "reason": meta.get("reason") or hints.get(meta.get("category"), "clip"),
                "detail": meta.get("detail", ""),
                "created": meta.get("created") or os.path.getmtime(path),
                "created_local": meta.get("created_local")
                or local_stamp(os.path.getmtime(path)),
                "stream": meta.get("stream", ""),
                "category": meta.get("category", ""),
                "complete": meta.get("complete_window", True),
                "vertical": None,
            }
        )
        if len(clips) >= 200:
            break

    for clip in clips:
        clip["vertical"] = verticals.get(clip["name"])
    return clips
