// market-music — dynamic background: a generative sky that shifts with the
// time of day, weather, and market mood. Motion intensity tracks the current
// energy (an ambient "visualizer" — Spotify's DRM stream can't be sampled for
// real audio-reactive FFT, so we drive it from the mood's energy instead).
(() => {
  "use strict";
  const canvas = document.getElementById("bgCanvas");
  if (!canvas) return;
  const ctx = canvas.getContext("2d");

  // Sky keyframes by local hour: [topRGB, bottomRGB]. Interpolated between.
  const SKY = [
    { h: 0,  top: [8, 10, 24],    bot: [16, 16, 36] },    // deep night
    { h: 5,  top: [28, 24, 58],   bot: [70, 46, 78] },    // pre-dawn
    { h: 7,  top: [86, 104, 160], bot: [240, 162, 120] }, // sunrise
    { h: 11, top: [70, 132, 212], bot: [176, 206, 236] }, // midday
    { h: 16, top: [84, 124, 198], bot: [214, 176, 152] }, // afternoon
    { h: 18, top: [64, 52, 112],  bot: [236, 120, 92] },  // sunset
    { h: 20, top: [26, 28, 64],   bot: [54, 42, 84] },    // dusk
    { h: 22, top: [10, 12, 30],   bot: [22, 20, 46] },    // night
    { h: 24, top: [8, 10, 24],    bot: [16, 16, 36] },    // wraps to night
  ];

  const lerp = (a, b, t) => a + (b - a) * t;
  const lerpRGB = (a, b, t) => [lerp(a[0], b[0], t), lerp(a[1], b[1], t), lerp(a[2], b[2], t)];

  function skyAt(hour) {
    let lo = SKY[0], hi = SKY[SKY.length - 1];
    for (let i = 0; i < SKY.length - 1; i++) {
      if (hour >= SKY[i].h && hour <= SKY[i + 1].h) { lo = SKY[i]; hi = SKY[i + 1]; break; }
    }
    const t = (hour - lo.h) / Math.max(0.001, hi.h - lo.h);
    return { top: lerpRGB(lo.top, hi.top, t), bot: lerpRGB(lo.bot, hi.bot, t) };
  }

  function accentRGB() {
    const c = getComputedStyle(document.documentElement).getPropertyValue("--accent").trim();
    const m = c.match(/^#?([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i);
    return m ? [parseInt(m[1], 16), parseInt(m[2], 16), parseInt(m[3], 16)] : [120, 124, 255];
  }
  const rgba = (c, a) => `rgba(${c[0] | 0},${c[1] | 0},${c[2] | 0},${a})`;

  // --- state (target values; current values ease toward them) ---
  const target = { hour: 22, isPrecip: false, code: 0, energy: 0.4, accent: [120, 124, 255] };
  const cur = { top: [12, 14, 32], bot: [20, 20, 46], glow: [120, 124, 255], energy: 0.4 };
  let particles = [];
  let mode = "stars";       // "rain" | "snow" | "stars" | "clouds" | "none"
  let W = 0, H = 0, paused = false, t0 = 0;

  function resize() {
    const dpr = Math.min(2, window.devicePixelRatio || 1);
    W = canvas.clientWidth; H = canvas.clientHeight;
    canvas.width = W * dpr; canvas.height = H * dpr;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    buildParticles();
  }

  function pickMode() {
    const c = target.code, hour = target.hour, night = hour < 6 || hour >= 20;
    if (target.isPrecip) {
      // snow codes 71-77, 85-86; everything else precipitating = rain
      if ((c >= 71 && c <= 77) || c === 85 || c === 86) return "snow";
      return "rain";
    }
    if (c >= 1 && c <= 3) return "clouds";   // partly cloudy / overcast
    if (c === 45 || c === 48) return "clouds"; // fog -> soft clouds
    return night ? "stars" : "none";
  }

  function buildParticles() {
    mode = pickMode();
    particles = [];
    const area = W * H;
    if (mode === "rain") {
      const n = Math.min(260, Math.floor(area / 4500));
      for (let i = 0; i < n; i++) particles.push({ x: Math.random() * W, y: Math.random() * H, len: 8 + Math.random() * 14, v: 6 + Math.random() * 8 });
    } else if (mode === "snow") {
      const n = Math.min(180, Math.floor(area / 7000));
      for (let i = 0; i < n; i++) particles.push({ x: Math.random() * W, y: Math.random() * H, r: 1 + Math.random() * 2.4, v: 0.5 + Math.random() * 1.2, drift: Math.random() * 2 - 1 });
    } else if (mode === "stars") {
      const n = Math.min(160, Math.floor(area / 9000));
      for (let i = 0; i < n; i++) particles.push({ x: Math.random() * W, y: Math.random() * H * 0.85, r: 0.5 + Math.random() * 1.4, tw: Math.random() * Math.PI * 2 });
    } else if (mode === "clouds") {
      const n = Math.min(7, Math.floor(area / 90000) + 2);
      for (let i = 0; i < n; i++) particles.push({ x: Math.random() * W, y: 40 + Math.random() * H * 0.5, r: 60 + Math.random() * 120, v: 0.1 + Math.random() * 0.25 });
    }
  }

  function draw(ts) {
    if (!t0) t0 = ts;
    const time = (ts - t0) / 1000;

    // Ease current colors toward targets for smooth day/weather/mood transitions.
    const sky = skyAt(target.hour);
    const ease = 0.02;
    cur.top = lerpRGB(cur.top, sky.top, ease);
    cur.bot = lerpRGB(cur.bot, sky.bot, ease);
    cur.glow = lerpRGB(cur.glow, target.accent, ease);
    cur.energy = lerp(cur.energy, target.energy, 0.04);

    // Sky gradient.
    const g = ctx.createLinearGradient(0, 0, 0, H);
    g.addColorStop(0, rgba(cur.top, 1));
    g.addColorStop(1, rgba(cur.bot, 1));
    ctx.fillStyle = g;
    ctx.fillRect(0, 0, W, H);

    // Mood glow: a soft radial that pulses with energy (the ambient visualizer).
    const pulse = 0.5 + 0.5 * Math.sin(time * (0.6 + cur.energy * 2.2));
    const glowA = 0.12 + cur.energy * 0.20 * pulse;
    const radius = Math.max(W, H) * (0.55 + cur.energy * 0.15 * pulse);
    const rg = ctx.createRadialGradient(W * 0.32, H * 0.18, 0, W * 0.32, H * 0.18, radius);
    rg.addColorStop(0, rgba(cur.glow, glowA));
    rg.addColorStop(1, rgba(cur.glow, 0));
    ctx.fillStyle = rg;
    ctx.fillRect(0, 0, W, H);

    // Particles — speed scales with energy.
    const spd = 0.6 + cur.energy * 1.1;
    if (mode === "rain") {
      ctx.strokeStyle = rgba([180, 200, 230], 0.35); ctx.lineWidth = 1;
      ctx.beginPath();
      for (const p of particles) {
        ctx.moveTo(p.x, p.y); ctx.lineTo(p.x - 1.5, p.y + p.len);
        p.y += p.v * spd * 2; p.x -= 0.6 * spd;
        if (p.y > H) { p.y = -p.len; p.x = Math.random() * W; }
      }
      ctx.stroke();
    } else if (mode === "snow") {
      ctx.fillStyle = rgba([235, 240, 255], 0.7);
      for (const p of particles) {
        ctx.beginPath(); ctx.arc(p.x, p.y, p.r, 0, Math.PI * 2); ctx.fill();
        p.y += p.v * spd; p.x += Math.sin(time + p.drift) * 0.4;
        if (p.y > H) { p.y = -4; p.x = Math.random() * W; }
      }
    } else if (mode === "stars") {
      for (const p of particles) {
        const a = 0.35 + 0.45 * (0.5 + 0.5 * Math.sin(time * 1.5 + p.tw));
        ctx.fillStyle = rgba([255, 255, 255], a);
        ctx.beginPath(); ctx.arc(p.x, p.y, p.r, 0, Math.PI * 2); ctx.fill();
      }
    } else if (mode === "clouds") {
      for (const p of particles) {
        const cg = ctx.createRadialGradient(p.x, p.y, 0, p.x, p.y, p.r);
        cg.addColorStop(0, rgba([200, 205, 220], 0.10));
        cg.addColorStop(1, rgba([200, 205, 220], 0));
        ctx.fillStyle = cg;
        ctx.beginPath(); ctx.arc(p.x, p.y, p.r, 0, Math.PI * 2); ctx.fill();
        p.x += p.v * spd; if (p.x - p.r > W) p.x = -p.r;
      }
    }
  }

  // Single self-guarded loop: a duplicate start() while running is a no-op, and
  // the loop cleanly stops when paused (no orphaned rAF chains on tab toggles).
  let running = false;
  function frame(ts) {
    if (paused) { running = false; return; }
    draw(ts);
    requestAnimationFrame(frame);
  }
  function start() {
    if (running) return;
    running = true; t0 = 0;
    requestAnimationFrame(frame);
  }

  // --- public API ---
  window.Scene = {
    update(state) {
      if (!state) return;
      if (state.time_ctx && typeof state.time_ctx.hour === "number") target.hour = state.time_ctx.hour;
      const w = state.weather;
      const prevMode = mode;
      target.isPrecip = !!(w && w.is_precip);
      target.code = w ? (w.code | 0) : 0;
      const plan = state.music_plan;
      if (plan && typeof plan.target_energy === "number") target.energy = plan.target_energy;
      target.accent = accentRGB();
      // Rebuild particles only when the weather scene type actually changes.
      if (pickMode() !== prevMode) buildParticles();
    },
  };

  window.addEventListener("resize", resize);
  document.addEventListener("visibilitychange", () => {
    paused = document.hidden;
    if (!paused) start();
  });
  resize();
  start();
})();
