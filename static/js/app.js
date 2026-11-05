(function(){
  const NOTE_INDEX = {
    "C":0,"B#":0,"C#":1,"Db":1,"D":2,"D#":3,"Eb":3,"E":4,"Fb":4,
    "F":5,"E#":5,"F#":6,"Gb":6,"G":7,"G#":8,"Ab":8,"A":9,"A#":10,"Bb":10,"B":11,"Cb":11
  };
  const NOTE_SHARP = ["C","C#","D","D#","E","F","F#","G","G#","A","A#","B"];
  const INDEX_KEY = "offsong:setlists:index";
  const FONT_MIN = -8;  // points relative to the default song font size
  const FONT_MAX = 24;
  const FILE_PREFIX = "offsong:setlist:file:";

  const urlParams = new URLSearchParams(window.location.search);
  let sel = urlParams.get("path");
  let transpose = parseInt(urlParams.get("transpose") || "0", 10) || 0;
  let currentBaseKey = (window.OFFSONG && window.OFFSONG.baseKey) || "";
  let activeSetlistId = null; // stable id of setlist being edited/viewed
  let activeSetlistSongIndex = null; // index within setlist when opened from there
  let lastSetlistSongIndex = null; // remembered across Songs-tab browsing
  let songLoadId = 0; // ignores stale openSong/meta callbacks
  let renderId = 0; // ignores stale /render responses
  let fontOffset = 0; // points above/below default; persisted per setlist song only
  let twoPane = false; // two-column song view; persisted per setlist song only

  const btnDown = document.getElementById("down");
  const btnUp = document.getElementById("up");
  const songEl = document.getElementById("song");
  const mainEl = songEl.closest("main");
  const twoPaneBtn = document.getElementById("two-pane-toggle");
  const fileLinks = document.querySelectorAll("#panel-songs .file");
  let es = null;

  // ---- YAML codec for setlist files: name / date / note / songs[{path,key,note,font_offset,two_pane}] ----
  function escapeYamlString(s) {
    if (s === "" || /[:#\n\r\t{}[\],&*!|>'"%@`_]/.test(s) || /^\s|\s$/.test(s)) {
      return JSON.stringify(s);
    }
    return s;
  }

  function parseYamlScalar(raw) {
    const s = (raw || "").trim();
    if ((s.startsWith('"') && s.endsWith('"')) || (s.startsWith("'") && s.endsWith("'"))) {
      try { return JSON.parse(s.replace(/^'/, '"').replace(/'$/, '"')); } catch (_) { return s.slice(1, -1); }
    }
    return s;
  }

  function parseFontOffset(v) {
    const n = parseInt(v, 10);
    if (!Number.isFinite(n)) return 0;
    return Math.max(FONT_MIN, Math.min(FONT_MAX, n));
  }

  function todayISO() {
    const d = new Date();
    const yyyy = d.getFullYear();
    const mm = String(d.getMonth() + 1).padStart(2, "0");
    const dd = String(d.getDate()).padStart(2, "0");
    return `${yyyy}-${mm}-${dd}`;
  }

  function emptySetlist(name, date) {
    return { name: name || "Untitled", date: date || todayISO(), note: "", songs: [] };
  }

  function setlistToYaml(doc) {
    const songs = Array.isArray(doc.songs) ? doc.songs : [];
    let out = `name: ${escapeYamlString(doc.name || "")}\n`;
    out += `date: ${escapeYamlString(doc.date || "")}\n`;
    out += `note: ${escapeYamlString(doc.note || "")}\n`;
    out += "songs:\n";
    if (!songs.length) {
      out += "  []\n";
      return out;
    }
    for (const song of songs) {
      out += `  - path: ${escapeYamlString(song.path || "")}\n`;
      out += `    key: ${escapeYamlString(song.key || "")}\n`;
      out += `    note: ${escapeYamlString(song.note || "")}\n`;
      out += `    font_offset: ${parseFontOffset(song.font_offset)}\n`;
      out += `    two_pane: ${song.two_pane ? "true" : "false"}\n`;
    }
    return out;
  }

  function yamlToSetlist(text, fallbackName) {
    const doc = emptySetlist(fallbackName || "Untitled", "");
    const songs = [];
    let current = null;
    let inSongs = false;
    const lines = String(text || "").split(/\r?\n/);

    // Legacy format: bare list of "- path:" at column 0
    const legacy = /^\s*-\s+path\s*:/.test(text || "") && !/^\s*name\s*:/m.test(text || "");
    if (legacy) {
      for (const line of lines) {
        if (!line.trim() || line.trim().startsWith("#")) continue;
        const item = line.match(/^\s*-\s+path\s*:\s*(.*)$/);
        if (item) {
          if (current) songs.push(current);
          current = { path: parseYamlScalar(item[1]), key: "", note: "", font_offset: 0, two_pane: false };
          continue;
        }
        const keyLine = line.match(/^\s+key\s*:\s*(.*)$/);
        if (keyLine && current) current.key = parseYamlScalar(keyLine[1]);
        const noteLine = line.match(/^\s+note\s*:\s*(.*)$/);
        if (noteLine && current) current.note = parseYamlScalar(noteLine[1]);
        const fontLine = line.match(/^\s+font_offset\s*:\s*(.*)$/);
        if (fontLine && current) current.font_offset = parseFontOffset(parseYamlScalar(fontLine[1]));
        const twoPaneLine = line.match(/^\s+two_pane\s*:\s*(.*)$/);
        if (twoPaneLine && current) current.two_pane = parseYamlScalar(twoPaneLine[1]) === "true";
      }
      if (current) songs.push(current);
      doc.songs = songs.filter(s => s.path);
      doc.date = doc.date || todayISO();
      return doc;
    }

    for (const line of lines) {
      if (!line.trim() || line.trim().startsWith("#")) continue;
      const nameLine = line.match(/^\s*name\s*:\s*(.*)$/);
      if (nameLine && !inSongs) {
        doc.name = parseYamlScalar(nameLine[1]);
        continue;
      }
      const dateLine = line.match(/^\s*date\s*:\s*(.*)$/);
      if (dateLine && !inSongs) {
        doc.date = parseYamlScalar(dateLine[1]);
        continue;
      }
      const topNote = line.match(/^\s*note\s*:\s*(.*)$/);
      if (topNote && !inSongs) {
        doc.note = parseYamlScalar(topNote[1]);
        continue;
      }
      if (/^\s*songs\s*:\s*$/.test(line)) {
        inSongs = true;
        continue;
      }
      if (/^\s*songs\s*:\s*\[\s*\]\s*$/.test(line)) {
        inSongs = true;
        continue;
      }
      if (!inSongs) continue;
      if (/^\s*\[\s*\]\s*$/.test(line)) continue;

      const item = line.match(/^\s*-\s+path\s*:\s*(.*)$/);
      if (item) {
        if (current) songs.push(current);
        current = { path: parseYamlScalar(item[1]), key: "", note: "", font_offset: 0, two_pane: false };
        continue;
      }
      const keyLine = line.match(/^\s+key\s*:\s*(.*)$/);
      if (keyLine && current) {
        current.key = parseYamlScalar(keyLine[1]);
        continue;
      }
      const noteLine = line.match(/^\s+note\s*:\s*(.*)$/);
      if (noteLine && current) {
        current.note = parseYamlScalar(noteLine[1]);
        continue;
      }
      const fontLine = line.match(/^\s+font_offset\s*:\s*(.*)$/);
      if (fontLine && current) {
        current.font_offset = parseFontOffset(parseYamlScalar(fontLine[1]));
        continue;
      }
      const twoPaneLine = line.match(/^\s+two_pane\s*:\s*(.*)$/);
      if (twoPaneLine && current) {
        current.two_pane = parseYamlScalar(twoPaneLine[1]) === "true";
      }
    }
    if (current) songs.push(current);
    doc.songs = songs.filter(s => s.path);
    if (!doc.date) doc.date = todayISO();
    if (doc.note == null) doc.note = "";
    return doc;
  }

  // ---- Client-side setlist store: one YAML file per setlist id ----
  function listSetlistIds() {
    try {
      const raw = localStorage.getItem(INDEX_KEY);
      const ids = raw ? JSON.parse(raw) : [];
      return Array.isArray(ids) ? ids : [];
    } catch (_) {
      return [];
    }
  }

  function saveSetlistIds(ids) {
    localStorage.setItem(INDEX_KEY, JSON.stringify(ids));
  }

  function setlistFileKey(id) {
    return FILE_PREFIX + id;
  }

  function newSetlistId() {
    return "sl_" + Date.now().toString(36) + "_" + Math.random().toString(36).slice(2, 8);
  }

  function loadSetlist(id) {
    const yaml = localStorage.getItem(setlistFileKey(id));
    if (yaml == null) return emptySetlist("Untitled", todayISO());
    return yamlToSetlist(yaml, id);
  }

  function saveSetlist(id, doc) {
    const normalized = {
      name: (doc.name || "Untitled").trim() || "Untitled",
      date: doc.date || todayISO(),
      note: doc.note || "",
      songs: Array.isArray(doc.songs) ? doc.songs.map(s => ({
        path: s.path || "",
        key: s.key || "",
        note: s.note || "",
        font_offset: parseFontOffset(s.font_offset),
        two_pane: !!s.two_pane
      })) : []
    };
    localStorage.setItem(setlistFileKey(id), setlistToYaml(normalized));
    const ids = listSetlistIds();
    if (!ids.includes(id)) {
      ids.push(id);
      saveSetlistIds(ids);
    }
    return normalized;
  }

  function deleteSetlist(id) {
    localStorage.removeItem(setlistFileKey(id));
    saveSetlistIds(listSetlistIds().filter(x => x !== id));
  }

  function migrateLegacyNameKeyedSetlists() {
    // Older builds indexed by name and stored a bare song list YAML under that name.
    const ids = listSetlistIds();
    let changed = false;
    const nextIds = [];
    for (const key of ids) {
      if (String(key).startsWith("sl_")) {
        nextIds.push(key);
        continue;
      }
      const yaml = localStorage.getItem(setlistFileKey(key));
      if (yaml == null) { changed = true; continue; }
      const doc = yamlToSetlist(yaml, key);
      if (!doc.name || doc.name === key) doc.name = key;
      const id = newSetlistId();
      localStorage.setItem(setlistFileKey(id), setlistToYaml(doc));
      localStorage.removeItem(setlistFileKey(key));
      nextIds.push(id);
      changed = true;
    }
    if (changed) saveSetlistIds(nextIds);
  }

  function basename(path) {
    const parts = path.split("/");
    return parts[parts.length - 1] || path;
  }

  function noteIndex(note) {
    if (!note) return null;
    const m = String(note).trim().match(/^([A-G](?:#|b)?)/);
    if (!m) return null;
    const idx = NOTE_INDEX[m[1]];
    return (idx === undefined) ? null : idx;
  }

  function transposeAmount(fromKey, toKey) {
    const a = noteIndex(fromKey);
    const b = noteIndex(toKey);
    if (a === null || b === null) return 0;
    let d = b - a;
    if (d > 6) d -= 12;
    if (d < -6) d += 12;
    return d;
  }

  function applyTransposeToKey(baseKey, semitones) {
    const a = noteIndex(baseKey);
    if (a === null) return null;
    return NOTE_SHARP[((a + semitones) % 12 + 12) % 12];
  }

  function fetchMeta(path) {
    return fetch(`/meta?path=${encodeURIComponent(path)}`)
      .then(r => { if (!r.ok) throw new Error("meta " + r.status); return r.json(); });
  }

  function render(){
    if (!sel) {
      songEl.innerHTML = '<div class="empty">Pick a file from the left.</div>';
      return;
    }
    const id = ++renderId;
    const t = Number(transpose) || 0;
    fetch(`/render?path=${encodeURIComponent(sel)}&transpose=${t}`)
      .then(r => r.text())
      .then(html => {
        if (id !== renderId) return;
        songEl.innerHTML = html;
      })
      .catch(err => {
        if (id !== renderId) return;
        songEl.innerHTML = `<div class="empty">Error: ${err}</div>`;
      });
  }

  function startSSE(){
    if (!sel) return;
    if (es) es.close();
    es = new EventSource(`/events?path=${encodeURIComponent(sel)}`);
    es.onmessage = (ev) => {
      if (ev.data === "reload") { render(); }
    };
    es.addEventListener("ping", () => {});
    es.onerror = () => {};
  }

  function updateLocation() {
    const url = new URL(window.location);
    url.searchParams.set("transpose", String(Number(transpose) || 0));
    if (sel) url.searchParams.set("path", sel);
    history.replaceState(null, "", url.toString());
  }

  function setActiveSongLink(path) {
    fileLinks.forEach(l => l.classList.toggle("active", l.getAttribute("data-path") === path));
  }

  function openSong(path, desiredKey, setlistSongIndex) {
    sel = path;
    activeSetlistSongIndex = (setlistSongIndex == null) ? null : setlistSongIndex;
    if (setlistSongIndex != null) lastSetlistSongIndex = setlistSongIndex;
    let font = 0;
    let twoPane = false;
    if (setlistSongIndex != null && activeSetlistId) {
      const song = loadSetlist(activeSetlistId).songs[setlistSongIndex];
      if (song && song.path === path) {
        font = song.font_offset;
        twoPane = song.two_pane;
      }
    }
    setFontOffset(font);
    setTwoPane(twoPane);
    setActiveSongLink(path);
    startSSE();
    syncNotesPane();
    const loadId = ++songLoadId;
    fetchMeta(path).then(meta => {
      if (path !== sel) return;
      currentBaseKey = meta.base_key || null;
      // If the user already transposed (or opened another song), keep their transpose
      if (loadId !== songLoadId) {
        syncNotesPane();
        return;
      }
      if (desiredKey && currentBaseKey) {
        transpose = transposeAmount(currentBaseKey, desiredKey);
      } else {
        // Songs tab, or setlist song with no mappable base key
        transpose = 0;
      }
      updateLocation();
      render();
      renderSetlistDetail();
      syncNotesPane();
    }).catch(() => {
      if (path !== sel || loadId !== songLoadId) return;
      currentBaseKey = null;
      transpose = 0;
      updateLocation();
      render();
      syncNotesPane();
    });
  }

  function setlistSongIndexForPersist() {
    if (activeSetlistSongIndex != null) return activeSetlistSongIndex;
    if (lastSetlistSongIndex == null || !activeSetlistId || !sel) return null;
    const doc = loadSetlist(activeSetlistId);
    const song = doc.songs[lastSetlistSongIndex];
    if (song && song.path === sel) return lastSetlistSongIndex;
    return null;
  }

  function persistActiveSetlistKeyFromTranspose() {
    const idx = setlistSongIndexForPersist();
    if (!activeSetlistId || idx == null || !sel) return;

    const apply = (baseKey) => {
      if (!baseKey) return;
      const newKey = applyTransposeToKey(baseKey, Number(transpose) || 0);
      if (!newKey) return;
      const doc = loadSetlist(activeSetlistId);
      if (!doc.songs[idx] || doc.songs[idx].path !== sel) return;
      doc.songs[idx].key = newKey;
      saveSetlist(activeSetlistId, doc);
      activeSetlistSongIndex = idx;
      lastSetlistSongIndex = idx;
      renderSetlistDetail();
    };

    if (currentBaseKey) {
      apply(currentBaseKey);
      return;
    }
    fetchMeta(sel).then(meta => {
      currentBaseKey = meta.base_key || null;
      apply(currentBaseKey);
    }).catch(() => {});
  }

  function bumpTranspose(delta) {
    // Invalidate in-flight openSong so it cannot reset transpose after +/- 
    songLoadId++;
    transpose = (Number(transpose) || 0) + delta;
    updateLocation();
    render();
    persistActiveSetlistKeyFromTranspose();
  }

  function setFontOffset(n) {
    fontOffset = parseFontOffset(n);
    songEl.style.setProperty("--font-offset", String(fontOffset));
  }

  function setTwoPane(on) {
    twoPane = !!on;
    mainEl.classList.toggle("two-pane", twoPane);
    twoPaneBtn.setAttribute("aria-pressed", twoPane ? "true" : "false");
    if (twoPane) mainEl.scrollTop = 0;
  }

  // Only songs opened from a setlist remember view settings; Songs-tab changes are temporary.
  function persistSetlistSongField(field, value) {
    const idx = setlistSongIndexForPersist();
    if (!activeSetlistId || idx == null || !sel) return;
    const doc = loadSetlist(activeSetlistId);
    if (!doc.songs[idx] || doc.songs[idx].path !== sel) return;
    doc.songs[idx][field] = value;
    saveSetlist(activeSetlistId, doc);
  }

  function bumpFont(delta) {
    setFontOffset(fontOffset + delta);
    persistSetlistSongField("font_offset", fontOffset);
  }

  function toggleTwoPane() {
    setTwoPane(!twoPane);
    persistSetlistSongField("two_pane", twoPane);
  }

  // ---- Tabs ----
  function restoreSetlistSelection() {
    if (!activeSetlistId || lastSetlistSongIndex == null) return;
    const doc = loadSetlist(activeSetlistId);
    const song = doc.songs[lastSetlistSongIndex];
    if (!song) return;
    openSong(song.path, song.key, lastSetlistSongIndex);
  }

  document.querySelectorAll(".tab").forEach(tab => {
    tab.addEventListener("click", () => {
      const name = tab.getAttribute("data-tab");
      document.querySelectorAll(".tab").forEach(t => {
        const on = t === tab;
        t.classList.toggle("active", on);
        t.setAttribute("aria-selected", on ? "true" : "false");
      });
      document.querySelectorAll(".panel").forEach(p => {
        p.classList.toggle("active", p.id === "panel-" + name);
      });
      if (name === "setlists") restoreSetlistSelection();
      syncNotesPane();
    });
  });

  // ---- Setlist UI ----
  const setlistsListEl = document.getElementById("setlists-list");
  const setlistIndexView = document.getElementById("setlist-index-view");
  const setlistDetailView = document.getElementById("setlist-detail-view");
  const setlistNameInput = document.getElementById("setlist-name-input");
  const setlistDateInput = document.getElementById("setlist-date-input");
  const setlistSongsEl = document.getElementById("setlist-songs");

  function showSetlistIndex() {
    activeSetlistId = null;
    activeSetlistSongIndex = null;
    lastSetlistSongIndex = null;
    setlistIndexView.hidden = false;
    setlistDetailView.hidden = true;
    renderSetlistIndex();
    syncNotesPane();
  }

  function showSetlistDetail(id) {
    activeSetlistId = id;
    setlistIndexView.hidden = true;
    setlistDetailView.hidden = false;
    const doc = loadSetlist(id);
    setlistNameInput.value = doc.name || "";
    setlistDateInput.value = doc.date || "";
    renderSetlistDetail();
    syncNotesPane();
  }

  // ---- Notes pane (setlist-wide + per-song) ----
  const notesPane = document.getElementById("notes-pane");
  const notesCompact = document.getElementById("notes-compact");
  const compactSetlistNoteBtn = document.getElementById("compact-setlist-note");
  const compactSongNoteBtn = document.getElementById("compact-song-note");
  const setlistNoteCard = document.getElementById("setlist-note-card");
  const songNoteCard = document.getElementById("song-note-card");
  const setlistNoteView = document.getElementById("setlist-note-view");
  const setlistNoteEdit = document.getElementById("setlist-note-edit");
  const setlistNoteHint = document.getElementById("setlist-note-hint");
  const songNoteView = document.getElementById("song-note-view");
  const songNoteEdit = document.getElementById("song-note-edit");
  const songNoteHint = document.getElementById("song-note-hint");
  const toggleSetlistNoteBtn = document.getElementById("toggle-setlist-note-edit");
  const toggleSongNoteBtn = document.getElementById("toggle-song-note-edit");
  let editingSetlistNote = false;
  let editingSongNote = false;
  let editingSongNoteIdx = null;

  function renderInlineMarkdown(src) {
    let s = escapeHtml(String(src || ""));
    s = s.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
    s = s.replace(/__(.+?)__/g, "<em>$1</em>");
    s = s.replace(/`([^`]+)`/g, "<code>$1</code>");
    s = s.replace(/\n/g, "<br>");
    return s;
  }

  function fillNoteView(viewEl, text) {
    viewEl.className = "note-card-body";
    viewEl.innerHTML = renderInlineMarkdown(text);
  }

  function setlistsTabActive() {
    const panel = document.getElementById("panel-setlists");
    return panel && panel.classList.contains("active");
  }

  function findSetlistSongForNotes() {
    if (!activeSetlistId || !sel) return null;
    const doc = loadSetlist(activeSetlistId);
    if (activeSetlistSongIndex != null) {
      const song = doc.songs[activeSetlistSongIndex];
      if (song && song.path === sel) return { doc, song, idx: activeSetlistSongIndex };
    }
    if (lastSetlistSongIndex != null) {
      const song = doc.songs[lastSetlistSongIndex];
      if (song && song.path === sel) return { doc, song, idx: lastSetlistSongIndex };
    }
    const idx = doc.songs.findIndex(s => s.path === sel);
    if (idx >= 0) return { doc, song: doc.songs[idx], idx };
    return null;
  }

  function syncNotesPane() {
    const show = setlistsTabActive() && !!activeSetlistId;
    notesPane.hidden = !show;
    if (!show) {
      editingSetlistNote = false;
      editingSongNote = false;
      editingSongNoteIdx = null;
      notesCompact.hidden = true;
      setlistNoteCard.hidden = true;
      songNoteCard.hidden = true;
      notesPane.classList.remove("is-compact-only");
      return;
    }

    const doc = loadSetlist(activeSetlistId);
    const setlistNoteText = doc.note || "";
    const setlistHasNote = !!setlistNoteText.trim();
    const showSetlistCard = editingSetlistNote || setlistHasNote;
    const showSetlistCompact = !showSetlistCard;

    if (showSetlistCard) {
      setlistNoteCard.hidden = false;
      if (!editingSetlistNote) {
        setlistNoteEdit.hidden = true;
        setlistNoteHint.hidden = true;
        setlistNoteView.hidden = false;
        toggleSetlistNoteBtn.textContent = "Edit";
        fillNoteView(setlistNoteView, setlistNoteText);
      }
    } else {
      setlistNoteCard.hidden = true;
      editingSetlistNote = false;
    }

    const match = findSetlistSongForNotes();
    let showSongCard = false;
    let showSongCompact = false;
    if (!match) {
      songNoteCard.hidden = true;
      editingSongNote = false;
      editingSongNoteIdx = null;
      compactSongNoteBtn.hidden = true;
    } else {
      const songNoteText = match.song.note || "";
      const songHasNote = !!songNoteText.trim();
      showSongCard = editingSongNote || songHasNote;
      showSongCompact = !showSongCard;
      if (showSongCard) {
        songNoteCard.hidden = false;
        if (editingSongNote && editingSongNoteIdx === match.idx) {
          // keep editor open
        } else {
          editingSongNote = false;
          editingSongNoteIdx = null;
          songNoteEdit.hidden = true;
          songNoteHint.hidden = true;
          songNoteView.hidden = false;
          toggleSongNoteBtn.textContent = "Edit";
          fillNoteView(songNoteView, songNoteText);
        }
      } else {
        songNoteCard.hidden = true;
        editingSongNote = false;
        editingSongNoteIdx = null;
      }
      compactSongNoteBtn.hidden = !showSongCompact;
    }

    const anyCompact = showSetlistCompact || showSongCompact;
    compactSetlistNoteBtn.hidden = !showSetlistCompact;
    notesCompact.hidden = !anyCompact;
    notesPane.classList.toggle("is-compact-only", anyCompact && !showSetlistCard && !showSongCard);
  }

  function startEditSetlistNote() {
    if (!activeSetlistId) return;
    const doc = loadSetlist(activeSetlistId);
    editingSetlistNote = true;
    setlistNoteCard.hidden = false;
    compactSetlistNoteBtn.hidden = true;
    notesCompact.hidden = compactSongNoteBtn.hidden;
    notesPane.classList.remove("is-compact-only");
    setlistNoteView.hidden = true;
    setlistNoteEdit.hidden = false;
    setlistNoteHint.hidden = false;
    setlistNoteEdit.value = doc.note || "";
    toggleSetlistNoteBtn.textContent = "Done";
    setlistNoteEdit.focus();
  }

  function finishEditSetlistNote() {
    if (!activeSetlistId || !editingSetlistNote) return;
    const doc = loadSetlist(activeSetlistId);
    doc.note = setlistNoteEdit.value;
    saveSetlist(activeSetlistId, doc);
    editingSetlistNote = false;
    syncNotesPane();
  }

  function startEditSongNote() {
    const match = findSetlistSongForNotes();
    if (!match) return;
    editingSongNote = true;
    editingSongNoteIdx = match.idx;
    songNoteCard.hidden = false;
    compactSongNoteBtn.hidden = true;
    notesCompact.hidden = compactSetlistNoteBtn.hidden;
    notesPane.classList.remove("is-compact-only");
    songNoteView.hidden = true;
    songNoteEdit.hidden = false;
    songNoteHint.hidden = false;
    songNoteEdit.value = match.song.note || "";
    toggleSongNoteBtn.textContent = "Done";
    songNoteEdit.focus();
  }

  function finishEditSongNote() {
    if (!activeSetlistId || !editingSongNote || editingSongNoteIdx == null) return;
    const doc = loadSetlist(activeSetlistId);
    if (!doc.songs[editingSongNoteIdx]) {
      editingSongNote = false;
      editingSongNoteIdx = null;
      syncNotesPane();
      return;
    }
    doc.songs[editingSongNoteIdx].note = songNoteEdit.value;
    saveSetlist(activeSetlistId, doc);
    editingSongNote = false;
    editingSongNoteIdx = null;
    syncNotesPane();
  }

  toggleSetlistNoteBtn.addEventListener("click", () => {
    if (editingSetlistNote) finishEditSetlistNote();
    else startEditSetlistNote();
  });
  toggleSongNoteBtn.addEventListener("click", () => {
    if (editingSongNote) finishEditSongNote();
    else startEditSongNote();
  });
  compactSetlistNoteBtn.addEventListener("click", startEditSetlistNote);
  compactSongNoteBtn.addEventListener("click", startEditSongNote);
  setlistNoteEdit.addEventListener("keydown", (e) => {
    if (e.key === "Escape") finishEditSetlistNote();
  });
  songNoteEdit.addEventListener("keydown", (e) => {
    if (e.key === "Escape") finishEditSongNote();
  });

  function sortedSetlistEntries() {
    return listSetlistIds().map(id => {
      const doc = loadSetlist(id);
      return { id, doc };
    }).sort((a, b) => {
      const da = a.doc.date || "";
      const db = b.doc.date || "";
      if (da !== db) return db.localeCompare(da);
      return (a.doc.name || "").localeCompare(b.doc.name || "");
    });
  }

  function renderSetlistIndex() {
    const entries = sortedSetlistEntries();
    if (!entries.length) {
      setlistsListEl.innerHTML = '<div class="empty">No setlists yet. Create one above.</div>';
      return;
    }
    setlistsListEl.innerHTML = entries.map(({ id, doc }) => {
      const count = (doc.songs || []).length;
      const dateLabel = doc.date || "No date";
      return `<div class="setlist-row" data-id="${escapeAttr(id)}">
        <div class="meta-line">
          <span class="name">${escapeHtml(doc.name || "Untitled")}</span>
          <span class="date">${escapeHtml(dateLabel)}</span>
        </div>
        <span class="hint">${count}</span>
      </div>`;
    }).join("");
    setlistsListEl.querySelectorAll(".setlist-row").forEach(row => {
      row.addEventListener("click", () => showSetlistDetail(row.getAttribute("data-id")));
    });
  }

  function updateActiveSetlistMetaFromInputs() {
    if (!activeSetlistId) return;
    const doc = loadSetlist(activeSetlistId);
    doc.name = (setlistNameInput.value || "").trim() || "Untitled";
    doc.date = setlistDateInput.value || "";
    saveSetlist(activeSetlistId, doc);
  }

  function renderSetlistDetail() {
    if (!activeSetlistId) return;
    const doc = loadSetlist(activeSetlistId);
    const songs = doc.songs || [];
    if (!songs.length) {
      setlistSongsEl.innerHTML = '<div class="empty">No songs in this setlist. Add one below.</div>';
      return;
    }
    setlistSongsEl.innerHTML = songs.map((song, i) => {
      const active = (sel === song.path && activeSetlistSongIndex === i) ? " active" : "";
      const keyLabel = song.key || "—";
      return `<div class="setlist-item${active}" data-index="${i}">
        <span class="name" title="${escapeAttr(song.path)}">${escapeHtml(basename(song.path))}</span>
        <span class="song-key" title="Key for this set">${escapeHtml(keyLabel)}</span>
        <div class="item-actions">
          <button type="button" class="small move-up" title="Move up" ${i === 0 ? "disabled" : ""}>↑</button>
          <button type="button" class="small move-down" title="Move down" ${i === songs.length - 1 ? "disabled" : ""}>↓</button>
          <button type="button" class="small danger remove-song" title="Remove">✕</button>
        </div>
      </div>`;
    }).join("");

    setlistSongsEl.querySelectorAll(".setlist-item").forEach(item => {
      const index = parseInt(item.getAttribute("data-index"), 10);
      const song = songs[index];
      item.querySelector(".name").addEventListener("click", () => {
        openSong(song.path, song.key, index);
      });
      item.querySelector(".song-key").addEventListener("click", () => {
        openSong(song.path, song.key, index);
      });
      item.querySelector(".remove-song").addEventListener("click", () => {
        const next = loadSetlist(activeSetlistId);
        next.songs.splice(index, 1);
        saveSetlist(activeSetlistId, next);
        if (activeSetlistSongIndex === index) activeSetlistSongIndex = null;
        else if (activeSetlistSongIndex != null && activeSetlistSongIndex > index) activeSetlistSongIndex -= 1;
        if (lastSetlistSongIndex === index) lastSetlistSongIndex = null;
        else if (lastSetlistSongIndex != null && lastSetlistSongIndex > index) lastSetlistSongIndex -= 1;
        renderSetlistDetail();
      });
      item.querySelector(".move-up").addEventListener("click", () => {
        if (index <= 0) return;
        const next = loadSetlist(activeSetlistId);
        [next.songs[index - 1], next.songs[index]] = [next.songs[index], next.songs[index - 1]];
        saveSetlist(activeSetlistId, next);
        if (activeSetlistSongIndex === index) activeSetlistSongIndex = index - 1;
        else if (activeSetlistSongIndex === index - 1) activeSetlistSongIndex = index;
        if (lastSetlistSongIndex === index) lastSetlistSongIndex = index - 1;
        else if (lastSetlistSongIndex === index - 1) lastSetlistSongIndex = index;
        renderSetlistDetail();
      });
      item.querySelector(".move-down").addEventListener("click", () => {
        const next = loadSetlist(activeSetlistId);
        if (index >= next.songs.length - 1) return;
        [next.songs[index + 1], next.songs[index]] = [next.songs[index], next.songs[index + 1]];
        saveSetlist(activeSetlistId, next);
        if (activeSetlistSongIndex === index) activeSetlistSongIndex = index + 1;
        else if (activeSetlistSongIndex === index + 1) activeSetlistSongIndex = index;
        if (lastSetlistSongIndex === index) lastSetlistSongIndex = index + 1;
        else if (lastSetlistSongIndex === index + 1) lastSetlistSongIndex = index;
        renderSetlistDetail();
      });
    });
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, c => ({
      "&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"
    })[c]);
  }
  function escapeAttr(s) {
    return escapeHtml(s).replace(/\n/g, "&#10;");
  }

  setlistNameInput.addEventListener("change", updateActiveSetlistMetaFromInputs);
  setlistNameInput.addEventListener("blur", updateActiveSetlistMetaFromInputs);
  setlistDateInput.addEventListener("change", updateActiveSetlistMetaFromInputs);

  document.getElementById("create-setlist").addEventListener("click", () => {
    const input = document.getElementById("new-setlist-name");
    const name = (input.value || "").trim() || "Untitled";
    const id = newSetlistId();
    saveSetlist(id, emptySetlist(name, todayISO()));
    input.value = "";
    showSetlistDetail(id);
  });
  document.getElementById("new-setlist-name").addEventListener("keydown", (e) => {
    if (e.key === "Enter") document.getElementById("create-setlist").click();
  });

  document.getElementById("back-to-setlists").addEventListener("click", () => {
    updateActiveSetlistMetaFromInputs();
    showSetlistIndex();
  });
  document.getElementById("delete-setlist").addEventListener("click", () => {
    if (!activeSetlistId) return;
    const doc = loadSetlist(activeSetlistId);
    if (!confirm(`Delete setlist “${doc.name || "Untitled"}”?`)) return;
    deleteSetlist(activeSetlistId);
    showSetlistIndex();
  });

  function addHighlightedSongToSetlist() {
    if (!activeSetlistId) return;
    const select = document.getElementById("add-song-select");
    const path = select.value;
    if (!path) return;
    fetchMeta(path).then(meta => {
      const doc = loadSetlist(activeSetlistId);
      doc.songs.push({ path, key: meta.base_key || "C", note: "", font_offset: 0, two_pane: false });
      saveSetlist(activeSetlistId, doc);
      select.value = "";
      renderSetlistDetail();
    }).catch(() => {
      const doc = loadSetlist(activeSetlistId);
      doc.songs.push({ path, key: "C", note: "", font_offset: 0, two_pane: false });
      saveSetlist(activeSetlistId, doc);
      select.value = "";
      renderSetlistDetail();
    });
  }

  document.getElementById("add-song-btn").addEventListener("click", addHighlightedSongToSetlist);

  function songOptionLabel(r) {
    if (r.title && r.artist) return `${r.title} — ${r.artist}`;
    if (r.title) return r.title;
    return r.name;
  }

  let addSongSearchId = 0;
  let addSongFilterTimer = null;
  function refreshAddSongOptions(q) {
    const searchId = ++addSongSearchId;
    fetch(`/search?q=${encodeURIComponent(q || "")}`)
      .then(r => { if (!r.ok) throw new Error("search " + r.status); return r.json(); })
      .then(data => {
        if (searchId !== addSongSearchId) return;
        const select = document.getElementById("add-song-select");
        const prev = select.value;
        const results = data.results || [];
        if (!results.length) {
          select.innerHTML = q
            ? '<option value="">No matches</option>'
            : '<option value="">Add a song…</option>';
          return;
        }
        select.innerHTML = '<option value="">Add a song…</option>' + results.map(r =>
          `<option value="${escapeAttr(r.path)}">${escapeHtml(songOptionLabel(r))}</option>`
        ).join("");
        if (prev && Array.from(select.options).some(o => o.value === prev)) {
          select.value = prev;
        } else if (q && results.length === 1) {
          select.value = results[0].path;
        }
      })
      .catch(() => {});
  }

  const addSongFilter = document.getElementById("add-song-filter");
  const addSongSelect = document.getElementById("add-song-select");
  addSongFilter.addEventListener("input", () => {
    clearTimeout(addSongFilterTimer);
    addSongFilterTimer = setTimeout(() => {
      refreshAddSongOptions(addSongFilter.value.trim());
    }, 180);
  });
  addSongFilter.addEventListener("keydown", (e) => {
    if (e.key !== "Enter") return;
    e.preventDefault();
    if (!addSongSelect.value) {
      const first = Array.from(addSongSelect.options).find(o => o.value);
      if (first) addSongSelect.value = first.value;
    }
    if (addSongSelect.value) addHighlightedSongToSetlist();
  });
  addSongSelect.addEventListener("keydown", (e) => {
    if (e.key !== "Enter") return;
    e.preventDefault();
    if (addSongSelect.value) addHighlightedSongToSetlist();
  });
  addSongSelect.addEventListener("dblclick", () => {
    if (addSongSelect.value) addHighlightedSongToSetlist();
  });
  refreshAddSongOptions("");

  if (btnDown) {
    btnDown.addEventListener("click", () => bumpTranspose(-1));
  }
  if (btnUp) {
    btnUp.addEventListener("click", () => bumpTranspose(1));
  }
  twoPaneBtn.addEventListener("click", toggleTwoPane);
  document.getElementById("font-down").addEventListener("click", () => bumpFont(-1));
  document.getElementById("font-up").addEventListener("click", () => bumpFont(1));
  document.getElementById("font-reset").addEventListener("click", () => bumpFont(-fontOffset));

  fileLinks.forEach(link => {
    link.addEventListener("click", (e) => {
      e.preventDefault();
      const path = link.getAttribute("data-path");
      if (!path) return;
      activeSetlistSongIndex = null;
      openSong(path, null, null);
      syncNotesPane();
    });
  });

  migrateLegacyNameKeyedSetlists();
  renderSetlistIndex();
  if (sel) {
    fetchMeta(sel).then(meta => {
      currentBaseKey = meta.base_key || currentBaseKey;
    }).catch(() => {});
  }
  render();
  startSSE();
})();
