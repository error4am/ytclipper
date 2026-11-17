/* ytclipper dashboard ------------------------------------------------------ */
const $ = (id) => document.getElementById(id);

const REASON = { audio: "loud moment", chat: "chat spike", manual: "manual cut" };

let primed = false;
let knownClips = new Set();
let clipSig = "";
let logKey = "";
let countdownTimer = null;
let countdownLeft = 0;
let awaitingClip = false;
let urlDirty = false;
let cookiesDirty = false;
let defaultNote = "saves 18s before + 12s after the moment";

/* -- formatting ----------------------------------------------------------- */
function fmtTime(value) {
  let s = Math.max(0, Math.round(Number(value) || 0));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  const pad = (n) => String(n).padStart(2, "0");
  return h > 0 ? `${h}:${pad(m)}:${pad(sec)}` : `${m}:${pad(sec)}`;
}

function fmtDur(value) {
  const s = Number(value);
  if (!s || s <= 0) return "—";
  if (s < 60) return `${s.toFixed(1)}s`;
  return fmtTime(s);
}

function clamp(value, lo, hi) {
  return Math.min(hi, Math.max(lo, value));
}

/* -- toasts --------------------------------------------------------------- */
function toast(text, isError, sub) {
  const box = document.createElement("div");
  box.className = "toast" + (isError ? " error" : "");
  const line = document.createElement("div");
  line.textContent = text;
  box.appendChild(line);
  if (sub) {
    const extra = document.createElement("div");
    extra.className = "sub";
    extra.textContent = sub;
    box.appendChild(extra);
  }
  $("toasts").appendChild(box);
  setTimeout(() => {
    box.style.transition = "opacity .3s ease, transform .3s ease";
    box.style.opacity = "0";
    box.style.transform = "translateY(6px)";
    setTimeout(() => box.remove(), 320);
  }, isError ? 5200 : 4200);
}

/* -- polling -------------------------------------------------------------- */
async function tick() {
  try {
    const response = await fetch("/api/state", { cache: "no-store" });
    if (!response.ok) throw new Error(`http ${response.status}`);
    render(await response.json());
  } catch (error) {
    $("live-dot").className = "live-dot";
    $("stream-sub").textContent = "dashboard lost contact with the clipper";
  }
}

setInterval(tick, 1000);
tick();

/* -- render --------------------------------------------------------------- */
function render(s) {
  const recorder = s.recorder || {};
  const state = recorder.state || "starting";

  $("live-dot").className =
    "live-dot " + (state === "recording" ? "on" : state === "waiting" || state === "starting" ? "wait" : "");
  $("stream-title").textContent = s.stream.title;

  let sub = "";
  if (state === "recording") {
    sub = `recording live · ${s.buffer.segments} segments buffered`;
  } else if (state === "waiting") {
    sub = recorder.error ? `waiting: ${recorder.error}` : "waiting for the stream to start";
  } else if (state === "ended") {
    sub = "stream ended · clips still available";
  } else if (state === "error") {
    sub = recorder.error || "recorder error";
  } else if (state === "idle") {
    sub = "no stream yet — paste a URL above";
  } else {
    sub = "starting up…";
  }
  $("stream-sub").textContent = sub;

  renderLive(s);

  const urlInput = $("url-input");
  if (!urlDirty) {
    const target =
      state === "idle" || state === "ended"
        ? s.stream.last_url || ""
        : s.stream.url || s.stream.last_url || "";
    if (urlInput.value !== target) urlInput.value = target;
  }
  const idle = state === "idle";
  $("url-stop").hidden = idle;
  $("clip-btn").disabled = idle;

  const cookies = $("cookies-select");
  if (!cookiesDirty) {
    const wantedCookies =
      state === "idle" || state === "ended"
        ? s.stream.last_cookies || ""
        : s.stream.cookies || s.stream.last_cookies || "";
    if (cookies.value !== wantedCookies) cookies.value = wantedCookies;
  }

  $("stat-uptime").textContent = fmtTime(s.uptime);
  $("stat-buffer").textContent = fmtTime(s.buffer.seconds);
  $("stat-buffer").title = `${s.buffer.seconds}s of ${Math.round(s.buffer.capacity)}s kept`;
  $("stat-clips").textContent = String((s.clips || []).length);

  if (!primed && s.defaults) {
    $("before").value = Math.round(s.defaults.before);
    $("after").value = Math.round(s.defaults.after);
    defaultNote = `saves ${Math.round(s.defaults.before)}s before + ${Math.round(s.defaults.after)}s after the moment`;
    $("clip-note").textContent = defaultNote;
    $("clips-dir").textContent = s.clips_dir || "";
    primed = true;
  }

  if (idle) {
    if (!countdownTimer) {
      $("clip-note").textContent = "paste a URL above to start watching";
    }
  } else if (
    !countdownTimer &&
    !awaitingClip &&
    $("clip-note").textContent === "paste a URL above to start watching"
  ) {
    $("clip-note").textContent = defaultNote;
  }

  $("auto-toggle").checked = Boolean(s.detector.auto);

  const authInfo = s.auth || {};
  $("logout-btn").hidden = !authInfo.enabled;

  const build = s.build || {};
  const line = $("build-line");
  line.textContent = "";
  const bits = [];
  if (build.encoder_label) bits.push(["encoder", build.encoder_label]);
  if (build.hotkey) {
    bits.push(["hotkey", build.hotkey + (build.hotkey_active ? "" : " (busy)")]);
  }
  if (build.vertical) {
    bits.push(["9:16", build.vertical_auto ? `auto · ${build.vertical}` : build.vertical]);
  }
  for (const [key, value] of bits) {
    const item = document.createElement("span");
    const label = document.createElement("b");
    label.textContent = key;
    item.append(label, document.createTextNode(` ${value}`));
    line.appendChild(item);
  }

  const caps = s.captions || {};
  const capToggle = $("captions-toggle");
  capToggle.checked = Boolean(caps.enabled);
  capToggle.disabled = caps.available === false;
  $("captions-switch").title =
    caps.available === false
      ? "auto captions need faster-whisper: python -m pip install faster-whisper"
      : `auto captions · whisper ${caps.model} · ${caps.language} · ${caps.status}`;

  renderMeter(s.detector);
  renderLog(s.log || []);

  const queued = s.detector.queued || 0;
  $("worker-note").textContent = s.detector.worker_busy
    ? "cutting clip…"
    : queued > 0
      ? `${queued} queued`
      : "";

  renderClips(s.clips || []);
}

function renderMeter(detector) {
  const audio = detector.audio || {};
  const toPct = (db) => clamp(((Number(db) + 60) / 60) * 100, 0, 100);
  const warm = Boolean(audio.warm);

  $("meter-fill").style.width = `${toPct(audio.last_db)}%`;
  $("meter-db").textContent = warm ? `${Math.round(audio.last_db)} dB` : "—";

  const base = $("meter-base");
  const trigger = $("meter-trig");
  if (warm) {
    base.style.display = "block";
    trigger.style.display = "block";
    base.style.left = `${toPct(audio.baseline_db)}%`;
    trigger.style.left = `${toPct(audio.baseline_db + audio.margin_db)}%`;
  } else {
    base.style.display = "none";
    trigger.style.display = "none";
  }

  if (!warm) {
    $("meter-note").textContent = "warming up…";
  } else if (!detector.auto) {
    $("meter-note").textContent = "auto detect off";
  } else {
    const at = Math.round(audio.baseline_db + audio.margin_db);
    $("meter-note").textContent = `fires above ${at} dB · ${Math.round(detector.cooldown)}s cooldown`;
  }

  const chat = detector.chat || {};
  $("chat-note").textContent = chat.available
    ? `chat ${chat.rate}/s`
    : "chat off (optional)";
}

function renderLog(items) {
  if (!items.length) return;
  const last = items[items.length - 1];
  const key = `${items.length}:${last.t}`;
  if (key === logKey) return;
  logKey = key;

  const box = $("log");
  const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
  box.textContent = "";

  const frag = document.createDocumentFragment();
  for (const item of items) {
    const row = document.createElement("div");
    row.className = `log-line ${item.level || "info"}`;
    const time = document.createElement("span");
    time.className = "t";
    time.textContent = item.clock;
    const msg = document.createElement("span");
    msg.className = "m";
    msg.textContent = item.msg;
    row.append(time, msg);
    frag.appendChild(row);
  }
  box.appendChild(frag);
  if (atBottom) box.scrollTop = box.scrollHeight;
}

function renderClips(clips) {
  const signature = clips
    .map(
      (c) =>
        `${c.name}:${c.size}:${c.vertical ? `${c.vertical.name}:${c.vertical.size}` : 0}`,
    )
    .join("|");
  if (signature === clipSig) return;
  clipSig = signature;

  const fresh = clips.filter((c) => !knownClips.has(c.name)).map((c) => c.name);
  clips.forEach((c) => knownClips.add(c.name));

  const wrap = $("clips");
  wrap.textContent = "";
  $("empty").style.display = clips.length ? "none" : "block";
  $("clip-count").textContent = String(clips.length);

  if (primed && fresh.length) {
    const latest = clips.find((c) => c.name === fresh[0]);
    toast("new clip saved", false, latest ? `${latest.name} · ${fmtDur(latest.duration)}` : undefined);
    if (awaitingClip) {
      awaitingClip = false;
      stopCountdown("saved - it's in the grid below");
    }
  }

  for (const clip of clips) {
    wrap.appendChild(clipCard(clip, fresh.includes(clip.name)));
  }
}

function clipCard(clip, isNew) {
  const card = document.createElement("article");
  card.className = "clip-card" + (isNew ? " new" : "");

  const thumb = document.createElement("div");
  thumb.className = "clip-thumb";
  if (clip.thumb) {
    const img = document.createElement("img");
    img.src = `${clip.thumb}?v=${Math.round(clip.created || 0)}`;
    img.alt = "";
    img.loading = "lazy";
    thumb.appendChild(img);
  }

  const badge = document.createElement("span");
  badge.className = `badge ${clip.category || ""}`;
  badge.textContent = REASON[clip.category] || clip.category || "clip";
  thumb.appendChild(badge);

  const play = document.createElement("button");
  play.className = "play";
  play.title = "play";
  play.innerHTML =
    '<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M8 5v14l11-7z"/></svg>';
  play.addEventListener("click", () => openPlayer(clip));
  thumb.appendChild(play);

  const dur = document.createElement("span");
  dur.className = "dur";
  dur.textContent = fmtDur(clip.duration);
  thumb.appendChild(dur);

  const chips = document.createElement("div");
  chips.className = "thumb-chips";
  if (clip.subtitles) {
    const cc = document.createElement("span");
    cc.className = "cc";
    cc.textContent = "CC";
    cc.title = "captions on this clip";
    chips.appendChild(cc);
  }
  if (clip.vertical) {
    const v9 = document.createElement("span");
    v9.className = "v9";
    v9.textContent = "9:16";
    v9.title = "play the vertical version";
    v9.style.cursor = "pointer";
    v9.addEventListener("click", () => openPlayer(clip.vertical));
    chips.appendChild(v9);
  }
  if (chips.childElementCount) thumb.appendChild(chips);

  if (clip.complete === false) {
    const warn = document.createElement("span");
    warn.className = "badge";
    warn.style.left = "auto";
    warn.style.right = "8px";
    warn.style.top = "8px";
    warn.style.color = "#ffb340";
    warn.textContent = "truncated";
    thumb.appendChild(warn);
  }

  const body = document.createElement("div");
  body.className = "clip-body";

  const title = document.createElement("div");
  title.className = "clip-title";
  title.textContent = clip.created_local || clip.name;
  body.appendChild(title);

  if (clip.detail) {
    const detail = document.createElement("div");
    detail.className = "clip-detail";
    detail.title = clip.detail;
    detail.textContent = clip.detail;
    body.appendChild(detail);
  }

  const meta = document.createElement("div");
  meta.className = "clip-meta";
  meta.textContent = clip.size_human || "";
  body.appendChild(meta);

  if (clip.vertical) {
    const row = document.createElement("div");
    row.className = "v9-row";
    if (clip.vertical.thumb) {
      const mini = document.createElement("img");
      mini.className = "v9-thumb";
      mini.src = `${clip.vertical.thumb}?v=${Math.round(clip.vertical.size || 0)}`;
      mini.alt = "";
      mini.title = "play 9:16";
      mini.addEventListener("click", () => openPlayer(clip.vertical));
      row.appendChild(mini);
    }
    const info = document.createElement("div");
    info.className = "v9-info";
    const label = document.createElement("div");
    label.className = "v9-label";
    label.textContent = "9:16 ready";
    const play = document.createElement("button");
    play.className = "icon-btn";
    play.textContent = "play";
    play.addEventListener("click", () => openPlayer(clip.vertical));
    const dl = document.createElement("a");
    dl.className = "icon-btn";
    dl.href = clip.vertical.url;
    dl.download = clip.vertical.name;
    dl.textContent = "download";
    info.append(label, play, dl);
    row.appendChild(info);
    body.appendChild(row);
  }

  const actions = document.createElement("div");
  actions.className = "clip-actions";

  const openBtn = document.createElement("button");
  openBtn.className = "icon-btn";
  openBtn.textContent = "play";
  openBtn.addEventListener("click", () => openPlayer(clip));

  const download = document.createElement("a");
  download.className = "icon-btn";
  download.href = clip.url;
  download.download = clip.name;
  download.textContent = "download";
  actions.append(openBtn, download);

  if (clip.srt) {
    const subs = document.createElement("a");
    subs.className = "icon-btn";
    subs.href = clip.srt;
    subs.download = clip.name.replace(/\.mp4$/, ".srt");
    subs.textContent = "srt";
    actions.appendChild(subs);
  }

  if (!clip.vertical) {
    const make = document.createElement("button");
    make.className = "icon-btn";
    make.textContent = "9:16";
    make.title = "make a vertical Shorts/TikTok version";
    make.addEventListener("click", () => exportVertical(clip, make));
    actions.appendChild(make);
  }

  const remove = document.createElement("button");
  remove.className = "icon-btn danger";
  remove.textContent = "delete";
  remove.addEventListener("click", () => deleteClip(clip));

  actions.appendChild(remove);
  body.appendChild(actions);

  card.append(thumb, body);
  return card;
}

/* -- live preview --------------------------------------------------------- */
let hls = null;
let liveSource = null;

function destroyLive(video) {
  if (hls) {
    try {
      hls.destroy();
    } catch (error) {}
    hls = null;
  }
  liveSource = null;
  try {
    video.pause();
    video.removeAttribute("src");
    video.load();
  } catch (error) {}
}

function startLive(video, src) {
  destroyLive(video);
  liveSource = src;
  const bust = src;
  if (window.Hls && window.Hls.isSupported()) {
    hls = new Hls({
      lowLatencyMode: true,
      liveSyncDurationCount: 2,
      backBufferLength: 10,
      manifestLoadingRetryDelay: 800,
      manifestLoadingMaxRetry: 20,
      levelLoadingMaxRetry: 20,
    });
    hls.loadSource(bust);
    hls.attachMedia(video);
    window.__hls = hls;
    window.__hlsErrors = [];
    hls.on(Hls.Events.ERROR, (_event, data) => {
      window.__hlsErrors.push({
        type: data.type,
        details: data.details,
        reason: data.reason || "",
        status: data.response ? data.response.code : "",
      });
      window.__hlsErrors = window.__hlsErrors.slice(-8);
    });
    hls.on(Hls.Events.MANIFEST_PARSED, () => {
      video.play().catch(() => {});
    });
  } else if (video.canPlayType("application/vnd.apple.mpegurl")) {
    video.src = bust;
    video.play().catch(() => {});
  }
}

function renderLive(s) {
  const video = $("live-video");
  const overlay = $("live-overlay");
  const tag = $("live-tag");
  const preview = s.preview || {};
  const live = s.stream.state === "recording";
  const wanted =
    live && preview.enabled !== false
      ? `/preview/index.m3u8?v=${preview.token || 0}`
      : null;

  if (!wanted) {
    destroyLive(video);
    overlay.classList.remove("hidden");
    overlay.textContent = live ? "preview unavailable" : "no stream";
    tag.className = "live-tag";
    tag.textContent = live ? "no preview" : "offline";
    return;
  }

  tag.className = "live-tag" + (preview.ready ? " on" : "");
  tag.textContent = preview.ready ? "live" : "connecting…";

  if (preview.ready) {
    overlay.classList.add("hidden");
  } else {
    overlay.classList.remove("hidden");
    overlay.textContent = preview.error || "starting preview…";
  }

  if (liveSource !== wanted) {
    startLive(video, wanted);
  }
}

/* -- actions -------------------------------------------------------------- */
async function doClip() {
  const before = clamp(Number($("before").value) || 18, 1, 120);
  const after = clamp(Number($("after").value) || 0, 0, 120);
  const button = $("clip-btn");
  button.disabled = true;
  try {
    const response = await fetch("/api/clip", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ before, after }),
    });
    const data = await response.json();
    if (!data.ok) {
      toast(data.error || "could not queue the clip", true);
      return;
    }
    awaitingClip = true;
    startCountdown(data.ready_in, data.length);
  } catch (error) {
    toast("dashboard lost contact with the clipper", true);
  } finally {
    setTimeout(() => (button.disabled = false), 900);
  }
}

function startCountdown(readyIn, length) {
  clearInterval(countdownTimer);
  countdownLeft = Math.max(0, Math.round(Number(readyIn) || 0));
  const note = $("clip-note");
  note.classList.add("hot");
  const paint = () => {
    if (countdownLeft > 0) {
      note.textContent = `clipping ${length || ""}s · capture completes in ${countdownLeft}s`;
    } else if (countdownLeft > -45) {
      note.textContent = "cutting & encoding…";
    } else {
      stopCountdown("no clip appeared - check the activity panel");
      return;
    }
    countdownLeft -= 1;
  };
  paint();
  countdownTimer = setInterval(paint, 1000);
}

function stopCountdown(message) {
  clearInterval(countdownTimer);
  countdownTimer = null;
  const note = $("clip-note");
  note.textContent = message;
  setTimeout(() => {
    if (!countdownTimer) {
      note.classList.remove("hot");
      note.textContent = defaultNote;
    }
  }, 3500);
}

async function toggleAuto(enabled) {
  try {
    await fetch("/api/auto", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled }),
    });
    toast(enabled ? "auto detect on" : "auto detect off", false);
  } catch (error) {
    toast("could not change auto detect", true);
  }
}

async function toggleCaptions(enabled) {
  try {
    const response = await fetch("/api/captions", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled }),
    });
    const data = await response.json();
    if (data.captions && data.captions.available === false) {
      toast("captions need the transcriber", true, "python -m pip install faster-whisper");
      return;
    }
    toast(enabled ? "captions on" : "captions off", false);
  } catch (error) {
    toast("could not change captions", true);
  }
}

async function startStream() {
  const input = $("url-input");
  const url = input.value.trim();
  if (!url) {
    toast("paste a stream URL first", true);
    input.focus();
    return;
  }
  const cookies = $("cookies-select").value;
  const button = $("url-start");
  button.disabled = true;
  try {
    const response = await fetch("/api/stream", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url, cookies }),
    });
    const data = await response.json();
    if (!data.ok) {
      toast(data.error || "could not start that stream", true);
      return;
    }
    urlDirty = false;
    cookiesDirty = false;
    toast(
      "connecting…",
      false,
      cookies
        ? `using ${cookies} cookies · the first segments arrive in a few seconds`
        : "the first segments arrive in a few seconds",
    );
  } catch (error) {
    toast("dashboard lost contact with the clipper", true);
  } finally {
    setTimeout(() => {
      button.disabled = false;
    }, 1200);
  }
}

async function stopStream() {
  try {
    await fetch("/api/stream/stop", { method: "POST" });
    toast("stream stopped", false, "clips already saved stay in the grid");
  } catch (error) {
    toast("could not stop the stream", true);
  }
}

async function startDemo() {
  try {
    const response = await fetch("/api/demo", { method: "POST" });
    const data = await response.json();
    if (!data.ok) {
      toast(data.error || "could not start the demo", true);
      return;
    }
    toast("starting the demo…", false, "fake live stream, ~30s to the first clip");
  } catch (error) {
    toast("could not start the demo", true);
  }
}

async function exportVertical(clip, button) {
  button.disabled = true;
  button.textContent = "…";
  try {
    const response = await fetch("/api/vertical", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: clip.name }),
    });
    const data = await response.json();
    if (!data.ok) {
      toast(data.error || "could not render 9:16", true);
      button.disabled = false;
      button.textContent = "9:16";
      return;
    }
    toast("rendering 9:16…", false, "it appears on this clip in a few seconds");
  } catch (error) {
    toast("could not render 9:16", true);
    button.disabled = false;
    button.textContent = "9:16";
  }
}

async function deleteClip(clip) {
  if (!confirm(`Delete ${clip.name} ?`)) return;
  try {
    const response = await fetch("/api/delete", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: clip.name }),
    });
    const data = await response.json();
    if (!data.ok) {
      toast("could not delete that clip", true);
      return;
    }
    knownClips.delete(clip.name);
    clipSig = "";
    toast("clip deleted");
    tick();
  } catch (error) {
    toast("could not delete that clip", true);
  }
}

async function revealFolder() {
  try {
    await fetch("/api/reveal", { method: "POST" });
  } catch (error) {
    toast("could not open the folder", true);
  }
}

/* -- player --------------------------------------------------------------- */
function openPlayer(clip) {
  const player = $("player");
  player.src = clip.url;
  $("modal-title").textContent = `${clip.name} · ${fmtDur(clip.duration)}`;
  $("modal-download").href = clip.url;
  $("modal-download").download = clip.name;
  $("modal").hidden = false;
  player.play().catch(() => {});
}

function closePlayer() {
  const player = $("player");
  player.pause();
  player.removeAttribute("src");
  player.load();
  $("modal").hidden = true;
}

/* -- wiring --------------------------------------------------------------- */
$("clip-btn").addEventListener("click", doClip);
$("logout-btn").addEventListener("click", async () => {
  try {
    await fetch("/api/logout", { method: "POST" });
  } catch (error) {}
  location.href = "/login";
});
$("live-mute").addEventListener("click", () => {
  const video = $("live-video");
  video.muted = !video.muted;
  if (!video.muted) video.play().catch(() => {});
  $("live-mute").textContent = video.muted ? "unmute" : "mute";
  $("live-mute").title = video.muted ? "enable sound" : "mute sound";
});
$("url-start").addEventListener("click", startStream);
$("url-stop").addEventListener("click", stopStream);
$("url-demo").addEventListener("click", startDemo);
$("url-input").addEventListener("input", () => {
  urlDirty = true;
});$("cookies-select").addEventListener("change", () => {
  cookiesDirty = true;
});
$("url-input").addEventListener("keydown", (event) => {
  if (event.key === "Enter") {
    event.preventDefault();
    startStream();
  }
});
$("auto-toggle").addEventListener("change", (event) => toggleAuto(event.target.checked));
$("captions-toggle").addEventListener("change", (event) =>
  toggleCaptions(event.target.checked),
);
$("reveal-btn").addEventListener("click", revealFolder);
$("refresh-btn").addEventListener("click", () => {
  clipSig = "";
  tick();
});
$("modal-close").addEventListener("click", closePlayer);
$("modal").addEventListener("click", (event) => {
  if (event.target === $("modal")) closePlayer();
});

document.addEventListener("keydown", (event) => {
  const tag = (event.target.tagName || "").toLowerCase();
  const typing = tag === "input" || tag === "textarea" || tag === "select";
  if (event.key === "Escape" && !$("modal").hidden) {
    closePlayer();
    return;
  }
  if ((event.code === "Space" || event.key === "c") && !typing && $("modal").hidden) {
    event.preventDefault();
    doClip();
  }
});
