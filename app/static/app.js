// market-music — front-end: poll /api/state and render the live mood.
(() => {
  "use strict";

  const EMOJI = {
    euphoric: "🤩", happy: "😄", calm: "😌", anxious: "😬", fearful: "😨",
    angry: "😡", surprised: "😲", chaotic: "🌀", depressed: "😞", manic: "🤪",
  };

  const $ = (id) => document.getElementById(id);

  // Build an element with text set via textContent (never innerHTML) so
  // API-derived strings (track/artist names, etc.) can't inject markup.
  function makeEl(tag, className, text) {
    const e = document.createElement(tag);
    if (className) e.className = className;
    if (text != null) e.textContent = text;
    return e;
  }
  // Only allow http(s) URLs to be assigned to href/src (blocks javascript:).
  function safeHttpUrl(url) {
    if (typeof url !== "string" || !url) return "";
    try {
      const u = new URL(url, location.origin);
      return (u.protocol === "http:" || u.protocol === "https:") ? u.href : "";
    } catch (_) { return ""; }
  }

  let loggedIn = false;
  let autoSync = false;
  let lastEmotion = null;

  // --- helpers -------------------------------------------------------------
  function toast(msg, isError = false) {
    const t = $("toast");
    t.textContent = msg;
    t.classList.toggle("error", isError);
    t.classList.add("show");
    clearTimeout(t._timer);
    t._timer = setTimeout(() => t.classList.remove("show"), 3500);
  }

  function fmtPct(v) {
    if (v === null || v === undefined) return "—";
    return (v >= 0 ? "+" : "") + v.toFixed(2) + "%";
  }

  function timeAgo(iso) {
    if (!iso) return "—";
    const secs = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
    if (secs < 60) return `${Math.floor(secs)}s ago`;
    if (secs < 3600) return `${Math.floor(secs / 60)}m ago`;
    return `${Math.floor(secs / 3600)}h ago`;
  }

  async function api(path, opts) {
    const resp = await fetch(path, opts);
    let data = null;
    try { data = await resp.json(); } catch (_) {}
    if (!resp.ok) {
      const msg = (data && data.error) || `Request failed (${resp.status})`;
      throw new Error(msg);
    }
    return data;
  }

  // --- renderers -----------------------------------------------------------
  function renderEmotion(emotion, plan, updatedAt) {
    if (!emotion) return;
    const name = emotion.emotion;
    document.documentElement.setAttribute("data-emotion", name);
    $("emoji").textContent = EMOJI[name] || "·";
    $("emotionName").textContent = name;
    $("confFill").style.width = Math.round((emotion.confidence || 0) * 100) + "%";
    $("confidenceLabel").textContent = `confidence ${Math.round((emotion.confidence || 0) * 100)}%`;
    $("summary").textContent = emotion.summary || "";
    $("narrative").textContent = plan ? plan.narrative : "";
    $("updatedAt").textContent = "updated " + timeAgo(updatedAt);
    $("planSource").textContent = plan ? `music: ${plan.source}` : "";

    if (lastEmotion && lastEmotion !== name) {
      toast(`Market mood shifted: ${lastEmotion} → ${name}`);
    }
    lastEmotion = name;
  }

  function setMeter(fillId, valId, value, bidirectional, asPct) {
    const fill = $(fillId);
    const valEl = $(valId);
    if (value === null || value === undefined) { valEl.textContent = "—"; return; }
    if (bidirectional) {
      // value in -1..1 -> fill from center
      const half = Math.min(50, Math.abs(value) * 50);
      if (value >= 0) { fill.style.left = "50%"; fill.style.width = half + "%"; }
      else { fill.style.left = (50 - half) + "%"; fill.style.width = half + "%"; }
      valEl.textContent = (value >= 0 ? "+" : "") + value.toFixed(2);
    } else {
      fill.style.left = "0%";
      fill.style.width = Math.min(100, Math.max(0, value * 100)) + "%";
      valEl.textContent = asPct ? Math.round(value * 100) + "%" : value.toFixed(2);
    }
  }

  function renderSnapshot(snap) {
    if (!snap) return;
    setMeter("riskFill", "riskVal", snap.risk_on_score, true);
    setMeter("volFill", "volVal", snap.vol_shock_score, false, true);
    setMeter("dispFill", "dispVal", snap.dispersion, false, true);
    setMeter("revFill", "revVal", snap.reversal_score, true);

    const grid = $("assetGrid");
    grid.innerHTML = "";
    Object.values(snap.assets || {}).forEach((a) => {
      const dir = a.trend || "flat";
      const sub = [];
      if (a.change_5m != null) sub.push("5m " + fmtPct(a.change_5m));
      if (a.change_1h != null) sub.push("1h " + fmtPct(a.change_1h));

      let symText = a.symbol;
      if (a.error) symText += " · error";
      else if (!a.market_open) symText += " · closed (last session)";

      const left = makeEl("div");
      left.appendChild(makeEl("div", "a-name", a.name));
      left.appendChild(makeEl("div", "a-sym", symText));

      const right = makeEl("div", "a-right");
      right.appendChild(makeEl("div", "a-chg " + dir, fmtPct(a.change_pct)));
      right.appendChild(makeEl("div", "a-price",
        (a.price != null) ? a.price.toLocaleString(undefined, {maximumFractionDigits: 2}) : "—"));
      right.appendChild(makeEl("div", "a-sub", sub.join(" · ")));

      const row = makeEl("div", "asset");
      row.appendChild(left);
      row.appendChild(right);
      grid.appendChild(row);
    });
  }

  function renderHistory(history) {
    const tl = $("timeline");
    tl.innerHTML = "";
    if (!history || !history.length) {
      tl.appendChild(makeEl("span", "placeholder", "No transitions yet."));
      return;
    }
    history.slice().reverse().forEach((h) => {
      const item = makeEl("div", "tl-item");
      item.appendChild(makeEl("span", "tl-emotion", `${EMOJI[h.emotion] || ""} ${h.emotion}`));
      item.appendChild(makeEl("span", "tl-time", timeAgo(h.ts)));
      tl.appendChild(item);
    });
  }

  function renderPlaylist(playlist) {
    const el = $("playlist");
    el.innerHTML = "";
    if (!playlist || !playlist.tracks || !playlist.tracks.length) {
      el.appendChild(makeEl("span", "placeholder",
        "No playlist yet. Connect Spotify and hit “Sync to mood”."));
      return;
    }
    playlist.tracks.forEach((t) => {
      const link = makeEl("a", "track");
      const href = safeHttpUrl(t.url);
      if (href) { link.href = href; link.target = "_blank"; link.rel = "noopener noreferrer"; }

      const img = document.createElement("img");
      img.alt = "";
      const imgSrc = safeHttpUrl(t.image);
      if (imgSrc) img.src = imgSrc; else img.style.visibility = "hidden";
      img.addEventListener("error", () => { img.style.visibility = "hidden"; });

      const meta = makeEl("div");
      meta.appendChild(makeEl("div", "t-name", t.name));
      meta.appendChild(makeEl("div", "t-artist", t.artist));

      link.appendChild(img);
      link.appendChild(meta);
      el.appendChild(link);
    });
  }

  // --- Spotify controls ----------------------------------------------------
  function renderControls(session, spotifyConfigured) {
    const c = $("spotifyControls");
    if (!spotifyConfigured) {
      c.innerHTML = '<span class="placeholder">Spotify is not configured on the server (set SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET).</span>';
      return;
    }
    if (!session.logged_in) {
      c.innerHTML = '<a class="btn spotify" href="/auth/login">Connect Spotify to build playlists</a>';
      return;
    }
    // Logged in: only (re)build controls once.
    if (c.dataset.built === "1") return;
    c.dataset.built = "1";
    c.innerHTML = `
      <div class="row">
        <button class="btn primary" id="syncBtn">⟳ Sync to mood</button>
        <button class="btn" id="playBtn">▶ Play</button>
        <select id="deviceSelect"><option value="">Active device</option></select>
      </div>
      <label class="toggle"><input type="checkbox" id="autoSync"/> Auto-sync playlist when the mood changes</label>`;

    $("syncBtn").addEventListener("click", doSync);
    $("playBtn").addEventListener("click", doPlay);
    $("autoSync").addEventListener("change", toggleAutoSync);
    $("deviceSelect").addEventListener("focus", loadDevices);
    loadDevices();
  }

  async function doSync() {
    const btn = $("syncBtn");
    btn.disabled = true; btn.textContent = "⟳ Syncing…";
    try {
      const data = await api("/api/playlist/sync", { method: "POST" });
      renderPlaylist(data.playlist);
      toast(`Playlist synced to “${data.playlist.updated_for_emotion}” (${data.playlist.track_count} tracks)`);
    } catch (e) { toast(e.message, true); }
    finally { btn.disabled = false; btn.textContent = "⟳ Sync to mood"; }
  }

  async function doPlay() {
    const btn = $("playBtn");
    const device_id = $("deviceSelect").value || null;
    btn.disabled = true;
    try {
      await api("/api/play", { method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({ device_id }) });
      toast("Playback started 🎧");
    } catch (e) { toast(e.message, true); }
    finally { btn.disabled = false; }
  }

  async function toggleAutoSync(ev) {
    const on = ev.target.checked;
    try {
      const data = await api("/api/settings", { method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({ auto_sync: on }) });
      toast(data.auto_sync ? "Auto-sync on — playlist follows the market." : "Auto-sync off.");
    } catch (e) { ev.target.checked = !on; toast(e.message, true); }
  }

  async function loadDevices() {
    try {
      const data = await api("/api/devices");
      const sel = $("deviceSelect");
      const current = sel.value;
      sel.innerHTML = '<option value="">Active device</option>';
      (data.devices || []).forEach((d) => {
        const o = document.createElement("option");
        o.value = d.id; o.textContent = `${d.name} (${d.type})${d.is_active ? " •" : ""}`;
        sel.appendChild(o);
      });
      sel.value = current;
    } catch (_) { /* ignore device load errors */ }
  }

  async function logout() {
    try { await api("/auth/logout", { method: "POST" }); location.href = "/"; }
    catch (e) { toast(e.message, true); }
  }

  // --- weather -------------------------------------------------------------
  function renderWeather(weather) {
    const now = $("weatherNow");
    const clearBtn = $("clearLocBtn");
    now.innerHTML = "";
    if (!weather) {
      now.appendChild(makeEl("span", "muted",
        "Add your location to fold local weather into the mood."));
      if (clearBtn) clearBtn.hidden = true;
      return;
    }
    const temp = (weather.temp_f != null) ? `${Math.round(weather.temp_f)}°F` : "";
    const txt = makeEl("div");
    txt.appendChild(makeEl("div", "w-main", weather.condition + (temp ? " · " + temp : "")));
    txt.appendChild(makeEl("div", "w-sub", weather.location_name));
    txt.appendChild(makeEl("div", "w-factor",
      weather.is_precip ? "tinting the mood cozier" : "factored into the mood"));
    now.appendChild(makeEl("span", "w-emoji", weather.emoji || "🌡️"));
    now.appendChild(txt);
    if (clearBtn) clearBtn.hidden = false;
    // Prefill the input once, but never clobber what the user is typing.
    const input = $("locationInput");
    if (input && document.activeElement !== input && !input.value) {
      input.value = weather.query || "";
    }
  }

  async function submitLocation(query) {
    const btn = $("setLocBtn");
    btn.disabled = true; const label = btn.textContent; btn.textContent = "…";
    try {
      const data = await api("/api/location", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ query }),
      });
      renderWeather(data.weather);
      toast(`Weather set: ${data.weather.condition} in ${data.weather.location_name}`);
      tick();
    } catch (e) { toast(e.message, true); }
    finally { btn.disabled = false; btn.textContent = label; }
  }

  // --- poll loop -----------------------------------------------------------
  async function tick() {
    try {
      const s = await api("/api/state");
      renderEmotion(s.emotion, s.music_plan, s.updated_at);
      renderSnapshot(s.snapshot);
      renderWeather(s.weather);
      renderHistory(s.history);

      const session = s.session || {};
      loggedIn = session.logged_in;
      renderControls(session, s.config.spotify_configured);
      if (session.playlist) renderPlaylist(session.playlist);

      // sync auto-sync checkbox state if present
      const autoBox = $("autoSync");
      if (autoBox && autoBox.checked !== session.auto_sync) autoBox.checked = session.auto_sync;

      if (s.last_error) $("footStatus").textContent = "⚠ " + s.last_error;
      else $("footStatus").textContent = `polling every ${window.__POLL_INTERVAL__}s · ${s.poll_count} polls`;
    } catch (e) {
      $("footStatus").textContent = "⚠ " + e.message;
    }
  }

  // wire up static logout button (rendered server-side)
  const lo = $("logoutBtn");
  if (lo) lo.addEventListener("click", logout);

  // wire up the weather/location form
  const wForm = $("weatherForm");
  if (wForm) wForm.addEventListener("submit", (e) => {
    e.preventDefault();
    const q = $("locationInput").value.trim();
    if (q) submitLocation(q);
  });
  const clearLoc = $("clearLocBtn");
  if (clearLoc) clearLoc.addEventListener("click", async () => {
    clearLoc.disabled = true;  // guard against rapid double-clicks
    try {
      await api("/api/location/clear", { method: "POST" });
      $("locationInput").value = "";
      renderWeather(null);
      toast("Location cleared.");
      tick();
    } catch (e) { toast(e.message, true); }
    finally { clearLoc.disabled = false; }
  });

  // handle ?auth=ok / ?auth_error
  const params = new URLSearchParams(location.search);
  if (params.get("auth") === "ok") { toast("Connected to Spotify 🎉"); history.replaceState({}, "", "/"); }
  if (params.get("auth_error")) { toast("Spotify login failed: " + params.get("auth_error"), true); history.replaceState({}, "", "/"); }

  tick();
  setInterval(tick, 5000);
})();
