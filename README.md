# ytclipper

**Clip a YouTube live stream while it is still streaming.**

It records the stream into a rolling buffer, watches for hype (loud moments, chat
spikes), and saves ready-to-post `.mp4` clips that include the seconds *before*
the moment — the part you would otherwise have missed. Captions are transcribed
locally, and any clip can be exported as vertical 9:16 for Shorts, Reels or
TikTok.

Everything runs on your machine. No API keys, no uploads, no cloud.

---

## Contents

- [What it does](#what-it-does)
- [Install](#install)
- [Quick start](#quick-start)
- [Using the dashboard](#using-the-dashboard)
- [What you get on disk](#what-you-get-on-disk)
- [How it works](#how-it-works)
- [Auto clipping & tuning](#auto-clipping--tuning)
- [Captions](#captions)
- [Vertical clips (9:16)](#vertical-clips-916)
- [Speed: hardware encoding](#speed-hardware-encoding)
- [Live preview](#live-preview)
- [Normal videos (non-live)](#normal-videos-non-live)
- [Going online (security)](#going-online-security)
- [CLI reference](#cli-reference)
- [Troubleshooting](#troubleshooting)
- [Project layout](#project-layout)
- [Limits](#limits)
- [License](#license)

---

## What it does

| | |
| --- | --- |
| **Rolling buffer** | Keeps the last 7 minutes of stream on disk as small segments, so a clip can reach backwards in time. |
| **Auto clipping** | Cuts a clip when the audio jumps well above its rolling baseline, or when live chat rate spikes. |
| **Manual clipping** | The big button, `space` in the dashboard, or **F8 from any window** (works over fullscreen games). |
| **Captions** | Local Whisper transcription, burned in and saved as `.srt`. |
| **9:16 export** | One click per clip: blurred-background or full-bleed vertical 1080×1920. |
| **Hardware encoding** | Picks NVENC / Quick Sync / AMF automatically, falls back to CPU. |
| **Live preview** | See the stream inside the dashboard while you clip. |
| **Hardened dashboard** | Password login, SSRF guard, CSRF/host checks, TLS, attack test suite. |

---

## Install

### 1. Python packages

```powershell
python -m pip install -U yt-dlp faster-whisper
```

Optional:

```powershell
python -m pip install kokoro-onnx      # nicer voice for the --test demo stream
python -m pip install chat-downloader  # chat-spike detection
```

### 2. ffmpeg

Windows:

```powershell
winget install Gyan.FFmpeg
```

Any platform: make sure both `ffmpeg` and `ffprobe` are on `PATH`, or pass
`--ffmpeg "C:\path\to\ffmpeg.exe"`.

### 3. Check

```powershell
ffmpeg -version
python -m yt_dlp --version
```

**First run** downloads models once: Whisper `base` (~145 MB) for captions, and
Kokoro (~340 MB) only if you use the demo stream.

---

## Quick start

```powershell
cd path\to\clipper

# 1. open the dashboard, then paste a URL into the bar at the top
python clipper.py

# 2. or start watching immediately
python clipper.py "https://www.youtube.com/watch?v=XXXXXXXXXXX"
```

Double-clicking **`run.bat`** does the same as option 1 — no console prompt.

### Try it without a live stream

```powershell
python clipper.py --test
```

That builds a fake live stream locally (spoken lines + a loud burst every ~16s),
so you can test clipping, captions, the hotkey and 9:16 in about a minute. It
cleans itself up on Ctrl+C. `demo.bat` does the same by double-click.

### Check a URL before committing

```powershell
python clipper.py "https://www.youtube.com/watch?v=XXXXXXXXXXX" --check
```

Prints the title, live status and how many media inputs were found.

---

## Using the dashboard

Open <http://127.0.0.1:8765> (it opens automatically).

### 1. Start watching

Paste a YouTube live URL into the bar and press **Enter** or **watch**. The
resolver takes a few seconds; the status line goes `starting → recording`.

- **cookies** dropdown — pick `chrome` / `edge` / `firefox` for members-only or
  age-restricted streams. Close that browser if the lookup fails.
- the bar remembers your last URL and cookie choice.
- **stop** ends recording without closing the app; pasting another URL switches
  streams.
- **demo** starts the local fake stream.

### 2. Clip

- **CLIP THAT** saves 18s before and 12s after the click (editable in the
  `before` / `after` boxes).
- **space** or **c** does the same.
- **F8** works globally, even when a game has focus. Change it with
  `--hotkey ctrl+alt+c`, disable with `--no-hotkey`.
- **auto detect** toggle — pause automatic clipping at any time.

Clips appear in the grid after the trailing seconds are recorded and encoded
(usually 10–30s).

### 3. Read the meter

The "room loudness" bar shows the current audio level, the rolling baseline
(violet), and the level that fires a clip (white). If the white line sits above
every peak, nothing will ever trigger — lower `--db-margin`.

### 4. Captions and 9:16

- clips with subtitles show a green **CC** tag and offer an `srt` download
- the **captions** switch turns transcription on/off live
- **9:16** on a card renders a vertical version; when ready the card shows a
  portrait preview with play/download

### 5. Activity log

The right column shows what the recorder, detectors, transcriber and encoder are
doing, including today's encoder choice and any stream errors.

---

## What you get on disk

Everything lands in `clips\` (change with `--out`):

```
clips\
  clip_20261117_033325_manual.mp4          the clip, H.264/AAC, ready to post
  clip_20261117_033325_manual.jpg          thumbnail
  clip_20261117_033325_manual.srt          subtitles (if transcribed)
  clip_20261117_033325_manual.json         metadata: reason, duration, stream, language
  clip_20261117_033325_manual_vertical.mp4 9:16 version, if exported
  clip_20261117_033325_manual_vertical.jpg
  clip_20261117_033325_manual_vertical.json
```

Names are `clip_<date>_<time>_<reason>.mp4` where reason is `manual`, `audio`
(loud moment) or `chat` (chat spike). Deleting a clip also deletes its vertical.

---

## How it works

```
YouTube ──yt-dlp──> direct media URLs
     │
     └─ ffmpeg ─> buffer\seg_sNN_YYYYmmdd_HHMMSS.mkv   (4s segments, rolling)
                        │
                        ├─ audio RMS per 0.5s ──> jump vs 90s baseline ─┐
                        ├─ live chat rate     ──> jump vs 2min baseline ─┤
                        └─ dashboard / F8     ─────────────────────────┤
                                                                       ▼
                                             wait for the trailing seconds
                                                                       │
                      stitch -> transcribe -> burn subtitles -> H.264 .mp4 (+ .srt)
```

* Segments are written with `-c copy`, so recording costs almost no CPU and
  keeps the original quality.
* Only the last `--buffer` seconds are kept, so disk use stays flat.
* Detection reads the finished segments, so trigger times line up exactly with
  what you heard. Expect a few seconds of latency.
* The stream URL is re-resolved hourly and the recorder reconnects if it drops.

---

## Auto clipping & tuning

Two independent triggers:

1. **Audio hype** — a 0.5s window that is `--db-margin` dB above the median of
   the last 90s, and louder than `--floor-db`.
2. **Chat hype** — message rate above 3× its rolling baseline (needs
   `chat-downloader`).

Guard rails: `--cooldown` seconds between automatic clips (default 45) and
`--max-per-hour` (default 40).

| Symptom | Fix |
| --- | --- |
| Too many clips | raise `--db-margin` (12) or `--cooldown` (90) |
| Missing moments | lower `--db-margin` (6) or `--floor-db` (-45) |
| Nothing ever fires | watch the meter; lower the margin until the white line sits under the peaks |
| Want faster reaction | `--seg-time 2` |

---

## Captions

Local [faster-whisper](https://github.com/SYSTRAN/faster-whisper) — no API key,
nothing uploaded.

```powershell
python clipper.py "<url>" --caption-model small --caption-lang en
```

| Flag | Meaning |
| --- | --- |
| `--caption-model` | `tiny`, `base` (default), `small`, `medium` |
| `--caption-lang` | force a language, e.g. `en`, `ur`, `es` |
| `--caption-mode srt` | keep the video clean, write only the `.srt` |
| `--caption-box` | solid background instead of an outline |
| `--caption-device cuda --caption-compute float16` | NVIDIA GPU |
| `--no-captions` | turn transcription off |

Captions cost roughly 0.5× the clip length on CPU with `base` (a 30s clip takes
~15s). If transcription or burning fails, the clip is still saved without them.

Note: subtitles are transcribed **in the spoken language**. `--caption-lang en`
on Urdu audio does not translate it, it mis-transcribes it. Translation is not
included.

---

## Vertical clips (9:16)

```powershell
# click 9:16 on any card, or:
python clipper.py "<url>" --vertical-auto             # every clip, blurred fill
python clipper.py "<url>" --vertical-mode crop        # full-bleed, cuts the sides
```

- `blur` (default) keeps the whole 16:9 frame centred over a blurred copy of
  itself — nothing is cropped, captions stay readable.
- `crop` fills 1080×1920 and loses the left/right edges, which can clip
  burned-in subtitles.
- Audio is copied, not re-encoded.

---

## Speed: hardware encoding

At startup the clipper proves each hardware encoder with a one-second test
encode (ffmpeg often advertises encoders the driver cannot run) and uses the
fastest that works:

```
encoder: h264_qsv (Intel Quick Sync) [hardware]
```

Measured on a 1080×1920 clip: **11.6s with libx264 vs 6.9s with Quick Sync**.
Force one with `--encoder nvenc|qsv|amf|x264`; if a GPU encode fails mid-clip,
it retries on CPU automatically.

---

## Live preview

The panel above the controls shows the stream itself. Browsers cannot play
YouTube's feed directly, so the clipper remuxes it into local 1-second HLS
segments and serves them from the dashboard (`hls.js` plays them).

- `-c copy` when the source is H.264/AAC: ~3% of one CPU core, no re-encode.
- automatic 540p transcode for AV1/VP9 sources.
- about 4–8 seconds behind live. Muted by default; hit **unmute**.
- costs one extra download of the stream — disable with `--no-preview`.

---

## Normal videos (non-live)

A regular YouTube video works, but it is treated like a live stream: it plays at
**1× real time**, buffers the last 7 minutes, and stops when the video ends.
There is no seeking or fast scanning — for long archives, let it run or clip
from the parts you watch.

---

## Going online (security)

By default the dashboard binds to **127.0.0.1** with no login. If you listen on
another interface, a password is **mandatory** — the app refuses to start
without one:

```powershell
# LAN / internet, password generated and printed once
python clipper.py --host 0.0.0.0 --generate-password

# your own password, plus the hostname if you use a domain
python clipper.py --host 0.0.0.0 --password "something-long" --allowed-host clips.mydomain.com

# serve HTTPS directly
python clipper.py --host 0.0.0.0 --password "..." --tls-cert cert.pem --tls-key key.pem
```

What is enforced:

- password login (PBKDF2-SHA256, 200k rounds), HttpOnly + SameSite=Strict session
  cookie, `Secure` under TLS
- brute-force lockout: 8 failures per IP per 10 minutes → `429`
- Host header validation (unknown hostnames → `421`) against DNS rebinding
- Origin checks on every state-changing request
- **SSRF guard**: stream URLs must be http(s) and must not resolve to private,
  loopback, link-local or reserved addresses
- path-traversal protection for clips, thumbs, subs, preview segments, static
- 64 KB body cap, 20s idle timeout
- strict CSP, `nosniff`, `X-Frame-Options: DENY`, `no-referrer`,
  `Permissions-Policy`
- **open folder** only works from localhost

Attack test suite:

```powershell
python tests\security_check.py
# all 36 checks passed
```

**Recommended for internet exposure:** put Caddy, nginx or a Cloudflare Tunnel
in front and bind to `127.0.0.1`, so TLS and rate limiting live at the edge.
Don't port-forward plain HTTP.

---

## CLI reference

### Source

| Flag | Default | Meaning |
| --- | --- | --- |
| `url` | – | YouTube live/video URL, or a direct media URL with `--direct` |
| `--test` | off | run the local demo stream instead of a URL |
| `--check` | off | resolve the stream, print info, exit |
| `--quality` | `1080` | max video height |
| `--direct` | off | treat the URL as a direct `.m3u8`/media URL |
| `--cookies-from-browser` | – | `chrome`, `edge`, `firefox`, … |
| `--cookies` | – | path to `cookies.txt` |
| `--refresh` | `3600` | re-resolve the stream URL after N seconds |

### Clip window

| Flag | Default | Meaning |
| --- | --- | --- |
| `--before` | `18` | seconds kept before the moment |
| `--after` | `12` | seconds kept after the moment |
| `--buffer` | `420` | seconds of stream kept on disk |
| `--seg-time` | `4` | segment length (lower = faster detection) |
| `--out` | `clips` | output folder |

### Auto detection

| Flag | Default | Meaning |
| --- | --- | --- |
| `--no-auto` | off | start with auto clipping disabled |
| `--db-margin` | `8.5` | loudness jump that counts as hype |
| `--floor-db` | `-34` | ignore anything quieter |
| `--cooldown` | `45` | min seconds between automatic clips |
| `--max-per-hour` | `40` | hard cap |
| `--no-chat` | off | disable chat-spike detection |

### Captions

| Flag | Default | Meaning |
| --- | --- | --- |
| `--no-captions` | off | disable transcription |
| `--caption-model` | `base` | `tiny`, `base`, `small`, `medium` |
| `--caption-lang` | auto | force a language |
| `--caption-mode` | `burn` | `burn` into the video, or `srt` only |
| `--caption-box` | off | solid caption background |
| `--caption-device` / `--caption-compute` | `cpu` / `int8` | `cuda` + `float16` for a GPU |

### Output & UI

| Flag | Default | Meaning |
| --- | --- | --- |
| `--encoder` | `auto` | `nvenc`, `qsv`, `amf`, `x264` |
| `--hotkey` / `--no-hotkey` | `F8` | system-wide clip hotkey |
| `--vertical-mode` | `blur` | `blur` or `crop` |
| `--vertical-auto` | off | 9:16 for every clip |
| `--no-preview` | off | disable the live preview |
| `--host` / `--port` | `127.0.0.1` / `8765` | dashboard binding |
| `--no-open` | off | don't open a browser |
| `--work-dir` | `.clipper` | buffer/scratch folder |
| `--ffmpeg` | – | explicit ffmpeg path |

### Security

| Flag | Meaning |
| --- | --- |
| `--password` | require a password |
| `--generate-password` | generate and print one |
| `--tls-cert` / `--tls-key` | serve HTTPS |
| `--allowed-host` | extra hostname allowed in the Host header |

### Demo only

| Flag | Default | Meaning |
| --- | --- | --- |
| `--voice` | `kokoro` | `kokoro`, `sapi` or `tone` |
| `--voice-name` | `af_heart` | e.g. `am_michael`, `bm_george` |
| `--voice-speed` | `1.0` | speech speed |

---

## Troubleshooting

**yt-dlp could not read that URL**
Update it: `python -m pip install -U yt-dlp`. For private streams use the
cookies dropdown or `--cookies-from-browser chrome`.

**`Could not copy Chrome cookie database`**
Chrome is running and holding its cookie file. Close it, or use edge/firefox
cookies.

**Only 720p available**
YouTube serves a lower ladder to non-browser clients. Try
`--cookies-from-browser chrome` so yt-dlp can request the higher formats.

**Clips say "truncated"**
The buffer did not cover the requested window (right after start or after a
reconnect). Increase `--buffer` or wait a minute.

**High CPU while clipping**
Caps are re-encoded for frame accuracy. Use hardware encoding (default), raise
`--cooldown`, or lower `--caption-model` to `tiny`.

**Dashboard asks for a password**
Only when you started with `--password`, `--generate-password`, or a non-local
`--host`. There is no default password; restart with a new one if you lost it.

**Dashboard says "no stream yet"**
Paste a URL in the bar and press watch. Nothing records until you do.

**Live preview is black**
Give it a few seconds (`connecting…`), then check the activity log. If it says
`preview unavailable`, the source may use a codec that needs the transcode
fallback.

---

## Project layout

```
clipper.py              entry point / CLI
run.bat / demo.bat      double-click launchers

ytclipper/
  config.py             runtime configuration
  resolve.py            URL -> direct media URLs (yt-dlp)
  recorder.py           ffmpeg segment recorder + supervisor
  buffer.py             segment index and stream timeline
  detector.py           audio + chat hype detection
  captions.py           whisper transcription, cues, .srt / burned subtitles
  encode.py             hardware encoder detection
  preview.py            local HLS re-stream for the live preview
  clips.py              clip cutting, encoding, thumbnails, 9:16 export
  security.py           password auth, SSRF guard, sessions
  engine.py             wiring, triggers, state for the UI
  web.py                dashboard server + JSON API
  demo.py / voice.py    synthetic test stream and its TTS
  hotkey.py             system-wide clip hotkey
  util.py               shared helpers

static/                 dashboard (HTML/CSS/JS + hls.js)
tests/security_check.py attack suite
```

Generated at runtime (git-ignored): `.clipper\` (buffer), `clips\`, `.models\`.

---

## Limits

- **No auto-publish.** Clips are saved locally; uploading to YouTube/TikTok is
  not included.
- **Detection is signal-based.** It clips loud moments and chat spikes, not
  "interesting" content it understands.
- **Vertical export reframes the finished clip.** Burned captions stay inside
  the original 16:9 frame, so they look smaller in blur mode.
- **Normal videos play in real time.** No seeking, no fast scan.
- **One stream at a time.**
- **Check the rights** of whatever you clip before publishing it.

---

## License

MIT — see [LICENSE](LICENSE).
