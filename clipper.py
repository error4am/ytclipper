#!/usr/bin/env python3
"""ytclipper - watch a YouTube live stream and cut clips while it streams.

    python clipper.py "https://www.youtube.com/watch?v=XXXX"

Then open http://127.0.0.1:8765 and hit CLIP, or let auto-detection do it.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
import webbrowser

from ytclipper.config import Config
from ytclipper.engine import Engine
from ytclipper.resolve import NotLiveYet, ResolveError, direct_info, resolve
from ytclipper.security import Auth, generate_password, is_loopback
from ytclipper.util import LogBus, find_exe, free_port
from ytclipper.web import serve

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(HERE, "static")


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Watch a YouTube live stream and cut clips while it streams.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "url",
        nargs="?",
        help="YouTube live/video URL (or a direct .m3u8 with --direct). Omit with --test",
    )
    parser.add_argument(
        "--test",
        action="store_true",
        help="run a local synthetic live stream instead of a URL (no network needed)",
    )

    demo = parser.add_argument_group("demo voice (only used with --test)")
    demo.add_argument(
        "--voice",
        choices=("kokoro", "sapi", "tone"),
        default="kokoro",
        help="speech engine for the fake stream",
    )
    demo.add_argument("--voice-name", default="af_heart", help="kokoro voice id, e.g. am_michael")
    demo.add_argument("--voice-speed", type=float, default=1.0, help="speech speed")

    clip = parser.add_argument_group("clip window")
    clip.add_argument("--before", type=float, default=18.0, help="seconds kept before the moment")
    clip.add_argument("--after", type=float, default=12.0, help="seconds kept after the moment")
    clip.add_argument("--seg-time", type=float, default=4.0, help="seconds per buffered segment")
    clip.add_argument("--buffer", type=float, default=420.0, help="seconds of stream kept on disk")
    clip.add_argument("--out", default="clips", help="folder for finished .mp4 clips")

    detect = parser.add_argument_group("auto detection")
    detect.add_argument("--no-auto", action="store_true", help="start with auto clipping off")
    detect.add_argument("--db-margin", type=float, default=8.5, help="loudness jump needed (dB)")
    detect.add_argument("--floor-db", type=float, default=-34.0, help="ignore anything quieter than this")
    detect.add_argument("--cooldown", type=float, default=45.0, help="min seconds between auto clips")
    detect.add_argument("--max-per-hour", type=int, default=40, help="hard cap on auto clips")
    detect.add_argument("--no-chat", action="store_true", help="disable live chat spike detection")

    cap = parser.add_argument_group("captions")
    cap.add_argument("--no-captions", action="store_true", help="do not transcribe clips")
    cap.add_argument("--caption-model", default="base", help="whisper model: tiny, base, small, medium")
    cap.add_argument("--caption-lang", default=None, help="force a language code, e.g. en (default: auto)")
    cap.add_argument("--caption-device", default="cpu", help="cpu or cuda")
    cap.add_argument("--caption-compute", default="int8", help="int8, float16, float32")
    cap.add_argument("--caption-box", action="store_true", help="draw captions in a solid box")
    cap.add_argument(
        "--caption-mode",
        choices=("burn", "srt"),
        default="burn",
        help="burn into the video, or only write the .srt file",
    )

    src = parser.add_argument_group("source")
    src.add_argument("--quality", type=int, default=1080, help="max video height")
    src.add_argument("--direct", action="store_true", help="treat URL as a direct media URL (skip yt-dlp)")
    src.add_argument("--cookies-from-browser", help="e.g. chrome, firefox, edge")
    src.add_argument("--cookies", help="path to a cookies.txt file")
    src.add_argument("--extra-header", help='extra ffmpeg header, e.g. "Referer: https://example.com"')
    src.add_argument("--refresh", type=float, default=3600.0, help="seconds before re-resolving the stream URL")

    ui = parser.add_argument_group("web UI")
    ui.add_argument("--port", type=int, default=8765)
    ui.add_argument("--host", default="127.0.0.1")
    ui.add_argument("--no-open", action="store_true", help="do not open the browser")
    ui.add_argument("--check", action="store_true", help="resolve the stream, print info, exit")

    out = parser.add_argument_group("encoding & output")
    out.add_argument(
        "--encoder",
        choices=("auto", "nvenc", "qsv", "amf", "x264"),
        default="auto",
        help="clip encoder; auto picks the fastest that works",
    )
    out.add_argument("--hotkey", default="F8", help="system-wide clip hotkey (F8, ctrl+alt+c, ...)")
    out.add_argument("--no-hotkey", action="store_true", help="do not register a global hotkey")
    out.add_argument(
        "--vertical-mode",
        choices=("blur", "crop"),
        default="blur",
        help="9:16 framing: blurred background or full-bleed crop",
    )
    out.add_argument(
        "--vertical-auto",
        action="store_true",
        help="render a 9:16 version of every clip automatically",
    )
    out.add_argument(
        "--no-preview", action="store_true", help="disable the live preview panel"
    )

    sec = parser.add_argument_group("security")
    sec.add_argument("--password", help="require this password to open the dashboard")
    sec.add_argument(
        "--generate-password",
        action="store_true",
        help="generate a random dashboard password and print it",
    )
    sec.add_argument("--tls-cert", help="PEM certificate file to serve HTTPS")
    sec.add_argument("--tls-key", help="PEM private key file to serve HTTPS")
    sec.add_argument(
        "--allowed-host",
        action="append",
        help="extra hostname accepted in the Host header (for reverse proxies)",
    )

    parser.add_argument("--work-dir", default=os.path.join(HERE, ".clipper"), help="buffer/scratch folder")
    parser.add_argument("--ffmpeg", default="", help="path to ffmpeg if it is not on PATH")
    return parser.parse_args(argv)


def require_tools(args: argparse.Namespace, bus: LogBus) -> tuple[str, str]:
    ffmpeg = args.ffmpeg or find_exe("ffmpeg")
    if not ffmpeg:
        sys.exit(
            "ffmpeg was not found.\n"
            "Install it (winget install Gyan.FFmpeg) or pass --ffmpeg <path-to-ffmpeg.exe>."
        )
    ffprobe = find_exe("ffprobe") or os.path.join(
        os.path.dirname(ffmpeg), "ffprobe.exe" if os.name == "nt" else "ffprobe"
    )
    if not os.path.isfile(ffprobe):
        bus.warn("ffprobe missing - segment durations will be estimated")
        ffprobe = ""
    return ffmpeg, ffprobe


def main(argv=None) -> int:
    args = parse_args(argv)
    bus = LogBus()
    ffmpeg, ffprobe = require_tools(args, bus)

    password = args.password or os.environ.get("YTCLIPPER_PASSWORD") or ""
    if args.generate_password and not password:
        password = generate_password()
        print()
        print(f"  dashboard password: {password}")
        print()
    auth = Auth(password or None, secure_cookie=bool(args.tls_cert))

    if bool(args.tls_cert) != bool(args.tls_key):
        print("--tls-cert and --tls-key must be used together")
        return 2

    if not is_loopback(args.host) and not auth.enabled:
        print(
            "refusing to listen on a public interface without a password.\n"
            f"  python clipper.py --host {args.host} --password YOUR_PASSWORD\n"
            "or let it generate one:\n"
            f"  python clipper.py --host {args.host} --generate-password"
        )
        return 2

    if args.check:
        if not args.url:
            print("--check needs a stream URL")
            return 2
        if args.direct:
            info = direct_info(args.url)
        else:
            try:
                info = resolve(
                    args.url,
                    quality=args.quality,
                    bus=bus,
                    cookies_from_browser=args.cookies_from_browser,
                    cookies_file=args.cookies,
                )
            except (ResolveError, NotLiveYet) as exc:
                print(f"cannot use this stream: {exc}")
                return 1
        print(f"title : {info.title}")
        print(f"status: {info.live_status}")
        print(f"inputs: {len(info.inputs)}")
        for item in info.inputs:
            print(f"  - {item.get('kind')} ({item.get('label')})")
        return 0

    demo = None
    if args.test:
        from ytclipper.demo import DemoStream

        demo = DemoStream(
            os.path.join(HERE, ".clipper", "demo"),
            ffmpeg,
            bus,
            voice_provider=args.voice,
            voice_name=args.voice_name,
            voice_speed=args.voice_speed,
            model_dir=os.path.join(HERE, ".models", "kokoro"),
        )
        try:
            args.url = demo.start()
        except Exception as exc:
            print(f"could not start the demo stream: {exc}")
            return 1
        args.direct = True
        # quicker feedback for a test run, unless the flags were set explicitly
        if args.before == 18.0:
            args.before = 6.0
        if args.after == 12.0:
            args.after = 4.0
        if args.seg_time == 4.0:
            args.seg_time = 2.0
        if args.cooldown == 45.0:
            args.cooldown = 20.0
        bus.info(f"demo stream live at {args.url} (a loud burst every ~16s)")

    engine = None
    try:
        cfg = Config(
            url=args.url,
            out_dir=args.out,
            work_dir=args.work_dir,
            before=args.before,
            after=args.after,
            seg_time=max(1.0, args.seg_time),
            buffer_seconds=args.buffer,
            quality=args.quality,
            auto=not args.no_auto,
            chat=not args.no_chat,
            captions=not args.no_captions,
            caption_model=args.caption_model,
            caption_language=args.caption_lang,
            caption_device=args.caption_device,
            caption_compute=args.caption_compute,
            caption_box=args.caption_box,
            caption_mode=args.caption_mode,
            db_margin=args.db_margin,
            floor_db=args.floor_db,
            cooldown=max(5.0, args.cooldown),
            max_per_hour=max(1, args.max_per_hour),
            direct=args.direct,
            cookies_from_browser=args.cookies_from_browser,
            cookies_file=args.cookies,
            extra_header=args.extra_header,
            refresh=max(300.0, args.refresh),
            host=args.host,
            port=args.port,
            open_browser=not args.no_open,
            encoder=args.encoder,
            hotkey=args.hotkey,
            hotkey_enabled=not args.no_hotkey,
            vertical_mode=args.vertical_mode,
            vertical_auto=args.vertical_auto,
            preview=not args.no_preview,
            models_dir=os.path.join(HERE, ".models"),
            voice=args.voice,
            voice_name=args.voice_name,
            voice_speed=args.voice_speed,
            ffmpeg=ffmpeg,
            ffprobe=ffprobe,
        )

        engine = Engine(cfg)
        engine.start()

        port = free_port(cfg.host, cfg.port)
        if port != cfg.port:
            bus.warn(f"port {cfg.port} busy, using {port} instead")
        serve(
            engine,
            cfg.host,
            port,
            STATIC,
            auth=auth,
            allowed_hosts=set(args.allowed_host or []),
            tls=(args.tls_cert, args.tls_key) if args.tls_cert else None,
        )

        scheme = "https" if args.tls_cert else "http"
        shown_host = "127.0.0.1" if cfg.host in ("0.0.0.0", "::") else cfg.host
        url = f"{scheme}://{shown_host}:{port}"
        print()
        print("  ytclipper is running")
        if demo:
            print("  source    : local demo stream (no YouTube needed)")
            print("              loud burst every ~16s, spoken lines looping")
        elif not args.url:
            print("  stream    : paste a URL in the dashboard to start watching")
        print(f"  dashboard : {url}")
        print(f"  clips     : {cfg.clips_dir}")
        print(f"  buffer    : {cfg.buffer_seconds:.0f}s of stream on disk")
        if auth.enabled:
            print("  login     : password required")
        elif not is_loopback(cfg.host):
            print("  login     : NONE - this is exposed to your network!")
        print("  press Ctrl+C to stop")
        print()

        if cfg.open_browser:
            try:
                webbrowser.open(url)
            except Exception:
                pass

        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        print()
        bus.info("shutting down…")
    finally:
        if engine is not None:
            engine.stop()
        if demo is not None:
            demo.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
