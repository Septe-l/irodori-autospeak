(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const log = $("log");

  // ---- playback queue -----------------------------------------------------
  // The server publishes clips strictly in speaking order, so this only has to
  // play them back-to-back and preload the next one to keep the gap short.
  const queue = [];
  let current = null;
  let playing = false;
  const player = new Audio();
  const preloader = new Audio();
  player.preload = "auto";
  preloader.preload = "auto";

  let turns = new Map();   // turn_id -> { el, body, spans: Map(seq -> span) }
  let voiceCache = [];     // reference voices available on disk
  let settingsCache = null;

  function post(path, body) {
    return fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    }).catch((e) => console.warn("post failed", path, e));
  }

  function preloadNext() {
    if (queue.length > 0) preloader.src = queue[0].url;
  }

  async function playNext() {
    if (playing) return;
    const item = queue.shift();
    if (!item) {
      current = null;
      $("now").hidden = true;
      markSpeaking(null);
      return;
    }
    current = item;
    playing = true;
    preloadNext();

    markSpeaking(item);
    $("now").hidden = false;
    $("now-speaker").textContent = item.speaker_name || "再生中";
    $("now-text").textContent = item.text;
    post("/api/playing", { turn_id: item.turn_id, seq: item.seq });

    player.src = item.url;
    try {
      await player.play();
    } catch (e) {
      // Autoplay was refused, or the file vanished. Skip rather than stall.
      console.warn("play failed", e);
      playing = false;
      finishCurrent();
      playNext();
    }
  }

  function finishCurrent() {
    if (!current) return;
    const span = spanFor(current.turn_id, current.seq);
    if (span) { span.classList.remove("speaking"); span.classList.add("spoken"); }
    post("/api/played", { turn_id: current.turn_id, seq: current.seq });
    current = null;
  }

  player.addEventListener("ended", () => {
    playing = false;
    finishCurrent();
    playNext();
  });
  player.addEventListener("error", () => {
    if (!playing) return;
    playing = false;
    finishCurrent();
    playNext();
  });

  function spanFor(turnId, seq) {
    const t = turns.get(turnId);
    return t ? t.spans.get(seq) : null;
  }

  function markSpeaking(item) {
    document.querySelectorAll(".turn-body span.speaking")
      .forEach((el) => el.classList.remove("speaking"));
    if (!item) return;
    const span = spanFor(item.turn_id, item.seq);
    if (span) {
      span.classList.add("speaking");
      span.scrollIntoView({ block: "nearest", behavior: "smooth" });
    }
  }

  // ---- transcript rendering ----------------------------------------------

  function ensureTurn(turnId, subtopic, sources, speakerIndex, speakerName) {
    if (turns.has(turnId)) return turns.get(turnId);
    $("empty").hidden = true;

    const el = document.createElement("div");
    el.className = "turn" + (speakerIndex ? " spk" + speakerIndex : "");

    const head = document.createElement("div");
    head.className = "turn-head";
    head.innerHTML = `<span class="turn-tag">#${turnId}</span>`;
    if (speakerName) {
      const who = document.createElement("span");
      who.className = "turn-who";
      who.textContent = speakerName;
      head.appendChild(who);
    }
    const sub = document.createElement("span");
    sub.className = "turn-sub";
    sub.textContent = subtopic || "";
    head.appendChild(sub);
    const time = document.createElement("span");
    time.className = "turn-time";
    head.appendChild(time);
    el.appendChild(head);

    const body = document.createElement("div");
    body.className = "turn-body";
    el.appendChild(body);

    if (sources && sources.length) {
      const src = document.createElement("div");
      src.className = "sources";
      sources.slice(0, 6).forEach((s) => {
        const a = document.createElement("a");
        a.href = s.url;
        a.target = "_blank";
        a.rel = "noopener noreferrer";
        a.textContent = s.title ? s.title.slice(0, 28) : s.url.slice(0, 28);
        src.appendChild(a);
      });
      el.appendChild(src);
    }

    log.appendChild(el);
    const entry = { el, body, spans: new Map() };
    turns.set(turnId, entry);
    return entry;
  }

  function addSentence(turnId, seq, text, speakerIndex, speakerName) {
    const entry = ensureTurn(turnId, "", null, speakerIndex, speakerName);
    if (entry.spans.has(seq)) return;
    const span = document.createElement("span");
    span.textContent = text;
    entry.body.appendChild(span);
    entry.spans.set(seq, span);
    const atBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 160;
    if (atBottom) log.scrollTop = log.scrollHeight;
  }

  function dropFrom(fromTurn) {
    for (let i = queue.length - 1; i >= 0; i--) {
      if (queue[i].turn_id >= fromTurn) queue.splice(i, 1);
    }
    turns.forEach((entry, id) => {
      if (id >= fromTurn) entry.el.classList.add("dropped");
    });
    preloadNext();
  }

  function clearLog() {
    log.querySelectorAll(".turn, .usermsg").forEach((el) => el.remove());
    turns = new Map();
    $("empty").hidden = false;
  }

  function addUserMessage(text, interjected) {
    $("empty").hidden = true;
    const el = document.createElement("div");
    el.className = "usermsg";
    const who = document.createElement("span");
    who.className = "who";
    who.textContent = interjected ? "割り込み" : "話題";
    const body = document.createElement("span");
    body.textContent = text;
    el.append(who, body);
    log.appendChild(el);
    log.scrollTop = log.scrollHeight;
  }

  function showTiming(turnId, t) {
    const entry = turns.get(turnId);
    if (!entry) return;
    const el = entry.el.querySelector(".turn-time");
    if (!el) return;
    const bits = [];
    if (t.search) bits.push(`検索 ${t.search}s`);
    if (t.llm) bits.push(`文章 ${t.llm}s`);
    else if (t.llm_first) bits.push(`初出 ${t.llm_first}s`);
    if (t.tts) bits.push(`音声 ${t.tts}s`);
    el.textContent = bits.join(" / ");
  }

  // ---- server events ------------------------------------------------------

  function connect() {
    const es = new EventSource("/events");
    es.onmessage = (ev) => {
      let msg;
      try { msg = JSON.parse(ev.data); } catch { return; }
      handle(msg);
    };
    es.onerror = () => {
      es.close();
      setTimeout(connect, 2000);
    };
  }

  function handle(msg) {
    switch (msg.type) {
      case "state":
        $("chip-status").textContent = msg.status;
        $("chip-status").classList.toggle("live", msg.running);
        $("btn-resume").disabled = !msg.can_resume;
        $("chip-mode").textContent =
          msg.speaker_mode === "duo" ? `2人 (${(msg.speakers || []).join(" / ")})` : "1人";
        $("chip-buffer").textContent = `buffer ${msg.buffered_seconds}s`;
        $("chip-turns").textContent = `先読み ${msg.turns_ahead}`;
        $("chip-queue").textContent = `TTS ${msg.tts_queue}`;
        break;
      case "turn_start":
        ensureTurn(msg.turn_id, msg.subtopic, msg.sources,
                   msg.speaker_index, msg.speaker_name);
        break;
      case "sentence":
        addSentence(msg.turn_id, msg.seq, msg.text, msg.speaker_index, msg.speaker_name);
        break;
      case "audio":
        // Backfill in case this clip's `sentence` event predates our connection.
        addSentence(msg.turn_id, msg.seq, msg.text, msg.speaker_index, msg.speaker_name);
        queue.push(msg);
        if (!playing) playNext(); else preloadNext();
        break;
      case "drop":
        dropFrom(msg.from_turn);
        break;
      case "clear":
        clearLog();
        break;
      case "user_message":
        addUserMessage(msg.text, msg.interjected);
        break;
      case "turn_timing":
        showTiming(msg.turn_id, msg);
        break;
      case "error":
        console.warn("engine error:", msg.message);
        break;
    }
  }

  // ---- controls -----------------------------------------------------------

  $("composer").addEventListener("submit", (e) => {
    e.preventDefault();
    const input = $("topic");
    const topic = input.value.trim();
    if (!topic) return;
    input.value = "";
    // Unlock autoplay: this click is the user gesture the browser wants.
    player.play().catch(() => {});
    player.pause();
    post("/api/topic", { topic });
  });

  $("btn-stop").addEventListener("click", () => {
    post("/api/stop", {});
    queue.length = 0;
    player.pause();
    player.removeAttribute("src");
    playing = false;
    current = null;
    $("now").hidden = true;
    markSpeaking(null);
  });

  // Picks the last topic back up, continuing rather than restarting it.
  $("btn-resume").addEventListener("click", async () => {
    player.play().catch(() => {});
    player.pause();
    const resp = await fetch("/api/resume", { method: "POST" });
    if (!resp.ok) {
      const data = await resp.json().catch(() => ({}));
      console.warn("resume failed", data.detail || resp.status);
    }
  });

  // Clears the log only — speech keeps going.
  $("btn-clear").addEventListener("click", () => post("/api/clear", {}));

  // ---- settings panel -----------------------------------------------------

  function speakerCard(index, sp) {
    const card = document.createElement("div");
    card.className = "speaker-card";
    card.dataset.index = String(index);

    const colour = index === 0 ? "var(--accent)" : "#b79cf0";
    card.innerHTML = `
      <h4><span class="swatch" style="background:${colour}"></span>話者${index + 1}</h4>
      <label>名前<input class="sp-name" type="text"></label>
      <label>声の指定方法
        <select class="sp-mode">
          <option value="reference">参照音声（クローン）</option>
          <option value="caption">テキストで指定（VoiceDesign）</option>
        </select>
      </label>
      <label class="sp-wrap-voice">参照音声<select class="sp-voice"></select></label>
      <label class="sp-wrap-caption">声の説明テキスト<textarea class="sp-caption" rows="3"></textarea></label>
      <label>ペルソナ（システムプロンプト）<textarea class="sp-persona" rows="10"></textarea></label>
    `;

    card.querySelector(".sp-name").value = sp.name || "";
    card.querySelector(".sp-mode").value = sp.voice_mode || "reference";
    card.querySelector(".sp-caption").value = sp.voice_caption || "";
    card.querySelector(".sp-persona").value = sp.persona || "";
    fillVoiceSelect(card.querySelector(".sp-voice"), sp.voice_id);

    const sync = () => {
      const caption = card.querySelector(".sp-mode").value === "caption";
      card.querySelector(".sp-wrap-voice").hidden = caption;
      card.querySelector(".sp-wrap-caption").hidden = !caption;
    };
    card.querySelector(".sp-mode").addEventListener("change", sync);
    sync();
    return card;
  }

  function fillVoiceSelect(select, selected) {
    const previous = selected || select.value;
    select.innerHTML = "";
    const none = document.createElement("option");
    none.value = "none";
    none.textContent = "none（参照音声なし）";
    select.appendChild(none);
    voiceCache.forEach((v) => {
      const o = document.createElement("option");
      o.value = v.id;
      o.textContent = v.label || v.id;
      select.appendChild(o);
    });
    select.value = previous;
    if (!select.value) select.value = "none";
  }

  // ---- TTS endpoints ------------------------------------------------------

  function ttsRow(ep) {
    const row = document.createElement("div");
    row.className = "tts-row" + (ep.enabled === false ? " off" : "");
    row.innerHTML = `
      <input class="tts-url" type="text" placeholder="http://127.0.0.1:8088">
      <select class="tts-kind">
        <option value="reference">参照音声</option>
        <option value="caption">テキスト指定</option>
      </select>
      <button class="tts-test" type="button">接続</button>
      <button class="tts-del" type="button">削除</button>
      <div class="tts-status"></div>
    `;
    row.querySelector(".tts-url").value = ep.url || "";
    row.querySelector(".tts-kind").value = ep.kind || "reference";
    row.querySelector(".tts-del").addEventListener("click", () => {
      row.remove();
      updateTtsNote();
    });
    row.querySelector(".tts-test").addEventListener("click", () => testEndpoint(row));
    return row;
  }

  async function testEndpoint(row) {
    const url = row.querySelector(".tts-url").value.trim();
    const status = row.querySelector(".tts-status");
    if (!url) { status.textContent = "URLを入力してください"; status.className = "tts-status ng"; return; }
    status.textContent = "確認中…";
    status.className = "tts-status";
    try {
      const data = await fetch("/api/tts/probe", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url }),
      }).then((r) => r.json());
      if (!data.ok) {
        status.textContent = `つながりません: ${data.error || data.detail || "不明"}`;
        status.className = "tts-status ng";
        return;
      }
      // The server reports its checkpoint, which is what decides the kind.
      row.querySelector(".tts-kind").value = data.kind;
      row.querySelector(".tts-url").value = data.url;
      const label = data.kind === "caption" ? "テキスト指定" : "参照音声";
      status.textContent = `OK  ${data.checkpoint}（${label} / ${data.device || "?"}）`;
      status.className = "tts-status ok";
      updateTtsNote();
    } catch (e) {
      status.textContent = `失敗: ${e}`;
      status.className = "tts-status ng";
    }
  }

  function collectEndpoints() {
    // Scoped to the endpoint list: other panels have their own address rows.
    return [...document.querySelectorAll("#tts-list .tts-row")]
      .map((row) => ({
        url: row.querySelector(".tts-url").value.trim(),
        kind: row.querySelector(".tts-kind").value,
        enabled: !row.classList.contains("off"),
      }))
      .filter((e) => e.url);
  }

  const KIND_LABEL = { caption: "テキスト指定", reference: "参照音声" };

  function updateTtsNote() {
    const eps = collectEndpoints();
    const kinds = [...new Set(eps.map((e) => e.kind))];
    const note = $("tts-note");
    if (eps.length === 0) {
      note.textContent = "TTSサーバーがありません。音声は生成されません。";
    } else {
      const names = kinds.map((k) => KIND_LABEL[k]).join(" / ");
      note.textContent =
        kinds.length >= 2
          ? `${eps.length}台（${names}）。参照音声とテキスト指定を混ぜた2人構成にできます。`
          : `${eps.length}台（${names}）。2人とも${names}にすれば2人交互にできます。`;
    }
    syncSpeakerMode();
  }

  function renderEndpoints(cfg) {
    const host = $("tts-list");
    host.innerHTML = "";
    (cfg.tts_endpoints || []).forEach((ep) => host.appendChild(ttsRow(ep)));
    updateTtsNote();
  }

  function fillModels(models, selected) {
    const sel = $("set-model");
    sel.innerHTML = "";
    (models || []).forEach((m) => {
      const o = document.createElement("option");
      o.value = m.id;
      // Only a model LM Studio already holds is guaranteed to be selectable;
      // loading another large one can fail on insufficient memory.
      o.textContent = m.loaded ? `${m.id}（ロード済み）` : m.id;
      sel.appendChild(o);
    });
    if (selected) sel.value = selected;
    if (!sel.value && sel.options.length) sel.selectedIndex = 0;
  }

  // ---- SearXNG / MCP connection tests -------------------------------------

  async function probeInto(path, inputId, statusId, describe) {
    const input = $(inputId);
    const status = $(statusId);
    const url = input.value.trim();
    if (!url) { status.textContent = "URLを入力してください"; status.className = "tts-status ng"; return; }
    status.textContent = "確認中…";
    status.className = "tts-status";
    try {
      const data = await fetch(path, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url }),
      }).then((r) => r.json());
      // Show the address the server actually used, so a typed "host:port"
      // becomes the "http://host:port" that gets saved.
      if (data.url) input.value = data.url;
      if (!data.ok) {
        status.textContent = `つながりません: ${data.error || data.detail || "不明"}`;
        status.className = "tts-status ng";
        return;
      }
      status.textContent = describe(data);
      status.className = "tts-status ok";
    } catch (e) {
      status.textContent = `失敗: ${e}`;
      status.className = "tts-status ng";
    }
  }

  $("btn-test-llm").addEventListener("click", async () => {
    const kept = $("set-model").value;
    await probeInto("/api/llm/probe", "set-llm-url", "llm-status", (d) => {
      fillModels(d.models, kept);
      return `OK  ${d.models.length} モデル`;
    });
  });

  $("btn-test-search").addEventListener("click", () =>
    probeInto("/api/search/probe", "set-searxng", "search-status",
      (d) => `OK  検索結果 ${d.results} 件`));

  $("btn-test-mcp").addEventListener("click", () =>
    probeInto("/api/mcp/probe", "set-mcp-url", "mcp-status",
      (d) => d.has_search
        ? `OK  ${d.server} ${d.version}（ツール: ${d.tools.join(", ")}）`
        : `つながりましたが memory_search がありません（${d.tools.join(", ")}）`));

  $("btn-add-tts").addEventListener("click", () => {
    $("tts-list").appendChild(ttsRow({ url: "", kind: "reference", enabled: true }));
    updateTtsNote();
  });

  function renderSpeakers(cfg) {
    const host = $("speakers");
    host.innerHTML = "";
    (cfg.speakers || []).forEach((sp, i) => host.appendChild(speakerCard(i, sp)));
    syncSpeakerMode();
  }

  function syncSpeakerMode() {
    // Duo works on a single server too, as long as both speakers use the kind
    // that server provides; only a mix of the two kinds needs a second one.
    const select = $("set-speakermode");
    select.disabled = collectEndpoints().length === 0;
    const duo = select.value === "duo" && !select.disabled;
    document.querySelectorAll(".speaker-card").forEach((card, i) => {
      card.classList.toggle("dimmed", i > 0 && !duo);
    });
  }
  $("set-speakermode").addEventListener("change", syncSpeakerMode);

  function renderVoices() {
    const host = $("voice-list");
    host.innerHTML = "";
    if (!voiceCache.length) {
      host.innerHTML = '<div class="hint">参照音声がまだありません。</div>';
      return;
    }
    voiceCache.forEach((v) => {
      const row = document.createElement("div");
      row.className = "voice-row";
      const name = document.createElement("span");
      name.className = "vname";
      name.textContent = v.label || v.id;
      const meta = document.createElement("span");
      meta.className = "vmeta";
      // The id is what the TTS server knows it as; surface it when a display
      // name has been set on top.
      meta.textContent = (v.label && v.label !== v.id) ? v.id : "";
      const ren = document.createElement("button");
      ren.type = "button";
      ren.className = "vrename";
      ren.textContent = "名前変更";
      ren.addEventListener("click", () => renameVoice(v.id));
      const del = document.createElement("button");
      del.type = "button";
      del.textContent = "削除";
      del.addEventListener("click", () => deleteVoice(v.id));
      row.append(name, meta, ren, del);
      host.appendChild(row);
    });
  }

  async function refreshVoices(selectedById) {
    const data = await fetch("/api/voices").then((r) => r.json());
    voiceCache = data.voices || [];
    renderVoices();
    document.querySelectorAll(".speaker-card").forEach((card, i) => {
      fillVoiceSelect(
        card.querySelector(".sp-voice"),
        (selectedById && selectedById[i]) || card.querySelector(".sp-voice").value
      );
    });
  }

  function refreshVoiceSelects() {
    renderVoices();
    document.querySelectorAll(".speaker-card").forEach((card) =>
      fillVoiceSelect(card.querySelector(".sp-voice"), card.querySelector(".sp-voice").value));
  }

  async function renameVoice(id) {
    const current = (voiceCache.find((v) => v.id === id) || {}).label || id;
    const name = prompt(`「${current}」の新しい表示名（日本語も使えます）`, current);
    if (!name || name === current) return;
    const resp = await fetch(`/api/voices/${encodeURIComponent(id)}/rename`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
    });
    const data = await resp.json();
    if (!resp.ok) {
      $("voice-msg").textContent = `名前を変えられませんでした: ${data.detail || resp.status}`;
      return;
    }
    voiceCache = data.voices || [];
    refreshVoiceSelects();
    // The server repoints any speaker that used the old name.
    document.querySelectorAll(".speaker-card").forEach((card) => {
      const sel = card.querySelector(".sp-voice");
      if (sel.dataset.was === id) sel.value = data.id;
    });
    $("voice-msg").textContent = `名前を変えました: ${id} → ${data.label}`;
  }

  async function deleteVoice(id) {
    if (!confirm(`参照音声「${id}」を削除します。よろしいですか？`)) return;
    const resp = await fetch(`/api/voices/${encodeURIComponent(id)}`, { method: "DELETE" });
    const data = await resp.json();
    if (!resp.ok) {
      $("voice-msg").textContent = `削除できませんでした: ${data.detail || resp.status}`;
      return;
    }
    voiceCache = data.voices || [];
    renderVoices();
    document.querySelectorAll(".speaker-card").forEach((card) =>
      fillVoiceSelect(card.querySelector(".sp-voice"), card.querySelector(".sp-voice").value));
    $("voice-msg").textContent = (data.in_use_by || []).length
      ? `削除しました。${data.in_use_by.join("、")}がこの音声を使っていたので、別の音声を選び直してください。`
      : `削除しました: ${data.deleted}`;
  }

  $("voice-file").addEventListener("change", async (e) => {
    const file = e.target.files && e.target.files[0];
    if (!file) return;
    const btn = document.querySelector(".upload-btn");
    const label = btn.textContent;
    btn.textContent = "アップロード中…";
    btn.classList.add("busy");
    $("voice-msg").textContent = `${file.name} を処理しています`;
    const form = new FormData();
    form.append("file", file);
    form.append("name", $("voice-name").value.trim());
    form.append("start", $("voice-start").value || "0");
    form.append("max_seconds", $("voice-len").value || "15");
    try {
      const resp = await fetch("/api/voices", { method: "POST", body: form });
      const data = await resp.json();
      if (!resp.ok) {
        $("voice-msg").textContent = `失敗: ${data.detail || resp.status}`;
        return;
      }
      voiceCache = data.voices || [];
      refreshVoiceSelects();
      $("voice-name").value = "";
      const renamed = data.voice.original && !data.voice.original.startsWith(data.voice.id);
      $("voice-msg").textContent = renamed
        // Voice ids must be ASCII, so a Japanese filename gets a generated id.
        ? `追加しました: ${data.voice.original} → ${data.voice.id}（${data.voice.duration}秒に調整）`
        : `追加しました: ${data.voice.id}（${data.voice.duration}秒に調整）`;
    } catch (err) {
      $("voice-msg").textContent = `失敗: ${err}`;
    } finally {
      btn.textContent = label;
      btn.classList.remove("busy");
      e.target.value = "";
    }
  });

  $("btn-settings").addEventListener("click", async () => {
    $("panel").hidden = false;
    const [cfg, opt] = await Promise.all([
      fetch("/api/settings").then((r) => r.json()),
      fetch("/api/options").then((r) => r.json()),
    ]);
    settingsCache = cfg;
    voiceCache = opt.voices || [];

    fillModels(opt.models, cfg.llm_model);
    $("set-llm-url").value = cfg.llm_url || "";
    $("llm-status").textContent = "";

    $("set-speakermode").value = cfg.speaker_mode;
    renderSpeakers(cfg);
    renderEndpoints(cfg);
    renderVoices();
    $("save-msg").textContent = "";

    $("set-search-on").checked = cfg.search_enabled;
    $("set-searxng").value = cfg.searxng_url || "";
    $("set-mcp-on").checked = cfg.mcp_enabled;
    $("set-mcp-url").value = cfg.mcp_url || "";
    $("search-status").textContent = "";
    $("mcp-status").textContent = "";

    $("set-steps").value = cfg.num_steps;
    $("set-speed").value = cfg.speed;
    $("set-turns").value = cfg.prefetch_turns;
    $("set-secs").value = cfg.prefetch_seconds;
    $("set-temp").value = cfg.temperature;
    $("voice-len").value = cfg.ref_target_seconds;
    $("voice-msg").textContent = "";

    $("health").innerHTML = Object.entries(opt.tts_health || {})
      .map(([url, up]) =>
        `<div><span class="${up ? "up" : "down"}">${up ? "●" : "×"}</span> ${url}</div>`)
      .join("");
  });

  $("btn-close").addEventListener("click", () => { $("panel").hidden = true; });

  $("btn-save").addEventListener("click", async () => {
    const speakers = [...document.querySelectorAll(".speaker-card")].map((card) => ({
      name: card.querySelector(".sp-name").value,
      persona: card.querySelector(".sp-persona").value,
      voice_mode: card.querySelector(".sp-mode").value,
      voice_id: card.querySelector(".sp-voice").value,
      voice_caption: card.querySelector(".sp-caption").value,
    }));
    const resp = await fetch("/api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        llm_url: $("set-llm-url").value.trim(),
        llm_model: $("set-model").value,
        speaker_mode: $("set-speakermode").value,
        speakers,
        tts_endpoints: collectEndpoints(),
        search_enabled: $("set-search-on").checked,
        searxng_url: $("set-searxng").value.trim(),
        mcp_enabled: $("set-mcp-on").checked,
        mcp_url: $("set-mcp-url").value.trim(),
        ref_target_seconds: Number($("voice-len").value || 15),
        num_steps: Number($("set-steps").value),
        speed: Number($("set-speed").value),
        prefetch_turns: Number($("set-turns").value),
        prefetch_seconds: Number($("set-secs").value),
        temperature: Number($("set-temp").value),
      }),
    });
    const cfg = await resp.json();

    // The server may have corrected the setup (e.g. forced solo on one TTS);
    // keep the panel open so the reason is actually read.
    if (cfg.warnings && cfg.warnings.length) {
      $("save-msg").textContent = cfg.warnings.join(" / ");
      $("set-speakermode").value = cfg.speaker_mode;
      renderSpeakers(cfg);
      renderEndpoints(cfg);
      return;
    }
    $("panel").hidden = true;
  });

  // The log starts empty on every load; the SSE stream fills it from here on.
  connect();
})();
