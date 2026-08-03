// Tab Navigation Logic
  // The CSRF-aware fetch wrapper now lives in the shared /static/csrf.js module,
  // loaded before this script (and before watch.js) so both pages behave the same.
  const primaryTabButtons = [...document.querySelectorAll('.tab-carousel .pill')];
  function activatePrimaryTab(button, {resetScroll = false} = {}) {
      const tab = button?.dataset.tab;
      if (!tab || !document.getElementById(tab)) return;
      primaryTabButtons.forEach(candidate => {
        const selected = candidate === button;
        candidate.classList.toggle('active', selected);
        candidate.setAttribute('aria-selected', String(selected));
        candidate.tabIndex = selected ? 0 : -1;
      });
      document.querySelectorAll('.tab-content').forEach(tc => tc.classList.add('is-hidden'));
      const panel = document.getElementById(tab);
      panel.classList.remove('is-hidden');
      if (resetScroll) {
        window.scrollTo({top:Math.max(0, Number(panel.offsetTop || 0) - 8), behavior:"auto"});
      }
      try { sessionStorage.setItem("pelositracker-active-tab", tab); } catch {}
      const restoreResearchSection = () => {
        if (tab !== "tab-us-research" || window.location.hash.length <= 1) return;
        const hashTarget = document.getElementById(
          decodeURIComponent(window.location.hash.slice(1))
        );
        if (hashTarget && panel.contains(hashTarget)) {
          hashTarget.scrollIntoView({block:"start"});
        }
      };
      if (tab === "tab-live") renderEvents(lastEvents);
      if (tab === "tab-discovery") {
        renderBestBets();
        loadDiscover();
        refreshUSExecutionStatus();
      }
      if (tab === "tab-us-research") {
        const researchRefreshes = [
          refreshUSStatus(),
          loadUSEvents(),
          loadUSTrading(),
          loadPerformanceLedger(),
          loadModelLab(),
          loadPolicyAdvisorSessions()
        ];
        Promise.allSettled(researchRefreshes).then(restoreResearchSection);
      }
      if (tab === "tab-bots") {
        refreshMetrics();
        refreshLeaderboard();
        refreshBotActivity();
        refreshBotGames();
      }
  }
  primaryTabButtons.forEach(btn => {
    btn.addEventListener('click', () => activatePrimaryTab(btn, {resetScroll:true}));
  });
  document.querySelector("#global-tab-nav")?.addEventListener("keydown", event => {
    if (event.key !== "ArrowRight" && event.key !== "ArrowLeft") return;
    const available = primaryTabButtons.filter(button => !button.hidden);
    const current = available.indexOf(document.activeElement);
    if (current < 0) return;
    event.preventDefault();
    const step = event.key === "ArrowRight" ? 1 : -1;
    const next = available[(current + step + available.length) % available.length];
    next.focus();
    activatePrimaryTab(next, {resetScroll:true});
  });

  // Auth & Login Logic
  document.querySelector("#login-form").addEventListener("submit", async e => {
    e.preventDefault();
    const form = e.currentTarget;
    const btn = form.querySelector("button");
    const err = document.querySelector("#login-error");
    const typedUser = (form.username && form.username.value || "").trim();
    btn.disabled = true; err.hidden = true;
    try {
      const r = await fetch("/api/login", {
        method: "POST",
        body: new URLSearchParams(new FormData(form))
      });
      const data = await r.json().catch(()=>({}));
      if (!r.ok) throw new Error(data.detail || "Invalid credentials");

      if (typedUser) { window.currentUsername = typedUser; try { sessionStorage.setItem("pt_user", typedUser); } catch {} }

      const overlay = document.querySelector("#login-overlay");
      overlay.classList.add("dissolve");
      setTimeout(() => { overlay.hidden = true; }, 1500);

      // Start the app!
      startApp();
    } catch(er) {
      err.textContent = er.message; err.hidden = false;
    } finally { btn.disabled = false; }
  });

  // The rest of the app logic...
  const esc = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"})[c]);
  const pct = value => value == null ? "—" : `${(value * 100).toFixed(1)}%`;
  const cents = value => value == null ? "—" : `${(value * 100).toFixed(1)}¢`;
  const signedCents = value => value == null ? "—" : `${value >= 0 ? "+" : ""}${(value * 100).toFixed(1)}¢`;
  const money = value => value == null ? "—" : `${value >= 0 ? "+" : "-"}$${Math.abs(value).toFixed(2)}`;
  const APPROVAL_TOKEN = "approve";
  const keyFor = (...parts) => encodeURIComponent(parts.join("|"));
  let refreshInFlight = false;
  const pendingEventRemovals = new Set();
  const pendingBotRemovals = new Set();
  let eventActionStatusTimer = null;
  let botActionStatusTimer = null;

  function showEventActionStatus(message, state = "pending", clearAfter = 0) {
    const box = document.querySelector("#event-action-status");
    if (!box) return;
    if (eventActionStatusTimer) clearTimeout(eventActionStatusTimer);
    box.textContent = message;
    box.className = `event-action-status is-${state}`;
    box.hidden = false;
    eventActionStatusTimer = clearAfter ? setTimeout(() => {
      box.hidden = true;
      eventActionStatusTimer = null;
    }, clearAfter) : null;
  }

  const botsTabVisible = () => !document.querySelector("#tab-bots").classList.contains("is-hidden");
  const liveTabVisible = () => !document.querySelector("#tab-live").classList.contains("is-hidden");
  const discoveryTabVisible = () => !document.querySelector("#tab-discovery").classList.contains("is-hidden");
  const usResearchTabVisible = () => !document.querySelector("#tab-us-research").classList.contains("is-hidden");

  const researchSectionLinks = [
    ...document.querySelectorAll("[data-research-section]")
  ];
  const researchSections = researchSectionLinks
    .map(link => document.getElementById(link.dataset.researchSection))
    .filter(Boolean);
  let researchScrollFrame = null;

  function setActiveResearchSection(sectionId) {
    researchSectionLinks.forEach(link => {
      const selected = link.dataset.researchSection === sectionId;
      link.classList.toggle("active", selected);
      if (selected) link.setAttribute("aria-current", "location");
      else link.removeAttribute("aria-current");
    });
  }

  function updateResearchSectionFromScroll() {
    researchScrollFrame = null;
    if (!usResearchTabVisible() || !researchSections.length) return;
    const referenceLine = Math.min(180, Math.max(116, window.innerHeight * 0.2));
    let active = researchSections[0];
    for (const section of researchSections) {
      if (section.getBoundingClientRect().top <= referenceLine) active = section;
      else break;
    }
    setActiveResearchSection(active.id);
  }

  researchSectionLinks.forEach(link => {
    link.addEventListener("click", () => {
      setActiveResearchSection(link.dataset.researchSection);
    });
  });
  window.addEventListener("scroll", () => {
    if (researchScrollFrame != null) return;
    researchScrollFrame = window.requestAnimationFrame(updateResearchSectionFromScroll);
  }, {passive:true});
  window.addEventListener("resize", updateResearchSectionFromScroll);

  function showBotActionStatus(message, state = "pending", clearAfter = 0) {
    const box = document.querySelector("#bot-action-status");
    if (!box) return;
    if (botActionStatusTimer) clearTimeout(botActionStatusTimer);
    box.textContent = message;
    box.className = `bot-action-status is-${state}`;
    box.hidden = false;
    botActionStatusTimer = clearAfter ? setTimeout(() => {
      box.hidden = true;
      botActionStatusTimer = null;
    }, clearAfter) : null;
  }

  function tagClass(action) {
    if (action === "ENTRY WINDOW") return "entry";
    if (action === "HOLD") return "hold";
    if (action === "CONSIDER CASH") return "cash";
    if (action === "EXIT WATCH") return "exit";
    if (action === "MARKET ONLY") return "marketonly";
    return "wait";
  }

  let activeLine = "all", activeEventId = null, lastEvents = [];
  const LINE_META = { moneyline:{label:"Moneyline",cls:"lt-ml"}, spread:{label:"Spread",cls:"lt-sp"}, total:{label:"Over / Under",cls:"lt-ou"} };
  const LINE_ORDER = ["moneyline","spread","total"];
  function lineType(market, outcome){
    const m=(market||"").toLowerCase(), o=String(outcome||"").trim().toLowerCase();
    if(o.startsWith("over")||o.startsWith("under")||/total|over.?under|o\/u/.test(m)) return "total";
    if(/spread|handicap|run.?line|puck.?line|\bline\b/.test(m) || /[+-]\d/.test(String(outcome||""))) return "spread";
    return "moneyline";
  }
  function lineBadge(market, outcome){ const meta=LINE_META[lineType(market,outcome)]; return `<span class="line-badge ${meta.cls}">${meta.label}</span>`; }

  // Polymarket US execution is a separate consumer of established engine output.
  // It never writes into the calculation path.
  let lastUSEvents = [];
  let usEventsLoading = false;
  let usCredentialsConfigured = false;
  let usExecutionEnabled = false;
  let usExecutionBySignal = new Map();

  const usDate = value => {
    if (!value) return "Start unavailable";
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString();
  };

  async function refreshUSStatus() {
    const box = document.querySelector("#us-key-status");
    if (!box) return;
    try {
      const response = await fetch("/api/polymarket-us/status", {cache:"no-store"});
      const data = await response.json().catch(()=>({}));
      if (!response.ok) throw new Error(data.detail || "Status unavailable");
      usCredentialsConfigured = !!data.configured;
      const credentialSource = data.credential_source === "runtime"
        ? "session memory"
        : data.credential_source === "environment"
          ? "server environment"
          : "";
      const key = data.configured
        ? `Configured · ${esc(data.key_id_hint || "hidden")}${credentialSource ? ` · ${credentialSource}` : ""}`
        : "Not configured";
      const automation = data.automation || {};
      const trading = !data.trading_enabled
        ? "Unavailable"
        : automation.live_order_possible_now
          ? "LIVE ARMED"
          : automation.policy?.execution_mode === "live"
            ? "Live mode · disarmed"
            : "Dry run";
      const deployment = data.workstation
        ? "Local / isolated"
        : data.trading_enabled ? "Hosted / enabled" : "Hosted / research only";
      const storage = automation.storage || {};
      const evidence = storage.durable
        ? "Durable PostgreSQL"
        : storage.backend
          ? "Local / ephemeral SQLite"
          : "Storage unavailable";
      box.innerHTML = `
        <div class="us-status-card"><span>Deployment</span><strong>${deployment}</strong></div>
        <div class="us-status-card"><span>API key</span><strong>${key}</strong></div>
        <div class="us-status-card"><span>Trading</span><strong>${esc(trading)}</strong></div>
        <div class="us-status-card"><span>Evidence</span><strong>${esc(evidence)}</strong></div>`;
    } catch (error) {
      box.innerHTML = `<div class="error">${esc(error.message || "Could not read execution status")}</div>`;
    }
  }

  function renderUSEvents() {
    const body = document.querySelector("#us-events");
    const search = document.querySelector("#us-events-search");
    if (!body) return;
    const query = String(search?.value || "").trim().toLowerCase();
    const events = lastUSEvents.filter(event => {
      if (!query) return true;
      const marketText = (event.markets || []).map(m =>
        [m.question, m.market_type, ...(m.sides || []).map(side => side.description)].join(" ")
      ).join(" ");
      return `${event.title} ${event.subtitle || ""} ${marketText}`.toLowerCase().includes(query);
    });
    if (!events.length) {
      body.innerHTML = `<div class="metrics-empty">${lastUSEvents.length ? "No US markets match that filter." : "No active US sports markets were returned."}</div>`;
      return;
    }
    body.innerHTML = events.map(event => {
      const markets = (event.markets || []).map(market => {
        const line = market.line == null ? "" : ` · line ${esc(market.line)}`;
        const sides = (market.sides || []).map(side => `
          <div class="us-side">
            <span>${side.long ? "LONG" : "SHORT"} · ${esc(side.description)}</span>
            <strong>${cents(side.reference_price)}</strong>
          </div>`).join("") || '<div class="us-side"><span>Outcome metadata unavailable</span></div>';
        return `<div class="us-market">
          <div>
            <div class="us-market-question">${esc(market.question)}</div>
            <div class="us-market-type">${esc(market.market_type)}${line}</div>
          </div>
          <div class="us-market-book">
            Raw long bid <strong>${cents(market.long_best_bid)}</strong><br>
            Raw long ask <strong>${cents(market.long_best_ask)}</strong>
          </div>
          <div class="us-sides">${sides}</div>
        </div>`;
      }).join("");
      const live = event.live ? '<span class="us-live-badge">LIVE</span>' : "";
      const game = [event.score, event.period].filter(Boolean).join(" · ");
      return `<article class="us-event${event.live ? " is-live" : ""}">
        <div class="us-event-head">
          <div>
            <div class="us-event-title">${esc(event.title)}</div>
            <div class="us-event-meta">${esc(usDate(event.start))}${game ? ` · ${esc(game)}` : ""} · ${event.markets.length} market${event.markets.length === 1 ? "" : "s"}</div>
          </div>
          ${live}
        </div>
        <div class="us-market-grid">${markets}</div>
      </article>`;
    }).join("");
  }

  async function loadUSEvents(force = false) {
    if (usEventsLoading) return;
    const button = document.querySelector("#us-events-refresh");
    const status = document.querySelector("#us-events-status");
    usEventsLoading = true;
    if (button) { button.disabled = true; button.textContent = "Refreshing…"; }
    if (status) status.textContent = "Reading the public Polymarket US sports inventory…";
    try {
      const response = await fetch(
        `/api/polymarket-us/events?refresh=${force ? "true" : "false"}&limit=60`,
        {cache:"no-store"}
      );
      const data = await response.json().catch(()=>({}));
      if (!response.ok) throw new Error(data.detail || "US inventory unavailable");
      lastUSEvents = Array.isArray(data.events) ? data.events : [];
      renderUSEvents();
      if (status) {
        const stamp = data.fetched_at ? new Date(data.fetched_at).toLocaleTimeString() : "now";
        status.textContent = `${lastUSEvents.length} active US sports event${lastUSEvents.length === 1 ? "" : "s"} · ${esc(data.venue || "Polymarket US")} · fetched ${stamp}`;
      }
    } catch (error) {
      if (status) status.textContent = error.message || "Could not load Polymarket US markets";
      const body = document.querySelector("#us-events");
      if (body) body.innerHTML = `<div class="error">${esc(error.message || "Could not load Polymarket US markets")}</div>`;
    } finally {
      usEventsLoading = false;
      if (button) { button.disabled = false; button.textContent = "Refresh US markets"; }
    }
  }

  function renderUSAccount(data) {
    const body = document.querySelector("#us-account");
    if (!body) return;
    const balances = Array.isArray(data.balances) ? data.balances : [];
    const positions = data.positions && typeof data.positions === "object" ? data.positions : {};
    const first = balances[0] || {};
    const tiles = [
      metricTile("Current balance", first.currentBalance == null ? "—" : `$${Number(first.currentBalance).toFixed(2)}`),
      metricTile("Buying power", first.buyingPower == null ? "—" : `$${Number(first.buyingPower).toFixed(2)}`),
      metricTile("US positions", String(Object.keys(positions).length), "", data.positions_eof ? "complete first page" : "additional pages available"),
      metricTile("Connection", "AUTHENTICATED", "good", "live orders use a separate expiring arm")
    ].join("");
    body.innerHTML = `<div class="us-account-tiles">${tiles}</div>
      <details class="advanced">
        <summary>Inspect returned position payload</summary>
        <pre class="us-json">${esc(JSON.stringify(positions, null, 2))}</pre>
      </details>`;
  }

  async function loadUSAccount() {
    const button = document.querySelector("#us-account-refresh");
    const status = document.querySelector("#us-account-status");
    if (!button || !status) return;
    button.disabled = true;
    button.textContent = "Testing…";
    status.textContent = usCredentialsConfigured
      ? "Signing a read-only balances and positions request…"
      : "No key is configured. Paste a session key above or add both values to the server environment.";
    try {
      const response = await fetch("/api/polymarket-us/account", {cache:"no-store"});
      const data = await response.json().catch(()=>({}));
      if (!response.ok) throw new Error(data.detail || "Account request failed");
      renderUSAccount(data);
      status.textContent = `Authenticated successfully · balances and positions read at ${new Date(data.fetched_at).toLocaleTimeString()} · live execution remains separately controlled`;
      await refreshUSStatus();
    } catch (error) {
      status.textContent = error.message || "Could not authenticate to Polymarket US";
    } finally {
      button.disabled = false;
      button.textContent = "Test account key";
    }
  }

  document.querySelector("#us-events-refresh")?.addEventListener("click", () => loadUSEvents(true));
  document.querySelector("#us-events-search")?.addEventListener("input", renderUSEvents);
  document.querySelector("#us-account-refresh")?.addEventListener("click", loadUSAccount);

  document.querySelector("#us-runtime-credential-form")?.addEventListener("submit", async event => {
    event.preventDefault();
    const button = document.querySelector("#us-runtime-credential-save");
    const status = document.querySelector("#us-account-status");
    const keyInput = document.querySelector("#us-runtime-key-id");
    const secretInput = document.querySelector("#us-runtime-secret-key");
    if (!button || !status || !keyInput || !secretInput) return;
    setActionBusy(button, true, "Verifying...");
    status.textContent = "Verifying the key directly with Polymarket US. Automation will be stopped before the session key becomes active...";
    try {
      const response = await fetch("/api/polymarket-us/runtime-credentials", {
        method: "POST",
        headers: {"content-type":"application/json"},
        body: JSON.stringify({
          key_id: keyInput.value.trim(),
          secret_key: secretInput.value
        })
      });
      const data = await response.json().catch(()=>({}));
      if (!response.ok) throw new Error(data.detail || "Could not verify session key");
      keyInput.value = "";
      secretInput.value = "";
      status.textContent = `${data.message} Key ${data.credentials?.key_id_hint || "verified"}.`;
      await refreshUSStatus();
      await loadUSAccount();
      await loadUSTrading();
    } catch (error) {
      secretInput.value = "";
      status.textContent = error.message || "Could not verify session key";
    } finally {
      setActionBusy(button, false);
    }
  });

  document.querySelector("#us-runtime-credential-clear")?.addEventListener("click", async event => {
    if (!window.confirm("Forget the runtime key and stop automation now? Environment credentials, if configured, will become active again.")) return;
    const button = event.currentTarget;
    const status = document.querySelector("#us-account-status");
    setActionBusy(button, true, "Forgetting...");
    if (status) status.textContent = "Stopping automation and clearing the runtime credential from server memory...";
    try {
      const response = await fetch("/api/polymarket-us/runtime-credentials", {method:"DELETE"});
      const data = await response.json().catch(()=>({}));
      if (!response.ok) throw new Error(data.detail || "Could not forget runtime key");
      document.querySelector("#us-runtime-key-id").value = "";
      document.querySelector("#us-runtime-secret-key").value = "";
      if (status) status.textContent = data.credentials?.configured
        ? "Runtime key forgotten. The server environment key is active; automation remains stopped."
        : "Runtime key forgotten. No Polymarket US key is active; automation remains stopped.";
      await refreshUSStatus();
      await loadUSTrading();
    } catch (error) {
      if (status) status.textContent = error.message || "Could not forget runtime key";
    } finally {
      setActionBusy(button, false);
    }
  });

  let usTradingLoading = false;
  let usTradingReloadQueued = false;
  let lastUSTradingStatus = null;
  let lastTradingPerformance = null;
  let lastManagedPositions = [];
  let lastPolicyAdvice = null;
  let usLedgerLoaded = false;
  let usLedgerLoading = false;
  let usLedgerQueryTimer = null;
  let usPositionMode = "all";
  let usTradingFormDirty = false;
  let usTradingLaneSwitching = false;
  let usTradingHydrationEpoch = 0;
  let lastRiskPresets = {};
  let lineExecutionProfiles = [];
  let activeTradingLane = (() => {
    try {
      return window.localStorage.getItem("pelosi-trading-lane") === "live"
        ? "live"
        : "dry_run";
    } catch {
      return "dry_run";
    }
  })();

  function tradingApi(path, lane = activeTradingLane) {
    const separator = path.includes("?") ? "&" : "?";
    return `${path}${separator}lane=${encodeURIComponent(lane)}`;
  }

  function renderTradingLanes(status = lastUSTradingStatus) {
    const lanes = status?.lanes || {};
    const running = [];
    for (const lane of ["dry_run", "live"]) {
      const laneStatus = lanes[lane] || {};
      const button = document.querySelector(`[data-trading-lane="${lane}"]`);
      button?.classList.toggle("is-active", lane === activeTradingLane);
      button?.classList.toggle("is-running", !!laneStatus.automation_enabled);
      button?.setAttribute(
        "aria-pressed",
        lane === activeTradingLane ? "true" : "false"
      );
      if (button) button.disabled = usTradingLaneSwitching;
      const summary = document.querySelector(
        lane === "live"
          ? "#us-live-lane-summary"
          : "#us-dry-run-lane-summary"
      );
      if (!summary) continue;
      const state = laneStatus.automation_enabled ? "RUNNING" : "STOPPED";
      if (laneStatus.automation_enabled) running.push(lane);
      const armed = lane === "live"
        ? laneStatus.armed ? " · ARMED" : " · DISARMED"
        : "";
      summary.textContent = (
        `${state}${armed} · ${Number(laneStatus.open_positions || 0)} open · ` +
        `$${Number(laneStatus.managed_exposure_usd || 0).toFixed(2)} exposure`
      );
    }
    const coordination = document.querySelector("#us-lane-coordination-status");
    if (coordination) {
      coordination.classList.toggle("is-both-running", running.length === 2);
      coordination.textContent = usTradingLaneSwitching
        ? `Loading the ${activeTradingLane === "live" ? "live" : "dry-run"} lane policy...`
        : running.length === 2
          ? "Dry-run and live automation are both running independently."
          : running.length === 1
            ? `${running[0] === "live" ? "Live" : "Dry-run"} automation is running. The other lane remains independently stopped.`
            : "Both automation lanes are stopped. Tap a lane to edit its saved policy.";
    }
    const selected = lanes[activeTradingLane] || {};
    const quickToggle = document.querySelector("#us-lane-automation-toggle");
    const quickTitle = document.querySelector("#us-quick-lane-title");
    const quickStatus = document.querySelector("#us-quick-lane-status");
    const selectedRunning = !!selected.automation_enabled;
    const selectedLabel = activeTradingLane === "live" ? "Live" : "Dry-run";
    if (quickTitle) quickTitle.textContent = `${selectedLabel} quick control`;
    if (quickToggle && !quickToggle.getAttribute("aria-busy")) {
      quickToggle.disabled = usTradingLaneSwitching;
      quickToggle.classList.toggle("danger", selectedRunning);
      quickToggle.classList.toggle("primary", !selectedRunning);
      quickToggle.textContent = selectedRunning
        ? `Stop ${selectedLabel.toLowerCase()} automation`
        : `Start ${selectedLabel.toLowerCase()} automation`;
    }
    if (
      quickStatus
      && !["is-working", "is-success", "is-error"].some(
        className => quickStatus.classList.contains(className)
      )
    ) {
      quickStatus.className = "";
      quickStatus.textContent = selectedRunning
        ? `${selectedLabel} cycles are running. This control stops only that lane.`
        : activeTradingLane === "live"
          ? "Starts live analysis cycles only. Real orders remain impossible until the separate live latch is armed."
          : "Starts the saved simulated policy on the server, so it keeps running after you close this page.";
    }
    const liveOnly = activeTradingLane === "live";
    const disarm = document.querySelector("#us-disarm");
    const arm = document.querySelector("#us-arm");
    const armApproval = document.querySelector("#us-arm-confirmation");
    const armDuration = document.querySelector("#us-arm-duration");
    if (disarm) disarm.disabled = !liveOnly;
    if (arm) arm.disabled = !liveOnly;
    if (armApproval) armApproval.disabled = !liveOnly;
    if (armDuration) armDuration.disabled = !liveOnly;
    const laneHint = document.querySelector("#us-live-control-hint");
    if (laneHint) {
      laneHint.textContent = liveOnly
        ? "Live-order controls apply only to this live lane."
        : "Switch to the live lane to arm or disarm real orders. Dry-run automation can run without arming.";
    }
    const save = document.querySelector("#us-policy-save");
    const run = document.querySelector("#us-run-now");
    const laneLabel = liveOnly ? "live" : "dry-run";
    if (save && !save.getAttribute("aria-busy")) {
      save.textContent = usTradingFormDirty
        ? `Save ${laneLabel} policy - unsaved`
        : `Save ${laneLabel} policy`;
    }
    if (run && !run.getAttribute("aria-busy")) {
      run.textContent = `Run ${laneLabel} cycle now`;
    }
  }

  async function switchTradingLane(lane) {
    const next = lane === "live" ? "live" : "dry_run";
    const selector = document.querySelector("#us-trading-mode");
    if (usTradingLaneSwitching) {
      if (selector) selector.value = activeTradingLane;
      return;
    }
    if (next === activeTradingLane) {
      if (selector) selector.value = next;
      return;
    }
    if (
      usTradingFormDirty
      && !window.confirm(
        "Discard the unsaved settings in this lane and switch automation lanes?"
      )
    ) {
      if (selector) selector.value = activeTradingLane;
      return;
    }
    activeTradingLane = next;
    const quickStatus = document.querySelector("#us-quick-lane-status");
    if (quickStatus) quickStatus.className = "";
    try {
      window.localStorage.setItem("pelosi-trading-lane", next);
    } catch {
      // Storage can be disabled; the safe default remains the dry-run lane.
    }
    if (selector) selector.value = next;
    const liquidationMode = document.querySelector(
      `input[name="liquidate-mode"][value="${next}"]`
    );
    if (liquidationMode) {
      liquidationMode.checked = true;
      updateLiquidationMode();
    }
    usTradingHydrationEpoch += 1;
    setUSTradingFormDirty(false);
    clearPolicySaveNotice();
    invalidatePolicyAdvice(
      `The recommendation target changed to the ${next === "live" ? "live" : "dry-run"} lane. Analyze again before previewing or applying settings.`
    );
    usTradingLaneSwitching = true;
    renderTradingLanes();
    try {
      await loadUSTrading();
    } finally {
      usTradingLaneSwitching = false;
      renderTradingLanes();
    }
  }

  function refreshTradingInBackground(delay = 0) {
    window.setTimeout(() => {
      Promise.all([refreshUSStatus(), loadUSTrading()]).catch(() => {
        // The loaders surface their own errors. Secondary dashboard hydration
        // must not keep an operator action visually busy.
      });
    }, delay);
  }

  function delayedProgress(statusBox, delay, message) {
    return window.setTimeout(() => {
      if (!statusBox) return;
      statusBox.className = "us-liquidate-status is-working";
      statusBox.textContent = message;
    }, delay);
  }

  const executionSignalKey = (eventId, market, outcome) => [
    eventId,
    market,
    outcome
  ].map(value => String(value ?? "").trim().toLowerCase()).join("|");

  function cacheUSExecutionStatus(status) {
    lastUSTradingStatus = status;
    const next = new Map();
    for (const item of (status?.last_cycle_evaluations || [])) {
      next.set(
        executionSignalKey(item.event_id, item.market, item.outcome),
        item
      );
    }
    usExecutionBySignal = next;
    if (discoveryTabVisible()) renderBestBets();
  }

  async function refreshUSExecutionStatus() {
    if (!usExecutionEnabled) return;
    try {
      const response = await fetch(
        tradingApi("/api/polymarket-us/trading/status"),
        {cache:"no-store"}
      );
      const status = await response.json().catch(()=>({}));
      if (!response.ok) throw new Error(detailMessage(status));
      cacheUSExecutionStatus(status);
    } catch {
      // Keep the last successful cycle visible during a transient fetch
      // failure; the full US panel reports the request error when it is open.
    }
  }

  const detailMessage = body => {
    if (typeof body?.detail === "string") return body.detail;
    if (Array.isArray(body?.detail)) {
      return body.detail
        .map(item => item?.msg || item?.message || JSON.stringify(item))
        .join("; ");
    }
    if (body?.detail) return JSON.stringify(body.detail);
    return "Request failed";
  };

  const policyFieldSelectors = {
    trading_allocation_usd: "#us-trading-allocation",
    risk_preset: "#us-risk-preset",
    max_total_exposure_usd: "#us-max-exposure",
    minimum_cash_reserve_usd: "#us-cash-reserve",
    max_position_usd: "#us-max-position",
    max_event_exposure_usd: "#us-max-event",
    max_daily_loss_usd: "#us-daily-loss",
    min_edge: "#us-min-edge",
    max_edge: "#us-max-edge",
    fee_edge_margin: "#us-fee-edge-margin",
    min_signal_quality: "#us-min-quality",
    max_signal_quality: "#us-max-quality",
    min_source_agreement: "#us-min-source-agreement",
    max_signal_age_seconds: "#us-max-signal-age",
    entry_confirmation_readings: "#us-entry-confirmation-readings",
    max_confirmation_price_drift: "#us-max-confirmation-price-drift",
    min_entry_price: "#us-min-price",
    max_entry_price: "#us-max-price",
    min_hold_minutes: "#us-min-hold",
    profit_target: "#us-profit-target",
    minimum_locked_profit: "#us-min-locked-profit",
    max_open_positions: "#us-max-open",
    max_orders_per_hour: "#us-max-orders-hour",
    max_entries_per_event_per_hour: "#us-max-event-entries-hour",
    candidate_cooldown_seconds: "#us-candidate-cooldown",
    min_mlb_fraction_remaining: "#us-min-mlb-remaining",
    min_reference_sources: "#us-min-refs",
    max_spread: "#us-max-spread",
    min_book_shares: "#us-min-depth",
    trailing_drawdown: "#us-trailing-drawdown",
    stop_loss: "#us-stop-loss",
    exit_edge: "#us-exit-edge",
    cycle_seconds: "#us-cycle-seconds",
    global_entry_enabled: "#us-line-type-policy",
    allowed_market_types: "#us-line-type-policy",
    allowed_market_scopes: "#us-market-scope-policy",
    adaptive_exit_horizon_minutes: "#us-adaptive-exit-horizon",
    adaptive_exit_min_samples: "#us-adaptive-exit-min-samples",
    adaptive_exit_max_tightening: "#us-adaptive-exit-max-tightening",
    stop_confirmation_readings: "#us-stop-confirmation-readings",
    stop_grace_minutes: "#us-stop-grace-minutes",
    catastrophic_stop_multiplier: "#us-catastrophic-stop-multiplier",
    post_exit_tracking_minutes: "#us-post-exit-tracking-minutes"
  };

  const policyErrorPatterns = [
    [/maximum total exposure|max_total_exposure/i, "#us-max-exposure"],
    [/max_position|maximum per position/i, "#us-max-position"],
    [/max_event|maximum per event/i, "#us-max-event"],
    [/hard trading allocation|trading_allocation/i, "#us-trading-allocation"],
    [/entry prices|min_entry_price|max_entry_price|5c.*95c/i, "#us-min-price"],
    [/edge filters|min_edge|max_edge/i, "#us-min-edge"],
    [/minimum locked profit|minimum_locked_profit/i, "#us-min-locked-profit"],
    [/cycle_seconds|analysis cycle/i, "#us-cycle-seconds"],
    [/candidate.*cooldown/i, "#us-candidate-cooldown"],
    [/line type|allowed_market_types/i, "#us-line-type-policy"],
    [/market segment|allowed_market_scopes/i, "#us-market-scope-policy"],
    [/reference source|min_reference_sources/i, "#us-min-refs"],
    [/maximum quality|max_signal_quality/i, "#us-max-quality"],
    [/signal quality|min_signal_quality/i, "#us-min-quality"],
    [/source agreement|min_source_agreement/i, "#us-min-source-agreement"],
    [/signal age|max_signal_age_seconds/i, "#us-max-signal-age"],
    [/confirmation readings|entry_confirmation_readings/i, "#us-entry-confirmation-readings"],
    [/confirmation.*drift|max_confirmation_price_drift/i, "#us-max-confirmation-price-drift"],
    [/spread|max_spread/i, "#us-max-spread"],
    [/book shares|min_book_shares/i, "#us-min-depth"]
  ];

  function policyErrorTarget(message, detail = null) {
    if (Array.isArray(detail)) {
      for (const item of detail) {
        const field = Array.isArray(item?.loc) ? item.loc.at(-1) : null;
        if (field && policyFieldSelectors[field]) {
          return document.querySelector(policyFieldSelectors[field]);
        }
      }
    }
    const match = policyErrorPatterns.find(([pattern]) => pattern.test(message));
    return match ? document.querySelector(match[1]) : null;
  }

  function clearPolicySaveNotice() {
    const box = document.querySelector("#us-policy-save-status");
    if (box) {
      box.hidden = true;
      box.className = "us-policy-save-status";
      box.replaceChildren();
    }
    document.querySelectorAll(".is-policy-error").forEach(item => {
      item.classList.remove("is-policy-error");
    });
  }

  function revealPolicyTarget(target, {focus = false} = {}) {
    if (!target) return;
    // A highlighted setting may live in a step that is not on screen; show it
    // before opening its details block, or the focus below cannot land.
    revealFieldStep(target);
    const details = target.closest("details");
    if (details) details.open = true;
    const container = target.matches("fieldset")
      ? target
      : target.closest("label,fieldset") || target;
    container.classList.add("is-policy-error");
    if (focus && typeof target.focus === "function") {
      target.focus({preventScroll:true});
      target.scrollIntoView({
        behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches
          ? "auto"
          : "smooth",
        block:"center"
      });
    }
  }

  function showPolicySaveNotice(
    message,
    {kind = "error", target = null, scroll = false} = {}
  ) {
    const box = document.querySelector("#us-policy-save-status");
    if (!box) return;
    clearPolicySaveNotice();
    revealPolicyTarget(target);
    const laneLabel = activeTradingLane === "live" ? "Live" : "Dry-run";
    box.hidden = false;
    box.classList.add(`is-${kind}`);
    box.innerHTML = `
      <strong>${esc(kind === "error" ? `${laneLabel} policy was not saved` : `${laneLabel} policy saved`)}</strong>
      <span>${esc(message)}</span>
      ${target ? '<button class="ghost compact-button" type="button">Go to highlighted setting</button>' : ""}`;
    box.querySelector("button")?.addEventListener(
      "click",
      () => revealPolicyTarget(target, {focus:true}),
      {once:true}
    );
    if (scroll) {
      window.requestAnimationFrame(() => {
        box.scrollIntoView({
          behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches
            ? "auto"
            : "smooth",
          block:"center"
        });
      });
    }
  }

  function setUSTradingFormDirty(dirty) {
    usTradingFormDirty = dirty;
    // Single source of truth for the sticky rail, so the indicator can never
    // disagree with the save button about whether edits are pending.
    updateSavedIndicator();
    const form = document.querySelector("#us-trading-form");
    const button = document.querySelector("#us-policy-save");
    form?.classList.toggle("is-dirty", dirty);
    if (button) {
      button.textContent = dirty
        ? "Save execution policy · unsaved"
        : "Save execution policy";
    }
  }

  document.querySelector("#us-trading-form")?.addEventListener("input", event => {
    if (event.target?.id === "us-trading-mode") return;
    if (event.target?.closest(".is-policy-error")) clearPolicySaveNotice();
    usTradingHydrationEpoch += 1;
    setUSTradingFormDirty(true);
    renderTradingLanes();
    invalidatePolicyAdvice(
      "Execution controls were edited. Save them, then analyze again before applying suggested filters."
    );
  });

  document.querySelector("#us-trading-mode")?.addEventListener(
    "change",
    event => {
      void switchTradingLane(event.currentTarget.value);
    }
  );
  document.querySelector("#us-trading-lanes")?.addEventListener(
    "click",
    event => {
      const button = event.target.closest("[data-trading-lane]");
      if (button) void switchTradingLane(button.dataset.tradingLane);
    }
  );

  document.querySelector("#us-lane-automation-toggle")?.addEventListener(
    "click",
    async event => {
      const button = event.currentTarget;
      const status = document.querySelector("#us-quick-lane-status");
      const laneStatus = lastUSTradingStatus?.lanes?.[activeTradingLane] || {};
      const running = !!laneStatus.automation_enabled;
      const laneLabel = activeTradingLane === "live" ? "live" : "dry-run";
      if (
        !running
        && activeTradingLane === "live"
        && !window.confirm(
          "Start live analysis cycles? This does not arm real orders; the separate live-order approval and timer remain required."
        )
      ) return;
      setActionBusy(
        button,
        true,
        running ? `Stopping ${laneLabel}...` : `Starting ${laneLabel}...`
      );
      if (status) {
        status.className = "is-working";
        status.textContent = running
          ? `Stopping only the ${laneLabel} lane and waiting for its active cycle to acknowledge...`
          : `Starting the saved ${laneLabel} policy on the server...`;
      }
      const requestEpoch = ++usTradingHydrationEpoch;
      try {
        const response = running
          ? await fetch(
              tradingApi("/api/polymarket-us/trading/stop"),
              {method:"POST"}
            )
          : await fetch(
              tradingApi("/api/polymarket-us/trading/config"),
              {
                method:"PUT",
                headers:{"content-type":"application/json"},
                body:JSON.stringify({automation_enabled:true})
              }
            );
        const body = await response.json().catch(()=>({}));
        if (!response.ok) throw new Error(detailMessage(body));
        if (requestEpoch === usTradingHydrationEpoch) {
          cacheUSExecutionStatus(body);
          renderTradingStatus(body);
        }
        if (status) {
          status.className = "is-success";
          status.textContent = running
            ? `${laneLabel === "live" ? "Live" : "Dry-run"} automation stopped. The other lane was not changed.`
            : `${laneLabel === "live" ? "Live" : "Dry-run"} automation started and will continue server-side after this page closes.`;
        }
        await loadUSTrading();
      } catch (error) {
        if (status) {
          status.className = "is-error";
          status.textContent = error.message || `Could not update ${laneLabel} automation`;
        }
      } finally {
        setActionBusy(button, false);
        renderTradingLanes();
        window.setTimeout(() => {
          if (!status || !status.classList.contains("is-success")) return;
          status.className = "";
          renderTradingLanes();
        }, 5000);
      }
    }
  );

  function setActionBusy(button, busy, pendingLabel = "") {
    if (!button) return;
    if (busy) {
      button.dataset.idleLabel = button.textContent;
      button.disabled = true;
      button.setAttribute("aria-busy", "true");
      if (pendingLabel) button.textContent = pendingLabel;
    } else {
      button.disabled = false;
      button.removeAttribute("aria-busy");
      if (button.dataset.idleLabel) {
        button.textContent = button.dataset.idleLabel;
        delete button.dataset.idleLabel;
      }
    }
  }

  async function fetchWithDeadline(
    url,
    options = {},
    timeoutMs = 45000,
    timeoutMessage = "The request took too long. Nothing was changed."
  ) {
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), timeoutMs);
    try {
      return await fetch(url, {...options, signal:controller.signal});
    } catch (error) {
      if (error?.name === "AbortError") throw new Error(timeoutMessage);
      throw error;
    } finally {
      window.clearTimeout(timeout);
    }
  }

  const engineGateInputs = () => [
    ...document.querySelectorAll("[data-engine-gate]")
  ];
  const entryMarketTypeInputs = () => [
    ...document.querySelectorAll("[data-entry-market-type]")
  ];
  const entryMarketScopeInputs = () => [
    ...document.querySelectorAll("[data-entry-market-scope]")
  ];

  function updateEngineGateAvailability() {
    const strict = document.querySelector("#us-require-engine")?.checked;
    for (const input of engineGateInputs()) input.disabled = !!strict;
    const grid = document.querySelector("#us-engine-gate-grid");
    grid?.classList.toggle("is-strict", !!strict);
  }

  function setEngineGatePreset(kind) {
    for (const input of engineGateInputs()) {
      input.checked = kind === "all" || (
        kind === "core" && input.hasAttribute("data-core-gate")
      );
    }
    const strict = document.querySelector("#us-require-engine");
    if (strict) strict.checked = false;
    updateEngineGateAvailability();
    usTradingHydrationEpoch += 1;
    setUSTradingFormDirty(true);
  }

  document.querySelector("#us-require-engine")?.addEventListener(
    "change",
    updateEngineGateAvailability
  );
  document.querySelector("#us-gates-core")?.addEventListener(
    "click",
    () => setEngineGatePreset("core")
  );
  document.querySelector("#us-gates-all")?.addEventListener(
    "click",
    () => setEngineGatePreset("all")
  );
  document.querySelector("#us-gates-none")?.addEventListener(
    "click",
    () => setEngineGatePreset("none")
  );

  function updateRiskPresetUI({hydrate = false} = {}) {
    const presetName = document.querySelector("#us-risk-preset")?.value || "custom";
    const allocation = Number(
      document.querySelector("#us-trading-allocation")?.value || 0
    );
    const preset = lastRiskPresets[presetName];
    const named = presetName !== "custom" && !!preset;
    for (const input of document.querySelectorAll("[data-derived-risk]")) {
      input.readOnly = named;
      input.closest("label")?.classList.toggle("is-derived-risk", named);
    }
    if (named && hydrate) {
      const derived = preset.derived_policy || {};
      const sourceAllocation = Number(derived.trading_allocation_usd || allocation || 1);
      const scale = allocation > 0 ? allocation / sourceAllocation : 1;
      const direct = {
        "#us-max-open": derived.max_open_positions,
        "#us-max-orders-hour": derived.max_orders_per_hour,
        "#us-max-event-entries-hour": derived.max_entries_per_event_per_hour,
        "#us-candidate-cooldown": derived.candidate_cooldown_seconds,
        "#us-min-refs": derived.min_reference_sources,
        "#us-min-quality": derived.min_signal_quality,
        "#us-max-quality": derived.max_signal_quality,
        "#us-min-source-agreement": derived.min_source_agreement,
        "#us-max-signal-age": derived.max_signal_age_seconds,
        "#us-entry-confirmation-readings": (
          derived.entry_confirmation_readings
        ),
        "#us-cycle-seconds": derived.cycle_seconds,
        "#us-min-depth": derived.min_book_shares
      };
      const moneyValues = {
        "#us-max-exposure": derived.max_total_exposure_usd,
        "#us-cash-reserve": derived.minimum_cash_reserve_usd,
        "#us-max-position": derived.max_position_usd,
        "#us-max-event": derived.max_event_exposure_usd,
        "#us-daily-loss": derived.max_daily_loss_usd
      };
      const percentValues = {
        "#us-min-edge": derived.min_edge,
        "#us-max-edge": derived.max_edge,
        "#us-min-mlb-remaining": derived.min_mlb_fraction_remaining,
        "#us-min-price": derived.min_entry_price,
        "#us-max-price": derived.max_entry_price,
        "#us-profit-target": derived.profit_target,
        "#us-min-locked-profit": derived.minimum_locked_profit,
        "#us-max-spread": derived.max_spread,
        "#us-trailing-drawdown": derived.trailing_drawdown,
        "#us-stop-loss": derived.stop_loss,
        "#us-exit-edge": derived.exit_edge,
        "#us-max-confirmation-price-drift": (
          derived.max_confirmation_price_drift
        )
      };
      for (const [selector, value] of Object.entries(direct)) {
        const input = document.querySelector(selector);
        if (input && value != null) input.value = value;
      }
      for (const [selector, value] of Object.entries(moneyValues)) {
        const input = document.querySelector(selector);
        if (input && value != null) input.value = (Number(value) * scale).toFixed(2);
      }
      for (const [selector, value] of Object.entries(percentValues)) {
        const input = document.querySelector(selector);
        if (input && value != null) input.value = Number(value) * 100;
      }
      const hold = document.querySelector("#us-min-hold");
      if (hold && derived.min_hold_minutes != null) hold.value = derived.min_hold_minutes;
    }
    const preview = document.querySelector("#us-risk-preset-preview");
    if (preview) {
      preview.innerHTML = named
        ? `<strong>${esc(preset.label)}</strong><span>${esc(preset.description)}</span><span>The server will derive and version every shaded field from the $${Number(allocation || 0).toFixed(2)} hard allocation when you save.</span>`
        : "<strong>Custom controls</strong><span>The hard allocation still caps total managed exposure. Every detailed field remains editable and is saved exactly as shown.</span>";
    }
  }

  document.querySelector("#us-risk-preset")?.addEventListener("change", () => {
    updateRiskPresetUI({hydrate:true});
  });
  document.querySelector("#us-trading-allocation")?.addEventListener("input", () => {
    if (document.querySelector("#us-risk-preset")?.value !== "custom") {
      updateRiskPresetUI({hydrate:true});
    }
  });

  const lineProfileInputs = () => [
    ...document.querySelectorAll("[data-profile-field]")
  ];

  function selectedLineProfileKey() {
    return {
      market_type:document.querySelector("#us-profile-market")?.value || "moneyline",
      game_stage:document.querySelector("#us-profile-stage")?.value || "all"
    };
  }

  function loadSelectedLineProfile() {
    const key = selectedLineProfileKey();
    const profile = lineExecutionProfiles.find(item =>
      item.market_type === key.market_type && item.game_stage === key.game_stage
    );
    const overrides = profile?.overrides || {};
    const enabled = document.querySelector("#us-profile-enabled");
    if (enabled) enabled.checked = profile?.enabled !== false;
    for (const input of lineProfileInputs()) {
      const value = overrides[input.dataset.profileField];
      input.value = value == null
        ? ""
        : input.hasAttribute("data-profile-percent")
          ? Number(value) * 100
          : value;
    }
  }

  function renderLineExecutionProfiles() {
    const body = document.querySelector("#us-line-profile-list");
    if (!body) return;
    if (!lineExecutionProfiles.length) {
      body.textContent = document.querySelector("#us-global-entry-enabled")?.checked
        ? "No line-specific profiles saved. Authorized global settings provide the fallback."
        : "No line-specific profiles saved. Global fallback is off, so no line is authorized.";
      return;
    }
    const labels = {
      min_edge:"edge floor",
      max_edge:"edge ceiling",
      min_signal_quality:"quality floor",
      max_signal_quality:"quality ceiling",
      min_source_agreement:"source agreement",
      max_signal_age_seconds:"signal age",
      entry_confirmation_readings:"confirmations",
      max_confirmation_price_drift:"confirmation drift",
      min_reference_sources:"references",
      min_entry_price:"price floor",
      max_entry_price:"price ceiling",
      max_spread:"spread",
      min_book_shares:"depth",
      min_hold_minutes:"hold",
      profit_target:"target",
      minimum_locked_profit:"retained floor",
      trailing_drawdown:"trailing",
      stop_loss:"stop",
      exit_edge:"reversal edge",
      min_mlb_fraction_remaining:"game remaining",
      max_position_usd:"max position $",
      max_event_exposure_usd:"max event $",
      max_entries_per_event_per_hour:"entries/event/hr",
      max_profile_exposure_usd:"profile exposure $",
      max_profile_open_positions:"profile open",
      max_profile_orders_per_hour:"profile entries/hr"
    };
    const percents = new Set([
      "min_edge", "max_edge", "min_entry_price", "max_entry_price",
      "max_spread", "profit_target", "minimum_locked_profit",
      "trailing_drawdown", "stop_loss", "exit_edge",
      "min_mlb_fraction_remaining", "max_confirmation_price_drift"
    ]);
    body.innerHTML = lineExecutionProfiles
      .slice()
      .sort((a, b) => (
        `${a.market_type}/${a.game_stage}`
          .localeCompare(`${b.market_type}/${b.game_stage}`)
      ))
      .map(profile => {
        const settings = Object.entries(profile.overrides || {}).map(
          ([field, value]) => (
            `${labels[field] || field} ${
              percents.has(field)
                ? `${(Number(value) * 100).toFixed(1)}%`
                : Number(value).toFixed(
                    [
                      "min_reference_sources",
                      "entry_confirmation_readings",
                      "max_entries_per_event_per_hour",
                      "max_profile_open_positions",
                      "max_profile_orders_per_hour"
                    ].includes(field) ? 0 : 1
                  )
            }`
          )
        );
        return `<article class="us-line-profile-chip">
          <strong>${esc(profile.market_type)} / ${esc(profile.game_stage)} / ${profile.enabled === false ? "not authorized" : "authorized"}</strong>
          <span>${esc(settings.join(" · ") || "Uses all global values")}</span>
        </article>`;
      }).join("");
  }

  function saveSelectedLineProfile() {
    const key = selectedLineProfileKey();
    const overrides = {};
    for (const input of lineProfileInputs()) {
      if (input.value === "") continue;
      const field = input.dataset.profileField;
      const number = Number(input.value);
      if (!Number.isFinite(number)) {
        input.focus();
        return false;
      }
      overrides[field] = input.hasAttribute("data-profile-percent")
        ? number / 100
        : input.hasAttribute("data-profile-integer")
          ? Math.trunc(number)
          : number;
    }
    const profile = {
      ...key,
      enabled:document.querySelector("#us-profile-enabled")?.checked !== false,
      overrides
    };
    lineExecutionProfiles = lineExecutionProfiles.filter(item => !(
      item.market_type === key.market_type && item.game_stage === key.game_stage
    ));
    lineExecutionProfiles.push(profile);
    usTradingHydrationEpoch += 1;
    setUSTradingFormDirty(true);
    renderLineExecutionProfiles();
    invalidatePolicyAdvice(
      "Line-specific execution controls changed. Save the policy before analyzing again."
    );
    return true;
  }

  function copyGlobalValuesIntoSelectedProfile() {
    const selectors = {
      min_edge:"#us-min-edge",
      max_edge:"#us-max-edge",
      min_signal_quality:"#us-min-quality",
      max_signal_quality:"#us-max-quality",
      min_source_agreement:"#us-min-source-agreement",
      max_signal_age_seconds:"#us-max-signal-age",
      entry_confirmation_readings:"#us-entry-confirmation-readings",
      max_confirmation_price_drift:"#us-max-confirmation-price-drift",
      min_reference_sources:"#us-min-refs",
      min_entry_price:"#us-min-price",
      max_entry_price:"#us-max-price",
      max_spread:"#us-max-spread",
      min_book_shares:"#us-min-depth",
      min_hold_minutes:"#us-min-hold",
      profit_target:"#us-profit-target",
      minimum_locked_profit:"#us-min-locked-profit",
      trailing_drawdown:"#us-trailing-drawdown",
      stop_loss:"#us-stop-loss",
      exit_edge:"#us-exit-edge",
      min_mlb_fraction_remaining:"#us-min-mlb-remaining",
      max_position_usd:"#us-max-position",
      max_event_exposure_usd:"#us-max-event",
      max_entries_per_event_per_hour:"#us-max-event-entries-hour"
      // The three profile-only caps have no global counterpart, so "copy
      // global values" deliberately leaves them blank (meaning no cap).
    };
    for (const input of lineProfileInputs()) {
      const selector = selectors[input.dataset.profileField];
      const source = selector ? document.querySelector(selector) : null;
      if (source) input.value = source.value;
    }
    saveSelectedLineProfile();
  }

  function removeSelectedLineProfile() {
    const key = selectedLineProfileKey();
    const before = lineExecutionProfiles.length;
    lineExecutionProfiles = lineExecutionProfiles.filter(item => !(
      item.market_type === key.market_type && item.game_stage === key.game_stage
    ));
    if (lineExecutionProfiles.length === before) return;
    loadSelectedLineProfile();
    renderLineExecutionProfiles();
    usTradingHydrationEpoch += 1;
    setUSTradingFormDirty(true);
    invalidatePolicyAdvice(
      "Line-specific execution controls changed. Save the policy before analyzing again."
    );
  }

  document.querySelector("#us-profile-market")?.addEventListener(
    "change", loadSelectedLineProfile
  );
  document.querySelector("#us-profile-stage")?.addEventListener(
    "change", loadSelectedLineProfile
  );
  document.querySelector("#us-profile-save")?.addEventListener("click", () => {
    if (!saveSelectedLineProfile()) return;
    document.querySelector("#us-trading-form")?.requestSubmit(
      document.querySelector("#us-policy-save")
    );
  });
  document.querySelector("#us-profile-copy-global")?.addEventListener(
    "click", copyGlobalValuesIntoSelectedProfile
  );
  document.querySelector("#us-profile-remove")?.addEventListener(
    "click", removeSelectedLineProfile
  );
  document.querySelector("#us-global-entry-enabled")?.addEventListener(
    "change", renderLineExecutionProfiles
  );

  function applyTradingPolicy(status, {force = false, requestEpoch = null} = {}) {
    if (
      !force
      && (
        usTradingFormDirty
        || (requestEpoch != null && requestEpoch !== usTradingHydrationEpoch)
      )
    ) {
      return false;
    }
    const policy = status?.policy || {};
    const returnedLane = status?.lane || policy.execution_mode;
    if (returnedLane === "live" || returnedLane === "dry_run") {
      activeTradingLane = returnedLane;
    }
    lastRiskPresets = status?.risk_presets || lastRiskPresets;
    lineExecutionProfiles = Array.isArray(policy.line_execution_profiles)
      ? policy.line_execution_profiles.map(profile => ({
          ...profile,
          overrides:{...(profile.overrides || {})}
        }))
      : [];
    const values = {
      "#us-trading-mode": policy.execution_mode,
      "#us-trading-allocation": policy.trading_allocation_usd,
      "#us-risk-preset": policy.risk_preset || "custom",
      "#us-max-exposure": policy.max_total_exposure_usd,
      "#us-cash-reserve": policy.minimum_cash_reserve_usd,
      "#us-max-position": policy.max_position_usd,
      "#us-max-event": policy.max_event_exposure_usd,
      "#us-daily-loss": policy.max_daily_loss_usd,
      "#us-min-edge": policy.min_edge == null ? null : policy.min_edge * 100,
      "#us-max-edge": policy.max_edge == null ? null : policy.max_edge * 100,
      "#us-fee-edge-margin": policy.fee_edge_margin,
      "#us-min-quality": policy.min_signal_quality,
      "#us-max-quality": policy.max_signal_quality,
      "#us-min-source-agreement": policy.min_source_agreement,
      "#us-max-signal-age": policy.max_signal_age_seconds,
      "#us-entry-confirmation-readings": policy.entry_confirmation_readings,
      "#us-max-confirmation-price-drift": (
        policy.max_confirmation_price_drift == null
          ? null
          : policy.max_confirmation_price_drift * 100
      ),
      "#us-min-price": policy.min_entry_price == null ? null : policy.min_entry_price * 100,
      "#us-max-price": policy.max_entry_price == null ? null : policy.max_entry_price * 100,
      "#us-min-hold": policy.min_hold_minutes,
      "#us-profit-target": policy.profit_target == null ? null : policy.profit_target * 100,
      "#us-min-locked-profit": (
        policy.minimum_locked_profit == null
          ? null
          : policy.minimum_locked_profit * 100
      ),
      "#us-max-open": policy.max_open_positions,
      "#us-max-orders-hour": policy.max_orders_per_hour,
      "#us-max-event-entries-hour": policy.max_entries_per_event_per_hour,
      "#us-candidate-cooldown": policy.candidate_cooldown_seconds,
      "#us-min-mlb-remaining": (
        policy.min_mlb_fraction_remaining == null
          ? null
          : policy.min_mlb_fraction_remaining * 100
      ),
      "#us-min-refs": policy.min_reference_sources,
      "#us-max-spread": policy.max_spread == null ? null : policy.max_spread * 100,
      "#us-min-depth": policy.min_book_shares,
      "#us-trailing-drawdown": policy.trailing_drawdown == null ? null : policy.trailing_drawdown * 100,
      "#us-stop-loss": policy.stop_loss == null ? null : policy.stop_loss * 100,
      "#us-exit-edge": policy.exit_edge == null ? null : policy.exit_edge * 100,
      "#us-reversal-confirmation-readings": policy.reversal_confirmation_readings,
      "#us-cycle-seconds": policy.cycle_seconds,
      "#us-adaptive-exit-profile": policy.adaptive_exit_profile || "observe",
      "#us-adaptive-exit-horizon": policy.adaptive_exit_horizon_minutes,
      "#us-adaptive-exit-min-samples": policy.adaptive_exit_min_samples,
      "#us-adaptive-exit-max-tightening": (
        policy.adaptive_exit_max_tightening == null
          ? null
          : policy.adaptive_exit_max_tightening * 100
      ),
      "#us-stop-confirmation-readings": policy.stop_confirmation_readings,
      "#us-stop-grace-minutes": policy.stop_grace_minutes,
      "#us-catastrophic-stop-multiplier": policy.catastrophic_stop_multiplier,
      "#us-post-exit-tracking-minutes": policy.post_exit_tracking_minutes
    };
    for (const [selector, value] of Object.entries(values)) {
      const input = document.querySelector(selector);
      if (input && value != null) input.value = value;
    }
    document.querySelector("#us-automation-enabled").checked = !!policy.automation_enabled;
    document.querySelector("#us-auto-cashout").checked = !!policy.auto_cashout;
    document.querySelector("#us-adaptive-exit-enabled").checked = (
      !!policy.adaptive_exit_enabled
    );
    document.querySelector("#us-volatility-stop-enabled").checked = (
      !!policy.volatility_stop_enabled
    );
    document.querySelector("#us-stateless-stop-confirmation").checked = (
      !!policy.stateless_stop_confirmation
    );
    document.querySelector("#us-require-engine").checked = policy.require_engine_entry !== false;
    document.querySelector("#us-global-entry-enabled").checked = (
      policy.global_entry_enabled !== false
    );
    const selectedGates = new Set(
      Array.isArray(policy.required_engine_gates)
        ? policy.required_engine_gates
        : engineGateInputs()
          .filter(input => input.hasAttribute("data-core-gate"))
          .map(input => input.dataset.engineGate)
    );
    for (const input of engineGateInputs()) {
      input.checked = selectedGates.has(input.dataset.engineGate);
    }
    const selectedMarketTypes = new Set(
      Array.isArray(policy.allowed_market_types)
        ? policy.allowed_market_types
        : ["moneyline", "spread", "total"]
    );
    for (const input of entryMarketTypeInputs()) {
      input.checked = selectedMarketTypes.has(input.dataset.entryMarketType);
    }
    const selectedMarketScopes = new Set(
      Array.isArray(policy.allowed_market_scopes)
        ? policy.allowed_market_scopes
        : policy.execution_mode === "live"
          ? ["full_game"]
          : ["full_game", "first_inning", "first_five_innings"]
    );
    for (const input of entryMarketScopeInputs()) {
      input.checked = selectedMarketScopes.has(input.dataset.entryMarketScope);
    }
    const liveSegmentApproval = document.querySelector(
      "#us-allow-live-segments"
    );
    liveSegmentApproval.checked = !!policy.allow_live_segment_markets;
    liveSegmentApproval.disabled = policy.execution_mode !== "live";
    liveSegmentApproval.closest("label")?.classList.toggle(
      "is-disabled",
      liveSegmentApproval.disabled
    );
    updateEngineGateAvailability();
    updateAdaptiveExitAvailability();
    updateStopGuardAvailability();
    updateRiskPresetUI();
    renderLineExecutionProfiles();
    loadSelectedLineProfile();
    renderTradingLanes(status);
    renderExecutionState(status);
    renderPolicyRail(status);
    showPolicyStep(currentPolicyStep);
    return true;
  }

  // --- Guided setup steps -------------------------------------------------
  // The steps wrap the existing fields in document order; no input moved, so
  // every selector elsewhere in this file keeps working.
  let currentPolicyStep = "capital";

  function policySteps() {
    return Array.from(
      document.querySelectorAll("#us-trading-form [data-policy-step]")
    );
  }

  function showPolicyStep(name, {focus = false} = {}) {
    const steps = policySteps();
    if (!steps.length) return;
    const known = steps.some(step => step.dataset.policyStep === name);
    currentPolicyStep = known ? name : steps[0].dataset.policyStep;
    for (const step of steps) {
      step.hidden = step.dataset.policyStep !== currentPolicyStep;
    }
    renderPolicyStepNav();
    if (currentPolicyStep === "review") renderEffectivePolicyReview();
    if (focus) {
      document.querySelector("#us-policy-rail")?.scrollIntoView({
        block:"start", behavior:"smooth"
      });
    }
  }

  function renderPolicyStepNav() {
    const nav = document.querySelector("#us-policy-steps");
    if (!nav) return;
    nav.innerHTML = policySteps().map(step => {
      const name = step.dataset.policyStep;
      // A hidden step cannot report validity, so check its fields directly.
      const invalid = Array.from(
        step.querySelectorAll("input, select")
      ).some(field => !field.disabled && !field.checkValidity());
      return `<button type="button" data-goto-step="${esc(name)}" class="${
        [
          name === currentPolicyStep ? "is-current" : "",
          invalid ? "has-invalid" : ""
        ].filter(Boolean).join(" ")
      }">${esc(step.dataset.stepLabel || name)}${invalid ? " ⚠" : ""}</button>`;
    }).join("");
  }

  document.querySelector("#us-policy-steps")?.addEventListener("click", event => {
    const target = event.target.closest("[data-goto-step]");
    if (target) showPolicyStep(target.dataset.gotoStep);
  });

  function revealFieldStep(element) {
    const step = element?.closest?.("[data-policy-step]");
    if (step && step.dataset.policyStep !== currentPolicyStep) {
      showPolicyStep(step.dataset.policyStep);
    }
  }

  function renderPolicyRail(status) {
    const lane = document.querySelector("#us-rail-lane");
    const capital = document.querySelector("#us-rail-capital");
    const saved = document.querySelector("#us-rail-saved");
    if (!lane || !capital || !saved) return;
    const state = status?.execution_state;
    const policy = status?.policy || {};
    lane.textContent = (policy.execution_mode === "live" ? "Live" : "Dry-run")
      + " lane";
    const exposure = state?.exposure;
    capital.textContent = exposure
      ? `$${Number(exposure.managed_exposure_usd || 0).toFixed(2)} of $${
          Number(exposure.trading_allocation_usd || 0).toFixed(2)
        } allocated · ${Number(exposure.open_positions || 0)} open · $${
          Number(exposure.remaining_capacity_usd || 0).toFixed(2)
        } capacity`
      : `$${Number(policy.trading_allocation_usd || 0).toFixed(2)} allocation`;
    updateSavedIndicator();
  }

  function updateSavedIndicator() {
    const saved = document.querySelector("#us-rail-saved");
    if (!saved) return;
    if (usTradingFormDirty) {
      saved.className = "us-rail-saved is-dirty";
      saved.textContent = "Unsaved changes — not in effect until saved";
    } else {
      saved.className = "us-rail-saved is-saved";
      saved.textContent = "Saved policy in effect";
    }
    renderPolicyStepNav();
  }

  function renderEffectivePolicyReview() {
    const box = document.querySelector("#us-effective-policy-review");
    if (!box) return;
    const status = lastUSTradingStatus;
    const state = status?.execution_state;
    if (!state) {
      box.textContent = "Save the policy to review its resolved effect.";
      return;
    }
    const auth = state.authorization || {};
    const exposure = state.exposure || {};
    const predictive = state.predictive_exit || {};
    const blockers = Array.isArray(state.entry_blockers)
      ? state.entry_blockers
      : [];
    const rows = [
      ["Lane", state.policy?.execution_mode === "live" ? "Live orders" : "Dry run"],
      ["Authorized line/stage", `${Number(auth.authorized_combinations || 0)} of ${
        (auth.combinations || []).length}`],
      ["Global fallback", auth.global_fallback_authorized ? "authorized" : "off"],
      ["Allocation", `$${Number(exposure.trading_allocation_usd || 0).toFixed(2)}`],
      ["Max total exposure", `$${Number(exposure.max_total_exposure_usd || 0).toFixed(2)}`],
      ["Cash reserve", `$${Number(exposure.minimum_cash_reserve_usd || 0).toFixed(2)}`],
      ["Open positions", `${Number(exposure.open_positions || 0)} of ${
        Number(exposure.max_open_positions || 0)}`],
      ["Predictive exit", String(predictive.status || "unknown").replace(/_/g, " ")],
      ["Automation", state.automation?.running ? "running" : "stopped"]
    ];
    box.innerHTML = `
      <h4>Effective policy${usTradingFormDirty ? " (saved version — you have unsaved edits)" : ""}</h4>
      <dl>${rows.map(([label, value]) => `<div><dt>${esc(label)}</dt><dd>${
        esc(value)}</dd></div>`).join("")}</dl>
      ${blockers.length
        ? `<p class="us-cold-start-warning">Entries are currently blocked: ${
            esc(blockers.map(item => item.detail || item.code).join("; "))}</p>`
        : "<p>No lane-level condition is blocking a new entry.</p>"}
      ${predictive.cold_start_warning
        ? `<p class="us-cold-start-warning">${esc(predictive.cold_start_warning)}</p>`
        : ""}`;
  }

  // Render the steps immediately so the form is navigable before the first
  // status response arrives.
  showPolicyStep(currentPolicyStep);

  const EXECUTION_STATE_UNAVAILABLE =
    "Execution state is unavailable for this lane.";

  function stateChip(label, value, tone) {
    return `<span class="us-state-chip is-${tone}">
      <strong>${esc(label)}</strong><span>${esc(value)}</span>
    </span>`;
  }

  function renderExecutionState(status) {
    const chipBox = document.querySelector("#us-execution-state-chips");
    const blockerBox = document.querySelector("#us-execution-blockers");
    const matrixBox = document.querySelector("#us-authorization-matrix");
    if (!chipBox || !blockerBox || !matrixBox) return;
    const state = status?.execution_state;
    if (!state) {
      chipBox.textContent = EXECUTION_STATE_UNAVAILABLE;
      blockerBox.textContent = "";
      matrixBox.textContent = "";
      const stale = document.querySelector("#us-adaptive-recommendation");
      if (stale) stale.innerHTML = "";
      return;
    }
    const auth = state.authorization || {};
    const exposure = state.exposure || {};
    const predictive = state.predictive_exit || {};
    const live = state.live_orders || {};
    const chips = [
      stateChip(
        "Automation",
        state.automation?.running ? "running" : "stopped",
        state.automation?.running ? "on" : "off"
      ),
      stateChip(
        "Global fallback",
        auth.global_fallback_authorized ? "authorized" : "not authorized",
        auth.global_fallback_authorized ? "on" : "off"
      ),
      stateChip(
        "Authorized line/stage",
        `${Number(auth.authorized_combinations || 0)} of ${
          (auth.combinations || []).length
        }`,
        auth.any_authorized ? "on" : "off"
      ),
      stateChip(
        "Account",
        state.account?.connected ? "connected" : "not connected",
        state.account?.connected ? "on" : "off"
      ),
      stateChip(
        "Exposure",
        `$${Number(exposure.managed_exposure_usd || 0).toFixed(2)} of $${
          Number(exposure.max_total_exposure_usd || 0).toFixed(2)
        } · ${Number(exposure.open_positions || 0)}/${
          Number(exposure.max_open_positions || 0)
        } open`,
        Number(exposure.remaining_capacity_usd || 0) > 0 ? "on" : "warn"
      ),
      stateChip(
        "Predictive exit",
        String(predictive.status || "unknown").replace(/_/g, " "),
        predictive.can_tighten_exits
          ? (predictive.status === "active" ? "on" : "warn")
          : "off"
      )
    ];
    if (live.applies_to_lane) {
      chips.splice(1, 0, stateChip(
        "Live latch",
        live.armed
          ? `armed · ${Math.round(Number(live.seconds_remaining || 0) / 60)}m left`
          : "disarmed",
        live.armed ? "warn" : "off"
      ));
      chips.splice(2, 0, stateChip(
        "Protective exits",
        state.protective_exits?.armed ? "armed" : "not armed",
        state.protective_exits?.armed ? "on" : "off"
      ));
    }
    chipBox.innerHTML = chips.join("");

    const blockers = Array.isArray(state.entry_blockers)
      ? state.entry_blockers
      : [];
    if (!blockers.length) {
      blockerBox.className = "us-execution-blockers is-clear";
      blockerBox.textContent = (
        "No lane-level condition is blocking a new entry. Individual "
        + "candidates can still be rejected on price, spread, depth, edge, or "
        + "quality."
      );
    } else {
      blockerBox.className = "us-execution-blockers is-blocked";
      blockerBox.innerHTML = `<strong>New entries are blocked:</strong><ul>${
        blockers.map(item => `<li>${esc(item.detail || item.code)}</li>`).join("")
      }</ul>`;
    }

    if (predictive.cold_start_warning) {
      blockerBox.innerHTML += `<p class="us-cold-start-warning">${
        esc(predictive.cold_start_warning)
      } The configured hard stop is never changed by it.</p>`;
    }
    // Its own region, not the entry-blocker box: an adaptive *exit*
    // recommendation is not a reason entries are blocked, and reading one
    // under that heading invites exactly the wrong conclusion.
    const recBox = document.querySelector("#us-adaptive-recommendation");
    const rec = predictive.recommendation;
    if (recBox) {
      if (!rec) {
        recBox.innerHTML = "";
      } else {
        const current = status?.policy?.adaptive_exit_profile || "observe";
        const matches = current === rec.profile
          && !!status?.policy?.adaptive_exit_enabled;
        recBox.innerHTML = `<div class="us-adaptive-recommendation">
          <strong>Adaptive cash-out · recommended: ${esc(rec.profile)}</strong>
          <span>Currently ${esc(
            status?.policy?.adaptive_exit_enabled
              ? current
              : `${current} (overlay off)`
          )}.</span>
          <span>${esc(rec.rationale)}</span>
          <span>Basis: ${esc(
            rec.basis === "overlay_scored"
              ? "the overlay's own scored forecasts, not realized return"
              : "insufficient scored evidence"
          )}. Lane-wide, so a line profile cannot carry these.</span>
          ${matches
            ? "<span>This lane already matches the recommendation.</span>"
            : '<button class="ghost compact-button" id="us-apply-adaptive-recommendation" type="button">Apply to this lane</button>'}
        </div>`;
        document.querySelector("#us-apply-adaptive-recommendation")
          ?.addEventListener("click", applyAdaptiveRecommendation);
      }
    }

    const combinations = Array.isArray(auth.combinations)
      ? auth.combinations
      : [];
    // `effective` is sent only where a profile actually changes a threshold;
    // an inheriting combination resolves against the saved global policy.
    const globalPolicy = status?.policy || {};
    matrixBox.innerHTML = combinations.map(row => {
      const effective = row.effective || globalPolicy;
      const detail = row.authorized
        ? [
            `edge ${(Number(effective.min_edge || 0) * 100).toFixed(1)}%`,
            `quality ${Number(effective.min_signal_quality || 0).toFixed(0)}+`,
            `stop ${(Number(effective.stop_loss || 0) * 100).toFixed(1)}%`,
            row.inherits_global
              ? "global values"
              : `${row.profile_key} overrides ${
                  (row.overridden_fields || []).length
                }`
          ].join(" · ")
        : (row.blocked_reason || "not authorized");
      return `<article class="us-authorization-row is-${
        row.authorized ? "authorized" : "blocked"
      }">
        <strong>${esc(row.market_type)} / ${esc(row.game_stage)}</strong>
        <span>${esc(detail)}</span>
      </article>`;
    }).join("") || "No line/stage combination is available.";
  }

  function updateAdaptiveExitAvailability() {
    const enabled = !!document.querySelector("#us-adaptive-exit-enabled")?.checked;
    for (const selector of [
      "#us-adaptive-exit-profile",
      "#us-adaptive-exit-horizon",
      "#us-adaptive-exit-min-samples",
      "#us-adaptive-exit-max-tightening"
    ]) {
      const input = document.querySelector(selector);
      if (input) input.disabled = !enabled;
    }
  }

  document.querySelector("#us-adaptive-exit-enabled")?.addEventListener(
    "change",
    updateAdaptiveExitAvailability
  );

  function updateStopGuardAvailability() {
    const enabled = !!document.querySelector("#us-volatility-stop-enabled")?.checked;
    for (const selector of [
      "#us-stop-confirmation-readings",
      "#us-stop-grace-minutes",
      "#us-catastrophic-stop-multiplier",
      "#us-stateless-stop-confirmation"
    ]) {
      const input = document.querySelector(selector);
      if (input) input.disabled = !enabled;
    }
  }

  document.querySelector("#us-volatility-stop-enabled")?.addEventListener(
    "change",
    updateStopGuardAvailability
  );

  function renderAdaptiveExitStatus(status) {
    const box = document.querySelector("#us-adaptive-exit-status");
    if (!box) return;
    const adaptive = status?.adaptive_exit || {};
    const profiles = adaptive.profiles || {};
    const profile = adaptive.selected_profile
      || status?.policy?.adaptive_exit_profile
      || "observe";
    const profileLabel = profiles[profile]?.label || profile;
    const observations = Number(adaptive.observations || 0);
    const events = Number(adaptive.events || 0);
    const labeled = Number(adaptive.labeled_observations || 0);
    const labeledEvents = Number(adaptive.labeled_events || 0);
    const score = adaptive.brier_score == null
      ? "not available yet"
      : Number(adaptive.brier_score).toFixed(3);
    const monitored = Number(adaptive.monitored_mlb_events || 0);
    const stateReady = Number(adaptive.live_state_mlb_events || 0);
    const eligible = Number(adaptive.eligible_open_positions || 0);
    const eligibleState = Number(adaptive.eligible_state_positions || 0);
    const segments = Array.isArray(adaptive.market_segments)
      ? adaptive.market_segments
      : [];
    const segmentText = segments.length
      ? segments
        .map(item => `${esc(item.market_type)} ${Number(item.observations || 0)}`)
        .join(" / ")
      : "no line-type observations yet";
    const recovery = adaptive.exit_recovery || {};
    const recovered = Number(recovery.recovered_entry || 0);
    const recoveryExits = Number(recovery.exits || 0);
    const tracking = Number(recovery.tracking || 0);
    const rebound = recovery.average_rebound == null
      ? "not available yet"
      : `${(Number(recovery.average_rebound) * 100).toFixed(1)} cents`;
    box.innerHTML = `
      <strong>${adaptive.enabled
        ? esc(profileLabel)
        : status?.policy?.volatility_stop_enabled
          ? "State-aware stop protection"
          : "Disabled"}</strong>
      <span>${observations} managed-position observations across ${events} MLB events</span>
      <span>${labeled} labeled / ${labeledEvents} event-balanced support</span>
      <span>${stateReady}/${monitored} monitored MLB events have usable state · ${eligibleState}/${eligible} eligible open positions are state-ready</span>
      <span>Brier movement score ${esc(score)}</span>
      <span>${segmentText}</span>
      <span>Post-exit audit: ${recovered}/${recoveryExits} recovered to cost basis / ${tracking} tracking / average rebound ${rebound}</span>
      <small>${esc(adaptive.collection_note || "Waiting for an eligible managed MLB position.")}</small>`;
    const recoveryList = document.querySelector("#us-exit-recovery-list");
    if (!recoveryList) return;
    const recent = Array.isArray(recovery.recent) ? recovery.recent : [];
    recoveryList.innerHTML = recent.length
      ? recent.map(item => {
        const entry = Number(item.entry_cost || 0);
        const sold = Number(item.exit_value || 0);
        const best = Number(item.best_exit_value || sold);
        const worst = Number(item.worst_exit_value || sold);
        const reboundCents = (best - sold) * 100;
        const recoveredText = item.recovered_entry_at
          ? "recovered to entry"
          : item.recovered_half_loss_at
            ? "recovered at least half the sold loss"
            : "did not recover to entry";
        const state = item.status === "tracking" ? "tracking" : "resolved";
        return `<article class="us-exit-recovery-row">
          <div>
            <strong>${esc(item.event_name || "Unknown MLB event")}</strong>
            <span>${esc(item.market_type || "line")} · ${esc(item.selection || "selection")}</span>
          </div>
          <div><span>Entry / sold</span><strong>${(entry * 100).toFixed(1)}¢ / ${(sold * 100).toFixed(1)}¢</strong></div>
          <div><span>Best / worst after sale</span><strong>${(best * 100).toFixed(1)}¢ / ${(worst * 100).toFixed(1)}¢</strong></div>
          <div><span>Audit result</span><strong>${esc(recoveredText)} · ${reboundCents >= 0 ? "+" : ""}${reboundCents.toFixed(1)}¢ · ${esc(state)}</strong></div>
        </article>`;
      }).join("")
      : "No tracked exits yet.";
  }

  function renderRiskSessionStatus(status) {
    const summary = document.querySelector("#us-risk-session-summary");
    if (!summary) return;
    const risk = status?.risk_session || {};
    const orders = Number(risk.orders_last_hour || 0);
    const orderLimit = Number(
      risk.orders_limit ?? status?.policy?.max_orders_per_hour ?? 0
    );
    const loss = Number(risk.realized_loss_24h_usd || 0);
    const lossLimit = Number(
      risk.realized_loss_limit_usd
        ?? status?.policy?.max_daily_loss_usd
        ?? 0
    );
    const blockers = Array.isArray(risk.active_entry_blockers)
      ? risk.active_entry_blockers.length
      : 0;
    const reset = risk.reset_at
      ? ` · session reset ${new Date(risk.reset_at).toLocaleTimeString()}`
      : "";
    summary.textContent = (
      `${orders}/${orderLimit} rolling live entries · ` +
      `$${loss.toFixed(2)}/$${lossLimit.toFixed(2)} rolling gross realized loss` +
      `${reset}${blockers ? " · ENTRY CIRCUIT BREAKER ACTIVE" : ""}`
    );
  }

  function renderTradingStatus(status) {
    cacheUSExecutionStatus(status);
    renderAdaptiveExitStatus(status);
    renderRiskSessionStatus(status);
    const box = document.querySelector("#us-trading-status");
    const badge = document.querySelector("#us-trading-mode-badge");
    if (!box || !badge) return;
    const policy = status.policy || {};
    const armed = !!status.armed;
    badge.className = `us-venue-badge${armed ? " is-armed" : ""}`;
    badge.textContent = armed
      ? "LIVE ARMED"
      : policy.execution_mode === "live" ? "LIVE · DISARMED" : "DRY RUN";
    const armText = policy.execution_mode !== "live"
      ? "Live-order arming does not apply to this dry-run lane."
      : armed && status.armed_until
        ? `Live order latch expires ${new Date(status.armed_until).toLocaleTimeString()}.`
        : "Live order latch is closed.";
    const protectiveText = policy.execution_mode !== "live"
      ? "Protective live exits are not applicable in dry run."
      : !policy.auto_cashout
        ? "Automatic cash-out is off."
        : status.protective_exits_armed
          ? "Protective auto-exits stay armed until policy save, disarm, stop, or restart."
          : "Protective auto-exits are disarmed; arm live trading to enable them.";
    const gateText = policy.require_engine_entry !== false
      ? "Strict engine gates"
      : `${(policy.required_engine_gates || []).length} selected engine gates`;
    const allocationText = `$${Number(
      policy.trading_allocation_usd || policy.max_total_exposure_usd || 0
    ).toFixed(2)} allocation / ${String(policy.risk_preset || "custom")} risk`;
    const lineTypeText = Array.isArray(policy.allowed_market_types)
      ? `${policy.allowed_market_types.join(" / ")} entries`
      : "moneyline / spread / total entries";
    const scopeText = Array.isArray(policy.allowed_market_scopes)
      ? policy.allowed_market_scopes
        .map(value => String(value).replaceAll("_", " "))
        .join(" / ")
      : "full game";
    box.className = `us-trading-status${armed ? " is-armed" : ""}`;
    box.innerHTML = `
      <strong>${policy.automation_enabled ? "Automation on" : "Automation off"}</strong>
      <span>${esc(armText)}</span>
      <span>${esc(protectiveText)}</span>
      <span>${esc(status.last_cycle_summary || "No cycle yet.")}</span>
      <span>${esc(allocationText)}</span>
      <span>${status.open_managed_positions || 0} open · $${Number(status.managed_exposure_usd || 0).toFixed(2)} exposure · ${esc(lineTypeText)} · ${esc(scopeText)} · ${esc(gateText)}</span>`;
    renderSegmentResearchStatus(status);
  }

  function renderSegmentResearchStatus(status) {
    const box = document.querySelector("#us-segment-research-status");
    if (!box) return;
    const research = status?.segment_research || {};
    const rows = Array.isArray(research.rows) ? research.rows : [];
    const policy = status?.policy || {};
    const liveLock = policy.execution_mode === "live"
      ? policy.allow_live_segment_markets
        ? "Live segment orders explicitly enabled."
        : "Live segment orders locked."
      : "Dry-run evidence collection only.";
    const evidence = rows.length
      ? rows.map(row => {
          const scope = String(row.market_scope || "").replaceAll("_", " ");
          const roi = row.after_cost_roi == null
            ? "ROI pending"
            : `${(Number(row.after_cost_roi) * 100).toFixed(1)}% after-cost ROI`;
          return `${scope} ${row.market_type}: ${row.trades} trades / ${row.closed} closed / ${roi}`;
        }).join(" · ")
      : "No retained MLB segment simulations yet.";
    const paidScopes = Array.isArray(status?.odds_api_market_scopes)
      ? status.odds_api_market_scopes
        .map(value => String(value).replaceAll("_", " "))
        .join(", ")
      : "full game";
    box.innerHTML = `<strong>${esc(liveLock)}</strong><span>${esc(evidence)}</span><small>Odds feed scope now: ${esc(paidScopes)}.</small>`;
  }

  function venueSyncLabel(value) {
    return ({
      awaiting_sync: "awaiting first account sync",
      entry_settlement_grace: "entry settling",
      in_sync: "account quantity confirmed",
      in_sync_with_manual_excess: "managed quantity confirmed; extra manual shares exist",
      mismatch_pending_confirmation: "account change pending confirmation",
      partially_sold_externally: "partially sold on phone/account",
      externally_closed: "closed on phone/account",
      sync_error: "account sync failed",
      not_applicable: "dry run"
    })[value] || String(value || "not synchronized");
  }

  function renderVenuePositions(status = lastUSTradingStatus) {
    const body = document.querySelector("#us-venue-positions");
    const syncStatus = document.querySelector("#us-venue-sync-status");
    if (!body || !syncStatus) return;
    const positions = Array.isArray(status?.venue_positions)
      ? status.venue_positions
      : [];
    const syncedAt = status?.last_venue_sync_at
      ? ` at ${new Date(status.last_venue_sync_at).toLocaleTimeString()}`
      : "";
    syncStatus.textContent = status?.last_venue_sync_error
      ? `Sync failed${syncedAt}: ${status.last_venue_sync_error}`
      : `${status?.last_venue_sync_summary || "Not synchronized yet."}${syncedAt}`;
    syncStatus.classList.toggle("is-error", !!status?.last_venue_sync_error);
    if (!positions.length) {
      body.innerHTML = '<div class="metrics-empty">No non-zero positions in the latest authenticated account snapshot.</div>';
      return;
    }
    body.innerHTML = positions.map(position => {
      const net = Number(position.net_position || 0);
      const available = position.qty_available == null
        ? "unknown"
        : Number(position.qty_available).toFixed(2);
      const cost = position.cost_basis == null
        ? "unknown"
        : `$${Number(position.cost_basis).toFixed(2)}`;
      const cash = position.cash_value == null
        ? "unknown"
        : `$${Number(position.cash_value).toFixed(2)}`;
      return `<article class="us-venue-position">
        <div><strong>${esc(position.title || position.market_slug)}</strong><span>${esc(position.outcome || position.market_slug)}</span></div>
        <div><span>net shares</span><strong>${net >= 0 ? "+" : ""}${net.toFixed(2)}</strong></div>
        <div><span>available</span><strong>${esc(available)}</strong></div>
        <div><span>venue cost</span><strong>${esc(cost)}</strong></div>
        <div><span>cash value</span><strong>${esc(cash)}</strong></div>
        <div><span>venue realized</span><strong>${position.realized_pnl == null ? "unknown" : esc(money(Number(position.realized_pnl)))}</strong></div>
      </article>`;
    }).join("");
  }

  function renderManagedPositions(positions = lastManagedPositions) {
    const body = document.querySelector("#us-managed-positions");
    if (!body) return;
    if (positions !== lastManagedPositions) lastManagedPositions = positions;
    const visible = usPositionMode === "all"
      ? lastManagedPositions
      : lastManagedPositions.filter(position => position.mode === usPositionMode);
    if (!visible.length) {
      const modeLabel = usPositionMode === "all"
        ? "managed"
        : usPositionMode === "live" ? "live" : "dry-run";
      body.innerHTML = `<div class="metrics-empty">No ${esc(modeLabel)} positions to display.</div>`;
      return;
    }
    body.innerHTML = visible.map(position => {
      const open = position.status === "open";
      const ret = position.return_fraction == null ? "—" : `${position.return_fraction >= 0 ? "+" : ""}${(position.return_fraction * 100).toFixed(1)}%`;
      const edge = position.current_execution_edge == null ? "—" : signedCents(position.current_execution_edge);
      const pnl = position.realized_pnl == null ? "—" : money(position.realized_pnl);
      const quantity = Number(position.quantity || 0);
      const initialQuantity = Number(
        position.initial_quantity ?? position.quantity ?? 0
      );
      const initialCost = Number(
        position.initial_cost_basis ?? position.cost_basis ?? 0
      );
      const remainingCost = Number(position.cost_basis || 0);
      const cashoutValue = position.current_exit_value == null
        ? null
        : Number(position.current_exit_value) * quantity;
      const markedPnl = cashoutValue == null
        ? null
        : cashoutValue - remainingCost;
      const profitTargetReadings = Number(
        position.profit_target_observation_count || 0
      );
      const profitLock = position.profit_lock_armed_at
        ? `profit lock armed ${new Date(position.profit_lock_armed_at).toLocaleTimeString()}`
        : profitTargetReadings > 0
          ? `profit target confirmation ${Math.min(2, profitTargetReadings)}/2`
          : "profit target not reached";
      const venueState = venueSyncLabel(position.venue_sync_status);
      const externalQuantity = Number(position.external_exit_quantity || 0);
      const adaptive = position.adaptive_exit;
      const adaptiveState = adaptive?.state || {};
      const adaptiveProbability = adaptive?.predicted_adverse_probability;
      const adaptiveLine = adaptive
        ? [
            adaptive.active
              ? "adaptive tightening active"
              : "adaptive observation",
            adaptiveProbability == null
              ? ""
              : `${(Number(adaptiveProbability) * 100).toFixed(1)}% adverse-move forecast`,
            adaptiveState.inning
              ? `${adaptiveState.half || ""} ${adaptiveState.inning}`.trim()
              : "",
            adaptive.context_events == null
              ? ""
              : `${Number(adaptive.context_events)} context events`,
            adaptive.tightening_fraction
              ? `${(Number(adaptive.tightening_fraction) * 100).toFixed(1)}% tightening`
              : "",
            adaptive.reason || ""
          ].filter(Boolean).join(" / ")
        : "";
      const stopGuard = position.stop_guard;
      const stopGuardLine = stopGuard
        ? [
            `stop guard ${stopGuard.status || "inactive"}`,
            stopGuard.confirmations == null
              ? ""
              : `${Number(stopGuard.confirmations)}/${Number(stopGuard.required_confirmations || 0)} readings`,
            stopGuard.grace_seconds == null
              ? ""
              : `${Math.max(
                0,
                Number(stopGuard.grace_seconds)
                  - Number(stopGuard.elapsed_seconds || 0)
              ).toFixed(0)}s review remaining`,
            stopGuard.reason || ""
          ].filter(Boolean).join(" / ")
        : "";
      const profitGuard = position.profit_guard;
      const profitGuardLine = profitGuard
        ? [
            `profit protection ${String(
              profitGuard.status || "inactive"
            ).replaceAll("_", " ")}`,
            profitGuard.protected_floor_value == null
              ? ""
              : `floor ${cents(profitGuard.protected_floor_value)}`,
            profitGuard.fee_adjusted_exit_value == null
              ? ""
              : `fee-adjusted executable ${cents(
                  profitGuard.fee_adjusted_exit_value
                )}`,
            Number(profitGuard.missed_count || 0) > 0
              ? `${Number(profitGuard.missed_count)} missed-floor reading${
                  Number(profitGuard.missed_count) === 1 ? "" : "s"
                }`
              : "",
            profitGuard.reason || ""
          ].filter(Boolean).join(" / ")
        : "";
      const action = open
        ? `<button class="us-position-action" type="button" data-exit-position="${esc(position.id)}" data-exit-mode="${esc(position.mode)}">${position.mode === "live" ? "Sell position" : "Remove simulation"}</button>`
        : "<div></div>";
      return `<article class="us-position${open ? " is-open" : ""}">
        <div>
          <strong>${esc(position.event_name)}</strong>
          <span>${esc(position.market_type)} · ${esc(String(position.market_scope || "full_game").replaceAll("_", " "))} · ${esc(position.selection)} · ${esc(position.mode)}</span>
        </div>
        <div><span>bought</span><strong>${initialQuantity.toFixed(2)} @ ${cents(position.entry_cost)}</strong></div>
        <div><span>total paid</span><strong>$${initialCost.toFixed(2)}</strong></div>
        <div><span>remaining</span><strong>${quantity.toFixed(2)} shares</strong></div>
        <div><span>${position.mode === "live" ? "fee-adjusted cash-out" : "cash-out quote"}</span><strong>${cents(position.current_exit_value)}${cashoutValue == null ? "" : ` / $${cashoutValue.toFixed(2)}`}</strong></div>
        <div><span>return</span><strong>${esc(ret)}</strong></div>
        <div><span>${open ? "marked P/L" : "realized"}</span><strong>${open ? esc(money(markedPnl)) : esc(pnl)}</strong></div>
        <div><span>current edge</span><strong>${esc(edge)}</strong></div>
        <div><span>status</span><strong>${open ? "OPEN" : esc(String(position.status).toUpperCase())}</strong></div>
        ${action}
        <div class="us-position-reason">
          ${esc(profitLock)} · ${esc(venueState)}
          ${externalQuantity > 0 ? ` · ${externalQuantity.toFixed(2)} shares sold outside workstation` : ""}
          ${position.exit_reason ? ` · ${esc(position.exit_reason)}` : ""}
          ${adaptiveLine ? `<span class="us-position-adaptive">${esc(adaptiveLine)}</span>` : ""}
          ${stopGuardLine ? `<span class="us-position-adaptive">${esc(stopGuardLine)}</span>` : ""}
          ${profitGuardLine ? `<span class="us-position-adaptive">${esc(profitGuardLine)}</span>` : ""}
        </div>
      </article>`;
    }).join("");
  }

  function renderTradingPerformance(performance) {
    const body = document.querySelector("#us-performance-summary");
    if (!body) return;
    lastTradingPerformance = performance;
    const modes = performance?.modes || {};
    const summaries = [
      modes.dry_run || {mode:"dry_run", label:"Dry run"},
      modes.live || {mode:"live", label:"Live"},
      performance?.combined || {mode:"combined", label:"Combined"}
    ];
    const dryTotal = Number(modes.dry_run?.total_positions || 0);
    const liveOpen = Number(modes.live?.open_positions || 0);
    const dryCount = document.querySelector("#us-liquidate-dry-count");
    const liveCount = document.querySelector("#us-liquidate-live-count");
    if (dryCount) {
      dryCount.textContent = `${dryTotal} trade${dryTotal === 1 ? "" : "s"}`;
    }
    if (liveCount) liveCount.textContent = `${liveOpen} open`;
    body.innerHTML = summaries.map(summary => {
      const wins = Number(summary.wins || 0);
      const losses = Number(summary.losses || 0);
      const pushes = Number(summary.pushes || 0);
      const totalNet = Number(summary.total_net_usd || 0);
      const netClass = totalNet > 0.000000001
        ? "is-positive"
        : totalNet < -0.000000001 ? "is-negative" : "";
      const modeClass = String(summary.mode || "").replaceAll("_", "-");
      const unpriced = Number(summary.unpriced_open_positions || 0);
      const complete = summary.total_net_complete !== false;
      const record = `${wins}–${losses}–${pushes}`;
      const activityNote = unpriced
        ? `${unpriced} open position${unpriced === 1 ? "" : "s"} unpriced · total net is partial`
        : `${Number(summary.closed_positions || 0)} closed · ${Number(summary.open_positions || 0)} open`;
      const sessionNote = summary.session_started_at
        ? `display session since ${new Date(summary.session_started_at).toLocaleString()}`
        : "";
      const note = [sessionNote, activityNote].filter(Boolean).join(" · ");
      return `<article class="us-performance-card is-${esc(modeClass)}" data-performance-mode="${esc(summary.mode || "")}">
        <div class="us-performance-head"><strong>${esc(summary.label || summary.mode || "Mode")}</strong><span>${Number(summary.total_positions || 0)} trades</span></div>
        <div class="us-performance-hero">
          <div><span class="us-performance-key">W–L–P</span><strong class="us-performance-value">${esc(record)}</strong></div>
          <div><span class="us-performance-key">Total net${complete ? "" : "*"}</span><strong class="us-performance-value ${netClass}">${esc(money(totalNet))}</strong></div>
        </div>
        <div class="us-performance-metrics">
          <div><span>Win rate</span><strong>${esc(pct(summary.win_rate))}</strong></div>
          <div><span>Realized net</span><strong>${esc(money(Number(summary.realized_net_usd || 0)))}</strong></div>
          <div><span>Open marked P/L</span><strong>${esc(money(Number(summary.open_unrealized_pnl_usd || 0)))}</strong></div>
          <div><span>Open exposure</span><strong>$${Number(summary.open_cost_basis_usd || 0).toFixed(2)}</strong></div>
        </div>
        <div class="us-performance-note${complete ? "" : " is-partial"}">${esc(note)}</div>
      </article>`;
    }).join("");
  }

  function ledgerSettingsText(settings) {
    if (!settings) return "entry settings unavailable";
    const lines = Array.isArray(settings.allowed_market_types)
      ? settings.allowed_market_types.join("/")
      : "legacy";
    const scopes = Array.isArray(settings.allowed_market_scopes)
      ? settings.allowed_market_scopes
        .map(value => String(value).replaceAll("_", " "))
        .join("/")
      : "legacy scope";
    return [
      `${lines} lines`,
      scopes,
      settings.min_edge == null ? "" : `edge ${(Number(settings.min_edge) * 100).toFixed(1)}%`,
      settings.min_signal_quality == null
        ? ""
        : `quality ${Number(settings.min_signal_quality).toFixed(0)}-${
            Number(settings.max_signal_quality ?? 100).toFixed(0)
          }`,
      settings.min_source_agreement == null
        ? ""
        : `agreement ${Number(settings.min_source_agreement).toFixed(0)}+`,
      settings.entry_confirmation_readings == null
        ? ""
        : `confirm ${Number(settings.entry_confirmation_readings).toFixed(0)}x`,
      settings.min_reference_sources == null ? "" : `refs ${settings.min_reference_sources}`,
      settings.min_entry_price == null || settings.max_entry_price == null
        ? ""
        : `buy ${(Number(settings.min_entry_price) * 100).toFixed(0)}-${(Number(settings.max_entry_price) * 100).toFixed(0)}c`,
      settings.max_spread == null ? "" : `spread <=${(Number(settings.max_spread) * 100).toFixed(1)}c`,
      settings.profit_target == null ? "" : `target ${(Number(settings.profit_target) * 100).toFixed(1)}%`,
      settings.minimum_locked_profit == null
        ? ""
        : `protected floor ${(Number(settings.minimum_locked_profit) * 100).toFixed(1)}%`,
      settings.stop_loss == null ? "" : `stop ${(Number(settings.stop_loss) * 100).toFixed(1)}%`
    ].filter(Boolean).join(" · ");
  }

  function ledgerFilterParams(format = "json") {
    return new URLSearchParams({
      mode: document.querySelector("#us-ledger-mode")?.value || "all",
      market_type: document.querySelector("#us-ledger-market-type")?.value || "all",
      result: document.querySelector("#us-ledger-result")?.value || "all",
      query: document.querySelector("#us-ledger-query")?.value?.trim() || "",
      format,
      limit: format === "csv" ? "10000" : "2000"
    });
  }

  function renderPerformanceLedger(data) {
    const status = document.querySelector("#us-ledger-status");
    const summaryBox = document.querySelector("#us-ledger-summary");
    const groupBox = document.querySelector("#us-ledger-settings-groups");
    const rowBox = document.querySelector("#us-ledger-rows");
    if (!status || !summaryBox || !groupBox || !rowBox) return;
    const summary = data.summary || {};
    const lineGroups = Array.isArray(data.line_type_summary)
      ? data.line_type_summary
      : [];
    const scopeGroups = Array.isArray(data.market_scope_summary)
      ? data.market_scope_summary
      : [];
    const dataSources = Array.isArray(data.data_sources)
      ? data.data_sources
      : [];
    const cards = [
      {label:"Filtered total", ...summary},
      ...lineGroups.map(group => ({
        label: String(group.market_type || "line").replaceAll("_", " "),
        ...group
      })),
      ...scopeGroups.map(group => ({
        label: String(group.market_scope || "segment").replaceAll("_", " "),
        ...group
      }))
    ];
    summaryBox.innerHTML = cards.map(card => `
      <div>
        <span>${esc(card.label)}</span>
        <strong>${Number(card.wins || 0)}-${Number(card.losses || 0)}-${Number(card.pushes || 0)} · ${esc(pct(card.win_rate))}</strong>
        <small>${Number(card.trades || 0)} trades / ${Number(card.events || 0)} events · ${esc(money(Number(card.realized_net_usd || 0)))} net · ${esc(pct(card.after_cost_roi))} ROI</small>
      </div>`).join("");

    const settingsGroups = Array.isArray(data.settings_groups)
      ? data.settings_groups
      : [];
    groupBox.innerHTML = settingsGroups.length
      ? settingsGroups.map(group => `
        <article class="us-ledger-settings-group">
          <div>
            <strong>${esc(group.market_type)} · ${esc(String(group.market_scope || "full_game").replaceAll("_", " "))} · ${esc(group.mode)} · #${esc(group.policy_signature)}</strong>
            <span>${Number(group.verifiable_closed || 0)} closes / ${Number(group.events || 0)} events · ${Number(group.wins || 0)}-${Number(group.losses || 0)}-${Number(group.pushes || 0)} · ${esc(pct(group.win_rate))} success</span>
          </div>
          <div>
            <strong class="${Number(group.realized_net_usd || 0) >= 0 ? "is-positive" : "is-negative"}">${esc(money(Number(group.realized_net_usd || 0)))}</strong>
            <span>${esc(pct(group.after_cost_roi))} after-cost ROI</span>
          </div>
          <small>${esc(ledgerSettingsText(group.settings))}</small>
        </article>`).join("")
      : '<div class="metrics-empty">No verifiable closed trades match these filters yet.</div>';

    const rows = Array.isArray(data.rows) ? data.rows : [];
    rowBox.innerHTML = rows.length
      ? rows.map(row => {
          const resultClass = ["win", "loss"].includes(row.result)
            ? ` is-${row.result}`
            : "";
          const settings = ledgerSettingsText(row.entry_policy);
          return `<tr>
            <td data-label="Opened">${esc(row.opened_at ? new Date(row.opened_at).toLocaleString() : "—")}</td>
            <td data-label="Mode">${esc(row.mode)}</td>
            <td data-label="Result"><strong class="us-ledger-result${resultClass}">${esc(row.result)}</strong></td>
            <td data-label="Event"><strong>${esc(row.event_name)}</strong><span>${esc(row.selection)}</span></td>
            <td data-label="Line">${esc(row.market_type)}<span>${esc(String(row.market_scope || "full_game").replaceAll("_", " "))}</span></td>
            <td data-label="Buy">${esc(cents(row.entry_cost))}</td>
            <td data-label="Stake">$${Number(row.cost_basis_usd || 0).toFixed(2)}</td>
            <td data-label="Net">${row.realized_net_usd == null ? "—" : esc(money(Number(row.realized_net_usd)))}</td>
            <td data-label="Edge">${esc(signedCents(row.entry_execution_edge ?? row.entry_signal_edge))}</td>
            <td data-label="Quality">${row.entry_signal_quality == null ? "—" : Number(row.entry_signal_quality).toFixed(0)}</td>
            <td data-label="Settings"><details><summary>#${esc(row.policy_signature)}</summary><span>${esc(settings)}</span></details></td>
          </tr>`;
        }).join("")
      : '<tr class="us-ledger-empty"><td colspan="11">No trades match the selected filters.</td></tr>';
    const generated = data.generated_at
      ? new Date(data.generated_at).toLocaleTimeString()
      : "now";
    status.className = "refresh-status";
    const sourceText = dataSources.length
      ? dataSources
        .map(source => `${String(source.lane || "store").replaceAll("_", " ")} ${Number(source.retained_positions || 0)}`)
        .join(" / ")
      : "current execution store";
    status.textContent = (
      `${Number(data.total_matching_rows || 0)} retained trade rows · refreshed ${generated}` +
      (data.rows_truncated ? " · screen truncated; CSV can include up to 10,000 rows" : "") +
      ` · sources: ${sourceText}` +
      " · high rates from small samples are not reliable by themselves"
    );
  }

  async function loadPerformanceLedger({quiet = false} = {}) {
    if (usLedgerLoading) return;
    usLedgerLoading = true;
    const button = document.querySelector("#us-ledger-refresh");
    const status = document.querySelector("#us-ledger-status");
    if (!quiet) {
      setActionBusy(button, true, "Refreshing…");
      if (status) status.textContent = "Reading retained trades and exact entry-time settings…";
    }
    try {
      const response = await fetch(
        tradingApi(
          `/api/polymarket-us/trading/performance-ledger?${ledgerFilterParams()}`
        ),
        {cache:"no-store"}
      );
      const data = await response.json().catch(()=>({}));
      if (!response.ok) throw new Error(detailMessage(data));
      usLedgerLoaded = true;
      renderPerformanceLedger(data);
    } catch (error) {
      if (status) {
        status.className = "refresh-status is-error";
        status.textContent = error.message || "Could not load the performance datasheet";
      }
    } finally {
      usLedgerLoading = false;
      if (!quiet) setActionBusy(button, false);
    }
  }

  document.querySelector("#us-ledger-refresh")?.addEventListener(
    "click",
    () => loadPerformanceLedger()
  );
  for (const selector of [
    "#us-ledger-mode",
    "#us-ledger-market-type",
    "#us-ledger-result"
  ]) {
    document.querySelector(selector)?.addEventListener(
      "change",
      () => loadPerformanceLedger()
    );
  }
  document.querySelector("#us-ledger-query")?.addEventListener("input", () => {
    window.clearTimeout(usLedgerQueryTimer);
    usLedgerQueryTimer = window.setTimeout(
      () => loadPerformanceLedger({quiet:true}),
      300
    );
  });
  document.querySelector("#us-ledger-export")?.addEventListener(
    "click",
    async event => {
      const button = event.currentTarget;
      const status = document.querySelector("#us-ledger-status");
      setActionBusy(button, true, "Exporting…");
      if (status) status.textContent = "Building a CSV from the current datasheet filters…";
      try {
        const response = await fetch(
          tradingApi(
            `/api/polymarket-us/trading/performance-ledger?${ledgerFilterParams("csv")}`
          ),
          {cache:"no-store"}
        );
        if (!response.ok) {
          const body = await response.json().catch(()=>({}));
          throw new Error(detailMessage(body));
        }
        const blob = await response.blob();
        const url = URL.createObjectURL(blob);
        const link = document.createElement("a");
        const disposition = response.headers.get("content-disposition") || "";
        const match = disposition.match(/filename="([^"]+)"/);
        link.href = url;
        link.download = match?.[1] || "trade-performance.csv";
        document.body.append(link);
        link.click();
        link.remove();
        URL.revokeObjectURL(url);
        if (status) status.textContent = "Filtered CSV downloaded. The on-screen datasheet remains unchanged.";
      } catch (error) {
        if (status) {
          status.className = "refresh-status is-error";
          status.textContent = error.message || "Could not export the filtered datasheet";
        }
      } finally {
        setActionBusy(button, false);
      }
    }
  );

  function advisorSettingValue(field, value) {
    if (field === "allowed_market_types") {
      return Array.isArray(value) ? value.join(", ") : String(value || "none");
    }
    if ([
      "min_edge",
      "max_edge",
      "min_entry_price",
      "max_entry_price",
      "min_mlb_fraction_remaining"
    ].includes(field)) {
      return `${(Number(value) * 100).toFixed(1)}%`;
    }
    if (field === "candidate_cooldown_seconds") return `${Number(value).toFixed(0)}s`;
    return Number(value).toFixed(
      field === "min_signal_quality" ? 0 : 0
    );
  }

  function advisorDiagnosticTable(title, rows) {
    if (!Array.isArray(rows) || !rows.length) return "";
    return `<section class="us-advisor-diagnostic">
      <h3>${esc(title)}</h3>
      <div class="table-wrap"><table>
        <thead><tr><th>Segment</th><th>Trades</th><th>Events</th><th>Net</th><th>ROI</th><th>Win rate</th><th>Event CI</th></tr></thead>
        <tbody>${rows.map(row => `<tr>
          <td>${esc(row.label || "unknown")}</td>
          <td>${Number(row.trades || 0)}</td>
          <td>${Number(row.events || 0)}</td>
          <td>${esc(money(Number(row.net_usd || 0)))}</td>
          <td>${pct(row.turnover_roi)}</td>
          <td>${pct(row.win_rate)}</td>
          <td>${row.event_block_bootstrap?.lower_95 == null
            ? esc(row.support || "descriptive")
            : `${pct(row.event_block_bootstrap.lower_95)}–${pct(row.event_block_bootstrap.upper_95)}`}</td>
        </tr>`).join("")}</tbody>
      </table></div>
    </section>`;
  }

  function advisorValidationBlockers(advice) {
    if (advice?.validation_passed) return [];
    const evidence = advice?.evidence || {};
    const test = evidence.suggested_test || {};
    const bootstrap = evidence.event_block_bootstrap || {};
    const blockers = [];
    const comparisons = [
      [
        Number(evidence.eligible_closed_trades || 0),
        Number(evidence.minimum_closed_trades || 0),
        "complete trades"
      ],
      [
        Number(evidence.independent_events || 0),
        Number(evidence.minimum_independent_events || 0),
        "independent events"
      ],
      [
        Number(test.trades || 0),
        Number(evidence.minimum_test_trades || 0),
        "later-event trades"
      ],
      [
        Number(test.events || 0),
        Number(evidence.minimum_test_events || 0),
        "later test events"
      ]
    ];
    for (const [actual, required, label] of comparisons) {
      if (required > 0 && actual < required) {
        blockers.push(`${actual}/${required} ${label}`);
      }
    }
    if (test.turnover_roi == null || Number(test.turnover_roi) <= 0) {
      blockers.push("later-event after-cost ROI is not positive");
    }
    if (
      test.maximum_event_stake_share == null
      || Number(test.maximum_event_stake_share) > 0.35
    ) {
      blockers.push("later-event stake concentration exceeds 35%");
    }
    if (
      bootstrap.probability_positive == null
      || Number(bootstrap.probability_positive) < 0.90
    ) {
      blockers.push("90% whole-event positive-return support is unavailable");
    }
    if (bootstrap.lower_95 == null || Number(bootstrap.lower_95) <= 0) {
      blockers.push("whole-event lower confidence bound is not positive");
    }
    return blockers;
  }

  function invalidatePolicyAdvice(message) {
    if (!lastPolicyAdvice) return;
    lastPolicyAdvice = null;
    const button = document.querySelector("#us-policy-advisor-apply");
    const status = document.querySelector("#us-policy-advisor-status");
    const body = document.querySelector("#us-policy-advisor-result");
    if (button) {
      button.disabled = true;
      button.textContent = "Analyze again before applying";
    }
    const download = document.querySelector("#us-policy-advisor-download");
    if (download) download.disabled = true;
    body?.classList.add("is-stale");
    if (status) {
      status.className = "refresh-status";
      status.textContent = message;
    }
  }

  const ADVISOR_BASIS_LABELS = {
    validated:"Validated on later events",
    observational:"Observational only",
    baseline_fallback:"Versioned baseline (not fitted)",
    not_identifiable:"Cannot be scored from retained outcomes"
  };
  const ADVISOR_GROUP_LABELS = {
    entry:"Entry filters",
    pacing:"Pacing and repetition",
    exit:"Exit controls",
    adaptive:"Adaptive overlay"
  };

  function advisorFieldValue(record, value) {
    if (value == null) return "not set";
    if (record.unit === "fraction") return `${(Number(value) * 100).toFixed(1)}%`;
    if (record.unit === "seconds") return `${Number(value).toFixed(0)}s`;
    if (record.unit === "minutes") return `${Number(value).toFixed(1)} min`;
    if (record.unit === "count" || record.unit === "shares") {
      return String(Number(value));
    }
    if (record.unit === "score") return Number(value).toFixed(0);
    return String(value);
  }

  function advisorFieldCoverage(advice) {
    const records = Array.isArray(advice.field_recommendations)
      ? advice.field_recommendations
      : [];
    if (!records.length) {
      return '<div class="metrics-empty">No field coverage was returned.</div>';
    }
    const groups = new Map();
    for (const record of records) {
      const key = record.group || "other";
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(record);
    }
    return [...groups.entries()].map(([group, items]) => `
      <section class="us-advisor-field-group">
        <h4>${esc(ADVISOR_GROUP_LABELS[group] || group)}</h4>
        ${items.map(record => {
          const support = record.support || "none";
          const later = record.later_event_performance;
          const metrics = [
            `${Number(record.trades || 0)} trades`,
            `${Number(record.independent_events || 0)} events`,
            support === "none" ? "no local support" : `${esc(support)} support`
          ];
          if (later && later.turnover_roi != null) {
            metrics.push(`later-event ROI ${pct(later.turnover_roi)}`);
          }
          if (later && later.maximum_drawdown_usd != null) {
            metrics.push(
              `drawdown $${Number(later.maximum_drawdown_usd).toFixed(2)}`
            );
          }
          return `<article class="us-advisor-field is-${esc(record.basis)}">
            <div class="us-advisor-field-head">
              <strong>${esc(record.label || record.field)}</strong>
              <span>${esc(advisorFieldValue(record, record.current))} → ${
                esc(advisorFieldValue(record, record.suggested))
              }</span>
            </div>
            <span class="us-advisor-field-basis">${
              esc(ADVISOR_BASIS_LABELS[record.basis] || record.basis)
            }${
              record.applyable
                ? " · written by Apply"
                : record.in_apply_set
                  ? " · Apply locked · set manually"
                  : " · set manually"
            }</span>
            <small>${esc(metrics.join(" · "))}</small>
            <small>${esc(record.rationale || "")}</small>
            ${record.measurement_note
              ? `<small class="us-advisor-field-caveat">${
                  esc(record.measurement_note)
                }</small>`
              : ""}
          </article>`;
        }).join("")}
      </section>`).join("");
  }

  function renderPolicyAdvice(advice) {
    const body = document.querySelector("#us-policy-advisor-result");
    const status = document.querySelector("#us-policy-advisor-status");
    if (!body || !status) return;
    lastPolicyAdvice = advice;
    body.classList.remove("is-stale");
    const evidence = advice.evidence || {};
    const dataSources = Array.isArray(evidence.data_sources)
      ? evidence.data_sources
      : [];
    const model = advice.model_evidence || {};
    const bootstrap = evidence.event_block_bootstrap || {};
    const changes = Object.entries(advice.changes || {});
    const blockers = Array.isArray(model.live_blockers) ? model.live_blockers : [];
    const currentRate = Number(evidence.current_estimated_qualified_per_hour || 0);
    const suggestedRate = Number(evidence.suggested_estimated_qualified_per_hour || 0);
    const diagnostics = advice.diagnostics || {};
    const frontier = Array.isArray(advice.candidate_frontier)
      ? advice.candidate_frontier
      : [];
    const validationBlockers = advisorValidationBlockers(advice);
    const scopedLine = adviceScopedLineType(advice);
    const actionLabel = !changes.length
      ? "No filter changes available"
      : advice.apply_allowed
        ? "Apply validated filters"
        : "Preview exploratory filters";
    const actionNote = advice.apply_allowed
      ? "Validated application saves only the displayed execution filters and disarms live orders."
      : "This result is not validated for one-click saving. Preview loads it into the form only; review it and use Save execution policy if you deliberately accept the risk.";
    const download = document.querySelector("#us-policy-advisor-download");
    if (download) download.disabled = false;
    status.className = "refresh-status";
    status.textContent = `${String(advice.status || "research").replaceAll("_", " ")} · ${evidence.analysis_mode || "current"} mode · ${Number(evidence.eligible_closed_trades || 0)} eligible closes across ${Number(evidence.independent_events || 0)} events · target ${activeTradingLane === "live" ? "live" : "dry-run"} lane · model stage ${String(model.stage || "unavailable").replaceAll("_", " ")}`;
    body.innerHTML = `
      <div class="us-policy-advisor-summary">
        <div><span>Current qualified pace</span><strong>${currentRate.toFixed(2)}/hr</strong></div>
        <div><span>Suggested qualified pace</span><strong>${suggestedRate.toFixed(2)}/hr</strong></div>
        <div><span>Suggested test ROI</span><strong>${pct(evidence.suggested_test?.turnover_roi)}</strong></div>
        <div><span>Whole-event confidence</span><strong>${bootstrap.probability_positive == null ? "not available" : `${(Number(bootstrap.probability_positive) * 100).toFixed(0)}% positive`}</strong></div>
      </div>
      <div class="us-policy-advisor-sources">
        ${dataSources.length ? dataSources.map(source => `<article>
          <span>${esc(String(source.lane || "store").replaceAll("_", " "))}</span>
          <strong>${Number(source.closed_trades || 0)} closed / ${Number(source.independent_events || 0)} events</strong>
          <small>${Number(source.retained_positions || 0)} retained positions / ${Number(source.opportunity_observations || 0)} logged opportunities</small>
        </article>`).join("") : '<div class="metrics-empty">No retained execution source inventory was returned.</div>'}
      </div>
      <div class="us-policy-advisor-changes">
        ${changes.length ? changes.map(([field, values]) => `<article class="us-policy-advisor-change">
          <span>${esc(field.replaceAll("_", " "))}</span>
          <strong>${esc(advisorSettingValue(field, values.current))} → ${esc(advisorSettingValue(field, values.suggested))}</strong>
        </article>`).join("") : '<div class="metrics-empty">The best supported comparison does not change the current execution filters.</div>'}
      </div>
      <div class="us-policy-advisor-evidence">
        Chronological evidence: ${Number(evidence.train_events || 0)} training events / ${Number(evidence.test_events || 0)} test events.
        Event-block ROI interval: ${bootstrap.lower_95 == null ? "not available" : `${(Number(bootstrap.lower_95) * 100).toFixed(1)}% to ${(Number(bootstrap.upper_95) * 100).toFixed(1)}%`}.
        Fitted-model decision coverage: ${Number(model.decision_score_coverage?.scored || 0)}/${Number(model.decision_score_coverage?.requested || 0)}.
        ${blockers.length ? `Live-model blockers: ${esc(blockers.join(" · "))}.` : ""}
        ${esc(advice.validation_note || "")}
        Policy snapshot: ${esc(String(advice.source_policy_hash || "").slice(0, 12) || "unavailable")}.
        ${esc(evidence.legacy_mode_warning || "")}
        ${esc(evidence.execution_domain_warning || "")}
        ${esc(advice.guarantee || "")}
      </div>
      <details class="advanced us-advisor-analysis">
        <summary>Every policy field and its evidence</summary>
        <div class="field-note">${esc(advice.field_coverage_note || "")}</div>
        ${advisorFieldCoverage(advice)}
      </details>
      <details class="advanced us-advisor-analysis" open>
        <summary>Trade-data diagnostics</summary>
        <div class="us-advisor-diagnostic-grid">
          ${advisorDiagnosticTable("Execution modes", diagnostics.execution_modes)}
          ${advisorDiagnosticTable("Line types", diagnostics.line_types)}
          ${advisorDiagnosticTable("Edge bands", diagnostics.edge_bands)}
          ${advisorDiagnosticTable("Signal-quality bands", diagnostics.quality_bands)}
          ${advisorDiagnosticTable("Entry-price bands", diagnostics.price_bands)}
          ${advisorDiagnosticTable("Repeat entries", diagnostics.entry_repetition)}
          ${advisorDiagnosticTable("MLB game stage", diagnostics.game_stage)}
          ${advisorDiagnosticTable("Line × MLB game stage", diagnostics.line_by_game_stage)}
          ${advisorDiagnosticTable("Exit reason", diagnostics.exit_reasons)}
        </div>
        <div class="us-policy-advisor-evidence">
          MLB stage metadata: ${Number(diagnostics.game_stage_coverage?.known || 0)}/${Number(diagnostics.game_stage_coverage?.total || 0)} complete.
          ${esc(evidence.selection_bias_warning || "")}
        ${esc(evidence.multiple_testing_warning || "")}
        ${validationBlockers.length ? `Validation blockers: ${esc(validationBlockers.join(" Â· "))}.` : ""}
      </div>
      </details>
      <details class="advanced us-advisor-analysis">
        <summary>Top candidate frontier</summary>
        <div class="us-advisor-frontier">
          ${frontier.length ? frontier.map((candidate, index) => `<article>
            <strong>#${index + 1} · ${Number(candidate.opportunity_rate_per_hour || 0).toFixed(2)} qualified/hr</strong>
            <span>Train ${pct(candidate.train?.turnover_roi)} · later events ${pct(candidate.test?.turnover_roi)}</span>
            <small>${esc((candidate.settings?.allowed_market_types || []).join(", "))} · edge ${(Number(candidate.settings?.min_edge || 0) * 100).toFixed(1)}–${(Number(candidate.settings?.max_edge || 0) * 100).toFixed(1)}% · quality ${Number(candidate.settings?.min_signal_quality || 0).toFixed(0)} · ${Number(candidate.settings?.max_entries_per_event_per_hour || 0)} entries/event/hr</small>
          </article>`).join("") : '<div class="metrics-empty">Not enough complete history to construct a candidate frontier.</div>'}
        </div>
      </details>
      <div class="us-policy-advisor-actions">
        <button class="primary compact-button" id="us-policy-advisor-apply" type="button" ${changes.length ? "" : "disabled"}>${esc(actionLabel)}</button>
        ${scopedLine ? `<button class="ghost compact-button" id="us-policy-advisor-apply-profile" type="button" ${changes.length ? "" : "disabled"}>Load entry filters into ${esc(scopedLine)} profile</button>` : ""}
        ${scopedLine ? `<button class="ghost compact-button" id="us-policy-advisor-apply-exits" type="button">Load exit thresholds into ${esc(scopedLine)} profile</button>` : ""}
        <small>${esc(actionNote)}</small>
        ${scopedLine
          ? `<small>This analysis covered ${esc(scopedLine)} only. Loading it into the ${esc(scopedLine)} profile keeps the suggestion scoped to the line it was measured on, instead of changing the global settings every line inherits.</small>`
          : `<small>Select a single line type before analyzing to load a suggestion into that line's profile instead of the global settings.</small>`}
      </div>`;
    document.querySelector("#us-policy-advisor-apply")?.addEventListener(
      "click",
      advice.apply_allowed ? applyPolicyAdvice : previewPolicyAdvice
    );
    document.querySelector("#us-policy-advisor-apply-profile")?.addEventListener(
      "click",
      previewAdviceIntoLineProfile
    );
    document.querySelector("#us-policy-advisor-apply-exits")?.addEventListener(
      "click",
      loadExitThresholdsIntoLineProfile
    );
  }

  async function loadPolicyAdvisorSessions() {
    const body = document.querySelector("#us-policy-advisor-sessions");
    if (!body) return;
    try {
      const response = await fetch(
        tradingApi(
          "/api/polymarket-us/trading/policy-advisor/sessions?limit=6"
        ),
        {cache:"no-store"}
      );
      const sessions = await response.json().catch(()=>([]));
      if (!response.ok) throw new Error(detailMessage(sessions));
      body.innerHTML = Array.isArray(sessions) && sessions.length
        ? sessions.map(session => `<article class="us-policy-session">
            <strong>${new Date(session.started_at).toLocaleString()} · ${esc(session.mode)}</strong>
            ${Number(session.trades || 0)} trades / ${Number(session.events || 0)} events / ${esc(money(Number(session.realized_net_usd || 0)))} realized
            <br>${esc(String(session.reason || "").replaceAll("_", " "))}
          </article>`).join("")
        : "";
    } catch (error) {
      body.innerHTML = `<div class="metrics-empty">${esc(error.message || "Could not load settings sessions")}</div>`;
    }
  }

  async function analyzePolicyAdvice({successPrefix = ""} = {}) {
    const button = document.querySelector("#us-policy-advisor-refresh");
    const status = document.querySelector("#us-policy-advisor-status");
    const objective = document.querySelector("#us-policy-advisor-objective")?.value || "balanced";
    const target = Number(document.querySelector("#us-policy-advisor-target")?.value || 4);
    const analysisMode = document.querySelector("#us-policy-advisor-mode")?.value || "combined";
    const lookbackDays = Number(
      document.querySelector("#us-policy-advisor-lookback")?.value || 0
    );
    const marketTypes = [
      ...document.querySelectorAll("[data-advisor-market-type]:checked")
    ].map(input => input.dataset.advisorMarketType);
    if (!marketTypes.length) {
      status.className = "refresh-status is-error";
      status.textContent = "Select at least one line type to analyze.";
      return null;
    }
    setActionBusy(button, true, "Analyzing...");
    status.className = "refresh-status";
    status.textContent = "Comparing chronological trade outcomes, logged opportunities, and fitted-model shadow readiness...";
    try {
      const response = await fetchWithDeadline(
        tradingApi(
          "/api/polymarket-us/trading/policy-advisor/recommend"
        ),
        {
          method:"POST",
          headers:{"content-type":"application/json"},
          body:JSON.stringify({
            objective,
            target_trades_per_hour:target,
            analysis_mode:analysisMode,
            lookback_days:lookbackDays,
            market_types:marketTypes
          })
        },
        60000,
        "The settings analysis exceeded 60 seconds. Try a shorter lookback or fewer line types."
      );
      const advice = await response.json().catch(()=>({}));
      if (!response.ok) throw new Error(detailMessage(advice));
      renderPolicyAdvice(advice);
      if (successPrefix) {
        status.textContent = `${successPrefix} ${status.textContent}`;
      }
      await loadPolicyAdvisorSessions();
      return advice;
    } catch (error) {
      status.className = "refresh-status is-error";
      status.textContent = error.message || "Could not generate settings advice";
      return null;
    } finally {
      setActionBusy(button, false);
      updatePolicyAdvisorActionLabel();
    }
  }

  function updatePolicyAdvisorActionLabel() {
    const button = document.querySelector("#us-policy-advisor-refresh");
    const mode = document.querySelector("#us-policy-advisor-mode")?.value;
    if (!button || button.getAttribute("aria-busy")) return;
    button.textContent = mode === "combined"
      ? "Analyze all retained data"
      : `Analyze ${mode === "dry_run" ? "dry-run" : "live"} data`;
  }

  // Advisor fields that a line/stage profile can actually carry. The rest of
  // the tunable set is lane-wide by design and has no profile equivalent, so
  // it is reported rather than silently dropped into a per-line edit.
  const PROFILE_APPLICABLE_ADVISOR_FIELDS = [
    "min_edge", "max_edge", "min_signal_quality", "min_reference_sources",
    "min_entry_price", "max_entry_price", "max_entries_per_event_per_hour",
    "min_mlb_fraction_remaining"
  ];
  const LANE_WIDE_ADVISOR_FIELDS = {
    allowed_market_types:"line-type authorization is a global control",
    max_orders_per_hour:"hourly entry cap is lane-wide (a profile has its own separate cap)",
    candidate_cooldown_seconds:"candidate cooldown is lane-wide"
  };

  // Exit thresholds a line profile can carry. The adaptive overlay's own
  // controls are lane-wide and deliberately absent here.
  const PROFILE_EXIT_FIELDS = [
    "profit_target", "stop_loss", "trailing_drawdown",
    "minimum_locked_profit", "exit_edge", "min_hold_minutes"
  ];

  function adviceFieldRecord(advice, field) {
    return (advice?.field_recommendations || []).find(
      item => item.field === field
    ) || null;
  }

  function loadExitThresholdsIntoLineProfile() {
    const advice = lastPolicyAdvice;
    const line = adviceScopedLineType(advice);
    const status = document.querySelector("#us-policy-advisor-status");
    if (!advice || !line) return;

    const marketSelect = document.querySelector("#us-profile-market");
    const stageSelect = document.querySelector("#us-profile-stage");
    if (!marketSelect || !stageSelect) return;
    marketSelect.value = line;
    loadSelectedLineProfile();
    const stage = stageSelect.value || "all";

    const measured = [];
    const baseline = [];
    for (const input of lineProfileInputs()) {
      const field = input.dataset.profileField;
      if (!PROFILE_EXIT_FIELDS.includes(field)) continue;
      const record = adviceFieldRecord(advice, field);
      if (!record || record.suggested == null) continue;
      input.value = input.hasAttribute("data-profile-percent")
        ? Number(record.suggested) * 100
        : Number(record.suggested);
      // An excursion-scored value and a versioned baseline are not the same
      // kind of number; the operator is told which is which.
      (record.basis === "observational" ? measured : baseline).push(field);
    }
    if (!measured.length && !baseline.length) return;
    const enabled = document.querySelector("#us-profile-enabled");
    if (enabled) enabled.checked = true;
    if (!saveSelectedLineProfile()) return;
    setUSTradingFormDirty(true);
    showPolicyStep("authorization");
    if (status) {
      const pretty = list => list.map(f => f.replaceAll("_", " ")).join(", ");
      status.className = "refresh-status";
      status.textContent =
        `Loaded exit thresholds into the ${line} / ${stage} profile. `
        + (measured.length
          ? `Scored from retained excursion evidence: ${pretty(measured)}. `
          : "")
        + (baseline.length
          ? `Versioned baseline, not fitted - changing these cannot be scored `
            + `from outcomes their own rule produced: ${pretty(baseline)}. `
          : "")
        + "Review and save the execution policy above.";
    }
  }

  function applyAdaptiveRecommendation() {
    const rec = lastUSTradingStatus?.execution_state
      ?.predictive_exit?.recommendation;
    const status = document.querySelector("#us-policy-advisor-status");
    if (!rec) return;
    const profile = document.querySelector("#us-adaptive-exit-profile");
    const enabled = document.querySelector("#us-adaptive-exit-enabled");
    if (!profile || !enabled) return;
    // Observe-only still needs the overlay running: that is how it collects
    // and scores the forecasts that would later justify a stronger response.
    enabled.checked = true;
    profile.value = rec.profile;
    updateAdaptiveExitAvailability();
    setUSTradingFormDirty(true);
    showPolicyStep("behavior");
    if (status) {
      status.className = "refresh-status";
      status.textContent =
        `Adaptive response set to "${rec.profile}" for this lane. `
        + `${rec.rationale} These controls are lane-wide; a line profile `
        + "cannot carry them. Review and save the execution policy above.";
    }
  }

  function adviceScopedLineType(advice) {
    const scoped = advice?.scope?.market_types;
    return Array.isArray(scoped) && scoped.length === 1 ? scoped[0] : null;
  }

  function previewAdviceIntoLineProfile() {
    const advice = lastPolicyAdvice;
    const suggested = advice?.suggested_policy;
    const line = adviceScopedLineType(advice);
    const status = document.querySelector("#us-policy-advisor-status");
    if (!suggested || !line) return;

    const marketSelect = document.querySelector("#us-profile-market");
    const stageSelect = document.querySelector("#us-profile-stage");
    if (!marketSelect || !stageSelect) return;
    marketSelect.value = line;
    // Load whatever that profile already holds so untouched overrides survive.
    loadSelectedLineProfile();
    const stage = stageSelect.value || "all";

    const applied = [];
    for (const input of lineProfileInputs()) {
      const field = input.dataset.profileField;
      if (!PROFILE_APPLICABLE_ADVISOR_FIELDS.includes(field)) continue;
      const value = suggested[field];
      if (value == null) continue;
      input.value = input.hasAttribute("data-profile-percent")
        ? Number(value) * 100
        : Number(value);
      applied.push(field);
    }
    // Authorize the profile, otherwise the values would be saved but inert.
    const enabled = document.querySelector("#us-profile-enabled");
    if (enabled) enabled.checked = true;
    if (!saveSelectedLineProfile()) return;

    const skipped = Object.entries(LANE_WIDE_ADVISOR_FIELDS)
      .filter(([field]) => field in suggested)
      .map(([, reason]) => reason);
    setUSTradingFormDirty(true);
    showPolicyStep("authorization");
    if (status) {
      status.className = "refresh-status";
      status.textContent =
        `Loaded ${applied.length} suggested value${applied.length === 1 ? "" : "s"} `
        + `into the ${line} / ${stage} profile and authorized it. `
        + `Review and save the execution policy above to apply them.`
        + (skipped.length
          ? ` Not copied, because these stay lane-wide: ${skipped.join("; ")}.`
          : "");
    }
  }

  function previewPolicyAdvice() {
    if (!lastPolicyAdvice?.suggested_policy) return;
    const advice = lastPolicyAdvice;
    const previewPolicy = {
      ...(lastUSTradingStatus?.policy || {}),
      ...advice.suggested_policy,
      risk_preset:"custom"
    };
    usTradingHydrationEpoch += 1;
    applyTradingPolicy(
      {
        ...(lastUSTradingStatus || {}),
        policy:previewPolicy,
        risk_presets:lastRiskPresets
      },
      {force:true}
    );
    setUSTradingFormDirty(true);
    const button = document.querySelector("#us-policy-advisor-apply");
    const status = document.querySelector("#us-policy-advisor-status");
    const body = document.querySelector("#us-policy-advisor-result");
    lastPolicyAdvice = null;
    body?.classList.add("is-stale");
    if (button) {
      button.disabled = true;
      button.removeAttribute("aria-busy");
      button.textContent = "Preview loaded Â· review and save above";
    }
    if (status) {
      status.className = "refresh-status";
      status.textContent = (
        "Exploratory filters were loaded into the execution form but were not saved. " +
        "Review the highlighted unsaved controls, then use Save execution policy only if you accept the validation blockers."
      );
    }
    document.querySelector("#us-policy-save")?.focus();
  }

  async function applyPolicyAdvice() {
    if (!lastPolicyAdvice?.id) return;
    const adviceId = lastPolicyAdvice.id;
    if (!window.confirm(
      "Apply these suggested execution filters? Live orders will be disarmed for review."
    )) return;
    const confirmation = APPROVAL_TOKEN;
    const button = document.querySelector("#us-policy-advisor-apply");
    const status = document.querySelector("#us-policy-advisor-status");
    let applied = false;
    let invalidated = false;
    setActionBusy(button, true, "Applying...");
    status.textContent = "Saving the recommended execution filters and disarming live orders...";
    try {
      const response = await fetchWithDeadline(
        tradingApi(
          `/api/polymarket-us/trading/policy-advisor/${encodeURIComponent(adviceId)}/apply`
        ),
        {
          method:"POST",
          headers:{"content-type":"application/json"},
          body:JSON.stringify({confirmation})
        },
        20000,
        "Applying the recommendation exceeded 20 seconds. The current policy will be reloaded before another attempt."
      );
      const result = await response.json().catch(()=>({}));
      if (!response.ok) throw new Error(detailMessage(result));
      usTradingHydrationEpoch += 1;
      usTradingFormDirty = false;
      status.className = "refresh-status";
      status.textContent = result.summary || "Suggested filters applied; live orders are disarmed.";
      lastPolicyAdvice = null;
      applied = true;
      setActionBusy(button, false);
      if (button) {
        button.disabled = true;
        button.textContent = "Applied Â· refreshing analysis...";
      }
      refreshTradingInBackground();
      void loadPolicyAdvisorSessions();
      const refreshed = await analyzePolicyAdvice({
        successPrefix: "Previous suggestion applied successfully. A fresh comparison is shown."
      });
      if (!refreshed && button?.isConnected) {
        button.disabled = true;
        button.removeAttribute("aria-busy");
        button.textContent = "Applied Â· analyze again";
      }
    } catch (error) {
      const message = error.message || "Could not apply settings advice";
      if (
        message.includes("already applied")
        || message.includes("changed after this recommendation")
        || message.includes("analyze the current settings")
        || message.includes("exceeded 20 seconds")
      ) {
        setActionBusy(button, false);
        invalidatePolicyAdvice(message);
        invalidated = true;
        refreshTradingInBackground();
      } else {
        status.className = "refresh-status is-error";
        status.textContent = message;
      }
    } finally {
      if (!applied && !invalidated) setActionBusy(button, false);
    }
  }

  document.querySelector("#us-policy-advisor-refresh")?.addEventListener(
    "click",
    analyzePolicyAdvice
  );
  document.querySelector("#us-policy-advisor-objective")?.addEventListener(
    "change",
    () => invalidatePolicyAdvice(
      "The advisor goal changed. Analyze current data again before applying."
    )
  );
  document.querySelector("#us-policy-advisor-target")?.addEventListener(
    "input",
    () => invalidatePolicyAdvice(
      "The desired trade rate changed. Analyze current data again before applying."
    )
  );
  for (const selector of [
    "#us-policy-advisor-mode",
    "#us-policy-advisor-lookback",
    "[data-advisor-market-type]"
  ]) {
    document.querySelectorAll(selector).forEach(input => input.addEventListener(
      "change",
      () => {
        updatePolicyAdvisorActionLabel();
        invalidatePolicyAdvice(
          "The analysis scope changed. Analyze current data again before applying."
        );
      }
    ));
  }
  updatePolicyAdvisorActionLabel();
  document.querySelector("#us-policy-advisor-download")?.addEventListener(
    "click",
    () => {
      if (!lastPolicyAdvice) return;
      const blob = new Blob(
        [JSON.stringify(lastPolicyAdvice, null, 2)],
        {type:"application/json"}
      );
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = `execution-policy-analysis-${Date.now()}.json`;
      document.body.append(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(url);
    }
  );

  // --- Execution journal paging -------------------------------------------
  // The journal is polled continuously. Returning every row of a 10,000-row
  // table made it the largest response in the dashboard, and the high-volume
  // qualification records buried the entry/exit rows that matter.
  const JOURNAL_PAGE_SIZE = 25;
  let journalOffset = 0;
  let journalKinds = [];
  let lastJournalPage = null;

  function renderJournalControls(page) {
    const kindsBox = document.querySelector("#us-journal-kinds");
    const range = document.querySelector("#us-journal-range");
    const prev = document.querySelector("#us-journal-prev");
    const next = document.querySelector("#us-journal-next");
    if (!kindsBox || !range || !prev || !next) return;
    const counts = page?.kind_counts || {};
    const active = new Set(page?.kinds || []);
    const chips = [
      `<button type="button" data-journal-kind="" class="${
        active.size ? "" : "is-current"
      }">All · ${Number(page?.total || 0)}</button>`
    ].concat(
      Object.entries(counts)
        .sort((a, b) => b[1] - a[1])
        .map(([kind, count]) => `<button type="button" data-journal-kind="${
          esc(kind)
        }" class="${active.has(kind) ? "is-current" : ""}">${esc(kind)} · ${
          Number(count)
        }</button>`)
    );
    kindsBox.innerHTML = chips.join("");
    const shown = (page?.items || []).length;
    const from = shown ? Number(page.offset || 0) + 1 : 0;
    const filtered = Number(page?.filtered_total || 0);
    range.textContent = shown
      ? `${from}–${from + shown - 1} of ${filtered}`
      : `0 of ${filtered}`;
    prev.disabled = Number(page?.offset || 0) <= 0;
    next.disabled = Number(page?.offset || 0) + shown >= filtered;
  }

  document.querySelector("#us-journal-kinds")?.addEventListener("click", event => {
    const target = event.target.closest("[data-journal-kind]");
    if (!target) return;
    const kind = target.dataset.journalKind;
    journalKinds = kind ? [kind] : [];
    journalOffset = 0;
    loadUSTrading();
  });
  document.querySelector("#us-journal-prev")?.addEventListener("click", () => {
    journalOffset = Math.max(0, journalOffset - JOURNAL_PAGE_SIZE);
    loadUSTrading();
  });
  document.querySelector("#us-journal-next")?.addEventListener("click", () => {
    journalOffset += JOURNAL_PAGE_SIZE;
    loadUSTrading();
  });

  function renderTradingJournalPage(page) {
    lastJournalPage = page;
    renderJournalControls(page);
    renderTradingJournal(Array.isArray(page?.items) ? page.items : []);
  }

  function renderTradingJournal(items) {
    const body = document.querySelector("#us-trading-journal");
    if (!body) return;
    if (!items.length) {
      body.innerHTML = '<div class="metrics-empty">No automation decisions yet.</div>';
      return;
    }
    body.innerHTML = items.map(item => {
      const details = item.details || {};
      const reasons = Array.isArray(details.reasons) && details.reasons.length
        ? details.reasons.join(" · ")
        : details.reason || "";
      const qualityMetric = details.signal_quality == null
        ? ""
        : `quality ${Math.round(details.signal_quality)}/${Math.round(details.configured_min_quality ?? 0)} min`;
      const referenceMetric = details.reference_sources == null
        ? ""
        : `refs ${details.reference_sources}/${details.configured_min_reference_sources ?? "—"} min`;
      const bookMetric = details.authenticated_book_state
        ? `book ${details.authenticated_book_state.replace("MARKET_STATE_", "")}`
        : "";
      const depthMetric = details.executable_book_shares == null
        ? ""
        : `depth ${Number(details.executable_book_shares).toFixed(2)}/${Number(details.configured_min_book_shares ?? 0).toFixed(2)} shares`;
      const selectedGateMetric = Array.isArray(details.selected_engine_gate_results)
        && details.selected_engine_gate_results.length
        ? `selected gates ${details.selected_engine_gate_results.map(gate => `${gate.code}:${gate.status || (gate.passed ? "pass" : "fail")}`).join(", ")}`
        : "";
      const stopGuard = details.stop_guard || {};
      const stopGuardMetric = stopGuard.status
        ? `stop guard ${stopGuard.status}${
          stopGuard.confirmations == null
            ? ""
            : ` ${Number(stopGuard.confirmations)}/${Number(stopGuard.required_confirmations || 0)}`
        }${stopGuard.reason ? `: ${stopGuard.reason}` : ""}`
        : "";
      const profitGuard = details.profit_guard || {};
      const profitGuardMetric = profitGuard.status
        ? [
            `profit protection ${String(
              profitGuard.status
            ).replaceAll("_", " ")}`,
            profitGuard.protected_floor_value == null
              ? ""
              : `floor ${cents(profitGuard.protected_floor_value)}`,
            profitGuard.fee_adjusted_exit_value == null
              ? ""
              : `net executable ${cents(
                  profitGuard.fee_adjusted_exit_value
                )}`,
            profitGuard.reason || ""
          ].filter(Boolean).join(": ")
        : "";
      const metrics = [
        details.signal_edge == null ? "" : `source edge ${signedCents(details.signal_edge)}`,
        details.configured_min_edge == null ? "" : `floor ${signedCents(details.configured_min_edge)}`,
        details.entry_cost == null ? "" : `US buy ${cents(details.entry_cost)}`,
        details.execution_edge == null ? "" : `US edge ${signedCents(details.execution_edge)}`,
        details.required_edge == null ? "" : `need ${signedCents(details.required_edge)}`,
        qualityMetric,
        referenceMetric,
        bookMetric,
        depthMetric,
        selectedGateMetric,
        details.authenticated_spread == null ? "" : `spread ${cents(details.authenticated_spread)}`,
        details.buying_power == null ? "" : `buying power $${Number(details.buying_power).toFixed(2)}`,
        details.available_capacity_usd == null ? "" : `capacity $${Number(details.available_capacity_usd).toFixed(2)}`,
        details.return_fraction == null ? "" : `return ${(details.return_fraction * 100).toFixed(1)}%`,
        details.held_minutes == null ? "" : `held ${Number(details.held_minutes).toFixed(1)}m`,
        details.profit_lock_armed == null
          ? ""
          : details.profit_lock_armed ? "profit lock ARMED" : "profit lock waiting",
        details.profit_target_observation_count == null
          ? ""
          : `target confirmation ${Math.min(
              Number(details.profit_target_confirmation_readings || 2),
              Number(details.profit_target_observation_count || 0)
            )}/${Number(details.profit_target_confirmation_readings || 2)}`,
        details.estimated_cashout_value == null ? "" : `cash-out $${Number(details.estimated_cashout_value).toFixed(2)}`,
        details.gross_exit_value == null ? "" : `gross exit ${cents(details.gross_exit_value)}`,
        details.estimated_exit_fee == null
          ? ""
          : `estimated exit fee $${Number(details.estimated_exit_fee).toFixed(2)}`,
        details.exit_book_depth == null ? "" : `exit depth ${Number(details.exit_book_depth).toFixed(2)} shares`,
        details.quote_source ? `quote ${details.quote_source}` : "",
        details.venue_sync_status ? `venue ${details.venue_sync_status}` : "",
        stopGuardMetric,
        profitGuardMetric
      ].filter(Boolean);
      return `<article class="us-journal-row">
        <div class="us-journal-time">${esc(new Date(item.created_at).toLocaleTimeString())}<span>${esc(item.kind)}</span></div>
        <div><strong>${esc(item.event_name || "System")}</strong><span>${esc([item.selection, item.market_slug].filter(Boolean).join(" · "))}</span></div>
        <div class="us-journal-decision">
          <b class="us-journal-status">${esc(item.status)}</b>
          ${metrics.length ? `<div class="us-journal-metrics">${metrics.map(metric => `<span>${esc(metric)}</span>`).join("")}</div>` : ""}
          ${reasons ? `<em>${esc(reasons)}</em>` : ""}
        </div>
      </article>`;
    }).join("");
  }

  async function loadUSTrading() {
    if (usTradingLoading) {
      usTradingReloadQueued = true;
      return;
    }
    usTradingLoading = true;
    const requestEpoch = usTradingHydrationEpoch;
    try {
      const [
        statusResponse,
        positionsResponse,
        journalResponse,
        performanceResponse
      ] = await Promise.all([
        fetch(tradingApi("/api/polymarket-us/trading/status"), {cache:"no-store"}),
        fetch(tradingApi("/api/polymarket-us/trading/positions"), {cache:"no-store"}),
        fetch(tradingApi(
          `/api/polymarket-us/trading/journal?limit=${JOURNAL_PAGE_SIZE}`
          + `&offset=${journalOffset}`
          + (journalKinds.length
            ? `&kinds=${encodeURIComponent(journalKinds.join(","))}`
            : "")
        ), {cache:"no-store"}),
        fetch(tradingApi("/api/polymarket-us/trading/performance"), {cache:"no-store"})
      ]);
      const [status, positions, journal, performance] = await Promise.all([
        statusResponse.json().catch(()=>({})),
        positionsResponse.json().catch(()=>([])),
        journalResponse.json().catch(()=>([])),
        performanceResponse.json().catch(()=>({}))
      ]);
      if (statusResponse.status === 404) {
        throw new Error(
          "RESTART REQUIRED: this page loaded the new workstation UI from disk, " +
          "but the running Python server is an older build without the trading API. " +
          "Close every PelosiTracker command window, start exactly one server, then reload."
        );
      }
      if (!statusResponse.ok) throw new Error(detailMessage(status));
      if (!positionsResponse.ok) throw new Error(detailMessage(positions));
      if (!journalResponse.ok) throw new Error(detailMessage(journal));
      if (!performanceResponse.ok) throw new Error(detailMessage(performance));
      // An operator action or form edit supersedes every response that began
      // before it. Rendering stale status here was the reason automation could
      // appear to switch itself back on after a reset.
      if (requestEpoch !== usTradingHydrationEpoch) return;
      applyTradingPolicy(status, {requestEpoch});
      renderTradingStatus(status);
      renderVenuePositions(status);
      renderTradingPerformance(performance);
      renderManagedPositions(Array.isArray(positions) ? positions : []);
      renderTradingJournalPage(journal);
    } catch (error) {
      const box = document.querySelector("#us-trading-status");
      if (box) box.textContent = error.message || "Could not load automation controls";
    } finally {
      usTradingLoading = false;
      if (usTradingReloadQueued) {
        usTradingReloadQueued = false;
        queueMicrotask(loadUSTrading);
      }
    }
  }

  document.querySelector("#us-trading-form")?.addEventListener("submit", async event => {
    event.preventDefault();
    const form = event.currentTarget;
    clearPolicySaveNotice();
    const invalid = form.querySelector(":invalid");
    if (invalid) {
      showPolicySaveNotice(
        invalid.validationMessage || "Review the highlighted setting.",
        {target:invalid, scroll:true}
      );
      return;
    }
    const button = document.querySelector("#us-policy-save");
    const statusBox = document.querySelector("#us-trading-status");
    setActionBusy(button, true, "Saving policy…");
    statusBox.textContent = "Validating and saving the execution policy…";
    const selectedProfile = selectedLineProfileKey();
    if (
      lineProfileInputs().some(input => input.value !== "")
      || lineExecutionProfiles.some(item => (
        item.market_type === selectedProfile.market_type
        && item.game_stage === selectedProfile.game_stage
      ))
    ) {
      saveSelectedLineProfile();
    }
    const globalEntryEnabled = document.querySelector(
      "#us-global-entry-enabled"
    ).checked;
    const allowedMarketTypes = entryMarketTypeInputs()
      .filter(input => input.checked)
      .map(input => input.dataset.entryMarketType);
    if (globalEntryEnabled && !allowedMarketTypes.length) {
      setActionBusy(button, false);
      statusBox.textContent = (
        "Select a global line type or turn off global fallback authorization."
      );
      showPolicySaveNotice(
        "Global fallback is authorized, so select at least one global line type.",
        {
          target:document.querySelector("#us-line-type-policy"),
          scroll:true
        }
      );
      return;
    }
    const allowedMarketScopes = entryMarketScopeInputs()
      .filter(input => input.checked)
      .map(input => input.dataset.entryMarketScope);
    if (!allowedMarketScopes.length) {
      setActionBusy(button, false);
      statusBox.textContent = "Select at least one game segment for automatic entry.";
      showPolicySaveNotice(
        "Select at least one game segment for automatic entry.",
        {
          target:document.querySelector("#us-market-scope-policy"),
          scroll:true
        }
      );
      return;
    }
    const payload = {
      execution_mode: document.querySelector("#us-trading-mode").value,
      automation_enabled: document.querySelector("#us-automation-enabled").checked,
      auto_cashout: document.querySelector("#us-auto-cashout").checked,
      adaptive_exit_enabled: document.querySelector("#us-adaptive-exit-enabled").checked,
      adaptive_exit_profile: document.querySelector("#us-adaptive-exit-profile").value,
      adaptive_exit_horizon_minutes: Number(
        document.querySelector("#us-adaptive-exit-horizon").value
      ),
      adaptive_exit_min_samples: Number(
        document.querySelector("#us-adaptive-exit-min-samples").value
      ),
      adaptive_exit_max_tightening: Number(
        document.querySelector("#us-adaptive-exit-max-tightening").value
      ) / 100,
      volatility_stop_enabled: document.querySelector(
        "#us-volatility-stop-enabled"
      ).checked,
      stateless_stop_confirmation: document.querySelector(
        "#us-stateless-stop-confirmation"
      ).checked,
      stop_confirmation_readings: Number(
        document.querySelector("#us-stop-confirmation-readings").value
      ),
      stop_grace_minutes: Number(
        document.querySelector("#us-stop-grace-minutes").value
      ),
      catastrophic_stop_multiplier: Number(
        document.querySelector("#us-catastrophic-stop-multiplier").value
      ),
      post_exit_tracking_minutes: Number(
        document.querySelector("#us-post-exit-tracking-minutes").value
      ),
      require_engine_entry: document.querySelector("#us-require-engine").checked,
      trading_allocation_usd: Number(
        document.querySelector("#us-trading-allocation").value
      ),
      risk_preset: document.querySelector("#us-risk-preset").value,
      required_engine_gates: engineGateInputs()
        .filter(input => input.checked)
        .map(input => input.dataset.engineGate),
      global_entry_enabled: globalEntryEnabled,
      allowed_market_types: allowedMarketTypes,
      allowed_market_scopes: allowedMarketScopes,
      allow_live_segment_markets: document.querySelector(
        "#us-allow-live-segments"
      ).checked,
      line_execution_profiles:lineExecutionProfiles,
      max_total_exposure_usd: Number(document.querySelector("#us-max-exposure").value),
      minimum_cash_reserve_usd: Number(document.querySelector("#us-cash-reserve").value),
      max_position_usd: Number(document.querySelector("#us-max-position").value),
      max_event_exposure_usd: Number(document.querySelector("#us-max-event").value),
      max_daily_loss_usd: Number(document.querySelector("#us-daily-loss").value),
      min_edge: Number(document.querySelector("#us-min-edge").value) / 100,
      max_edge: Number(document.querySelector("#us-max-edge").value) / 100,
      fee_edge_margin: Number(
        document.querySelector("#us-fee-edge-margin").value
      ),
      min_signal_quality: Number(document.querySelector("#us-min-quality").value),
      max_signal_quality: Number(document.querySelector("#us-max-quality").value),
      min_source_agreement: Number(
        document.querySelector("#us-min-source-agreement").value
      ),
      max_signal_age_seconds: Number(
        document.querySelector("#us-max-signal-age").value
      ),
      entry_confirmation_readings: Number(
        document.querySelector("#us-entry-confirmation-readings").value
      ),
      max_confirmation_price_drift: Number(
        document.querySelector("#us-max-confirmation-price-drift").value
      ) / 100,
      min_entry_price: Number(document.querySelector("#us-min-price").value) / 100,
      max_entry_price: Number(document.querySelector("#us-max-price").value) / 100,
      min_hold_minutes: Number(document.querySelector("#us-min-hold").value),
      profit_target: Number(document.querySelector("#us-profit-target").value) / 100,
      minimum_locked_profit: Number(
        document.querySelector("#us-min-locked-profit").value
      ) / 100,
      max_open_positions: Number(document.querySelector("#us-max-open").value),
      max_orders_per_hour: Number(document.querySelector("#us-max-orders-hour").value),
      max_entries_per_event_per_hour: Number(
        document.querySelector("#us-max-event-entries-hour").value
      ),
      candidate_cooldown_seconds: Number(
        document.querySelector("#us-candidate-cooldown").value
      ),
      min_mlb_fraction_remaining: Number(
        document.querySelector("#us-min-mlb-remaining").value
      ) / 100,
      min_reference_sources: Number(document.querySelector("#us-min-refs").value),
      max_spread: Number(document.querySelector("#us-max-spread").value) / 100,
      min_book_shares: Number(document.querySelector("#us-min-depth").value),
      trailing_drawdown: Number(document.querySelector("#us-trailing-drawdown").value) / 100,
      stop_loss: Number(document.querySelector("#us-stop-loss").value) / 100,
      exit_edge: Number(document.querySelector("#us-exit-edge").value) / 100,
      reversal_confirmation_readings: Number(
        document.querySelector("#us-reversal-confirmation-readings").value
      ),
      cycle_seconds: Number(document.querySelector("#us-cycle-seconds").value)
    };
    const saveEpoch = ++usTradingHydrationEpoch;
    try {
      const response = await fetch(
        tradingApi("/api/polymarket-us/trading/config"),
        {
        method: "PUT",
        headers: {"content-type":"application/json"},
        body: JSON.stringify(payload)
        }
      );
      const body = await response.json().catch(()=>({}));
      if (!response.ok) {
        const policyError = new Error(detailMessage(body));
        policyError.policyDetail = body.detail;
        throw policyError;
      }
      if (saveEpoch === usTradingHydrationEpoch) {
        setUSTradingFormDirty(false);
        applyTradingPolicy(body, {force:true});
      }
      renderTradingStatus(body);
      const laneLabel = activeTradingLane === "live" ? "Live" : "Dry-run";
      const running = body?.policy?.automation_enabled ? "running" : "stopped";
      showPolicySaveNotice(
        `${laneLabel} automation is ${running}. The other lane was not changed.`,
        {kind:"success"}
      );
      await Promise.all([refreshUSStatus(), loadUSTrading()]);
    } catch (error) {
      statusBox.textContent = error.message || "Could not save execution policy";
      showPolicySaveNotice(
        error.message || "Could not save execution policy",
        {
          target:policyErrorTarget(error.message || "", error.policyDetail),
          scroll:true
        }
      );
    } finally {
      setActionBusy(button, false);
      setUSTradingFormDirty(usTradingFormDirty);
      renderTradingLanes();
    }
  });

  document.querySelector("#us-adaptive-exit-clear")?.addEventListener(
    "click",
    async event => {
      if (!window.confirm(
        "Clear only the retained adaptive MLB movement history? Positions, trade history, journal entries, and core model data are preserved."
      )) return;
      const confirmation = APPROVAL_TOKEN;
      const button = event.currentTarget;
      const status = document.querySelector("#us-adaptive-exit-status");
      setActionBusy(button, true, "Clearing learningâ€¦");
      status.textContent = "Clearing retained adaptive movement observationsâ€¦";
      try {
        const response = await fetch(
          tradingApi("/api/polymarket-us/trading/adaptive-exit/history"),
          {
            method: "DELETE",
            headers: {"content-type":"application/json"},
            body: JSON.stringify({confirmation})
          }
        );
        const body = await response.json().catch(()=>({}));
        if (!response.ok) throw new Error(detailMessage(body));
        status.textContent = `Cleared ${Number(body.deleted_observations || 0)} observations. Positions and execution history were preserved.`;
        await loadUSTrading();
      } catch (error) {
        status.textContent = error.message || "Could not clear adaptive learning history";
      } finally {
        setActionBusy(button, false);
      }
    }
  );

  async function tradingAction(path, pending, options = {}) {
    const statusBox = document.querySelector("#us-trading-status");
    statusBox.textContent = pending;
    const response = await fetch(
      tradingApi(path),
      {method:"POST", ...options}
    );
    const body = await response.json().catch(()=>({}));
    if (!response.ok) throw new Error(detailMessage(body));
    refreshTradingInBackground();
    return body;
  }

  document.querySelector("#us-run-now")?.addEventListener("click", async event => {
    const button = event.currentTarget;
    setActionBusy(button, true, "Running cycle…");
    try {
      await tradingAction("/api/polymarket-us/trading/run", "Reviewing all monitored events and exact US lines…");
    } catch (error) {
      document.querySelector("#us-trading-status").textContent = error.message;
    } finally {
      setActionBusy(button, false);
    }
  });
  document.querySelector("#us-disarm")?.addEventListener("click", async event => {
    const button = event.currentTarget;
    setActionBusy(button, true, "Disarming…");
    try {
      await tradingAction("/api/polymarket-us/trading/disarm", "Closing the live-order latch…");
    } catch (error) {
      document.querySelector("#us-trading-status").textContent = error.message;
    } finally {
      setActionBusy(button, false);
    }
  });
  function updateArmDurationLabel() {
    const duration = document.querySelector("#us-arm-duration");
    const button = document.querySelector("#us-arm");
    if (!duration || !button || button.disabled) return;
    button.textContent = `Arm for ${duration.selectedOptions[0]?.textContent || "30 minutes"}`;
  }

  document.querySelector("#us-arm-duration")?.addEventListener(
    "change",
    updateArmDurationLabel
  );
  updateArmDurationLabel();

  document.querySelector("#us-arm")?.addEventListener("click", async event => {
    const button = event.currentTarget;
    const duration = document.querySelector("#us-arm-duration");
    const approval = document.querySelector("#us-arm-confirmation");
    const durationLabel = duration?.selectedOptions[0]?.textContent || "30 minutes";
    if (!approval?.checked) {
      document.querySelector("#us-trading-status").textContent = (
        "Check the approval box before arming live orders."
      );
      approval?.focus();
      return;
    }
    setActionBusy(button, true, "Arming…");
    try {
      await tradingAction(
        "/api/polymarket-us/trading/arm",
        `Requesting a ${durationLabel} live-order latch…`,
        {
          headers: {"content-type":"application/json"},
          body: JSON.stringify({
            confirmation: APPROVAL_TOKEN,
            seconds: Number(duration?.value || 1800)
          })
        }
      );
      approval.checked = false;
    } catch (error) {
      document.querySelector("#us-trading-status").textContent = error.message;
    } finally {
      setActionBusy(button, false);
      updateArmDurationLabel();
    }
  });
  document.querySelector("#us-emergency-stop")?.addEventListener("click", async event => {
    if (!window.confirm("Stop automation, disarm live execution, and request cancellation of this service's managed open orders?")) return;
    const button = event.currentTarget;
    const stopStatus = document.querySelector("#us-stop-status");
    setActionBusy(button, true, "Stopping…");
    stopStatus.className = "us-liquidate-status is-working";
    stopStatus.textContent = "Stopping automation, disarming live execution, and checking managed open orders…";
    const progressTimer = delayedProgress(
      stopStatus,
      2000,
      "The stop request is still processing. This usually means the local database is committing the stop or Polymarket US is answering a managed-order cancellation request."
    );
    try {
      const body = await tradingAction(
        "/api/polymarket-us/trading/emergency-stop",
        "Stopping automation and checking managed open orders…"
      );
      usTradingHydrationEpoch += 1;
      setUSTradingFormDirty(false);
      applyTradingPolicy(body, {force:true});
      renderTradingStatus(body);
      const canceled = Array.isArray(body.cancel_requested)
        ? body.cancel_requested.length
        : 0;
      const failures = Array.isArray(body.cancel_failures)
        ? body.cancel_failures.length
        : 0;
      stopStatus.className = failures
        ? "us-liquidate-status is-error"
        : "us-liquidate-status is-success";
      stopStatus.textContent = failures
        ? `Automation is OFF. ${canceled} managed order cancellation request${canceled === 1 ? "" : "s"} sent; ${failures} venue response${failures === 1 ? "" : "s"} failed. Review the journal.`
        : `Stopped. Automation is OFF, live trading is disarmed, and ${canceled} managed order cancellation request${canceled === 1 ? "" : "s"} ${canceled === 1 ? "was" : "were"} sent.`;
    } catch (error) {
      stopStatus.className = "us-liquidate-status is-error";
      stopStatus.textContent = `Stop could not be confirmed: ${error.message || "unknown error"}. Check the local service and manage live positions from Polymarket if necessary.`;
    } finally {
      window.clearTimeout(progressTimer);
      setActionBusy(button, false);
    }
  });
  document.querySelector("#us-risk-session-reset")?.addEventListener(
    "click",
    async event => {
      const button = event.currentTarget;
      const status = document.querySelector("#us-risk-session-status");
      if (lastUSTradingStatus?.armed) {
        status.className = "us-liquidate-status is-error";
        status.textContent = "Disarm live trading first. Starting a fresh risk window while live orders are authorized is not allowed.";
        return;
      }
      if (!window.confirm(
        "Start a fresh hourly-entry and rolling realized-loss window? This can permit new entries again, but it does not erase P/L or bypass position stops, exposure, cash, venue, liquidity, edge, quality, or engine safeguards."
      )) return;
      const confirmation = APPROVAL_TOKEN;
      setActionBusy(button, true, "Starting session…");
      status.className = "us-liquidate-status is-working";
      status.textContent = "Recording a new auditable risk-session boundary and clearing the candidate retry cooldown…";
      const progressTimer = delayedProgress(
        status,
        1500,
        "The local trade database is committing the new risk boundary. Existing trades and journal evidence are being preserved."
      );
      try {
        const response = await fetch(
          tradingApi("/api/polymarket-us/trading/risk-session/reset"),
          {
            method: "POST",
            headers: {"content-type":"application/json"},
            body: JSON.stringify({confirmation})
          }
        );
        const body = await response.json().catch(()=>({}));
        if (!response.ok) throw new Error(detailMessage(body));
        status.className = "us-liquidate-status is-success";
        status.textContent = body.summary || "New risk session started.";
        if (body.current) {
          renderRiskSessionStatus({
            policy: lastUSTradingStatus?.policy || {},
            risk_session: body.current
          });
        }
        await loadUSTrading();
      } catch (error) {
        status.className = "us-liquidate-status is-error";
        status.textContent = error.message || "Could not start a new risk session";
      } finally {
        window.clearTimeout(progressTimer);
        setActionBusy(button, false);
      }
    }
  );
  for (const selector of ["#us-performance-refresh", "#us-trading-refresh"]) {
    document.querySelector(selector)?.addEventListener("click", async event => {
      const button = event.currentTarget;
      setActionBusy(button, true, "Refreshing…");
      try {
        await loadUSTrading();
      } finally {
        setActionBusy(button, false);
      }
    });
  }

  document.querySelector("#us-dry-tally-reset")?.addEventListener(
    "click",
    async event => {
      const button = event.currentTarget;
      const status = document.querySelector("#us-performance-action-status");
      const openDry = Number(
        lastTradingPerformance?.modes?.dry_run?.open_positions || 0
      );
      if (openDry) {
        status.className = "us-liquidate-status is-error";
        status.textContent = `${openDry} dry-run position${openDry === 1 ? "" : "s"} still open. Let them close (or exit them) so the session boundary stays unambiguous.`;
        return;
      }
      if (!window.confirm(
        "Start a fresh dry-run W-L-P and net display at zero for this session? Every historical position, the execution journal, and the performance datasheet remain preserved."
      )) return;

      setActionBusy(button, true, "Starting session…");
      status.className = "us-liquidate-status is-working";
      status.textContent = "Recording a new dry-run session boundary; no trade record is being deleted…";
      try {
        const response = await fetch(
          "/api/polymarket-us/trading/performance/reset-dry-run",
          {
            method: "POST",
            headers: {"content-type":"application/json"},
            body: JSON.stringify({confirmation:APPROVAL_TOKEN})
          }
        );
        const body = await response.json().catch(()=>({}));
        if (!response.ok) throw new Error(detailMessage(body));
        status.className = "us-liquidate-status is-success";
        status.textContent = body.summary || "Dry-run session tally started.";
        refreshTradingInBackground();
      } catch (error) {
        status.className = "us-liquidate-status is-error";
        status.textContent = error.message || "Could not start a new dry-run session tally";
      } finally {
        setActionBusy(button, false);
      }
    }
  );

  document.querySelector("#us-live-tally-reset")?.addEventListener(
    "click",
    async event => {
      const button = event.currentTarget;
      const status = document.querySelector("#us-performance-action-status");
      const openLive = Number(
        lastTradingPerformance?.modes?.live?.open_positions || 0
      );
      if (openLive) {
        status.className = "us-liquidate-status is-error";
        status.textContent = `${openLive} live position${openLive === 1 ? "" : "s"} still open. Sell or synchronize those positions before resetting the live tally.`;
        return;
      }
      if (lastUSTradingStatus?.armed) {
        status.className = "us-liquidate-status is-error";
        status.textContent = "Disarm live trading first. Resetting a tally while new entries are authorized would create an ambiguous session boundary.";
        return;
      }
      if (!window.confirm(
        "Start a fresh live W-L-P and net display at zero? Historical positions, the execution journal, and daily-loss safeguards will remain preserved."
      )) return;

      setActionBusy(button, true, "Resetting tally…");
      status.className = "us-liquidate-status is-working";
      status.textContent = "Creating a new local live-performance baseline; no order is being sent and no trade record is being deleted…";
      const progressTimer = delayedProgress(
        status,
        1500,
        "The local trade database is recording and verifying the new tally boundary. Positions, audit history, and risk controls remain untouched."
      );
      try {
        const response = await fetch(
          tradingApi(
            "/api/polymarket-us/trading/performance/reset-live",
            "live"
          ),
          {
            method: "POST",
            headers: {"content-type":"application/json"},
            body: JSON.stringify({confirmation:APPROVAL_TOKEN})
          }
        );
        const body = await response.json().catch(()=>({}));
        if (!response.ok) throw new Error(detailMessage(body));
        status.className = "us-liquidate-status is-success";
        status.textContent = body.summary || "Live performance tally reset.";
        refreshTradingInBackground();
      } catch (error) {
        status.className = "us-liquidate-status is-error";
        status.textContent = error.message || "Could not reset the live tally";
      } finally {
        window.clearTimeout(progressTimer);
        setActionBusy(button, false);
      }
    }
  );

  document.querySelector("#us-venue-sync")?.addEventListener("click", async event => {
    const button = event.currentTarget;
    const syncStatus = document.querySelector("#us-venue-sync-status");
    setActionBusy(button, true, "Synchronizing…");
    if (syncStatus) {
      syncStatus.classList.remove("is-error");
      syncStatus.textContent = "Reading authenticated Polymarket US account positions…";
    }
    try {
      const response = await fetch("/api/polymarket-us/trading/sync", {
        method: "POST"
      });
      const body = await response.json().catch(()=>({}));
      if (!response.ok) throw new Error(detailMessage(body));
      if (syncStatus) {
        syncStatus.textContent = body.summary || "Account sync completed.";
      }
      await loadUSTrading();
    } catch (error) {
      if (syncStatus) {
        syncStatus.classList.add("is-error");
        syncStatus.textContent = error.message || "Account sync failed";
      }
    } finally {
      setActionBusy(button, false);
    }
  });

  document.querySelector(".us-position-filter")?.addEventListener("click", event => {
    const button = event.target.closest("[data-position-mode]");
    if (!button) return;
    usPositionMode = button.dataset.positionMode || "all";
    document.querySelectorAll("[data-position-mode]").forEach(item => {
      item.classList.toggle("is-active", item === button);
    });
    renderManagedPositions();
  });

  document.querySelector("#us-clear-exited-positions")?.addEventListener(
    "click",
    async event => {
      const exited = lastManagedPositions.filter(
        position => position.status !== "open"
      );
      if (!window.confirm(
        `Clear ${exited.length} exited position card${exited.length === 1 ? "" : "s"} from the managed-position view? Performance tallies, model observations, and the execution journal will be preserved.`
      )) return;

      const button = event.currentTarget;
      const status = document.querySelector("#us-position-action-status");
      const previousPositions = lastManagedPositions;
      setActionBusy(button, true, "Clearing…");
      status.className = "us-liquidate-status is-working";
      status.textContent = "Archiving exited position cards from this view…";
      usTradingHydrationEpoch += 1;
      lastManagedPositions = lastManagedPositions.filter(
        position => position.status === "open"
      );
      renderManagedPositions();
      try {
        const response = await fetch(
          tradingApi(
            "/api/polymarket-us/trading/positions/archive-exited"
          ),
          {method:"POST"}
        );
        const body = await response.json().catch(()=>({}));
        if (!response.ok) throw new Error(detailMessage(body));
        status.className = "us-liquidate-status is-success";
        status.textContent = body.summary || "Exited position cards cleared from view.";
      } catch (error) {
        lastManagedPositions = previousPositions;
        renderManagedPositions();
        status.className = "us-liquidate-status is-error";
        status.textContent = error.message || "Could not clear exited positions";
      } finally {
        setActionBusy(button, false);
        usTradingHydrationEpoch += 1;
        refreshTradingInBackground();
      }
    }
  );

  document.querySelector("#us-managed-positions")?.addEventListener(
    "click",
    async event => {
      const button = event.target.closest("[data-exit-position]");
      if (!button) return;
      const positionId = button.dataset.exitPosition;
      const position = lastManagedPositions.find(item => item.id === positionId);
      if (!position || position.status !== "open") return;
      const live = position.mode === "live";
      const confirmationMessage = live
        ? `Sell ${position.selection} in ${position.event_name} now? This sends a previewed fill-or-kill sell using the current authenticated US book.`
        : `Remove the simulated ${position.selection} position from ${position.event_name}? This is immediate, needs no quote, and does not stop automation or erase other dry-run trades.`;
      if (!window.confirm(confirmationMessage)) return;

      const status = document.querySelector("#us-position-action-status");
      const previousPositions = lastManagedPositions;
      setActionBusy(
        button,
        true,
        live ? "Selling…" : "Removing…"
      );
      status.className = "us-liquidate-status is-working";
      status.textContent = live
        ? `Loading the current US book and attempting to sell ${position.selection}…`
        : `Removing simulated position ${position.selection} now…`;
      const progressTimer = delayedProgress(
        status,
        live ? 2500 : 1200,
        live
          ? `Waiting for an authenticated US quote and sell response for ${position.selection}; the position remains visible on your Polymarket account until the venue confirms it.`
          : `Removal request sent for ${position.selection}. Waiting for the local trade database to commit and verify the deletion.`
      );
      usTradingHydrationEpoch += 1;
      if (!live) {
        lastManagedPositions = lastManagedPositions.filter(
          item => item.id !== positionId
        );
        renderManagedPositions();
      }
      try {
        const response = await fetch(
          `/api/polymarket-us/trading/positions/${encodeURIComponent(positionId)}/exit`,
          {
            method: "POST",
            headers: {"content-type":"application/json"},
            body: JSON.stringify({
              confirmation: live ? APPROVAL_TOKEN : ""
            })
          }
        );
        const body = await response.json().catch(()=>({}));
        if (!response.ok) throw new Error(detailMessage(body));
        status.className = body.status === "blocked"
          ? "us-liquidate-status is-error"
          : "us-liquidate-status is-success";
        status.textContent = body.summary || (
          live ? "Live sell attempt completed." : "Simulation removed."
        );
      } catch (error) {
        lastManagedPositions = previousPositions;
        renderManagedPositions();
        status.className = "us-liquidate-status is-error";
        status.textContent = error.message || "Could not close the position";
      } finally {
        window.clearTimeout(progressTimer);
        setActionBusy(button, false);
        usTradingHydrationEpoch += 1;
        refreshTradingInBackground();
      }
    }
  );

  function updateLiquidationMode() {
    const form = document.querySelector("#us-liquidate-form");
    if (!form) return;
    const mode = form.querySelector('input[name="liquidate-mode"]:checked')?.value || "dry_run";
    const confirmationRow = document.querySelector("#us-liquidate-confirm-row");
    const confirmation = document.querySelector("#us-liquidate-confirmation");
    const button = document.querySelector("#us-liquidate-submit");
    if (confirmationRow) confirmationRow.hidden = mode !== "live";
    if (mode !== "live" && confirmation) confirmation.checked = false;
    if (button) {
      button.textContent = mode === "live"
        ? "Sell all open live positions"
        : "Stop automation + wipe dry-run trades now";
    }
  }

  document.querySelector("#us-liquidate-form")?.addEventListener("change", event => {
    if (event.target.matches('input[name="liquidate-mode"]')) updateLiquidationMode();
  });

  document.querySelector("#us-liquidate-form")?.addEventListener("submit", async event => {
    event.preventDefault();
    const form = event.currentTarget;
    const mode = form.querySelector('input[name="liquidate-mode"]:checked')?.value || "dry_run";
    const live = mode === "live";
    const button = document.querySelector("#us-liquidate-submit");
    const status = document.querySelector("#us-liquidate-status");
    const openCount = lastManagedPositions.filter(
      position => position.status === "open" && position.mode === mode
    ).length;
    const dryTotal = Number(
      lastTradingPerformance?.modes?.dry_run?.total_positions
      ?? lastManagedPositions.filter(position => position.mode === "dry_run").length
    );
    const liveApproval = document.querySelector("#us-liquidate-confirmation");
    if (live && !liveApproval?.checked) {
      status.className = "us-liquidate-status is-error";
      status.textContent = "Check the approval box before selling live positions.";
      liveApproval?.focus();
      return;
    }
    const confirmationMessage = live
      ? `Attempt to sell all ${openCount} open live position${openCount === 1 ? "" : "s"}? Each order will be previewed and submitted as fill-or-kill.`
      : `EXECUTIVE DRY-RUN RESET: switch automatic analysis OFF and permanently remove all ${dryTotal} dry-run trade record${dryTotal === 1 ? "" : "s"}? No market mapping, quote, or fill is required. Live trades and the execution journal are preserved.`;
    if (!window.confirm(confirmationMessage)) return;

    const automationInput = document.querySelector("#us-automation-enabled");
    const runNowButton = document.querySelector("#us-run-now");
    const previousAutomation = automationInput?.checked;
    const previousPositions = lastManagedPositions;
    const previousStatus = lastUSTradingStatus;
    setActionBusy(
      button,
      true,
      live ? "Selling live positions…" : "Stopping & clearing now…"
    );
    status.className = "us-liquidate-status is-working";
    status.textContent = live
      ? `Loading current US quotes and attempting ${openCount} live sale${openCount === 1 ? "" : "s"}...`
      : "Sending one atomic request to stop automation and wipe the simulated ledger…";
    let progressTimer = delayedProgress(
      status,
      1500,
      live
        ? "Still loading authenticated books and submitting fill-or-kill sell attempts. Polymarket US response time controls this step."
        : "The reset request is processing. It does not need a market quote; a delay here means the local trade database is committing and verifying the wipe."
    );
    usTradingHydrationEpoch += 1;
    if (!live) {
      if (automationInput) {
        automationInput.checked = false;
        automationInput.disabled = true;
      }
      if (runNowButton) runNowButton.disabled = true;
      lastManagedPositions = lastManagedPositions.filter(
        position => position.mode !== "dry_run"
      );
      renderManagedPositions();
      if (lastUSTradingStatus) {
        renderTradingStatus({
          ...lastUSTradingStatus,
          policy: {
            ...(lastUSTradingStatus.policy || {}),
            automation_enabled: false
          },
          last_cycle_summary: (
            "Stop accepted locally; clearing the simulated ledger now."
          )
        });
      }
    }
    try {
      const response = await fetch(
        live
          ? tradingApi("/api/polymarket-us/trading/liquidate", "live")
          : tradingApi(
              "/api/polymarket-us/trading/history/dry-run",
              "dry_run"
            ),
        {
          method: live ? "POST" : "DELETE",
          headers: {"content-type":"application/json"},
          body: JSON.stringify(live
            ? {
                mode,
                confirmation: APPROVAL_TOKEN
              }
            : {confirmation:APPROVAL_TOKEN})
        });
      const body = await response.json().catch(()=>({}));
      if (!response.ok) throw new Error(detailMessage(body));
      status.className = `us-liquidate-status ${live && body.failed ? "is-working" : "is-success"}`;
      status.textContent = body.summary || (
        live
          ? "Position sale attempts completed."
          : "Automation stopped and all dry-run positions and history were wiped."
      );
      if (live) document.querySelector("#us-liquidate-confirmation").checked = false;
      if (!live) {
        const stoppedStatus = {
          ...(lastUSTradingStatus || {}),
          armed: false,
          armed_until: null,
          protective_exits_armed: false,
          policy: {
            ...(lastUSTradingStatus?.policy || {}),
            automation_enabled: false
          },
          last_cycle_summary: body.summary
        };
        setUSTradingFormDirty(false);
        applyTradingPolicy(stoppedStatus, {force:true});
        renderTradingStatus(stoppedStatus);
      }
      usTradingHydrationEpoch += 1;
      refreshTradingInBackground();
    } catch (error) {
      if (!live) {
        lastManagedPositions = previousPositions;
        renderManagedPositions();
        if (automationInput) automationInput.checked = !!previousAutomation;
        if (previousStatus) renderTradingStatus(previousStatus);
      }
      status.className = "us-liquidate-status is-error";
      status.textContent = error.message || (
        live
          ? "Could not attempt position sales"
          : "Could not complete the atomic dry-run reset"
      );
    } finally {
      window.clearTimeout(progressTimer);
      if (automationInput) automationInput.disabled = false;
      if (runNowButton) runNowButton.disabled = false;
      setActionBusy(button, false);
      updateLiquidationMode();
    }
  });
  updateLiquidationMode();

  let modelLabLoading = false;
  let modelLabFitInProgress = false;
  let modelLabFitStartedAt = 0;
  let modelLabFitProgressTimer = null;

  function renderModelLab(data) {
    const thresholds = data.thresholds || {};
    const profiles = Array.isArray(data.profiles) ? data.profiles : [];
    const segments = Array.isArray(data.segments) ? data.segments : [];
    const recent = Array.isArray(data.recent_momentum) ? data.recent_momentum : [];
    const profileBox = document.querySelector("#model-lab-profiles");
    const mlbBox = document.querySelector("#mlb-model-blueprint");
    const segmentBox = document.querySelector("#model-lab-segments");
    const momentumBox = document.querySelector("#model-lab-momentum");
    const targetBox = document.querySelector("#model-lab-targets");
    const archiveBox = document.querySelector("#model-lab-archive-status");
    const targetDefinitions = data.target_definitions || {};
    const targetCounts = Array.isArray(data.target_counts) ? data.target_counts : [];
    const archive = data.archive || {};
    if (targetBox) {
      targetBox.innerHTML = Object.entries(targetDefinitions).map(([name, definition]) => {
        const count = targetCounts.find(row => row.target_name === name);
        const unavailable = definition.version === "not-yet-linked";
        return `<article class="model-lab-target">
          <strong>${esc(name.replaceAll("_", " "))}</strong>
          <span>${unavailable ? "NOT YET LINKED" : `${Number(count?.target_count || 0)} labels across ${Number(count?.event_count || 0)} events`}</span>
          <small>${esc(definition.meaning || "")}</small>
        </article>`;
      }).join("");
    }
    if (archiveBox) {
      const latestArchive = archive.latest_export;
      archiveBox.textContent = latestArchive
        ? `Latest immutable snapshot: ${new Date(latestArchive.created_at).toLocaleString()} · ${Number(latestArchive.observation_count || 0)} observations · manifest ${String(latestArchive.manifest_hash || "").slice(0, 12)}`
        : archive.available
          ? `No immutable research snapshot yet. Archive directory: ${archive.directory || "local workstation data"}`
          : "Immutable filesystem exports are unavailable for this database.";
    }
    if (profileBox) {
      profileBox.innerHTML = profiles.map(profile => {
        const missing = (profile.missing_features || []).join(", ");
        return `<article class="model-lab-profile ${profile.supported ? "is-ready" : "is-blocked"}">
          <div><strong>${esc(profile.label)}</strong><b>${profile.supported ? "CAPTURE READY" : "STATE INCOMPLETE"}</b></div>
          <span>${esc(profile.note)}</span>
          <small>Inputs: ${esc((profile.available_features || []).join(", ") || "none")}${missing ? ` · missing ${esc(missing)}` : ""}</small>
        </article>`;
      }).join("");
    }
    if (mlbBox) {
      const blueprint = data.mlb_research_blueprint || {};
      const steps = Array.isArray(blueprint.priority_order)
        ? blueprint.priority_order
        : [];
      const references = Array.isArray(data.research_references)
        ? data.research_references
        : [];
      mlbBox.innerHTML = `<h3>MLB predictive hierarchy</h3>
        <p>${esc(blueprint.objective || "Building a calibrated live-state model.")}</p>
        <div class="mlb-blueprint-grid">${steps.map(step => `<article class="mlb-blueprint-step">
          <strong>${Number(step.rank || 0)}. ${esc(String(step.group || "").replaceAll("_", " "))}</strong>
          <span>${esc((step.parameters || []).join(" · "))}</span>
          <small>${esc(step.availability || "")}</small>
        </article>`).join("")}</div>
        <div class="mlb-blueprint-references">Research basis: ${references.map(reference =>
          `<a href="${esc(reference.url)}" target="_blank" rel="noopener">${esc(reference.title)}</a>`
        ).join(" · ")}</div>`;
    }
    if (segmentBox) {
      segmentBox.innerHTML = segments.length
        ? segments.map(segment => `<article class="model-lab-segment">
            <div><strong>${esc([segment.sport, segment.league].filter(Boolean).join(" / "))}</strong><b>${segment.research_fit_ready ? "FIT READY" : segment.fit_supported ? "COLLECTING" : "INPUT BLOCKED"}</b></div>
            <span>${Number(segment.observations || 0)} observations across ${Number(segment.observed_events || 0)} monitored events · ${Number(segment.state_observations || 0)} with live state across ${Number(segment.state_events || 0)} events</span>
            <span>${Number(segment.settled_observations || 0)} labeled across ${Number(segment.settled_events || 0)} settled events · ${Number(segment.fit_observations || 0)} fit-ready rows across ${Number(segment.fit_events || 0)} events</span>
            ${String(segment.sport || "").toLowerCase() === "baseball" ? `<span>${Number(segment.rich_state_observations || 0)} official base/out/count rows across ${Number(segment.rich_state_events || 0)} events · average state completeness ${(Number(segment.average_state_completeness || 0) * 100).toFixed(0)}%</span>` : ""}
            <small>${segment.last_observed_at ? `last ${esc(new Date(segment.last_observed_at).toLocaleString())}` : "no timestamp"} · research minimum ${Number(thresholds.research_min_events || 0)} events / ${Number(thresholds.research_min_observations || 0)} observations</small>
          </article>`).join("")
        : '<div class="metrics-empty">No moneyline observations yet. Monitoring live events will populate this automatically.</div>';
    }
    if (momentumBox) {
      momentumBox.innerHTML = recent.length
        ? recent.map(row => {
            const move = row.market_move == null ? "first sample" : `market ${signedCents(row.market_move)}`;
            const score = row.score_swing == null ? "score baseline" : `score swing ${Number(row.score_swing) >= 0 ? "+" : ""}${Number(row.score_swing).toFixed(1)}`;
            const baseballState = row.baseball_inning == null
              ? ""
              : `${row.baseball_half === "bottom" ? "Bot" : row.baseball_half === "top" ? "Top" : "End"} ${Number(row.baseball_inning)}`;
            const clock = baseballState || (
              row.fraction_remaining == null
                ? "clock unavailable"
                : `${(Number(row.fraction_remaining) * 100).toFixed(0)}% remaining`
            );
            return `<article>
              <div><strong>${esc(row.event_name)}</strong><span>${esc(row.outcome)}</span></div>
              <div><b>${esc(move)}</b><span>${esc(score)} · ${esc(clock)}</span></div>
              <div><b>${signedCents(Number(row.model_probability) - Number(row.market_probability))}</b><span>model-market gap · quality ${Number(row.signal_quality || 0).toFixed(0)}</span></div>
            </article>`;
          }).join("")
        : '<div class="metrics-empty">Waiting for live moneyline observations.</div>';
    }
    const latest = Array.isArray(data.candidates) ? data.candidates[0] : null;
    const result = document.querySelector("#model-lab-fit-result");
    if (result && latest && !modelLabFitInProgress) {
      const details = latest.details || {};
      result.innerHTML = `<strong>${esc(latest.status)}</strong><span>${esc([latest.sport, latest.league, latest.market].filter(Boolean).join(" / "))}</span><small>${esc(details.reason || `Brier improvement ${Number(details.brier_improvement || 0).toFixed(4)}`)} · research-only · never promoted</small>`;
    }
  }

  async function loadModelLab() {
    if (modelLabLoading) return;
    const status = document.querySelector("#model-lab-status");
    if (!status) return;
    modelLabLoading = true;
    try {
      const response = await fetch("/api/model-lab/summary", {cache:"no-store"});
      const data = await response.json().catch(()=>({}));
      if (!response.ok) throw new Error(detailMessage(data));
      renderModelLab(data);
      const total = (data.segments || []).reduce(
        (sum, segment) => sum + Number(segment.observations || 0), 0
      );
      status.className = "refresh-status";
      status.textContent = `${total} bounded observations · 15-second selection buckets · engine impact: none`;
    } catch (error) {
      status.className = "refresh-status is-error";
      status.textContent = error.message || "Could not load the local Model Lab";
    } finally {
      modelLabLoading = false;
    }
  }

  document.querySelector("#research-evidence-export")?.addEventListener("click", async event => {
    const button = event.currentTarget;
    const status = document.querySelector("#research-evidence-status");
    setActionBusy(button, true, "Building archive...");
    if (status) {
      status.className = "refresh-status";
      status.textContent = "Building a compressed, secret-free evidence archive. Large local histories can take a minute...";
    }
    try {
      const response = await fetch("/api/research-data/export", {cache:"no-store"});
      if (!response.ok) {
        const data = await response.json().catch(()=>({}));
        throw new Error(detailMessage(data));
      }
      const blob = await response.blob();
      const disposition = response.headers.get("content-disposition") || "";
      const match = disposition.match(/filename=\"?([^\";]+)\"?/i);
      const filename = match?.[1] || "pelositracker-research.ndjson.gz";
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = filename;
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      URL.revokeObjectURL(url);
      if (status) status.textContent = `Downloaded ${filename} (${(blob.size / 1048576).toFixed(1)} MB). Open the other installation and merge it there; live and dry-run rows keep their source lane.`;
    } catch (error) {
      if (status) {
        status.className = "refresh-status is-error";
        status.textContent = error.message || "Could not export research evidence";
      }
    } finally {
      setActionBusy(button, false);
    }
  });

  document.querySelector("#research-evidence-import")?.addEventListener("click", async event => {
    const button = event.currentTarget;
    const input = document.querySelector("#research-evidence-file");
    const status = document.querySelector("#research-evidence-status");
    const file = input?.files?.[0];
    if (!file) {
      if (status) {
        status.className = "refresh-status is-error";
        status.textContent = "Choose a .ndjson.gz evidence archive first.";
      }
      return;
    }
    if (!window.confirm(`Merge ${file.name} into this site's durable research store? Existing rows are preserved and repeated imports are safe.`)) return;
    setActionBusy(button, true, "Uploading & validating...");
    if (status) {
      status.className = "refresh-status";
      status.textContent = `Uploading ${(file.size / 1048576).toFixed(1)} MB, validating its checksum, then merging by immutable evidence ID...`;
    }
    try {
      const form = new FormData();
      form.append("bundle", file, file.name);
      const response = await fetch("/api/research-data/import", {
        method: "POST",
        body: form
      });
      const data = await response.json().catch(()=>({}));
      if (!response.ok) throw new Error(detailMessage(data));
      if (input) input.value = "";
      if (status) {
        status.textContent = `${Number(data.rows || 0).toLocaleString()} evidence rows validated and processed idempotently · checksum ${String(data.sha256 || "").slice(0, 12)} · existing hosted rows preserved.`;
      }
      await loadModelLab();
      await loadUSTrading();
    } catch (error) {
      if (status) {
        status.className = "refresh-status is-error";
        status.textContent = error.message || "Could not merge research evidence";
      }
    } finally {
      setActionBusy(button, false);
    }
  });

  document.querySelector("#model-lab-refresh")?.addEventListener("click", async event => {
    const button = event.currentTarget;
    setActionBusy(button, true, "Refreshing...");
    await loadModelLab();
    setActionBusy(button, false);
  });

  document.querySelector("#model-lab-export")?.addEventListener("click", async event => {
    const button = event.currentTarget;
    const status = document.querySelector("#model-lab-status");
    setActionBusy(button, true, "Archiving...");
    if (status) {
      status.className = "refresh-status";
      status.textContent = "Writing a content-hashed local snapshot. Trading calculations remain unchanged...";
    }
    try {
      const response = await fetch("/api/model-lab/export", {method: "POST"});
      const data = await response.json().catch(()=>({}));
      if (!response.ok) throw new Error(detailMessage(data));
      if (status) {
        status.textContent = `Snapshot archived: ${Number(data.counts?.observations || 0)} observations · ${Number(data.counts?.targets || 0)} explicit labels · manifest ${String(data.manifest_hash || "").slice(0, 12)}`;
      }
      await loadModelLab();
    } catch (error) {
      if (status) {
        status.className = "refresh-status is-error";
        status.textContent = error.message || "Could not archive the research snapshot";
      }
    } finally {
      setActionBusy(button, false);
    }
  });

  document.querySelector("#model-lab-fit-form")?.addEventListener("submit", async event => {
    event.preventDefault();
    const button = document.querySelector("#model-lab-fit-submit");
    const result = document.querySelector("#model-lab-fit-result");
    if (modelLabFitInProgress) return;
    modelLabFitInProgress = true;
    modelLabFitStartedAt = Date.now();
    setActionBusy(button, true, "Fitting offline...");
    const renderFitProgress = () => {
      const elapsed = Math.max(
        0,
        Math.floor((Date.now() - modelLabFitStartedAt) / 1000)
      );
      result.innerHTML = `<strong>Research fit running</strong><span>${elapsed}s elapsed</span><small>Optimizing the chronological event-block candidate and walk-forward checks. The rest of the workstation remains active.</small>`;
    };
    renderFitProgress();
    modelLabFitProgressTimer = window.setInterval(renderFitProgress, 1000);
    try {
      const response = await fetchWithDeadline(
        "/api/model-lab/fit",
        {
          method: "POST",
          headers: {"content-type":"application/json"},
          body: JSON.stringify({
            sport: document.querySelector("#model-lab-fit-sport").value,
            league: document.querySelector("#model-lab-fit-league").value.trim(),
            market: "moneyline"
          })
        },
        180000,
        "The research fit exceeded three minutes. Restart the workstation to cancel the old fit, then try again with the optimized fitter."
      );
      const data = await response.json().catch(()=>({}));
      if (!response.ok) throw new Error(detailMessage(data));
      const details = data.details || {};
      result.innerHTML = `<strong>${esc(data.status)}</strong><span>${esc(details.reason || "Candidate comparison completed.")}</span><small>Artifact ${esc(String(details.artifact_hash || "").slice(0, 12))} · research-only · not installed</small>`;
    } catch (error) {
      result.textContent = error.message || "Could not fit the research candidate";
    } finally {
      modelLabFitInProgress = false;
      window.clearInterval(modelLabFitProgressTimer);
      modelLabFitProgressTimer = null;
      setActionBusy(button, false);
      await loadModelLab();
    }
  });

  function renderCarousel(present){
    const el=document.querySelector("#line-filter");
    const types=LINE_ORDER.filter(t=>present.has(t));
    if(types.length<=1){ el.innerHTML=""; return; }
    const pill=(k,l)=>`<button class="pill${activeLine===k?" active":""}" data-line="${k}">${l}</button>`;
    el.innerHTML=pill("all","All")+types.map(t=>pill(t,LINE_META[t].label)).join("");
  }
  function renderEventNavigator(events) {
    const nav=document.querySelector("#event-navigator");
    const list=document.querySelector("#event-jump-list");
    const count=document.querySelector("#event-navigator-count");
    if(!events.length){
      nav.hidden=true;
      list.innerHTML="";
      activeEventId=null;
      return;
    }
    nav.hidden=false;
    if(activeEventId&&!events.some(view=>view.event.id===activeEventId))activeEventId=null;
    count.textContent=`${events.length} monitored`;
    list.innerHTML=events.map(view=>{
      const event=view.event,state=view.latest_state;
      const live=!!state?.live&&!state?.ended;
      const ended=!!state?.ended;
      const score=state&&state.home_score!=null&&state.away_score!=null
        ? `${state.home_score}-${state.away_score}`:"";
      const stateLabel=ended?"FINAL":live?"LIVE":score?"UPDATED":"WATCHING";
      const active=event.id===activeEventId;
      return `<button class="event-jump${active?" active":""}" type="button" data-jump-event="${esc(event.id)}" title="Jump to ${esc(event.name)}"${active?' aria-current="true"':""}>
        <span class="event-jump-state ${live?"is-live":ended?"is-final":""}">${stateLabel}</span>
        <span class="event-jump-name">${esc(event.name)}</span>
        <span class="event-jump-meta">${score?`${esc(score)} · `:""}${esc(event.league||event.sport||"sport")}</span>
      </button>`;
    }).join("");
  }

  function marketRow(eventId, market, openDetails) {
    const detailKey = keyFor("market", eventId, market.token_id);
    const modelClass = market.edge == null ? "" : market.edge >= 0 ? "positive" : "negative";
    const edgeIsGross = market.edge_basis === "gross";
    const edgeLabel = edgeIsGross ? "Edge (gross)" : "Net edge";
    const edgeHint = (edgeIsGross ? "Consensus − ask · pre-cost, uncalibrated" : "After execution costs")
      + ` · need ${cents(market.required_edge)} · buffer ${signedCents(market.edge_buffer)}`;
    const whyNoEntry = market.why_no_entry ? `<div class="why-no-entry">${esc(market.why_no_entry)}</div>` : "";
    const guide = market.price_ceiling == null
      ? "Add a matching sportsbook event to calculate a validated entry ceiling."
      : market.room_to_ceiling >= 0
        ? `<strong>Entry ceiling ${cents(market.price_ceiling)}</strong> · current ask is ${cents(market.room_to_ceiling)} below the ceiling.`
        : `<strong>Wait for ${cents(market.price_ceiling)} or lower</strong> · current ask is ${cents(-market.room_to_ceiling)} above the ceiling.`;
    const risks = market.risk_flags.length ? market.risk_flags : ["No elevated execution flags detected; continue watching price and news latency."];
    const quality = market.quality_components;
    const qualityReason = quality ? `<li>Signal-quality policy: completeness ${quality.data_completeness.toFixed(0)}, provider freshness ${quality.provider_freshness.toFixed(0)}, identity ${quality.identity_confidence.toFixed(0)}, execution ${quality.execution_quality.toFixed(0)}, source independence ${quality.source_independence.toFixed(0)}, model sample support ${quality.model_sample_support.toFixed(0)}, calibration support ${quality.calibration_support.toFixed(0)}. These are reliability checks, not a win probability.</li>` : "";
    const ages = `Provider age ${market.provider_age_seconds == null ? "unknown" : market.provider_age_seconds.toFixed(0)+"s"} · receipt age ${market.receipt_age_seconds == null ? "unknown" : market.receipt_age_seconds.toFixed(0)+"s"}`;
    const uncertainty = market.uncertainty_low == null ? "unavailable" : `${pct(market.uncertainty_low)}–${pct(market.uncertainty_high)} historical bootstrap interval`;
    const calibration = market.calibrated_consensus_probability == null ? "unavailable" : pct(market.calibrated_consensus_probability);
    const positiveEv = market.probability_net_ev_positive == null ? "unavailable" : pct(market.probability_net_ev_positive);
    const netEv = market.net_expected_value_total == null ? "unavailable" : money(market.net_expected_value_total);
    const independentModel = market.independent_model_probability == null
      ? "unavailable (no approved exact-segment artifact)"
      : `${pct(market.independent_model_probability)} · ${esc(market.independent_model_version||"unknown version")} · calibration ${esc(market.independent_calibration_version||"unknown")} · test n=${Number(market.independent_model_sample_size||0)} across ${Number(market.independent_model_event_count||0)} events`;
    const executionAudit = `<li>Requested-size VWAP ${cents(market.requested_size_vwap)}; fee-adjusted requested cost ${cents(market.requested_effective_cost)}; simulated fee ${market.execution_fee == null ? "—" : "$"+market.execution_fee.toFixed(4)}; historical execution-cost adjustment ${signedCents(market.expected_execution_cost_offset)}; fillable ${market.paper_fillable_size == null ? "—" : market.paper_fillable_size.toFixed(2)+" shares"}.</li>`;
    const lineage = `<li>Engine ${esc(market.engine_version||"unavailable")} · consensus model ${esc(market.model_version||"unavailable")} (selection n=${Number(market.model_sample_size||0)}) · calibration ${esc(market.calibration_version||"unavailable")} (n=${Number(market.calibration_sample_size||0)}) · independent registry ${esc(market.independent_model_registry_version||"unavailable")} · independent model hash ${esc((market.independent_model_hash||"unavailable").slice(0,12))} · independent calibration hash ${esc((market.independent_calibration_hash||"unavailable").slice(0,12))} · execution ${esc(market.execution_policy_version||"unavailable")} · config ${esc((market.configuration_hash||"unavailable").slice(0,12))}.</li>`;
    const gates = (market.gate_results||[]).map(gate => `<li class="${gate.status === "fail" ? "risk" : ""}">Gate ${esc(gate.code)}: ${esc(gate.status)} · ${esc(gate.explanation||"")}${gate.value == null ? "" : ` · value ${Number(gate.value).toFixed(4)}`}${gate.threshold == null ? "" : ` · threshold ${Number(gate.threshold).toFixed(4)}`}</li>`).join("");
    return `<div class="market" data-line="${lineType(market.market,market.outcome)}" data-token-id="${esc(market.token_id)}">
      <div class="market-top"><div class="outcome">${lineBadge(market.market,market.outcome)}${esc(market.outcome)}<small>${esc(market.question)}</small></div><span class="tag ${tagClass(market.entry_action)}">${esc(market.entry_action)}</span></div>
      <div class="figs">
        <div class="fig"><div class="key">Buy now</div><div class="value">${cents(market.buy_price)}</div><div class="hint">Executable ask</div></div>
        <div class="fig"><div class="key">Sell now</div><div class="value">${cents(market.sell_price)}</div><div class="hint">Executable bid</div></div>
        <div class="fig"><div class="key">${edgeLabel}</div><div class="value ${modelClass}">${signedCents(market.edge)}</div><div class="hint">${edgeHint}</div></div>
        <div class="fig"><div class="key">Signal quality</div><div class="value">${market.confidence == null ? "—" : market.confidence.toFixed(0)+"/100"}</div><div class="hint">Data reliability · ${market.reference_sources} source family/families</div></div>
      </div>
      ${whyNoEntry}
      <div class="guide">${guide} ${esc(market.consensus_method||"display-only")} consensus ${pct(market.consensus_probability)} · calibrated consensus ${calibration} · independent model ${independentModel} · uncertainty ${uncertainty} · P(net EV &gt; 0) ${positiveEv} · net EV ${netEv}. Spread ${cents(market.spread)} · ask depth ${market.ask_size == null ? "—" : market.ask_size.toFixed(1)+" shares"} · liquidity ${market.market_liquidity == null ? "—" : "$"+Number(market.market_liquidity).toLocaleString(undefined,{maximumFractionDigits:0})}.</div>
      <details class="why" data-detail-key="${detailKey}"${openDetails.has(detailKey) ? " open" : ""}><summary>What to look out for</summary>
        <ul><li>${ages}</li>${executionAudit}${qualityReason}${lineage}${gates}${market.reasons.map(reason => `<li>${esc(reason)}</li>`).join("")}${risks.map(risk => `<li class="risk">${esc(risk)}</li>`).join("")}</ul></details>
      <details class="why"><summary>Add or update my position</summary>
        <form class="position-form" data-save-position data-event-id="${esc(eventId)}" data-token-id="${esc(market.token_id)}" data-market="${esc(market.market)}" data-outcome="${esc(market.outcome)}">
          <div><label>Shares</label><input name="shares" type="number" min="0.01" max="1000000" step="0.01" required placeholder="25"></div>
          <div><label>Average entry (cents)</label><input name="entry_cents" type="number" min="0.1" max="99.9" step="0.1" required placeholder="52.5"></div>
          <button type="submit">Save position</button>
        </form>
      </details>
    </div>`;
  }

  function positionRow(eventId, position, openDetails) {
    const detailKey = keyFor("position", eventId, position.token_id);
    const pnlClass = position.unrealized_pnl == null ? "" : position.unrealized_pnl >= 0 ? "positive" : "negative";
    return `<div class="position">
      <div class="position-top"><div class="outcome">${esc(position.outcome)}<small>${position.shares.toFixed(2)} shares · average ${cents(position.avg_entry_price)}</small></div><span class="tag ${tagClass(position.advice)}">${esc(position.advice)}</span></div>
      <div class="figs">
        <div class="fig"><div class="key">Cash-out bid</div><div class="value">${cents(position.current_bid)}</div><div class="hint">Before fees/slippage</div></div>
        <div class="fig"><div class="key">Cash value</div><div class="value">${position.cash_value == null ? "—" : "$"+position.cash_value.toFixed(2)}</div><div class="hint">Shares × bid</div></div>
        <div class="fig"><div class="key">Unrealized P/L</div><div class="value ${pnlClass}">${money(position.unrealized_pnl)}</div><div class="hint">${position.roi == null ? "—" : pct(position.roi)} return</div></div>
        <div class="fig"><div class="key">Remaining hold edge</div><div class="value">${signedCents(position.remaining_hold_edge)}</div><div class="hint">Calibrated consensus minus executable bid · lower-bound edge ${signedCents(position.conservative_hold_edge)}</div></div>
      </div>
      <details class="why" data-detail-key="${detailKey}"${openDetails.has(detailKey) ? " open" : ""}><summary>Why this hold/cash status?</summary><ul>${position.reasons.map(reason => `<li>${esc(reason)}</li>`).join("")}</ul></details>
      <button class="position-remove" type="button" data-remove-position data-event-id="${esc(eventId)}" data-token-id="${esc(position.token_id)}">Remove position</button>
    </div>`;
  }

  function fallbackSignal(signal) {
    return `<div class="market" data-line="${lineType(signal.market,signal.outcome)}"><div class="market-top"><div class="outcome">${lineBadge(signal.market,signal.outcome)}${esc(signal.outcome)}<small>${esc(signal.market)} reference signal</small></div><span class="tag ${signal.action === "PAPER_BET" ? "entry" : "wait"}">${esc(signal.action.replace("_"," "))}</span></div>
      <div class="figs"><div class="fig"><div class="key">Consensus prob</div><div class="value">${pct(signal.consensus_probability??signal.model_probability)}</div><div class="hint">One observation per source family</div></div><div class="fig"><div class="key">Display gap</div><div class="value">${signedCents(signal.edge)}</div><div class="hint">Not actionable without calibration and execution gates</div></div><div class="fig"><div class="key">Signal quality</div><div class="value">${signal.confidence.toFixed(0)}/100</div><div class="hint">Reliability, not win probability</div></div></div></div>`;
  }

  function eventCard(view, openDetails) {
    const {event,state_points,quote_points,latest_state:state,actionable_markets:markets,positions,signals} = view;
    const removing = pendingEventRemovals.has(event.id);
    const health = view.edge_health;
    const usingFallback = !markets.length && !event.polymarket_slug;
    const anyReference = markets.some(m => (m.reference_sources||0) >= 1);
    let priceOnly = markets.length && !anyReference
      ? '<div class="notice price-only"><strong>Price only</strong> · no sportsbook reference matched yet, so there\'s no validated edge here.</div>'
      : "";
    if (health) {
      const sources = health.fresh_reference_sources.length ? health.fresh_reference_sources.map(esc).join(", ") : "none";
      priceOnly = `<div class="notice"><strong>Edge pipeline: ${esc(health.status.replaceAll("_"," "))}</strong> · ${esc(health.message)} Fresh references: ${sources}.</div>` + priceOnly;
    }
    let mkts = markets, sigs = signals.slice(0,3);
    if (activeLine !== "all") {
      mkts = markets.filter(m => lineType(m.market,m.outcome) === activeLine);
      sigs = sigs.filter(s => lineType(s.market,s.outcome) === activeLine);
    }
    if (activeLine !== "all" && !mkts.length && !(usingFallback && sigs.length) && !positions.length) return "";
    const score = state ? `${state.home_score}<span class="sep">–</span>${state.away_score}` : "—";
    const portfolio = positions.length ? `<div class="section-strip"><span>My positions</span> · paper hold/cash monitor</div><div class="portfolio">${positions.map(p => positionRow(event.id,p,openDetails)).join("")}</div>` : "";
    const marketBody = mkts.length ? mkts.map(m => marketRow(event.id,m,openDetails)).join("")
      : (usingFallback && sigs.length) ? sigs.map(fallbackSignal).join("")
      : activeLine !== "all" ? '<div class="pending">No matching lines for this filter.</div>'
      : '<div class="pending">Waiting for a fresh executable ask and reference prices…</div>';
    const link = event.polymarket_url ? `<a href="${esc(event.polymarket_url)}" target="_blank" rel="noopener">Open event ↗</a>` : "manual event";
    const restriction = event.polymarket_restricted ? '<strong>Region notice:</strong> Polymarket marks this event restricted. The monitor shows public data only and does not bypass availability rules.' : 'Only selections accepting orders with a visible ask are listed.';
    return `<article class="event${removing ? " is-removing" : ""}" data-event-id="${esc(event.id)}"${removing ? ' aria-busy="true"' : ""}><div class="event-head"><div><div class="name">${esc(event.name)}</div><div class="meta">${esc(event.sport)} · ${link} · ${state_points} state / ${quote_points} updates</div></div><div class="event-actions"><button class="ghost chart-button" data-chart-event="${esc(event.id)}" data-chart-title="${esc(event.name)}">View Chart</button><div class="score">${score}</div><button class="remove${removing ? " is-removing" : ""}" data-remove-event="${esc(event.id)}"${removing ? ' disabled aria-busy="true"' : ""}>${removing ? "Removing…" : "Remove"}</button></div></div>
      <div class="notice">${restriction}</div>${priceOnly}${portfolio}<div class="section-strip"><span>Actionable selections</span> · buy, sell, margin, and risk</div><div>${marketBody}</div></article>`;
  }

  function showActionError(message) { const box=document.querySelector("#action-error"); box.textContent=message; box.hidden=false; }
  function metricTile(key,value,cls="",sub="") { return `<div class="mtile"><div class="k">${key}</div><div class="v ${cls}">${value}</div>${sub?`<div class="sub2">${sub}</div>`:""}</div>`; }
  function reliabilityView(bins) { if(!bins?.length)return "";const columns=bins.map(bin=>`<div class="rbin" title="predicted ${pct(bin.mean_predicted)} · actual ${pct(bin.empirical_rate)} · n=${bin.count}"><meter class="reliability-meter" min="0" max="1" value="${Number(bin.empirical_rate).toFixed(4)}">${pct(bin.empirical_rate)}</meter><div class="rlabel">${Math.round(bin.lo*100)} · p ${Math.round(bin.mean_predicted*100)}</div></div>`).join("");return `<div class="reliability">${columns}</div><div class="metrics-sub">Meter = actual win rate · label p = predicted probability</div>`; }

  async function refreshMetrics() {
    if (!botsTabVisible()) return;
    const body=document.querySelector("#metrics-body"), sub=document.querySelector("#metrics-sub");
    try {
      const response=await fetch("/api/metrics");
      if(!response.ok)return;
      const m=await response.json();
      if(!m?.n_bets){
        sub.textContent="";
        body.innerHTML='<div class="metrics-empty">No eligible paper fills yet. Close-price CLV and calibration appear only after validated signals and event closure.</div>';
        return;
      }
      const clv=m.clv||{}, model=m.model||{}, base=m.market_baseline||{};
      const independent=m.independent_model||{};
      const execution=m.execution||{}, portfolio=m.portfolio||{}, coverage=m.eligibility_coverage||{};
      const opportunities=coverage.all_opportunities==null?"coverage unavailable":`${coverage.all_opportunities} evaluated decision(s)`;
      sub.textContent=`${m.n_bets} paper fill(s) · ${m.n_settled} settled · ${opportunities}`;
      const tiles=[
        metricTile("Beat close",clv.beat_close_rate==null?"—":pct(clv.beat_close_rate),clv.beat_close_rate>=.5?"good":"bad",clv.n?`n=${clv.n}`:"awaiting closes"),
        metricTile("Mean CLV",clv.mean_clv==null?"—":signedCents(clv.mean_clv),clv.mean_clv>=0?"good":"bad","fill vs last executable close"),
        metricTile("Fill rate",execution.fill_rate==null?"—":pct(execution.fill_rate),"",`${execution.filled||0}/${execution.submitted||0} simulated orders`),
        metricTile("Net paper return",execution.net_paper_return==null?"—":money(execution.net_paper_return),execution.net_paper_return>=0?"good":"bad",execution.turnover==null?"":`$${Number(execution.turnover).toFixed(2)} turnover`),
        metricTile("Max drawdown",portfolio.max_drawdown_dollars==null?"—":`$${Number(portfolio.max_drawdown_dollars).toFixed(2)}`,"","settled paper sequence"),
        metricTile("Consensus Brier",model.brier==null?"—":model.brier.toFixed(3),model.brier!=null&&base.brier!=null&&model.brier<base.brier?"good":"",base.brier==null?"awaiting settle":`executable baseline ${base.brier.toFixed(3)}`)
      ];
      if(model.log_loss!=null)tiles.push(metricTile("Log loss",model.log_loss.toFixed(3)));
      if(model.ece!=null)tiles.push(metricTile("ECE",model.ece.toFixed(3),"","calibration gap"));
      if(model.calibration?.slope!=null)tiles.push(metricTile("Calibration slope",model.calibration.slope.toFixed(2),"",`intercept ${model.calibration.intercept.toFixed(2)}`));
      if(independent.brier!=null){
        const paired=independent.same_rows_calibrated_consensus||{};
        tiles.push(metricTile("Independent Brier",independent.brier.toFixed(3),paired.brier!=null&&independent.brier<paired.brier?"good":"",`cross-check n=${independent.n_settled||0} · same-row consensus ${paired.brier==null?"—":paired.brier.toFixed(3)}`));
      }
      const rejected=Object.entries(coverage.rejection_gates||{}).map(([code,count])=>`${esc(code)} ${count}`).join(" · ")||"none recorded";
      body.innerHTML=`<div class="metric-tiles">${tiles.join("")}</div>`+reliabilityView(m.reliability)+`<div class="metrics-sub">Failed-gate counts: ${rejected}. No statistical edge claim is supported by this report.</div>`;
    } catch {}
  }

  function renderActivityRows(items, emptyMessage="No bot decisions have been recorded yet.") {
    if (!items.length) return `<div class="metrics-empty">${esc(emptyMessage)}</div>`;
    return items.map(item => {
      const details = item.details || {};
      const metrics = [];
      const probability = details.model_probability ?? details.decision_probability;
      const edge = details.actual_edge ?? details.quoted_edge;
      const stake = details.filled_stake ?? details.requested_stake;
      if (probability != null) metrics.push(`decision ${cents(probability)}`);
      if (details.market_probability != null) metrics.push(`market ${cents(details.market_probability)}`);
      if (edge != null) metrics.push(`edge ${signedCents(edge)}`);
      if (stake != null) metrics.push(`plan $${Number(stake).toFixed(2)}`);
      if (details.confidence != null) metrics.push(`quality ${Number(details.confidence).toFixed(0)}/100`);
      if (details.reference_sources != null) metrics.push(`${details.reference_sources}/${details.minimum_sources ?? "?"} refs`);
      const failedGates = details.failed_engine_gates || [];
      const failedCodes = failedGates.map(gate => gate.code).filter(Boolean);
      const engineDetail = failedGates.length ? `<details class="activity-engine-detail">
        <summary>Exact failed engine gate${failedGates.length === 1 ? "" : "s"}</summary>
        <ul>${failedGates.map(gate => `<li><strong>${esc(gate.code||"unknown")}</strong> · ${esc(gate.explanation||"failed")}${gate.value == null ? "" : ` · value ${Number(gate.value).toFixed(4)}`}${gate.threshold == null ? "" : ` · threshold ${Number(gate.threshold).toFixed(4)}`}</li>`).join("")}</ul>
      </details>` : "";
      const timestamp = new Date(Number(item.observed_ts) * 1000).toLocaleTimeString(
        [], {hour:"numeric", minute:"2-digit", second:"2-digit"}
      );
      return `<div class="activity-row">
        <div class="activity-who">
          <div class="activity-bot">${esc(item.account)}</div>
          <div class="activity-time">${esc(timestamp)} · ${esc(item.stage)}</div>
        </div>
        <div class="activity-selection">
          <div class="activity-event">${esc(item.event_name)}</div>
          <div class="activity-meta">${esc(item.market)} · ${esc(item.outcome)}</div>
        </div>
        <div class="activity-reason">
          <span class="activity-status ${item.status === "placed" ? "placed" : "rejected"}">${esc(item.status)}</span>
          <div>${esc(item.reason)}${failedCodes.length ? ` · failed ${esc(failedCodes.join(", "))}` : ""}</div>
          ${metrics.length ? `<div class="activity-meta">${metrics.join(" · ")}</div>` : ""}
          ${engineDetail}
        </div>
      </div>`;
    }).join("");
  }

  function activityCoverage(items) {
    const events = new Map();
    for (const item of items || []) {
      events.set(item.event_id, item.event_name);
    }
    return {
      count: events.size,
      names: [...events.values()],
    };
  }

  let lastActivitySignature = "";
  async function refreshBotActivity() {
    if (!botsTabVisible()) return;
    const body = document.querySelector("#bot-activity");
    const status = document.querySelector("#bot-activity-status");
    if (!body || !status) return;
    try {
      const response = await fetch(
        "/api/bot-activity?limit=80&per_event_limit=4",
        {cache: "no-store"}
      );
      if (!response.ok) throw new Error();
      const items = await response.json();
      const coverage = activityCoverage(items);
      const signature = items.map(item => `${item.id}:${item.observed_ts}:${item.status}`).join("|");
      if (signature !== lastActivitySignature) {
        body.innerHTML = renderActivityRows(items);
        lastActivitySignature = signature;
      }
      status.textContent = items.length
        ? `${items.length} recent decision${items.length === 1 ? "" : "s"} across ${coverage.count} event${coverage.count === 1 ? "" : "s"} · balanced feed · updates every 5s`
        : "Waiting for the next monitored-game evaluation...";
    } catch {
      status.textContent = "Decision feed temporarily unavailable";
      if (!body.children.length) {
        body.innerHTML = '<div class="metrics-empty">Could not load bot decisions.</div>';
      }
    }
  }

  async function viewBot(name) {
    const dialog = document.querySelector("#bot-modal");
    document.querySelector("#bot-modal-title").textContent = name + " Activity";
    document.querySelector("#bot-modal-content").innerHTML = "Loading...";
    dialog.showModal();
    try {
      const [betsResponse, activityResponse] = await Promise.all([
        fetch(`/api/accounts/${encodeURIComponent(name)}/bets`, {cache: "no-store"}),
        fetch(
          `/api/accounts/${encodeURIComponent(name)}/activity?limit=80&per_event_limit=8`,
          {cache: "no-store"}
        )
      ]);
      if (!betsResponse.ok || !activityResponse.ok) throw new Error();
      const [bets, activity] = await Promise.all([
        betsResponse.json(), activityResponse.json()
      ]);
      const coverage = activityCoverage(activity);
      const coverageNames = coverage.names.length
        ? coverage.names.map(eventName => `<span>${esc(eventName)}</span>`).join("")
        : "<span>Waiting for evaluated games</span>";
      const activitySection = `<h3 class="activity-section-title">Latest decisions across ${coverage.count} event${coverage.count === 1 ? "" : "s"}</h3>
        <div class="activity-coverage" aria-label="Events represented in this report">${coverageNames}</div>${renderActivityRows(
        activity, "No evaluated trade candidates yet."
      )}`;
      const rows = bets.map(b => {
        const displayedPnl = b.status === "open" ? b.last_mark_pnl : b.pnl;
        const mark = b.last_mark_value == null
          ? "Unpriced — no complete executable bid mark"
          : `Net cash-out value: $${b.last_mark_value.toFixed(2)} at ${cents(b.last_mark_price)}`;
        const exit = b.status === "cashed_out"
          ? `<span>Exit: ${cents(b.exit_price)} · fee $${(b.exit_fee || 0).toFixed(2)}</span><span>${esc(b.exit_reason || "")}</span>`
          : `<span>${esc(mark)}</span>`;
        return `<div class="game bot-bet">
        <div class="bot-bet-head">
          <div><div class="g-title">${esc(b.event_name)}</div><div class="g-league">${esc(b.market)}: ${esc(b.outcome)}</div></div>
          <div class="align-right"><div class="${displayedPnl > 0 ? 'positive' : displayedPnl < 0 ? 'negative' : ''}">${displayedPnl == null ? "—" : money(displayedPnl)}</div><div class="g-league">${b.status.replaceAll("_", " ").toUpperCase()}</div></div>
        </div>
        <div class="bot-bet-meta">
          <span>Stake: $${b.stake.toFixed(2)}</span>
          <span>All-in entry: ${cents(b.entry_price)}</span>
          <span>Entry fee: $${(b.entry_fee || 0).toFixed(2)}</span>
          <span>Edge: ${pct(b.edge)}</span>
          ${exit}
        </div>
      </div>`}).join("");
      const positionsSection = `<h3 class="activity-section-title">Positions</h3>${
        rows || '<div class="metrics-empty">No paper positions placed yet. The decision feed above shows what is blocking entry.</div>'
      }`;
      document.querySelector("#bot-modal-content").innerHTML =
        activitySection + positionsSection;
    } catch { document.querySelector("#bot-modal-content").innerHTML = '<div class="error">Failed to load activity</div>'; }
  }

  async function refreshLeaderboard() {
    if (!botsTabVisible()) return [];
    const body = document.querySelector("#leaderboard-body");
    try {
      const response = await fetch("/api/leaderboard", {cache: "no-store"});
      if (!response.ok) throw new Error();
      const leaderboard = await response.json();
      if (!leaderboard.length) {
        body.innerHTML = '<div class="metrics-empty">No dummy accounts seeded.</div>';
        return leaderboard;
      }
      const columns = leaderboard.map(account => {
        const equity = account.equity == null ? `$${account.known_equity.toFixed(2)} known` : `$${account.equity.toFixed(2)} eq`;
        const roi = account.roi == null ? "UNPRICED" : pct(account.roi);
        const roiClass = account.roi == null ? "" : account.roi >= 0 ? "good" : "bad";
        const removing = pendingBotRemovals.has(account.name);
        const scopeCount = (account.event_scope || []).length;
        const scope = scopeCount
          ? `${scopeCount} selected event${scopeCount === 1 ? "" : "s"} only`
          : "All monitored events";
        const removeButton = account.is_custom
          ? `<button class="bot-remove${removing ? " is-removing" : ""}" type="button" data-remove-bot data-account="${esc(account.name)}"${removing ? ' disabled aria-busy="true"' : ""}>${removing ? "Removing…" : "Remove bot"}</button>`
          : "";
        const unpriced = account.unpriced_open_positions ? ` · ${account.unpriced_open_positions} unpriced` : "";
        return `<div class="mtile${removing ? " is-removing" : ""}" title="${esc(account.strategy)}" data-bot="${esc(account.name)}"${removing ? ' aria-busy="true"' : ""}>
          <div class="k bot-name">${esc(account.name)}</div>
          <div class="v ${roiClass}">${roi}</div>
          <div class="sub2">${equity}${unpriced} · ${account.win_rate == null ? "—" : pct(account.win_rate)} WR</div>
          <div class="sub2 bot-count">${account.n_bets} positions · ${account.n_cashouts} cash-outs · fees $${account.execution_fees.toFixed(2)}</div>
          <div class="sub2 bot-scope">Scope: ${scope}</div>
          <div class="bot-card-actions">
            <label class="cashout-label bot-cashout-label"><input type="checkbox" data-cashout-toggle data-account="${esc(account.name)}" ${account.cash_out_enabled ? "checked" : ""}> Auto cash-out</label>
            ${removeButton}
          </div>
        </div>`;
      }).join("");
      body.innerHTML = `<div class="metric-tiles">${columns}</div>`;
      return leaderboard;
    } catch {
      return null;
    }
  }
  const BEST_BETS_LIMIT = 12;
  const BEST_BET_MIN_BUY_PRICE = 0.05;
  const BEST_BET_MAX_BUY_PRICE = 0.95;
  function bestBetBuyPriceAllowed(price) {
    const value = Number(price);
    return Number.isFinite(value)
      && value > BEST_BET_MIN_BUY_PRICE
      && value < BEST_BET_MAX_BUY_PRICE;
  }
  function collectBestBets(events) {
    const rows = [];
    for (const v of events || []) {
      for (const m of (v.actionable_markets || [])) {
        // A Best Bet must map back to a concrete, currently listed Polymarket
        // contract—not a sportsbook-only or synthetic selection.
        if (!m.token_id || !m.market_slug) continue;
        // Display-only price bracket: terminal 1c/99c-style contracts can carry
        // misleadingly large stale edges, but are not useful current entries.
        if (!bestBetBuyPriceAllowed(m.buy_price)) continue;
        // Show positive-edge selections plus anything the engine flags as a live
        // entry window (which should already be positive, but never hide one).
        if (m.edge == null) continue;
        if (m.edge <= 0 && m.entry_action !== "ENTRY WINDOW") continue;
        rows.push({ event: v.event, m });
      }
    }
    rows.sort((a, b) => {
      const ea = a.m.entry_action === "ENTRY WINDOW" ? 1 : 0;
      const eb = b.m.entry_action === "ENTRY WINDOW" ? 1 : 0;
      if (ea !== eb) return eb - ea;              // entry windows first
      return (b.m.edge || 0) - (a.m.edge || 0);   // then by edge, strongest first
    });
    return rows.slice(0, BEST_BETS_LIMIT);
  }
  function bestBetExecution(event, market) {
    return usExecutionBySignal.get(
      executionSignalKey(event.id, market.market, market.outcome)
    ) || null;
  }
  function executionPresentation(evaluation) {
    if (!usExecutionEnabled) {
      return {label:"POSITIVE EDGE", cls:"entry", reason:"Positive research edge."};
    }
    if (!evaluation) {
      return {label:"CHECKING US", cls:"wait", reason:"Waiting for the next US execution cycle."};
    }
    const state = evaluation.state || "research_only";
    if (state === "simulated_fill" || state === "live_fill" || state === "position_open") {
      return {label:"POSITION OPEN", cls:"entry", reason:evaluation.reason || "The execution service manages this position."};
    }
    if (state === "us_qualified") {
      return {label:"US QUALIFIED", cls:"entry", reason:"Exact US contract and execution gates cleared."};
    }
    if (state === "queued") {
      return {label:"QUEUED", cls:"hold", reason:evaluation.reason || "Qualified and queued for a later cycle."};
    }
    if (state === "cooldown") {
      return {label:"RECHECKING", cls:"hold", reason:evaluation.reason || "Waiting for the configured retry interval."};
    }
    if (state === "live_disarmed") {
      return {label:"ARM TO TRADE", cls:"cash", reason:evaluation.reason || "Qualified, but live execution is disarmed."};
    }
    return {
      label:"RESEARCH ONLY",
      cls:"marketonly",
      reason:evaluation.reason || "No executable US entry is available for this selection."
    };
  }
  function gotoEvent(id) {
    activeEventId=id;
    document.querySelector('[data-tab="tab-live"]').click();
    if(activeLine!=="all"){
      activeLine="all";
      renderEvents(lastEvents);
    }else{
      renderEventNavigator(lastEvents);
    }
    requestAnimationFrame(() => {
      const safe = (window.CSS && CSS.escape) ? CSS.escape(id) : id;
      const card = document.querySelector(`.event[data-event-id="${safe}"]`);
      if (card) {
        const reduceMotion=window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
        card.scrollIntoView({ behavior: reduceMotion ? "auto" : "smooth", block: "start" });
        card.classList.add("flash");
        setTimeout(() => card.classList.remove("flash"), 1400);
      }
    });
  }
  function renderBestBets() {
    const box = document.querySelector("#best-bets");
    if (!box) return;
    const sub = document.querySelector("#best-bets-sub");
    const rows = collectBestBets(lastEvents);
    if (!rows.length) {
      if (sub) sub.textContent = "";
      box.innerHTML = '<div class="discover-empty">No positive-edge selections right now. Monitor games below to populate this list.</div>';
      return;
    }
    const executionRows = rows.map(({event, m}) => bestBetExecution(event, m));
    const mapped = executionRows.filter(item => item?.us_market_slug).length;
    const open = executionRows.filter(item =>
      ["simulated_fill", "live_fill", "position_open"].includes(item?.state)
    ).length;
    if (sub) sub.textContent = usExecutionEnabled
      ? `${rows.length} shown · ${mapped} US-mapped · ${open} open`
      : `${rows.length} shown`;
    box.innerHTML = rows.map(({ event, m }) => {
      const evaluation = bestBetExecution(event, m);
      const presentation = executionPresentation(evaluation);
      const shownEdge = evaluation?.us_execution_edge ?? m.edge;
      const shownPrice = evaluation?.us_entry_cost ?? m.buy_price;
      const basis = evaluation?.us_execution_edge != null
        ? "US execution edge"
        : m.edge_basis === "gross" ? "research gross edge" : "research net edge";
      const edgeCls = shownEdge >= 0 ? "positive" : "negative";
      const title = [event.name, presentation.reason].filter(Boolean).join(" · ");
      return `<div class="best-bet" role="button" tabindex="0" data-goto-event="${esc(event.id)}" title="${esc(title)}">
        <div class="bb-main">
          <div class="bb-outcome">${lineBadge(m.market, m.outcome)}${esc(m.outcome)}</div>
          <div class="bb-event">${esc(event.name)} · ${esc(event.sport)}</div>
        </div>
        <div class="bb-figs">
          <div class="bb-fig"><div class="value ${edgeCls}">${signedCents(shownEdge)}</div><div class="hint">${basis}</div></div>
          <div class="bb-fig"><div class="value">${m.confidence == null ? "—" : Math.round(m.confidence)}</div><div class="hint">signal quality</div></div>
          <div class="bb-fig"><div class="value">${cents(shownPrice)}</div><div class="hint">${evaluation?.us_entry_cost != null ? "US buy now" : "research buy"}</div></div>
          <span class="tag ${presentation.cls}" title="${esc(presentation.reason)}">${esc(presentation.label)}</span>
        </div>
      </div>`;
    }).join("");
  }
  function renderEvents(events) {
    events = (events || []).filter(view => !pendingEventRemovals.has(view.event.id));
    lastEvents = events;
    renderBestBets();
    if (discoveryTabVisible()) renderDiscover();
    // Keep the newest data for navigation/recommendations, but do not build and
    // replace the heavy Live Radar DOM while another tab is visible.
    if (!liveTabVisible() || document.activeElement?.closest("[data-save-position]")) return;
    const root=document.querySelector("#events");
    const openDetails=new Set([...root.querySelectorAll("details[open][data-detail-key]")].map(d=>d.dataset.detailKey));
    const present=new Set();
    for (const v of events) {
      if ((v.actionable_markets||[]).length) v.actionable_markets.forEach(m=>present.add(lineType(m.market,m.outcome)));
      else if (!v.event.polymarket_slug) (v.signals||[]).slice(0,3).forEach(s=>present.add(lineType(s.market,s.outcome)));
    }
    if (activeLine !== "all" && !present.has(activeLine)) activeLine = "all";
    renderEventNavigator(events);
    renderCarousel(present);
    const cards = events.map(e=>eventCard(e,openDetails)).filter(Boolean).join("");
    root.innerHTML = cards ? `<div class="stack">${cards}</div>`
      : (events.length && activeLine !== "all"
          ? `<div class="panel empty"><b>No ${LINE_META[activeLine].label} lines</b>Nothing matches this filter right now.</div>`
          : '<div class="panel empty"><b>No events yet</b>Go to the Discovery tab to begin.</div>');
  }
  async function refresh() {
    if (refreshInFlight || document.activeElement?.closest("[data-save-position]")) return;
    refreshInFlight=true;
    try { const response=await fetch("/api/events"); if(!response.ok) throw new Error(); renderEvents(await response.json()); }
    catch { const root=document.querySelector("#events"); if(!root.children.length) root.innerHTML='<div class="panel empty"><b>Dashboard disconnected</b>Check your connection.</div>'; }
    finally { refreshInFlight=false; }
  }
  let streamSource = null, streamConnected = false;
  let pendingStreamEvents = null, streamRenderTimer = null;
  function scheduleStreamRender(events) {
    pendingStreamEvents = events;
    if (streamRenderTimer) return;
    streamRenderTimer = setTimeout(() => {
      streamRenderTimer = null;
      const newest = pendingStreamEvents;
      pendingStreamEvents = null;
      renderEvents(newest);
    }, 100);
  }
  function startStream() {
    if (streamSource) return;
    try { streamSource = new EventSource("/api/stream"); } catch { return; }
    streamSource.onopen = () => { streamConnected = true; };
    streamSource.onerror = () => { streamConnected = false; };
    streamSource.onmessage = event => {
      if(!event.data) return;
      try { scheduleStreamRender(JSON.parse(event.data)); } catch {}
    };
  }
  async function refreshBotGames() {
    if (!botsTabVisible()) return;
    const box = document.querySelector("#bot-games");
    if (!box) return;
    try {
      const r = await fetch("/api/monitored-games");
      if (!r.ok) throw new Error();
      const games = await r.json();
      const checked = new Set([...box.querySelectorAll("input:checked")].map(i => i.value));
      if (!games.length) { box.innerHTML = `<div class="field-note">${box.dataset.empty}</div>`; return; }
      box.innerHTML = games.map(g => `<label class="game-check"><input type="checkbox" value="${esc(g.id)}"${checked.has(g.id) ? " checked" : ""}><span>${esc(g.name)}</span><span class="g-league">${esc(String(g.league || g.sport || ""))}</span></label>`).join("");
    } catch { box.innerHTML = '<div class="field-note">Could not load games.</div>'; }
  }

  document.querySelector("#form").addEventListener("submit",async event=>{event.preventDefault();const form=event.currentTarget,button=document.querySelector("#submit-event"),box=document.querySelector("#form-error");
    const payload=Object.fromEntries(new FormData(form));Object.keys(payload).forEach(k=>{if(!payload[k])delete payload[k]});button.disabled=true;box.hidden=true;
    try{const response=await fetch("/api/events",{method:"POST",headers:{"content-type":"application/json"},body:JSON.stringify(payload)});const body=await response.json().catch(()=>({}));if(!response.ok)throw new Error(body.detail||`Could not monitor event (${response.status})`);form.reset();await refresh();
      const status=document.querySelector("#discover-batch-status");
      if(status){status.className="refresh-status discover-batch-status is-success";status.textContent="Event added to Live Radar. You can keep working in Discovery."}
    }
    catch(error){box.textContent=error.message;box.hidden=false}finally{button.disabled=false}});

  document.querySelector("#bot-form").addEventListener("submit", async e => {
    e.preventDefault();
    const form = e.currentTarget, btn = form.querySelector("button"), err = document.querySelector("#bot-error");
    btn.disabled = true; err.hidden = true;
    const sizing = form.querySelector("#bot-sizing").value;
    const mult = Number(form.querySelector("#bot-multiplier").value);
    const payload = {
      name: form.querySelector("#bot-name").value,
      edge_threshold: Number(form.querySelector("#bot-edge").value) / 100,
      sizing: sizing,
      kelly_multiplier: sizing === "kelly" ? mult : 1.0,
      flat_stake: sizing === "flat" ? mult : 100.0,
      start_bankroll: 10000.0,
      webhook_url: form.querySelector("#bot-webhook").value || "",
      cash_out_enabled: form.querySelector("#bot-cashout").checked,
      events: [...form.querySelectorAll("#bot-games input:checked")].map(i => i.value)
    };
    try {
      const r = await fetch("/api/accounts", { method: "POST", headers: {"content-type":"application/json"}, body: JSON.stringify(payload) });
      const b = await r.json().catch(()=>({}));
      if (!r.ok) throw new Error(b.detail || "Failed to create bot");
      form.reset();
      await refreshLeaderboard();
      await refreshBotActivity();
    } catch (er) { err.textContent = er.message; err.hidden = false; }
    finally { btn.disabled = false; }
  });

  document.querySelector("#events").addEventListener("submit",async event=>{const form=event.target.closest("[data-save-position]");if(!form)return;event.preventDefault();const button=form.querySelector("button"),data=new FormData(form);button.disabled=true;document.querySelector("#action-error").hidden=true;
    const payload={token_id:form.dataset.tokenId,market:form.dataset.market,outcome:form.dataset.outcome,shares:Number(data.get("shares")),avg_entry_price:Number(data.get("entry_cents"))/100};
    try{const response=await fetch(`/api/events/${encodeURIComponent(form.dataset.eventId)}/positions`,{method:"PUT",headers:{"content-type":"application/json"},body:JSON.stringify(payload)});const body=await response.json().catch(()=>({}));if(!response.ok)throw new Error(body.detail||"Could not save position");document.activeElement.blur();await refresh()}
    catch(error){showActionError(error.message)}finally{button.disabled=false}});
  let currentChart = null, chartLibraryPromise = null;
  function loadChartLibrary() {
    if (window.Chart) return Promise.resolve();
    if (chartLibraryPromise) return chartLibraryPromise;
    chartLibraryPromise = new Promise((resolve, reject) => {
      const script = document.createElement("script");
      script.src = "/static/vendor/chart.umd.min.js";
      script.onload = resolve;
      script.onerror = () => {
        chartLibraryPromise = null;
        reject(new Error("Chart library failed to load"));
      };
      document.head.appendChild(script);
    });
    return chartLibraryPromise;
  }
  async function viewChart(eventId, eventName) {
    const dialog = document.querySelector("#chart-modal");
    document.querySelector("#chart-modal-title").textContent = eventName + " History";
    dialog.showModal();
    try {
      await loadChartLibrary();
      const r = await fetch(`/api/events/${encodeURIComponent(eventId)}/history?limit=1200`);
      if (!r.ok) throw new Error();
      const data = await r.json();

      const ctx = document.getElementById('historyChart').getContext('2d');
      if (currentChart) currentChart.destroy();

      const datasets = [];
      const colors = ['#29e7d6', '#ffcf3f', '#ff4d2e', '#a020f0', '#00ff00', '#ff00ff'];
      let cIdx = 0;

      const byOutcome = {};
      for (const q of data.quotes) {
        if (!byOutcome[q.outcome]) byOutcome[q.outcome] = [];
        byOutcome[q.outcome].push({x: q.observed_at * 1000, y: q.probability * 100});
      }
      for (const [outcome, pts] of Object.entries(byOutcome)) {
        datasets.push({ label: outcome + ' Prob (%)', data: pts, borderColor: colors[cIdx % colors.length], fill: false, stepped: true });
        cIdx++;
      }

      const homeScores = data.states.filter(s => s.home_score != null).map(s => ({x: s.observed_at * 1000, y: s.home_score}));
      if (homeScores.length > 0) datasets.push({ label: 'Home Score', data: homeScores, borderColor: '#ffffff', borderDash: [5, 5], stepped: true });

      currentChart = new Chart(ctx, {
        type: 'line', data: { datasets },
        options: { responsive: true, maintainAspectRatio: false, scales: { x: { type: 'linear', ticks: { callback: v => new Date(v).toLocaleTimeString() } }, y: { min: 0 } }, animation: false }
      });
    } catch {
      console.error("Failed to load chart");
    }
  }
  document.querySelector("#chart-modal").addEventListener("close", () => {
    if (currentChart) {
      currentChart.destroy();
      currentChart = null;
    }
  });

  document.querySelector("#events").addEventListener("click",async event=>{const removeEvent=event.target.closest("[data-remove-event]"),removePosition=event.target.closest("[data-remove-position]"),chartBtn=event.target.closest("[data-chart-event]");
    if(removeEvent){
      const eventId=removeEvent.dataset.removeEvent;
      if(pendingEventRemovals.has(eventId))return;
      const eventName=removeEvent.closest("[data-event-id]")?.querySelector(".name")?.textContent||"event";
      pendingEventRemovals.add(eventId);
      document.querySelector("#action-error").hidden=true;
      showEventActionStatus(`Removing "${eventName}" and stopping its live feeds…`);
      lastEvents=lastEvents.filter(view=>view.event.id!==eventId);
      renderEvents(lastEvents);
      try{
        const response=await fetch(`/api/events/${encodeURIComponent(eventId)}`,{method:"DELETE"});
        if(!response.ok){
          const body=await response.json().catch(()=>({}));
          throw new Error(body.detail||`Could not remove event (${response.status})`);
        }
        showEventActionStatus(`Removed "${eventName}". Its live buffers and feed tasks were released.`,"success",15000);
      }catch(error){
        pendingEventRemovals.delete(eventId);
        showEventActionStatus(`${error.message||"Could not remove event"}. The event remains monitored.`,"error");
        await refresh();
      }
    }
    if(removePosition){removePosition.disabled=true;try{const response=await fetch(`/api/events/${encodeURIComponent(removePosition.dataset.eventId)}/positions/${encodeURIComponent(removePosition.dataset.tokenId)}`,{method:"DELETE"});if(response.ok)await refresh()}catch{}finally{removePosition.disabled=false}}
    if(chartBtn){viewChart(chartBtn.dataset.chartEvent, chartBtn.dataset.chartTitle)}});
  let discoverGames = [], discoverBatchActive = false;
  const selectedDiscoverSlugs = new Set();
  function discoverStatus(game){
    if(game.status==="live")return '<span class="g-live">● LIVE</span> ';
    if(game.status==="started")return '<span class="g-started">◌ STARTED</span> ';
    if(game.game_start){
      const start=new Date(game.game_start);
      if(!Number.isNaN(start.getTime())){
        return `<span class="g-scheduled">◷ ${start.toLocaleTimeString([], {hour:"numeric",minute:"2-digit"})}</span> `;
      }
    }
    return "";
  }
  let discoverLeague="mlb";
  try{
    const saved=localStorage.getItem("pelosi-discover-league");
    if(saved!=null)discoverLeague=saved;
  }catch{}
  function renderDiscoverLeagueFilter(){
    document.querySelectorAll("#discover-league-filter [data-league]").forEach(pill=>{
      pill.classList.toggle("active",pill.dataset.league===discoverLeague);
      pill.setAttribute("aria-selected",String(pill.dataset.league===discoverLeague));
    });
  }
  document.querySelector("#discover-league-filter")?.addEventListener("click",event=>{
    const pill=event.target.closest("[data-league]");
    if(!pill||pill.dataset.league===discoverLeague)return;
    discoverLeague=pill.dataset.league;
    try{localStorage.setItem("pelosi-discover-league",discoverLeague);}catch{}
    renderDiscoverLeagueFilter();
    loadDiscover(true);
  });
  renderDiscoverLeagueFilter();
  function monitoredDiscoverSlugs(){
    return new Set(lastEvents.map(view=>String(view?.event?.polymarket_slug||"")).filter(Boolean));
  }
  function visibleDiscoverGames(){
    const q=(document.querySelector("#discover-search").value||"").toLowerCase();
    return discoverGames.filter(g=>!q||`${g.title} ${g.league||""}`.toLowerCase().includes(q));
  }
  function updateDiscoverBatchControls(shown,monitored){
    for(const slug of [...selectedDiscoverSlugs]){
      if(monitored.has(slug)||!discoverGames.some(game=>game.slug===slug))selectedDiscoverSlugs.delete(slug);
    }
    const available=shown.filter(game=>!monitored.has(game.slug));
    const allVisibleSelected=available.length>0&&available.every(game=>selectedDiscoverSlugs.has(game.slug));
    const count=selectedDiscoverSlugs.size;
    const countBox=document.querySelector("#discover-selection-count");
    const selectVisible=document.querySelector("#discover-select-visible");
    const clear=document.querySelector("#discover-clear-selection");
    const monitor=document.querySelector("#discover-monitor-selected");
    if(countBox)countBox.textContent=`${count} selected`;
    if(selectVisible){
      selectVisible.disabled=discoverBatchActive||!available.length;
      selectVisible.textContent=allVisibleSelected?"Deselect visible":"Select visible";
      selectVisible.dataset.mode=allVisibleSelected?"deselect":"select";
    }
    if(clear)clear.disabled=discoverBatchActive||!count;
    if(monitor){
      monitor.disabled=discoverBatchActive||!count;
      monitor.textContent=discoverBatchActive
        ?"Adding events…"
        : count ? `Monitor ${count} selected` : "Monitor selected";
    }
    const plan=document.querySelector("#gameday-plan");
    if(plan){
      plan.disabled=discoverBatchActive||gamedayPlanActive||!count;
      plan.textContent=gamedayPlanActive
        ?"Planning…"
        : count ? `Plan game-day dry run (${count})` : "Plan game-day dry run";
    }
  }

  let gamedayPlanActive=false;
  function renderGamedayStatus(state){
    const status=document.querySelector("#gameday-status");
    const cancel=document.querySelector("#gameday-cancel");
    if(!status)return;
    if(cancel)cancel.hidden=!state?.armed;
    if(!state||(!state.armed&&state.phase!=="completed")){
      status.className="refresh-status gameday-status";
      status.textContent="No game-day plan armed.";
      return;
    }
    const events=Array.isArray(state.events)?state.events:[];
    const live=events.filter(item=>item.live).length;
    const finals=events.filter(item=>item.final).length;
    const label=events.length===1?"game":"games";
    if(state.phase==="waiting"){
      status.className="refresh-status gameday-status is-working";
      status.textContent=`Armed for ${events.length} ${label}. Waiting for first pitch — Odds API polling and dry-run automation start automatically.`;
    }else if(state.phase==="active"){
      status.className="refresh-status gameday-status is-success";
      status.textContent=`Autopilot active: ${live} live / ${finals} of ${events.length} final. Polling and dry-run automation are on; everything stops after the last final.`;
    }else{
      status.className="refresh-status gameday-status";
      status.textContent=`Game day complete: ${finals} of ${events.length} ${label} final. Odds API polling and dry-run automation are off.`;
    }
  }
  async function loadGamedayStatus(){
    try{
      const response=await fetch("/api/gameday");
      if(!response.ok)return;
      renderGamedayStatus(await response.json());
    }catch{}
  }
  async function planGamedaySelection(){
    const slugs=[...selectedDiscoverSlugs];
    if(!slugs.length||gamedayPlanActive)return;
    gamedayPlanActive=true;
    renderDiscover();
    try{
      await monitorSelectedDiscoverGames();
      const response=await fetch("/api/gameday",{
        method:"POST",
        headers:{"content-type":"application/json"},
        body:JSON.stringify({slugs})
      });
      const body=await response.json().catch(()=>({}));
      if(!response.ok)throw new Error(body.detail||`Could not plan the game day (${response.status})`);
      renderGamedayStatus(body);
    }catch(error){
      const status=document.querySelector("#gameday-status");
      if(status){
        status.className="refresh-status gameday-status is-error";
        status.textContent=error.message||"Could not plan the game day.";
      }
    }finally{
      gamedayPlanActive=false;
      renderDiscover();
    }
  }
  async function cancelGameday(){
    try{
      const response=await fetch("/api/gameday",{method:"DELETE"});
      if(response.ok)renderGamedayStatus(await response.json());
    }catch{}
  }
  function renderDiscover() {
    const list=document.querySelector("#discover-list");
    const monitored=monitoredDiscoverSlugs();
    const shown=visibleDiscoverGames();
    updateDiscoverBatchControls(shown,monitored);
    if(!discoverGames.length){list.innerHTML='<div class="discover-empty">No live or upcoming games found right now.</div>';return}
    list.innerHTML=shown.length?shown.map(g=>{
      const isMonitored=monitored.has(g.slug);
      const isSelected=!isMonitored&&selectedDiscoverSlugs.has(g.slug);
      const state=isMonitored?"Monitoring":isSelected?"Selected":"Select";
      return `<label class="game discover-game${isMonitored?" is-monitored":""}${isSelected?" is-selected":""}" data-slug="${esc(g.slug)}" title="${esc(g.title)}">
        <input class="discover-game-check" type="checkbox" ${isMonitored?"checked disabled":discoverBatchActive?"disabled":""} ${isSelected?"checked":""} data-discover-select="${esc(g.slug)}" aria-label="${esc(isMonitored?`${g.title} is already monitored`:`Select ${g.title}`)}">
        <span class="discover-game-copy"><span class="g-title">${esc(g.title)}</span><span class="g-league">${discoverStatus(g)}${esc(g.league||"sports")}${g.listed===false?' · awaiting Polymarket listing':''}${g.reference_adapter===false?' · PRICE ONLY — NO REFERENCE ADAPTER':''}</span></span>
        <span class="g-add${isMonitored?" is-monitored":""}">${esc(state)}</span>
      </label>`;
    }).join(""):'<div class="discover-empty">No games match.</div>';
  }
  let discoverRequest = null;
  async function loadDiscover(manual=false) {
    if (!manual && !discoveryTabVisible()) return;
    loadGamedayStatus();
    const list=document.querySelector("#discover-list");
    const button=document.querySelector("#discover-refresh");
    const status=document.querySelector("#discover-refresh-status");
    if(discoverRequest){
      if(manual)status.textContent="A refresh is already running...";
      return discoverRequest;
    }
    if(manual){
      button.disabled=true;
      button.classList.add("is-refreshing");
      button.textContent="Refreshing...";
      status.className="refresh-status";
      status.textContent="Checking Polymarket for current games...";
    }
    discoverRequest=(async()=>{
      try{
        const leagueQuery=discoverLeague?`league=${encodeURIComponent(discoverLeague)}`:"";
        const refreshQuery=manual?"refresh=true":"";
        const query=[refreshQuery,leagueQuery].filter(Boolean).join("&");
        const r=await fetch(`/api/discover${query?`?${query}`:""}`,{cache:"no-store"});
        const body=await r.json().catch(()=>null);
        if(!r.ok)throw new Error(body?.detail||`Refresh failed (${r.status})`);
        discoverGames=body||[];
        renderDiscover();
        if(manual){
          status.className="refresh-status is-success";
          status.textContent=`Updated ${new Date().toLocaleTimeString()} · ${discoverGames.length} game${discoverGames.length===1?"":"s"} found`;
        }
      }
      catch(error){
        if(!discoverGames.length)list.innerHTML='<div class="discover-empty">Could not load games.</div>';
        if(manual){
          status.className="refresh-status is-error";
          status.textContent=error.message||"Could not refresh the game list.";
        }
      }
      finally{
        if(manual){
          button.disabled=false;
          button.classList.remove("is-refreshing");
          button.textContent="Refresh List";
        }
        discoverRequest=null;
      }
    })();
    return discoverRequest;
  }
  async function addDiscoveredGame(slug) {
    const response=await fetch("/api/events",{method:"POST",headers:{"content-type":"application/json"},body:JSON.stringify({polymarket_url:`https://polymarket.com/event/${slug}`})});
    const body=await response.json().catch(()=>({}));
    if(!response.ok)throw new Error(body.detail||`Could not monitor (${response.status})`);
    return body;
  }
  async function monitorSelectedDiscoverGames(){
    const status=document.querySelector("#discover-batch-status");
    const monitored=monitoredDiscoverSlugs();
    const slugs=[...selectedDiscoverSlugs].filter(slug=>!monitored.has(slug));
    if(!slugs.length){renderDiscover();return}
    discoverBatchActive=true;
    renderDiscover();
    const failures=[];
    let added=0;
    for(let index=0;index<slugs.length;index+=1){
      const slug=slugs[index];
      if(status){
        status.className="refresh-status discover-batch-status is-working";
        status.textContent=`Adding ${index+1} of ${slugs.length} to Live Radar…`;
      }
      try{
        await addDiscoveredGame(slug);
        selectedDiscoverSlugs.delete(slug);
        added+=1;
      }catch(error){
        failures.push(`${slug}: ${error.message||"could not add"}`);
      }
    }
    discoverBatchActive=false;
    await refresh();
    await refreshMetrics();
    renderDiscover();
    if(status){
      status.className=`refresh-status discover-batch-status ${failures.length?"is-error":"is-success"}`;
      status.textContent=failures.length
        ? `Added ${added} of ${slugs.length}. ${failures.length} failed: ${failures.join(" | ")}`
        : `Added ${added} event${added===1?"":"s"} to Live Radar. Discovery remains open.`;
    }
  }
  document.querySelector("#discover-list").addEventListener("change",event=>{
    const input=event.target.closest("[data-discover-select]");
    if(!input||input.disabled)return;
    const slug=input.dataset.discoverSelect;
    if(input.checked)selectedDiscoverSlugs.add(slug);else selectedDiscoverSlugs.delete(slug);
    renderDiscover();
  });
  document.querySelector("#discover-select-visible").addEventListener("click",event=>{
    const monitored=monitoredDiscoverSlugs();
    const available=visibleDiscoverGames().filter(game=>!monitored.has(game.slug));
    if(event.currentTarget.dataset.mode==="deselect"){
      available.forEach(game=>selectedDiscoverSlugs.delete(game.slug));
    }else{
      available.forEach(game=>selectedDiscoverSlugs.add(game.slug));
    }
    renderDiscover();
  });
  document.querySelector("#discover-clear-selection").addEventListener("click",()=>{
    selectedDiscoverSlugs.clear();
    renderDiscover();
  });
  document.querySelector("#discover-monitor-selected").addEventListener("click",monitorSelectedDiscoverGames);
  document.querySelector("#gameday-plan")?.addEventListener("click",planGamedaySelection);
  document.querySelector("#gameday-cancel")?.addEventListener("click",cancelGameday);
  document.querySelector("#best-bets").addEventListener("click",e=>{const row=e.target.closest("[data-goto-event]");if(row)gotoEvent(row.dataset.gotoEvent)});
  document.querySelector("#best-bets").addEventListener("keydown",e=>{if(e.key!=="Enter"&&e.key!==" ")return;const row=e.target.closest("[data-goto-event]");if(row){e.preventDefault();gotoEvent(row.dataset.gotoEvent)}});
  document.querySelector("#discover-search").addEventListener("input",renderDiscover);
  document.querySelector("#discover-refresh").addEventListener("click",()=>loadDiscover(true));
  document.querySelector("#event-jump-list").addEventListener("click",e=>{const button=e.target.closest("[data-jump-event]");if(button)gotoEvent(button.dataset.jumpEvent)});
  document.querySelector("#line-filter").addEventListener("click",e=>{const p=e.target.closest("[data-line]");if(!p)return;activeLine=p.dataset.line;renderEvents(lastEvents);});

  document.addEventListener("click", async event => {
    const close = event.target.closest("[data-close-dialog]");
    if (close) document.getElementById(close.dataset.closeDialog)?.close();
    const removeBot = event.target.closest("[data-remove-bot]");
    if (removeBot) {
      event.stopPropagation();
      const name = removeBot.dataset.account;
      if (!window.confirm(`Remove custom bot "${name}"? Its historical decisions and positions will be retained for analysis.`)) return;
      const card = removeBot.closest("[data-bot]");
      pendingBotRemovals.add(name);
      removeBot.disabled = true;
      removeBot.setAttribute("aria-busy", "true");
      removeBot.classList.add("is-removing");
      removeBot.textContent = "Removing…";
      card?.classList.add("is-removing");
      card?.setAttribute("aria-busy", "true");
      showBotActionStatus(`Removing "${name}" and stopping future paper trades…`);
      try {
        const response = await fetch(`/api/accounts/${encodeURIComponent(name)}`, {
          method: "DELETE"
        });
        const body = await response.json().catch(()=>({}));
        if (!response.ok) throw new Error(body.detail || "Could not remove bot");
        card?.remove();
        showBotActionStatus(
          `Removed "${name}". It can no longer trade; its historical decisions and positions remain in the audit record.`,
          "success",
          30000
        );
        await refreshBotActivity();
      } catch (error) {
        showBotActionStatus(
          `${error.message || "Could not remove bot"}. The bot is still active.`,
          "error"
        );
      } finally {
        pendingBotRemovals.delete(name);
        await refreshLeaderboard();
        const currentButton = [...document.querySelectorAll("[data-remove-bot]")]
          .find(button => button.dataset.account === name) || removeBot;
        const currentCard = currentButton.closest("[data-bot]") || card;
        if (currentButton.isConnected) {
          currentButton.disabled = false;
          currentButton.removeAttribute("aria-busy");
          currentButton.classList.remove("is-removing");
          currentButton.textContent = "Remove bot";
          currentCard?.classList.remove("is-removing");
          currentCard?.removeAttribute("aria-busy");
        }
      }
      return;
    }
    const cashout = event.target.closest("[data-cashout-toggle]");
    if (cashout) {
      event.stopPropagation();
      cashout.disabled = true;
      fetch(`/api/accounts/${encodeURIComponent(cashout.dataset.account)}`, {
        method: "PATCH",
        headers: {"content-type":"application/json"},
        body: JSON.stringify({cash_out_enabled: cashout.checked})
      }).then(response => {
        if (!response.ok) throw new Error();
        return refreshLeaderboard();
      }).catch(() => { cashout.checked = !cashout.checked; })
        .finally(() => { cashout.disabled = false; });
      return;
    }
    const bot = event.target.closest("[data-bot]");
    if (bot) viewBot(bot.dataset.bot);
  });

  document.querySelector("#auto-monitor-toggle")?.addEventListener("change", async e => {
    try { await fetch("/api/config", { method: "POST", headers: {"content-type":"application/json"}, body: JSON.stringify({ auto_monitor: e.target.checked }) }); } catch {}
  });

  function showOddsApiStatus(enabled, pollSeconds, message = "") {
    const status = document.querySelector("#odds-api-status");
    if (!status) return;
    status.className = `odds-api-status ${enabled ? "is-enabled" : "is-disabled"}`;
    status.textContent = message || (enabled
      ? `Odds API calls are ON · backend polling every ${Number(pollSeconds||45).toLocaleString(undefined, {maximumFractionDigits:1})} seconds per eligible monitored event.`
      : "Odds API calls are OFF · backend pollers are paused even when no browser is open.");
  }

  document.querySelector("#odds-api-toggle")?.addEventListener("change", async event => {
    const toggle = event.currentTarget;
    const requested = toggle.checked;
    const status = document.querySelector("#odds-api-status");
    toggle.disabled = true;
    if (status) {
      status.className = "odds-api-status";
      status.textContent = requested
        ? "Starting Odds API polling…"
        : "Stopping new Odds API calls…";
    }
    try {
      const response = await fetch("/api/config", {
        method: "POST",
        headers: {"content-type":"application/json"},
        body: JSON.stringify({odds_api_enabled: requested})
      });
      const config = await response.json().catch(()=>({}));
      if (!response.ok) throw new Error(
        config.detail||`Could not update (${response.status})`
      );
      toggle.checked = !!config.odds_api_enabled;
      showOddsApiStatus(config.odds_api_enabled, config.odds_api_poll_seconds);
    } catch (error) {
      toggle.checked = !requested;
      if (status) {
        status.className = "odds-api-status is-error";
        status.textContent = `${error.message||"Could not update Odds API polling"}. Setting was not changed.`;
      }
    } finally {
      toggle.disabled = false;
    }
  });

  document.querySelector("#odds-api-interval-save")?.addEventListener("click", async event => {
    const button = event.currentTarget;
    const input = document.querySelector("#odds-api-interval");
    const seconds = Number(input?.value);
    const status = document.querySelector("#odds-api-status");
    if (!Number.isFinite(seconds) || seconds < 1 || seconds > 3600) {
      if (status) {
        status.className = "odds-api-status is-error";
        status.textContent = "Enter an interval from 1 to 3,600 seconds.";
      }
      input?.focus();
      return;
    }
    button.disabled = true;
    if (input) input.disabled = true;
    button.textContent = "Applying…";
    if (status) {
      status.className = "odds-api-status";
      status.textContent = `Applying a ${seconds.toLocaleString(undefined, {maximumFractionDigits:1})}-second Odds API interval…`;
    }
    try {
      const response = await fetch("/api/config", {
        method: "POST",
        headers: {"content-type":"application/json"},
        body: JSON.stringify({odds_api_poll_seconds: seconds})
      });
      const config = await response.json().catch(()=>({}));
      if (!response.ok) {
        const detail = Array.isArray(config.detail)
          ? config.detail.map(item => item.msg).filter(Boolean).join("; ")
          : config.detail;
        throw new Error(detail || `Could not update (${response.status})`);
      }
      if (input) input.value = Number(config.odds_api_poll_seconds || seconds);
      showOddsApiStatus(
        config.odds_api_enabled,
        config.odds_api_poll_seconds,
        `Interval saved · paid polling will use ${Number(config.odds_api_poll_seconds).toLocaleString(undefined, {maximumFractionDigits:1})} seconds per eligible monitored event without a restart.`
      );
    } catch (error) {
      if (status) {
        status.className = "odds-api-status is-error";
        status.textContent = `${error.message||"Could not update the interval"}. Setting was not changed.`;
      }
    } finally {
      button.disabled = false;
      if (input) input.disabled = false;
      button.textContent = "Apply interval";
    }
  });

  // Only start the app data fetching after successful login or if already authenticated.
  // We check if the events endpoint succeeds. If so, we are logged in, hide overlay immediately.
  async function checkAuthAndStart() {
    try {
      const r = await fetch("/api/events");
      if (r.ok) {
        const events = await r.json();
        document.querySelector("#login-overlay").hidden = true;
        startApp(events);
      }
    } catch {}
  }

  function startApp(initialEvents = null) {
    if (window.appStarted) {
      if (initialEvents) renderEvents(initialEvents);
      return;
    }
    window.appStarted = true;
    try {
      const savedTab = sessionStorage.getItem("pelositracker-active-tab");
      const restorableTabs = new Set([
        "tab-live",
        "tab-discovery",
        "tab-us-research"
      ]);
      const savedButton = restorableTabs.has(savedTab)
        ? document.querySelector(`[data-tab="${savedTab}"]`)
        : null;
      // Always activate the effective tab so its loaders fire on a fresh
      // session too, not only when restoring a different saved tab.
      const startButton = savedButton
        || document.querySelector(".tab-carousel .pill.active");
      if (startButton) activatePrimaryTab(startButton);
    } catch {}
    fetch("/api/config").then(r=>r.json()).then(c=>{
      const prefix = c.workstation?.enabled ? "Local workstation · " : "";
      usExecutionEnabled = !!c.workstation?.polymarket_us_trading_enabled;
      document.querySelector("#config").textContent=`${prefix}Quality ≥ ${c.confidence_threshold} · Base edge ≥ ${(c.edge_threshold*100).toFixed(1)}%`;
      const botsButton = document.querySelector("#bots-tab-button");
      if (botsButton) botsButton.hidden = c.paper_bot_policy?.enabled === false;
      if(document.querySelector("#auto-monitor-toggle")) document.querySelector("#auto-monitor-toggle").checked = !!c.auto_monitor;
      if(document.querySelector("#odds-api-toggle")) document.querySelector("#odds-api-toggle").checked = !!c.odds_api_enabled;
      if(document.querySelector("#odds-api-interval")) document.querySelector("#odds-api-interval").value = Number(c.odds_api_poll_seconds || 45);
      showOddsApiStatus(c.odds_api_enabled, c.odds_api_poll_seconds);
      const policy=document.querySelector("#bot-policy");
      if(policy&&c.paper_bot_policy){
        policy.textContent=c.paper_bot_policy.message;
        const anyModel=Object.values(c.paper_bot_policy.models||{}).some(Boolean);
        policy.classList.toggle("is-ready",!!c.paper_bot_policy.calibration_loaded||!!c.paper_bot_policy.allow_uncalibrated||anyModel);
      }
      if (usExecutionEnabled && discoveryTabVisible()) refreshUSExecutionStatus();
    }).catch(()=>document.querySelector("#config").textContent="Thresholds unavailable");

    if (initialEvents) renderEvents(initialEvents); else refresh();
    startStream();

    // Set intervals
    if (!window.intervalsStarted) {
      window.intervalsStarted = true;
      // SSE is the primary events transport. A full JSON poll is only a
      // disconnected-stream fallback, avoiding duplicate parse/render cycles.
      setInterval(()=>{if(!streamConnected)refresh()},30000);
      // The bots and discovery pollers only run while their tab is showing;
      // activating a tab always refreshes it immediately, so nothing is stale
      // on arrival and hidden tabs stop generating request noise.
      setInterval(()=>{if(botsTabVisible())refreshMetrics()},5000);
      setInterval(()=>{if(botsTabVisible())refreshLeaderboard()},5000);
      setInterval(()=>{if(botsTabVisible())refreshBotActivity()},5000);
      setInterval(()=>{if(discoveryTabVisible())loadDiscover()},60000);
      setInterval(()=>{if(botsTabVisible())refreshBotGames()},30000);
      setInterval(()=>{if(usResearchTabVisible())loadUSEvents()},60000);
      setInterval(()=>{if(usResearchTabVisible())loadModelLab()},30000);
      setInterval(()=>{
        if(usResearchTabVisible() && usLedgerLoaded) {
          loadPerformanceLedger({quiet:true});
        }
      },30000);
      setInterval(()=>{
        if(usResearchTabVisible()) loadUSTrading();
        else if(discoveryTabVisible()) refreshUSExecutionStatus();
      },5000);
    }
  }

  document.addEventListener("visibilitychange", () => {
    if (document.hidden || !window.appStarted) return;
    if (liveTabVisible()) renderEvents(lastEvents);
    if (!streamConnected) refresh();
    if (usResearchTabVisible()) {
      refreshUSStatus();
      loadUSEvents();
      loadUSTrading();
      loadPerformanceLedger({quiet:true});
      loadModelLab();
      loadPolicyAdvisorSessions();
    }
    if (botsTabVisible()) {
      refreshMetrics();
      refreshLeaderboard();
      refreshBotActivity();
      refreshBotGames();
    }
  });

  checkAuthAndStart();
