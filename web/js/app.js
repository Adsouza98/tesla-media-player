/* Vela cabin UI. Touch-first, landscape, no video element. */

(function () {
  const view = document.getElementById("view");
  const lock = document.getElementById("lock");
  const shell = document.getElementById("shell");
  const playerEl = document.getElementById("player");
  const canvas = document.getElementById("stage");
  const shade = document.getElementById("player-shade");
  const qualities = ["480", "720", "1080", "max"];

  const state = {
    quality: localStorage.getItem("vela-quality") || "1080",
    compat: localStorage.getItem("vela-compat") === "1",
    volume: Number(localStorage.getItem("vela-volume") || "90"),
    settings: null,
    current: null,
    hideTimer: 0,
  };

  const player = new VelaPlayer.CabinPlayer(canvas, {
    onTime: (current, duration) => {
      const seek = document.getElementById("seek");
      const clock = document.getElementById("clock");
      if (!seek.matches(":active") && document.activeElement !== seek) {
        const max = duration || current || 1;
        seek.max = String(Math.max(1, Math.round(max)));
        seek.value = String(Math.round(current));
      }
      clock.textContent = duration ? `${fmt(current)} / ${fmt(duration)}` : fmt(current);
    },
    onState: (mode) => {
      document.getElementById("play-toggle").textContent = mode === "paused" ? "Play" : "Pause";
    },
    onError: (message) => toast(message),
    onEnded: () => document.getElementById("play-toggle").textContent = "Play",
  });

  function esc(value) {
    return String(value ?? "").replace(/[&<>"']/g, (ch) => {
      const named = { "&": "amp", "<": "lt", ">": "gt", '"': "quot", "'": "#39" };
      return "&" + named[ch] + ";";
    });
  }

  function fmt(seconds) {
    seconds = Math.max(0, Math.floor(seconds || 0));
    const h = Math.floor(seconds / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    const s = seconds % 60;
    const mm = String(m).padStart(h ? 2 : 1, "0");
    const ss = String(s).padStart(2, "0");
    return h ? `${h}:${mm}:${ss}` : `${m}:${ss}`;
  }

  function toast(message) {
    const el = document.getElementById("toast");
    el.textContent = message;
    el.classList.add("show");
    clearTimeout(toast._t);
    toast._t = setTimeout(() => el.classList.remove("show"), 5200);
  }

  async function api(path, options) {
    const opts = options || {};
    const headers = opts.body && !(opts.body instanceof FormData) ? { "Content-Type": "application/json" } : {};
    const response = await fetch(path, {
      method: opts.method || "GET",
      headers,
      body: opts.body instanceof FormData ? opts.body : (opts.body ? JSON.stringify(opts.body) : undefined),
      credentials: "same-origin",
    });
    if (response.status === 401) {
      showLock();
      throw new Error("auth");
    }
    const text = await response.text();
    let data = null;
    if (text) {
      try { data = JSON.parse(text); } catch (err) { data = { detail: text }; }
    }
    if (!response.ok) {
      const detail = data && data.detail;
      throw new Error(typeof detail === "string" ? detail : "Request failed");
    }
    return data;
  }

  function showLock() {
    lock.classList.remove("hidden");
    shell.classList.add("hidden");
    closePlayer();
  }

  function showShell() {
    lock.classList.add("hidden");
    shell.classList.remove("hidden");
  }

  function httpsBanner() {
    if (VelaPlayer.webCodecsAvailable()) return "";
    return `<div class="banner">This page is not a secure context, so full H.264 playback is unavailable. Open Vela through HTTPS (Nginx Proxy Manager or Caddy). Compatibility mode still works over plain HTTP, at lower quality and higher CPU use.</div>`;
  }

  function renderHome() {
    view.innerHTML = `
      <section class="hero">
        <p class="eyebrow">Passenger deck</p>
        <h2>What should the cabin play?</h2>
        <p class="fine">High quality when the browser can take H.264. The server does the fetching, so the car only talks to your NAS.</p>
        ${httpsBanner()}
        <div class="cards">
          <a class="card" href="#/youtube"><strong>YouTube</strong><span>Search, Premium cookies, and a Max quality option. Streams stay proxied.</span></a>
          <a class="card" href="#/plex"><strong>Plex</strong><span>Libraries, shows, and direct H.264 when the file is already cabin-friendly.</span></a>
        </div>
      </section>`;
  }

  function renderYouTube() {
    view.innerHTML = `
      <section>
        <div class="page-head"><div><p class="eyebrow">YouTube</p><h2>Search</h2></div></div>
        ${httpsBanner()}
        <form class="search-row" id="yt-form">
          <input id="yt-q" type="search" placeholder="Title, channel, or paste a link" autocomplete="off" />
          <button class="primary" type="submit">Search</button>
        </form>
        <div id="yt-results" class="grid"></div>
      </section>`;
    document.getElementById("yt-form").addEventListener("submit", async (event) => {
      event.preventDefault();
      const q = document.getElementById("yt-q").value.trim();
      const box = document.getElementById("yt-results");
      box.innerHTML = `<p class="fine">Searching…</p>`;
      try {
        const data = await api(`/api/youtube/search?q=${encodeURIComponent(q)}`);
        if (!data.items.length) {
          box.innerHTML = `<p class="fine">No results.</p>`;
          return;
        }
        box.innerHTML = data.items.map((item) => `
          <button class="tile" type="button" data-yt="${esc(item.id)}" data-title="${esc(item.title)}" data-channel="${esc(item.channel)}" data-duration="${item.duration || 0}">
            <img alt="" src="${esc(item.thumbnail)}" />
            <div class="meta"><strong>${esc(item.title)}</strong><em>${esc(item.channel)} · ${item.duration ? fmt(item.duration) : ""}</em></div>
          </button>`).join("");
      } catch (err) {
        if (err.message !== "auth") box.innerHTML = `<p class="error">${esc(err.message)}</p>`;
      }
    });
  }

  async function renderPlexLibraries() {
    view.innerHTML = `<section><div class="page-head"><div><p class="eyebrow">Plex</p><h2>Libraries</h2></div></div>${httpsBanner()}<div id="plex-box" class="grid"><p class="fine">Loading…</p></div></section>`;
    try {
      const data = await api("/api/plex/libraries");
      const box = document.getElementById("plex-box");
      if (!data.items.length) {
        box.innerHTML = `<p class="fine">No movie or show libraries. Check the Plex URL and token in Settings.</p>`;
        return;
      }
      box.innerHTML = data.items.map((item) => `
        <a class="card" href="#/plex/library/${esc(item.key)}"><strong>${esc(item.title)}</strong><span>${esc(item.type)}</span></a>`).join("");
    } catch (err) {
      if (err.message !== "auth") document.getElementById("plex-box").innerHTML = `<p class="error">${esc(err.message)}</p>`;
    }
  }

  async function renderLibrary(key) {
    view.innerHTML = `
      <section>
        <div class="page-head"><div><p class="eyebrow">Plex</p><h2 id="lib-title">Library</h2></div>
          <form class="search-row" id="lib-form"><input id="lib-q" type="search" placeholder="Search this library" /><button type="submit">Search</button></form>
        </div>
        <div id="lib-box" class="grid"><p class="fine">Loading…</p></div>
      </section>`;
    const load = async (q) => {
      const data = await api(`/api/plex/library/${key}/items?q=${encodeURIComponent(q || "")}&size=48`);
      document.getElementById("lib-title").textContent = data.title || "Library";
      const box = document.getElementById("lib-box");
      box.innerHTML = data.items.map(plexTile).join("") || `<p class="fine">Nothing in here.</p>`;
    };
    document.getElementById("lib-form").addEventListener("submit", (event) => {
      event.preventDefault();
      load(document.getElementById("lib-q").value).catch((err) => toast(err.message));
    });
    try { await load(""); } catch (err) { if (err.message !== "auth") toast(err.message); }
  }

  function plexTile(item) {
    const img = item.thumb ? `/api/plex/image?path=${encodeURIComponent(item.thumb)}` : "";
    const poster = item.type === "movie" || item.type === "show" || item.type === "season" || item.type === "episode";
    const sub = [item.grandparentTitle, item.parentTitle, item.year, item.duration ? fmt(item.duration) : ""].filter(Boolean).join(" · ");
    return `
      <button class="tile ${poster ? "poster" : ""}" type="button" data-plex="${esc(item.ratingKey)}" data-type="${esc(item.type)}" data-title="${esc(item.title)}" data-duration="${item.duration || 0}">
        ${img ? `<img alt="" src="${esc(img)}" />` : `<span class="ph"></span>`}
        <div class="meta"><strong>${esc(item.title)}</strong><em>${esc(sub)}</em></div>
      </button>`;
  }

  async function renderChildren(key, kind) {
    view.innerHTML = `<section><div class="page-head"><div><p class="eyebrow">${esc(kind)}</p><h2 id="child-title">Loading…</h2></div></div><div id="child-box" class="grid"></div></section>`;
    try {
      const data = await api(`/api/plex/children/${key}`);
      document.getElementById("child-title").textContent = data.title || kind;
      document.getElementById("child-box").innerHTML = data.items.map(plexTile).join("") || `<p class="fine">Empty.</p>`;
    } catch (err) {
      if (err.message !== "auth") toast(err.message);
    }
  }

  async function renderSettings() {
    let current = state.settings;
    try { current = await api("/api/settings"); state.settings = current; } catch (err) { if (err.message === "auth") return; }
    view.innerHTML = `
      <section class="settings">
        <div><p class="eyebrow">Settings</p><h2>Cabin setup</h2></div>
        <p class="fine">Saved on the server in the config volume. The password itself stays in APP_PASSWORD, not here.</p>
        <label>Plex URL<input id="set-url" type="url" value="${esc(current && current.plex_url || "")}" placeholder="http://192.168.1.42:32400" /></label>
        <label>Plex token<input id="set-token" type="password" placeholder="${current && current.plex_token_set ? "Saved — leave blank to keep" : "Paste token"}" autocomplete="off" /></label>
        <label>Default quality
          <select id="set-quality">${qualities.map((q) => `<option value="${q}" ${current && current.default_quality === q ? "selected" : ""}>${q === "max" ? "Max" : q + "p"}</option>`).join("")}</select>
        </label>
        <p class="fine">YouTube cookies: ${current && current.youtube_cookies ? "loaded" : "not loaded"}. Export a Netscape cookies.txt while signed into YouTube Premium, then upload it.</p>
        <label>Cookies file<input id="set-cookies" type="file" accept=".txt,text/plain" /></label>
        <div class="row-actions">
          <button id="set-save" class="primary" type="button">Save</button>
          <button id="set-plex" type="button">Test Plex</button>
          <button id="set-out" type="button">Sign out</button>
        </div>
        <p id="set-note" class="fine"></p>
      </section>`;
    document.getElementById("set-save").addEventListener("click", saveSettings);
    document.getElementById("set-plex").addEventListener("click", testPlex);
    document.getElementById("set-out").addEventListener("click", async () => {
      await api("/api/auth/logout", { method: "POST", body: {} });
      showLock();
    });
  }

  async function saveSettings() {
    const note = document.getElementById("set-note");
    try {
      const body = {
        plex_url: document.getElementById("set-url").value.trim(),
        default_quality: document.getElementById("set-quality").value,
      };
      const token = document.getElementById("set-token").value.trim();
      if (token) body.plex_token = token;
      state.settings = await api("/api/settings", { method: "PUT", body });
      const file = document.getElementById("set-cookies").files[0];
      if (file) {
        const form = new FormData();
        form.append("file", file);
        await api("/api/youtube/cookies", { method: "POST", body: form });
      }
      state.quality = state.settings.default_quality || state.quality;
      note.textContent = "Saved.";
    } catch (err) {
      if (err.message !== "auth") note.textContent = err.message;
    }
  }

  async function testPlex() {
    const note = document.getElementById("set-note");
    try {
      const status = await api("/api/plex/status");
      note.textContent = status.ok ? `Connected to ${status.name}.` : status.error;
    } catch (err) {
      if (err.message !== "auth") note.textContent = err.message;
    }
  }

  function route() {
    const hash = location.hash || "#/";
    document.querySelectorAll("[data-nav]").forEach((link) => {
      link.classList.toggle("active", hash.startsWith(`#/${link.dataset.nav}`));
    });
    if (hash.startsWith("#/youtube")) return renderYouTube();
    if (hash.startsWith("#/plex/library/")) return renderLibrary(hash.split("/")[3]);
    if (hash.startsWith("#/plex/show/")) return renderChildren(hash.split("/")[3], "Show");
    if (hash.startsWith("#/plex/season/")) return renderChildren(hash.split("/")[3], "Season");
    if (hash.startsWith("#/settings")) return renderSettings();
    if (hash.startsWith("#/plex")) return renderPlexLibraries();
    return renderHome();
  }

  function qualityButtons() {
    const box = document.getElementById("qualities");
    box.innerHTML = qualities.map((q) => `<button type="button" data-quality="${q}" aria-pressed="${q === state.quality}">${q === "max" ? "Max" : q}</button>`).join("");
  }

  function streamUrls(source) {
    const q = encodeURIComponent(state.quality);
    const start = Math.floor(source.start || 0);
    const compat = state.compat || !VelaPlayer.webCodecsAvailable();
    if (source.kind === "youtube") {
      return {
        url: `/api/youtube/stream/${source.id}?quality=${q}&start=${start}`,
        compatUrl: `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/api/compat/youtube/${source.id}?quality=${q}&start=${start}`,
        compat,
      };
    }
    return {
      url: `/api/plex/stream/${source.id}?quality=${q}&start=${start}`,
      compatUrl: `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/api/compat/plex/${source.id}?quality=${q}&start=${start}`,
      compat,
    };
  }

  async function openPlayer(source) {
    state.current = source;
    playerEl.classList.remove("hidden");
    document.getElementById("player-title").textContent = source.title || "Playing";
    document.getElementById("player-kicker").textContent = source.kicker || "";
    document.getElementById("player-compat").setAttribute("aria-pressed", state.compat || !VelaPlayer.webCodecsAvailable() ? "true" : "false");
    qualityButtons();
    shade.classList.remove("idle");
    const built = streamUrls(source);
    try {
      await player.play({
        url: built.url,
        compatUrl: built.compatUrl,
        compat: built.compat,
        duration: source.duration || 0,
        start: source.start || 0,
      });
    } catch (err) {
      toast(err.message || "Playback failed");
    }
  }

  function closePlayer() {
    player.stop();
    playerEl.classList.add("hidden");
    state.current = null;
  }

  document.getElementById("lock-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const error = document.getElementById("lock-error");
    error.textContent = "";
    try {
      await api("/api/auth/login", { method: "POST", body: { password: document.getElementById("password").value } });
      document.getElementById("password").value = "";
      showShell();
      route();
    } catch (err) {
      error.textContent = err.message === "auth" ? "Wrong password" : err.message;
    }
  });

  document.getElementById("open-settings").addEventListener("click", () => { location.hash = "#/settings"; });
  document.getElementById("player-back").addEventListener("click", closePlayer);
  document.getElementById("play-toggle").addEventListener("click", () => player.toggle());
  document.getElementById("skip-back").addEventListener("click", () => {
    const now = Number(document.getElementById("seek").value) || 0;
    if (state.current) openPlayer({ ...state.current, start: Math.max(0, now - 15) });
  });
  document.getElementById("skip-fwd").addEventListener("click", () => {
    const now = Number(document.getElementById("seek").value) || 0;
    if (state.current) openPlayer({ ...state.current, start: now + 30 });
  });
  document.getElementById("seek").addEventListener("change", () => {
    if (state.current) openPlayer({ ...state.current, start: Number(document.getElementById("seek").value) || 0 });
  });
  document.getElementById("volume").addEventListener("input", (event) => {
    state.volume = Number(event.target.value);
    localStorage.setItem("vela-volume", String(state.volume));
    player.setVolume(state.volume / 100);
  });
  document.getElementById("player-compat").addEventListener("click", () => {
    state.compat = !state.compat;
    localStorage.setItem("vela-compat", state.compat ? "1" : "0");
    if (state.current) openPlayer(state.current);
  });
  document.getElementById("qualities").addEventListener("click", (event) => {
    const button = event.target.closest("[data-quality]");
    if (!button) return;
    state.quality = button.dataset.quality;
    localStorage.setItem("vela-quality", state.quality);
    const now = Number(document.getElementById("seek").value) || 0;
    if (state.current) openPlayer({ ...state.current, start: now });
  });

  view.addEventListener("click", (event) => {
    const yt = event.target.closest("[data-yt]");
    if (yt) {
      openPlayer({
        kind: "youtube",
        id: yt.dataset.yt,
        title: yt.dataset.title,
        kicker: yt.dataset.channel,
        duration: Number(yt.dataset.duration) || 0,
        start: 0,
      });
      return;
    }
    const plex = event.target.closest("[data-plex]");
    if (!plex) return;
    const type = plex.dataset.type;
    const id = plex.dataset.plex;
    if (type === "show") location.hash = `#/plex/show/${id}`;
    else if (type === "season") location.hash = `#/plex/season/${id}`;
    else if (type === "movie" || type === "episode") {
      openPlayer({
        kind: "plex",
        id,
        title: plex.dataset.title,
        kicker: type,
        duration: Number(plex.dataset.duration) || 0,
        start: 0,
      });
    }
  });

  function bumpChrome() {
    shade.classList.remove("idle");
    clearTimeout(state.hideTimer);
    state.hideTimer = setTimeout(() => {
      if (!playerEl.classList.contains("hidden")) shade.classList.add("idle");
    }, 4000);
  }
  playerEl.addEventListener("pointerdown", bumpChrome);
  shade.addEventListener("pointermove", bumpChrome);

  window.addEventListener("hashchange", () => { if (!shell.classList.contains("hidden")) route(); });
  window.addEventListener("resize", () => player.fit());
  document.getElementById("volume").value = String(state.volume);
  player.setVolume(state.volume / 100);

  api("/api/auth/me").then((me) => {
    if (me.authenticated) {
      showShell();
      route();
    } else showLock();
  }).catch(() => showLock());
})();
