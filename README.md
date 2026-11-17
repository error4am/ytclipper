# ytclipper

Watches a YouTube live stream and cuts clips **while it is still streaming**.

It records the stream into a rolling on-disk buffer, listens for hype (loud
moments in the audio, spikes in live chat), and when something happens it saves
a real `.mp4` with the seconds *before* and *after* the moment — the part you
would otherwise have missed. Captions are transcribed locally and burned into
the clip, with an `.srt` saved next to it.

You can also just smash the big **CLIP THAT** button (or press space) whenever
you see something good, and it will still include the last N seconds.

```
python clipper.py "https://www.youtube.com/watch?v=XXXXXXXXXXX"
```

then open <http://127.0.0.1:8765>.

---

## Requirements

| Tool | Why | Check |
| --- | --- | --- |
| Python 3.10+ | runs the clipper | `python --version` |
| ffmpeg + ffprobe | records & encodes | `ffmpeg -version` |
| yt-dlp | resolves YouTube streams | `python -m yt_dlp --version` |
| faster-whisper | auto captions | `python -c "import faster_whisper"` |

Install the Python bits:

```powershell
python -m pip install -U yt-dlp faster-whisper
```

ffmpeg on Windows:

```powershell
winget install Gyan.FFmpeg
```

(If ffmpeg is not on `PATH`, pass `--ffmpeg "C:\path\to\ffmpeg.exe"`.)

Optional — live chat detection:

```powershell
python -m pip install chat-downloader
```

Without it everything still works, audio detection just has no chat signal.

---

## Quick start

```powershell
cd C:\Users\HP\clipper

# open the dashboard, then paste a stream URL into the bar at the top
python clipper.py

# or start watching straight away (the URL bar fills itself in)
python clipper.py "https://www.youtube.com/watch?v=XXXXXXXXXXX"

# a channel that is live right now
python clipper.py "https://www.youtube.com/@somechannel/live"

# private / age-restricted stream
python clipper.py "https://www.youtube.com/watch?v=XXX" --cookies-from-browser chrome

# just resolve it and print the stream info, no recording
python clipper.py "https://www.youtube.com/watch?v=XXX" --check
```

The dashboard opens automatically. Clips are written to `.\clips\`.

`run.bat` no longer asks for anything — it opens the dashboard and you paste the
URL there. You can still drag a URL onto it, or run `run.bat "URL"` to skip a
step.

---

## Test it in one command

No live stream around? Run the built-in demo:

```powershell
python clipper.py --test
```

That starts a **local fake live stream** (test pattern + spoken lines using your
Windows voice, with a loud burst every ~16 seconds), serves it over localhost,
and points the normal pipeline at it. Nothing touches YouTube.

Within a minute you should see this in the activity log:

```
demo stream live at http://127.0.0.1:8901/demo.m3u8 (a loud burst every ~16s)
caption model 'base' ready (4s, cpu/int8)
queued clip [audio] 10s (ready in ~2s)
transcribed 10s -> 5 caption cues (4.1s, en)
saved clip_..._audio.mp4  10.0s  3.6MB +captions
```

and a captioned clip in the grid.

Things worth trying in that session:

| Try this | What it proves |
| --- | --- |
| watch the loudness meter when the burst hits | audio detection + trigger line |
| press **space** / **CLIP THAT** | manual clip with a look-back |
| press **F8** from another window | the global hotkey clips without focusing the dashboard |
| toggle **captions** off, cut another clip | burn-in vs clean video |
| click `play` on a card | in-browser playback (range requests) |
| click `srt` | subtitle sidecar download |
| `open folder`, `delete` | file management |

Stop with `Ctrl+C` (or double-click `demo.bat` and close it) — the fake stream
and its temp files are removed automatically. Demo clips stay in `.\clips`.

### Demo voice

The fake stream speaks with **Kokoro**, a small local neural TTS (82M params,
onnxruntime) that sounds far more natural than the Windows voice. It only
affects the demo — your real clips keep their original audio.

```powershell
python clipper.py --test                                # default: af_heart
python clipper.py --test --voice-name am_michael         # calm male, "presenter"
python clipper.py --test --voice-name bm_george --voice-speed 0.95
python clipper.py --test --voice sapi                    # old Windows voice
python clipper.py --test --voice tone                    # just a beep (no speech)
```

First run downloads the model once (~230 MB) into `.models\kokoro`. If the
download or the model fails, the demo falls back to the Windows voice and then
to a tone; the activity log always says which voice it used.

Voices: `af_heart` (default, warm), `af_bella`, `af_nicole`, `am_michael`,
`am_adam`, `bf_emma`, `bm_george` — plus everything else in Kokoro v1.0.

---

## What the dashboard gives you

* **LIVE preview** — the stream itself, right above the controls, so you can see
  what you're clipping instead of guessing. Muted by default with an unmute
  button. It's a local re-stream (see below), roughly 4–8 seconds behind live.
* **URL bar** — paste a YouTube live URL (or any `.m3u8` with `--direct`) and
  hit **watch**. It resolves and starts recording in a few seconds, and the bar
  pre-fills with the last URL you used. **stop** ends the recording without
  closing the app; pasting a different URL switches streams. The **cookies**
  dropdown pulls cookies from chrome/edge/firefox/… for members-only or
  age-restricted streams (close that browser if the lookup fails).
* **demo** — starts the local synthetic stream (speech + loud bursts) so you can
  try everything without leaving the dashboard.
* **CLIP THAT** — saves `--before` seconds before the click and `--after`
  seconds after it. Press **space** or **c** instead of clicking.
* **F8 anywhere** — a system-wide hotkey clips whatever is on screen, even from
  a fullscreen game. Change it with `--hotkey ctrl+alt+c` or `--no-hotkey`.
* **9:16 button** — renders a vertical Shorts/TikTok version of any clip
  (`blur` keeps the whole frame over a blurred fill; `crop` goes full-bleed).
* **auto detect** — toggle live. When on, loud moments and chat spikes create
  clips on their own (with a cooldown so you don't get 40 clips a minute).
* **captions** — toggle live. Each clip gets its speech transcribed and burned
  in as subtitles; an `.srt` is saved too. Cards with captions show a `CC` tag.
* **loudness meter** — current audio level, the rolling baseline, and the
  level that will fire a clip. Useful for tuning.
* **clip grid** — thumbnails, duration, why it was cut, inline player,
  download, delete, `.srt` and `9:16` downloads.
* **activity log** — what the recorder, the detectors and the encoder are doing.

---

## How it works

```
YouTube ──yt-dlp──> direct stream URL
     │
     └─ ffmpeg ─> buffer\seg_sNN_YYYYmmdd_HHMMSS.mkv   (4s segments, rolling)
                        │
                        ├─ audio RMS per 0.5s ──> jump vs 90s baseline ─┐
                        ├─ live chat rate     ──> jump vs 2min baseline ─┤
                        └─ dashboard button   ─────────────────────────┤
                                                                       ▼
                                              wait for the trailing seconds
                                                                       │
                     stitch -> transcribe -> burn subtitles -> H.264 .mp4 (+ .srt)
```

* **Buffer** — segments are written with `-c copy` (no CPU cost, original
  quality). Only the last `--buffer` seconds are kept, so disk use stays flat.
* **Detection timing** — audio is analysed per finished segment, and the
  trigger time comes from that segment's position on the stream timeline, so
  the clip is aligned to what you actually heard. Expect a few seconds of
  latency.
* **Clips** — the selected segments are concatenated losslessly, then trimmed
  frame-accurately and re-encoded to H.264/AAC MP4 (`+faststart`, ready for
  uploading or editing). Detection still runs while a clip encodes.
* **Long streams** — the stream URL is re-resolved every `--refresh` seconds
  and the recorder auto-reconnects if the connection drops. The web UI and
  buffer survive restarts.
* **Nothing is uploaded.** Everything stays on your machine.

---

## Live preview

The dashboard shows the stream in a panel above the controls. Browsers can't
play YouTube's HLS directly (cross-origin, expiring URLs, split video/audio),
so the clipper remuxes the feed into local 1-second HLS segments with ffmpeg and
serves them from the dashboard itself; `hls.js` plays them.

* **`-c copy`** when the source is H.264/AAC — about **3% of one CPU core** and
  no re-encoding, so the preview is pixel-identical to what you record.
* **Fallback transcode** to 540p if the source is VP9/AV1 or copy fails.
* Costs one extra download of the stream (a few Mbps). Disable it with
  `--no-preview` if bandwidth or CPU is tight.
* Starts and stops with the stream, and resets itself when you switch URLs.
* Audio is muted by default (browser autoplay rules); hit **unmute** when you
  want sound.

## Going online (security)

By default the dashboard binds to **127.0.0.1** and has no login — that is fine
for your own machine. The moment it listens on another interface, a password is
**required** (the app refuses to start without one):

```powershell
# LAN / internet, password generated and printed once
python clipper.py --host 0.0.0.0 --generate-password

# or pick your own
python clipper.py --host 0.0.0.0 --password "something-long"

# behind a domain/reverse proxy, add the hostname to the allowlist
python clipper.py --host 0.0.0.0 --password "..." --allowed-host clips.mydomain.com

# serve HTTPS directly
python clipper.py --host 0.0.0.0 --password "..." --tls-cert cert.pem --tls-key key.pem
```

What is enforced:

* **Password login** (PBKDF2-SHA256, 200k rounds) with an HttpOnly,
  SameSite=Strict session cookie, `Secure` when TLS is on.
* **Brute-force lockout** — 8 failed attempts per IP in 10 minutes, then 429.
* **Host header validation** — IP literals are fine, unknown hostnames are 421,
  which blocks DNS-rebinding attacks against the local API.
* **Origin checks** on every state-changing request, so other websites cannot
  drive your dashboard.
* **SSRF guard** — stream URLs must be http(s) and must not resolve to private,
  loopback, link-local or reserved addresses. `localhost`, `10.x`, `192.168.x`,
  `169.254.169.254`, `file://` and friends are rejected. (The built-in demo
  bypasses this deliberately.)
* **Path traversal** is blocked for clips, thumbnails, subtitles, preview
  segments and static files.
* **Request bodies capped at 64 KB**, slow connections dropped after 20s.
* **Security headers**: strict CSP, `nosniff`, `X-Frame-Options: DENY`,
  `no-referrer`, `Permissions-Policy`, `Cross-Origin-Opener-Policy`.
* **`open folder`** only works from localhost — a remote session can't launch
  Explorer on your machine.

The whole thing is covered by an attack test suite:

```powershell
python tests/security_check.py
# 36 checks: auth, CSRF, SSRF, traversal, brute force, headers, HTTPS...
```

**Recommended for real internet exposure:** put a reverse proxy or tunnel in
front (Caddy, nginx, Cloudflare Tunnel) so TLS and rate limiting are handled at
the edge, and add your hostname with `--allowed-host`. Don't port-forward the
plain HTTP port.

## Auto captions

Every clip is transcribed locally with [faster-whisper](https://github.com/SYSTRAN/faster-whisper)
(Whisper on ctranslate2 — CPU is fine, no API key, no upload):

* the clip window's audio is decoded, transcribed, split into short readable
  cues (~42 characters per line, two lines max) and burned in bottom-centre
  during the same encode pass — no extra re-encode, no quality loss
* an `.srt` file is always written next to the `.mp4` for editing or re-upload
* `--caption-mode srt` keeps the video clean and only writes subtitles
* `--caption-box` draws captions on a solid background instead of an outline
* the first run downloads the model once (~145 MB for `base`) into your
  HuggingFace cache; later runs load it in a couple of seconds
* `--caption-model tiny|base|small|medium` trades speed for accuracy
* `--caption-lang en` skips language auto-detection (faster and more accurate
  when you know the language)
* with an NVIDIA GPU: `--caption-device cuda --caption-compute float16`
* captions add a few seconds per clip — roughly 0.5× the clip length to
  transcribe on CPU with `base` (a 30s clip ≈ 15s), plus the encode

The dashboard has a **captions** switch, and clips with subtitles are tagged
`CC`. Caption failures never lose a clip: it is still saved without them.

---

## Speed: hardware encoding

Clip encoding uses the fastest encoder that actually works on your machine:
NVENC (NVIDIA), Quick Sync (Intel), AMF (AMD), or libx264 as a last resort. The
choice is verified with a real one-second test encode at startup, because ffmpeg
often ships encoders the driver cannot run.

```
[10:00:01]   encoder: h264_qsv (Intel Quick Sync) [hardware]
```

Measured on a 1080x1920 clip: **11.6s with libx264 vs 6.9s with Quick Sync**
(and 2.2 MB vs 2.9 MB at the same quality). On 1080p+ game footage the gap is
usually 3–6×, and the CPU stays free for the recorder.

Force one with `--encoder nvenc|qsv|amf|x264`.

## Vertical clips (9:16)

Every clip card has a **9:16** button. It renders `<clip>_vertical.mp4` at
1080x1920 with the audio copied, no re-recording:

* `--vertical-mode blur` (default) — full frame centred over a blurred fill of
  itself. Nothing is cut, captions stay readable. Best for news/talking heads.
* `--vertical-mode crop` — full-bleed centre crop. Fills the screen, but the
  sides are gone, so burned-in captions can get clipped.

Add `--vertical-auto` to render a vertical version of every clip automatically;
otherwise it's one click per clip. Delete a clip and its vertical goes with it.

---

## Options

| Flag | Default | Meaning |
| --- | --- | --- |
| `--before` | `18` | seconds kept before the moment |
| `--after` | `12` | seconds kept after the moment |
| `--buffer` | `420` | seconds of stream kept on disk |
| `--seg-time` | `4` | segment length (lower = faster detection, more files) |
| `--out` | `clips` | folder for finished clips |
| `--no-auto` | off | start with auto detection disabled |
| `--db-margin` | `8.5` | how many dB above baseline counts as "loud" |
| `--floor-db` | `-34` | ignore anything quieter than this |
| `--cooldown` | `45` | min seconds between automatic clips |
| `--max-per-hour` | `40` | hard cap on automatic clips |
| `--no-chat` | off | disable live chat detection |
| `--no-captions` | off | disable transcription entirely |
| `--caption-model` | `base` | whisper model: `tiny`, `base`, `small`, `medium` |
| `--caption-lang` | auto | force a language, e.g. `en` |
| `--caption-mode` | `burn` | `burn` = subtitles inside the video, `srt` = file only |
| `--caption-box` | off | solid caption background instead of an outline |
| `--caption-device` / `--caption-compute` | `cpu` / `int8` | `cuda` + `float16` for a GPU |
| `--encoder` | `auto` | `auto`, `nvenc`, `qsv`, `amf` or `x264` |
| `--hotkey` | `F8` | system-wide clip hotkey (`F8`, `ctrl+alt+c`, …) |
| `--no-hotkey` | off | don't register a global hotkey |
| `--vertical-mode` | `blur` | 9:16 framing: `blur` or `crop` |
| `--vertical-auto` | off | render 9:16 for every clip automatically |
| `--no-preview` | off | disable the live preview panel |
| `--password` | none | require a password (mandatory when binding publicly) |
| `--generate-password` | off | generate and print a random password |
| `--tls-cert` / `--tls-key` | none | serve HTTPS with a PEM certificate |
| `--allowed-host` | none | extra hostname accepted in the Host header |
| `--quality` | `1080` | max video height to download |
| `--direct` | off | treat the URL as a direct `.m3u8`/media URL |
| `--test` | off | run the local demo stream instead of a URL |
| `--voice` | `kokoro` | demo voice: `kokoro`, `sapi` or `tone` |
| `--voice-name` | `af_heart` | kokoro voice id |
| `--voice-speed` | `1.0` | demo speech speed |
| `--cookies-from-browser` | – | `chrome`, `firefox`, `edge`, … for restricted streams |
| `--refresh` | `3600` | re-resolve the stream URL after N seconds |
| `--port` / `--host` | `8765` / `127.0.0.1` | dashboard binding |
| `--no-open` | off | don't open a browser window |
| `--check` | off | resolve and print stream info, then exit |

### Tuning auto detection

* Too many clips → raise `--db-margin` (e.g. `12`) or `--cooldown` (e.g. `90`).
* Missing moments → lower `--db-margin` (e.g. `6`), or lower `--floor-db`
  (e.g. `-45`) if the stream is quiet.
* Watch the **loudness meter**: the trigger line is where clips start firing.
  If it sits above every peak, nothing will ever trigger.
* `--seg-time 2` makes detection snappier; `6` makes it cheaper on a slow PC.

---

## Troubleshooting

**"yt-dlp could not read that URL"**
Update it: `python -m pip install -U yt-dlp`. For members-only or
age-restricted streams use `--cookies-from-browser chrome`.

**The stream is 12 hours long and clips drift / stop**
The URL is refreshed hourly by default. If the dashboard shows `waiting`,
check the activity log — the recorder retries automatically.

**Only 720p shows up**
YouTube often serves a lower HLS ladder to non-browser clients. Try
`--quality 1080` (default) and `--cookies-from-browser chrome` so yt-dlp can
request the higher formats.

**Clips are "truncated"**
The buffer did not cover the requested window (usually right after start or
after a reconnect). Increase `--buffer`, or wait a minute after launching.

**High CPU during encoding**
Clips are re-encoded with `libx264 veryfast`. Raise `--cooldown` so fewer clips
are cut, or edit the encoder settings in `ytclipper/clips.py`.

**The dashboard says "no stream yet"**
Paste a URL in the bar at the top and press **watch** (or Enter). Nothing is
recording until you do.

**"Could not copy Chrome cookie database"**
Chrome was running and holding its cookie file. Close Chrome (or pick another
browser in the cookies dropdown) and press **watch** again.

**Chat detection is off**
`python -m pip install chat-downloader`. It is optional and reconnects on its
own when chat drops.

**Captions are missing**
`python -m pip install faster-whisper`. If it is installed but the model is
still downloading, watch the activity log — the first clip waits for it.

**Captions come out garbled or in the wrong language**
Force the language (`--caption-lang en`) or use a bigger model
(`--caption-model small`). `medium` is slower but noticeably better on noisy
streams.

**The first caption takes a long time**
That is the one-time model download. Later clips reuse it.

---

## Notes

* Respect the platform's terms of service and the streamer's rights when you
  publish clips. This tool records content you choose to point it at.
* Clip files live entirely on your disk; nothing leaves the machine.
* `--host 0.0.0.0` exposes the dashboard to your network. Keep the default
  `127.0.0.1` unless you know what you are doing — there is no login.

## Layout

```
clipper.py              entry point / CLI
ytclipper/config.py     runtime config
ytclipper/resolve.py    URL -> direct stream URLs (yt-dlp)
ytclipper/recorder.py   ffmpeg segment recorder + supervisor
ytclipper/buffer.py     segment index & stream timeline
ytclipper/detector.py   audio + chat hype detection
ytclipper/captions.py   whisper transcription, cues, .srt / burned subtitles
ytclipper/encode.py     hardware encoder detection (nvenc / qsv / amf / x264)
ytclipper/hotkey.py     system-wide clip hotkey
ytclipper/preview.py    local HLS re-stream for the live preview
ytclipper/security.py   password auth, SSRF guard, session/cookie helpers
ytclipper/clips.py      clip cutting / encoding / thumbnails / 9:16 export
tests/security_check.py attack suite for the dashboard
ytclipper/engine.py     wiring, triggers, state for the UI
ytclipper/web.py        local dashboard server + JSON API
static/                 dashboard (HTML/CSS/JS)
```
