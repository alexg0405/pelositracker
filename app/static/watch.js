const esc = v => String(v ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"})[c]);
  const WATCH = {
    mlb:[["MLB.TV","https://www.mlb.com/tv"],["ESPN","https://www.espn.com/watch/"]],
    wnba:[["WNBA League Pass","https://www.wnba.com/league-pass"]],
    nba:[["NBA League Pass","https://www.nba.com/watch"]],
    nfl:[["NFL+","https://www.nfl.com/plus/"]],
    nhl:[["ESPN+","https://www.espn.com/watch/"]],
    mls:[["MLS Season Pass","https://tv.apple.com/mls"]],
    epl:[["Peacock (official)","https://www.peacocktv.com/sports/premier-league"]],
    soccer:[["ESPN+ / official","https://www.espn.com/watch/"]],
    ufc:[["UFC Fight Pass","https://ufcfightpass.com"],["ESPN+","https://www.espn.com/watch/"]],
    boxing:[["DAZN","https://www.dazn.com"]],
    tennis:[["Tennis Channel","https://www.tennischannel.com"],["ESPN+","https://www.espn.com/watch/"]],
    golf:[["ESPN+ / Golf Channel","https://www.espn.com/watch/"]],
    nascar:[["How to watch (official)","https://www.nascar.com/how-to-watch/"]],
  };
  let games = [], selected = null, hls = null, activeLeague = "all";

  function whereToWatch(league, title){
    const lk = (league||"").toLowerCase();
    let list = [];
    for (const k in WATCH){ if (lk.includes(k)){ list = WATCH[k].slice(); break; } }
    list.push(["Search official streams", "https://www.google.com/search?q=" + encodeURIComponent(title + " live stream official")]);
    return list;
  }

  function renderLeagues(leagues, hasLive){
    const el = document.getElementById("league-filter");
    if (leagues.length <= 1 && !hasLive){ el.innerHTML = ""; return; }  // nothing to filter
    const pill = (k, label, cls="") => `<button class="pill${cls}${activeLeague===k?" active":""}" data-league="${esc(k)}">${esc(label)}</button>`;
    let html = pill("all", "All");
    if (hasLive) html += pill("__live__", "● Live", " live-pill");
    html += leagues.map(lg => pill(lg, lg)).join("");
    el.innerHTML = html;
  }

  function statusBadge(game){
    if (game.status === "live") return '<span class="g-live">● LIVE</span> ';
    if (game.status === "started") return '<span class="g-started">◌ STARTED · VERIFYING</span> ';
    return "";
  }

  function statusLabel(game){
    if (game.status === "live") return "● Live now";
    if (game.status === "started") return "Started · awaiting live-feed confirmation";
    return "Upcoming";
  }

  function render(){
    const q = (document.getElementById("search").value||"").toLowerCase();
    const el = document.getElementById("games");
    if (!games.length){ el.innerHTML = '<div class="muted">No games right now.</div>'; document.getElementById("league-filter").innerHTML = ""; return; }
    const leagues = [...new Set(games.map(g => g.league || "Sports"))].sort();
    const hasLive = games.some(g => g.status === "live");
    if (activeLeague === "__live__" && !hasLive) activeLeague = "all";
    if (activeLeague !== "all" && activeLeague !== "__live__" && !leagues.includes(activeLeague)) activeLeague = "all";
    renderLeagues(leagues, hasLive);
    const shown = games.filter(g => {
      const inFilter = activeLeague === "all" ? true
        : activeLeague === "__live__" ? g.status === "live"
        : (g.league || "Sports") === activeLeague;
      const inSearch = !q || `${g.title} ${g.league||""}`.toLowerCase().includes(q);
      return inFilter && inSearch;
    });
    el.innerHTML = shown.map(g => `<div class="game${selected&&selected.slug===g.slug?" active":""}" data-slug="${esc(g.slug)}" title="${esc(g.title)}">
      <div><div class="g-title">${esc(g.title)}</div><div class="g-league">${statusBadge(g)}${esc(g.league||"sports")}</div></div></div>`).join("")
      || '<div class="muted">No games match.</div>';
  }

  function select(g){
    selected = g; render();
    document.getElementById("now").textContent = g.title;
    const links = whereToWatch(g.league, g.title)
      .map(([name,url]) => `<a href="${esc(url)}" target="_blank" rel="noopener">${esc(name)} ↗</a>`).join("");
      document.getElementById("where").innerHTML = `<h2 class="title watch-heading">Where to watch (official)</h2><div class="watch-links">${links}</div>`;
    document.getElementById("actions").innerHTML =
        `<button id="track" class="nav track-button">Track edges on dashboard</button><span class="muted">${statusLabel(g)}</span>`;
    document.getElementById("track").addEventListener("click", trackGame);
  }

  async function trackGame(){
    if (!selected) return;
    const btn = document.getElementById("track");
    btn.disabled = true; btn.textContent = "Adding…";
    try {
      const r = await fetch("/api/events", {method:"POST", headers:{"content-type":"application/json"},
        body: JSON.stringify({polymarket_url: `https://polymarket.com/event/${selected.slug}`})});
      if (!r.ok){ const b = await r.json().catch(()=>({})); throw new Error(b.detail || "Could not add"); }
      btn.textContent = "Tracking ✓ (see dashboard)";
    } catch (e){ btn.textContent = "Failed: " + e.message; btn.disabled = false; }
  }

  function play(){
    const url = document.getElementById("stream-url").value.trim();
    if (!url) return;
    const player = document.getElementById("player");
    let video = player.querySelector("video");
    if (!video){ player.innerHTML = ""; video = document.createElement("video"); video.controls = true; video.playsInline = true; player.appendChild(video); }
    if (hls){ hls.destroy(); hls = null; }
    if (url.includes(".m3u8") && window.Hls && Hls.isSupported()){
      hls = new Hls({ lowLatencyMode: true });
      hls.loadSource(url); hls.attachMedia(video);
      hls.on(Hls.Events.MANIFEST_PARSED, () => video.play().catch(()=>{}));
    } else {
      video.src = url; video.play().catch(()=>{});   // native HLS (Safari) / mp4 / webm
    }
  }

  async function load(){
    try { const r = await fetch("/api/discover"); if (r.ok) games = await r.json(); } catch {}
    render();
    const slug = new URLSearchParams(location.search).get("slug");
    if (slug){ const g = games.find(x => x.slug === slug); if (g) select(g); }
  }

  document.getElementById("games").addEventListener("click", e => {
    const row = e.target.closest("[data-slug]"); if (!row) return;
    const g = games.find(x => x.slug === row.dataset.slug); if (g) select(g);
  });
  document.getElementById("search").addEventListener("input", render);
  document.getElementById("league-filter").addEventListener("click", e => {
    const p = e.target.closest("[data-league]"); if (!p) return;
    activeLeague = p.dataset.league; render();
  });
  document.getElementById("play").addEventListener("click", play);
  document.getElementById("stream-url").addEventListener("keydown", e => { if (e.key === "Enter") play(); });
  load();
  setInterval(load, 60000);
