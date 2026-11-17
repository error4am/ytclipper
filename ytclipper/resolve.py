"""Turn a YouTube URL into direct media URLs ffmpeg can record."""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from .util import LogBus, run_cancellable, tail_text, yt_dlp_prefix


class ResolveError(RuntimeError):
    """Could not turn the page URL into playable streams."""


class NotLiveYet(ResolveError):
    """The stream is scheduled but has not started."""


@dataclass
class StreamInfo:
    title: str
    live_status: str
    webpage_url: str
    video_id: str | None = None
    inputs: list[dict] = field(default_factory=list)

    @property
    def is_live(self) -> bool:
        return self.live_status in ("is_live", "post_live")

    @property
    def ended(self) -> bool:
        return self.live_status in ("not_live", "was_live")


def _pick_formats(info: dict, quality: int) -> list[dict]:
    """Build the ffmpeg input list from a yt-dlp info dict."""
    requested = info.get("requested_formats") or []
    picked: list[dict] = []

    for fmt in requested:
        kind = "audio" if fmt.get("vcodec") in (None, "none") else "video"
        if fmt.get("acodec") not in (None, "none") and kind == "video":
            kind = "av"
        picked.append(
            {
                "url": fmt["url"],
                "kind": kind,
                "ua": (fmt.get("http_headers") or {}).get("User-Agent"),
                "label": fmt.get("format_id"),
                "vcodec": fmt.get("vcodec"),
                "acodec": fmt.get("acodec"),
            }
        )

    if not picked and info.get("url"):
        picked.append(
            {
                "url": info["url"],
                "kind": "av",
                "ua": (info.get("http_headers") or {}).get("User-Agent"),
                "label": info.get("format_id"),
                "vcodec": info.get("vcodec"),
                "acodec": info.get("acodec"),
            }
        )

    # Drop duplicate URLs some extractors report twice.
    seen: set[str] = set()
    unique = []
    for item in picked:
        if item["url"] in seen:
            continue
        seen.add(item["url"])
        unique.append(item)
    return unique


def resolve(
    url: str,
    quality: int,
    bus: LogBus,
    cookies_from_browser: str | None = None,
    cookies_file: str | None = None,
    extra_header: str | None = None,
    cancel=None,
) -> StreamInfo:
    prefix = yt_dlp_prefix()
    if prefix is None:
        raise ResolveError(
            "yt-dlp is not installed. Run: python -m pip install -U yt-dlp"
        )

    fmt = (
        f"bv*[height<={quality}]+ba/b[height<={quality}]/bv*+ba/b"
    )
    cmd = prefix + [
        "-J",
        "--no-playlist",
        "--no-warnings",
        "--no-progress",
        "--no-call-home",
        "-f",
        fmt,
    ]
    if cookies_from_browser:
        cmd += ["--cookies-from-browser", cookies_from_browser]
    if cookies_file:
        cmd += ["--cookies", cookies_file]
    cmd.append(url)

    code, out, err = run_cancellable(cmd, timeout=90, cancel=cancel)
    if code == 499:
        raise ResolveError("cancelled")
    if code != 0 or not out.strip():
        raise ResolveError(f"yt-dlp could not read that URL: {tail_text(err) or 'no output'}")

    try:
        info = json.loads(out)
    except json.JSONDecodeError as exc:
        raise ResolveError(f"yt-dlp returned unreadable data: {exc}") from exc

    if info.get("_type") == "playlist":
        entries = [e for e in (info.get("entries") or []) if e]
        if not entries:
            raise ResolveError("that URL is a playlist with no playable videos")
        info = entries[0]

    status = info.get("live_status") or ("is_live" if info.get("is_live") else "not_live")
    title = info.get("title") or "Untitled stream"
    video_id = info.get("id")

    if status == "is_upcoming":
        raise NotLiveYet(f"'{title}' has not started yet")

    inputs = _pick_formats(info, quality)
    if not inputs:
        reason = info.get("availability") or status or "unknown"
        raise ResolveError(f"no playable formats for '{title}' (availability: {reason})")

    for item in inputs:
        if extra_header:
            item["extra_header"] = extra_header

    bus.info(
        f"stream resolved: {title} [{status}] -> {len(inputs)} input(s)"
    )
    return StreamInfo(
        title=title,
        live_status=status,
        webpage_url=info.get("webpage_url") or url,
        video_id=video_id,
        inputs=inputs,
    )


def direct_info(url: str) -> StreamInfo:
    """Use a raw media URL (m3u8 / mp4 / local test stream) without yt-dlp."""
    return StreamInfo(
        title=url.rsplit("/", 1)[-1] or url,
        live_status="is_live",
        webpage_url=url,
        video_id=None,
        inputs=[{"url": url, "kind": "av", "ua": None, "label": "direct"}],
    )
