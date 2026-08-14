#!/usr/bin/env python3
"""
ChordPro/OnSong Live-Reload Web Renderer
=======================================

A small Python web app that renders local ChordPro/OnSong files in your browser
and automatically updates the rendering when the underlying file changes.

- Features
  - Browse a directory of .cho/.chordpro/.pro/.onsong/.txt files
  - Render common ChordPro directives: {title}, {subtitle}, {artist}, {key}, {capo},
    {comment}, {start_of_chorus}/{end_of_chorus} (aka {soc}/{eoc}), {start_of_bridge}/{end_of_bridge}
  - Inline [Chord] notation with clean, readable styling
  - Optional transposition via query string (?transpose=+2 or ?transpose=-3)
  - Live reload in the browser when the file changes (via Server‑Sent Events + watchdog)
  - Client-side setlists (YAML in localStorage): name, date, and songs[{path, key}]; no server writes

- Quick start
  1) Install deps:  pip install flask watchdog
  2) Run:          python app.py --dir /path/to/your/songs  (default: current dir)
  3) Open:         http://localhost:5000

Security note: This app only serves files under the configured base directory.

Tested with: Python 3.10+.
"""
from __future__ import annotations

import argparse
import html
import os
import queue
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from flask import Flask, Response, abort, jsonify, redirect, render_template_string, request, send_from_directory, url_for

import webchord

try:
    from watchdog.events import FileSystemEventHandler
    from watchdog.observers import Observer
except ImportError:  # graceful hint
    raise SystemExit("Missing dependency 'watchdog'. Install with: pip install watchdog")

# -------------------------- Configuration ---------------------------
app = Flask(__name__)

CHORD_EXTS = {".cho", ".chordpro", ".pro", ".onsong", ".txt"}

# Global state for live‑reload subscriptions
_subscriptions: Dict[Path, List["Subscriber"]] = {}
_subscriptions_lock = threading.Lock()

@dataclass
class Subscriber:
    q: "queue.Queue[str]"
    last_heartbeat: float

# ------------------------- Utility Helpers --------------------------

def safe_under(base: Path, candidate: Path) -> Path:
    """Ensure candidate is inside base directory; return resolved path or abort 403."""
    base = base.resolve()
    cand = candidate.resolve()
    if not str(cand).startswith(str(base)):
        abort(403, description="Path outside base directory")
    return cand

# ------------- Chord / Music helpers (transpose, parsing) -----------

NOTE_SEQUENCE_SHARP = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
NOTE_SEQUENCE_FLAT  = ["C", "Db", "D", "Eb", "E", "F", "Gb", "G", "Ab", "A", "Bb", "B"]

CHORD_RE = re.compile(r"\[([^\]]+)\]")  # [Chord]
DIRECTIVE_RE = re.compile(r"^\{\s*([a-zA-Z_]+)\s*:\s*(.*?)\s*\}\s*$")
START_DIRECTIVES = {"start_of_chorus", "soc", "start_of_bridge", "sob"}
END_DIRECTIVES   = {"end_of_chorus", "eoc", "end_of_bridge", "eob"}

ACCIDENTAL_PREFS = {
    # Heuristics for when to prefer flats vs sharps on transpose given a key
    "F": "flat", "Bb": "flat", "Eb": "flat", "Ab": "flat", "Db": "flat", "Gb": "flat",
    "C": "sharp", "G": "sharp", "D": "sharp", "A": "sharp", "E": "sharp", "B": "sharp", "F#": "sharp", "C#": "sharp",
}

CHORD_TOKEN_RE = re.compile(r"([A-G](?:#|b)?)(.*)")  # split root note from the rest (maj/min/7/etc)


def _normalize_key_value(val: str) -> str:
    """Normalize key strings, tolerating optional surrounding square brackets."""
    v = val.strip()
    if len(v) >= 2 and v.startswith("[") and v.endswith("]"):
        v = v[1:-1].strip()
    return v


def transpose_chord(chord: str, semitones: int, prefer: str = "sharp") -> str:
    """Transpose a single chord root by semitones, keeping suffix (m,7,add9...) intact."""
    m = CHORD_TOKEN_RE.match(chord.strip())
    if not m:
        return chord  # unknown format, leave untouched
    root, suffix = m.group(1), m.group(2)
    # Choose working sequence (accept either # or b in source)
    if "b" in root and root not in ("B",):
        seq = NOTE_SEQUENCE_FLAT
    else:
        seq = NOTE_SEQUENCE_SHARP
    try:
        idx = seq.index(root)
    except ValueError:
        # If not found (e.g., Fb), try the other spelling
        alt_seq = NOTE_SEQUENCE_FLAT if seq is NOTE_SEQUENCE_SHARP else NOTE_SEQUENCE_SHARP
        try:
            idx = alt_seq.index(root)
            seq = alt_seq
        except ValueError:
            return chord
    new_idx = (idx + semitones) % 12
    if prefer == "flat":
        new_root = NOTE_SEQUENCE_FLAT[new_idx]
    else:
        new_root = NOTE_SEQUENCE_SHARP[new_idx]
    return f"{new_root}{suffix}"


def transpose_line(line: str, semitones: int, prefer: str) -> str:
    def _repl(m: re.Match[str]) -> str:
        inside = m.group(1)
        # split slash chords A/C# etc.
        if "/" in inside:
            left, right = inside.split("/", 1)
            return f"[{transpose_chord(left, semitones, prefer)}/{transpose_chord(right, semitones, prefer)}]"
        return f"[{transpose_chord(inside, semitones, prefer)}]"
    return CHORD_RE.sub(_repl, line)

# --------------------- ChordPro helpers ---------------------

def prepare_chopro(text: str) -> tuple[str, Optional[str]]:
    """
    Normalize ChordPro text:
    - If there are no {title}/{artist}/{key} tags at the top, assume:
      first non-empty line = title, second = artist, a line like "Key: G"
      gives the key, and convert these into proper ChordPro directives.
    - Return (possibly modified_text, base_key).
    """
    lines = text.splitlines()
    if not lines:
        return text, None

    # Find indices of the first few non-empty lines
    non_empty_indices = [i for i, l in enumerate(lines) if l.strip()]
    if not non_empty_indices:
        return text, None

    # Check if any of the first 3 non-empty lines are already ChordPro directives.
    has_directive_at_top = False
    for idx in non_empty_indices[:3]:
        stripped = lines[idx].lstrip()
        if stripped.startswith("{") and "}" in stripped:
            has_directive_at_top = True
            break

    title: Optional[str] = None
    artist: Optional[str] = None
    inferred_key: Optional[str] = None
    consumed_indices: set[int] = set()

    if not has_directive_at_top:
        # Infer title and artist from first two non-empty lines
        if len(non_empty_indices) >= 1:
            i = non_empty_indices[0]
            title = lines[i].strip()
            consumed_indices.add(i)
        if len(non_empty_indices) >= 2:
            i = non_empty_indices[1]
            artist = lines[i].strip()
            consumed_indices.add(i)

        # Look for a loose "Key: X" style line among the next few non-empty lines
        key_pattern = re.compile(r"^\s*key\s*:\s*(.+)$", re.IGNORECASE)
        for i in non_empty_indices[2:8]:
            m = key_pattern.match(lines[i])
            if m:
                inferred_key = _normalize_key_value(m.group(1))
                consumed_indices.add(i)
                break

        # Build new text with synthetic ChordPro directives at the top
        new_lines: List[str] = []
        if title:
            new_lines.append(f"{{title: {title}}}")
        if artist:
            new_lines.append(f"{{artist: {artist}}}")
        if inferred_key:
            new_lines.append(f"{{key: {inferred_key}}}")

        for idx, l in enumerate(lines):
            if idx in consumed_indices:
                continue
            new_lines.append(l)

        text = "\n".join(new_lines)

    # Now that text is normalized, extract base key from proper directives
    base_key: Optional[str] = None
    for raw in text.splitlines():
        m = DIRECTIVE_RE.match(raw)
        if not m:
            continue
        key, val = m.group(1).lower(), m.group(2).strip()
        if key in ("key", "k"):
            base_key = _normalize_key_value(val)
            break

    return text, base_key

# ------------------------------ Routes ------------------------------

def list_song_files(base_dir: Path) -> List[Path]:
    results: List[Path] = []
    for p in sorted(base_dir.rglob("*")):
        if p.suffix.lower() in CHORD_EXTS and p.is_file():
            results.append(p)
    return results


def extract_title_artist_lyrics(text: str) -> tuple[Optional[str], Optional[str], str]:
    """Pull title/artist directives and chord-stripped lyrics for search."""
    normalized, _ = prepare_chopro(text)
    title: Optional[str] = None
    artist: Optional[str] = None
    lyric_lines: List[str] = []
    for raw in normalized.splitlines():
        stripped = raw.strip()
        m = DIRECTIVE_RE.match(stripped)
        if m:
            key, val = m.group(1).lower(), m.group(2).strip()
            if key in ("title", "t") and not title:
                title = val
            elif key in ("artist", "a") and not artist:
                artist = val
            continue
        if stripped.startswith("#"):
            continue
        lyric_lines.append(CHORD_RE.sub("", raw))
    return title, artist, "\n".join(lyric_lines)


def build_song_search_index(base_dir: Path, files: List[Path]) -> List[dict]:
    """Build an in-memory search index (path, title, artist, lyrics haystack)."""
    index: List[dict] = []
    for p in files:
        rel = str(p.relative_to(base_dir))
        try:
            raw = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            index.append({
                "path": rel,
                "name": p.name,
                "title": None,
                "artist": None,
                "haystack": f"{rel}\n{p.name}".lower(),
            })
            continue
        title, artist, lyrics = extract_title_artist_lyrics(raw)
        haystack = "\n".join([rel, p.name, title or "", artist or "", lyrics]).lower()
        index.append({
            "path": rel,
            "name": p.name,
            "title": title,
            "artist": artist,
            "haystack": haystack,
        })
    return index


@app.route("/")
def index():
    # Resolve base directory from config
    base_dir = app.config["BASE_DIR"]
    # Use cached file list to avoid rescanning large trees on every request
    files: List[Path] = app.config.get("SONG_FILES") or []
    selected = request.args.get("path")
    transpose = int(request.args.get("transpose", 0))

    # Ensure selected is safe and default to first file
    if selected:
        selected_path = safe_under(base_dir, base_dir / selected)
    else:
        selected_path = files[0] if files else None
        if selected_path:
            return redirect(url_for("index", path=str(selected_path.relative_to(base_dir))))

    # Derive base key (from ChordPro, with heuristics) and current key after transpose
    base_key: Optional[str] = None
    current_key: Optional[str] = None
    if selected_path:
        try:
            text = selected_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        _, base_key = prepare_chopro(text)
        if base_key:
            base_clean = base_key.strip()
            prefer = ACCIDENTAL_PREFS.get(base_clean, "sharp")
            current_key = transpose_chord(base_clean, transpose, prefer) if transpose else base_clean

    page = MAIN_TEMPLATE
    return render_template_string(
        page,
        files=[(str(p.relative_to(base_dir)), p.name) for p in files],
        selected=str(selected_path.relative_to(base_dir)) if selected_path else None,
        transpose=transpose,
        base_key=base_key,
        current_key=current_key,
    )


@app.route("/render")
def render_song():
    base_dir = app.config["BASE_DIR"]
    rel = request.args.get("path")
    if not rel:
        abort(400, description="Missing 'path' query param")
    song_path = safe_under(base_dir, base_dir / rel)
    if not song_path.exists():
        abort(404)

    transpose = int(request.args.get("transpose", 0))

    raw_text = song_path.read_text(encoding="utf-8", errors="replace")

    # Normalize ChordPro and extract base key (including heuristics for plain text)
    text, base_key = prepare_chopro(raw_text)

    # Apply transposition to the normalized ChordPro before feeding it to WebChord.
    prefer = ACCIDENTAL_PREFS.get((base_key or "C").strip(), "sharp")
    if transpose:
        text = "\n".join(transpose_line(line, transpose, prefer) for line in text.splitlines())

    # Use the legacy WebChord converter (Python port) for HTML generation.
    # webchord.chopro2html returns a full HTML document; extract the <body> content
    # so it can be embedded inside our SPA shell.
    full_html = webchord.chopro2html(text)
    lower = full_html.lower()
    body_start = lower.find("<body")
    fragment = full_html
    if body_start != -1:
        body_tag_end = full_html.find(">", body_start)
        body_end = lower.rfind("</body>")
        if body_tag_end != -1 and body_end != -1 and body_end > body_tag_end:
            fragment = full_html[body_tag_end + 1 : body_end]

    # Prepend a metadata block showing the key, updated when transposed.
    if base_key:
        base_clean = base_key.strip()
        label = f"Key: {html.escape(base_clean)}"
        if transpose:
            current_key = transpose_chord(base_clean, transpose, prefer)
            if current_key != base_clean:
                label = (
                    f"Key: {html.escape(base_clean)} "
                    f"→ {html.escape(current_key)}"
                )
        meta_html = f'<div class="meta">{label}</div>'
        fragment = meta_html + fragment

    return fragment


@app.route("/meta")
def song_meta():
    """Return base key (and related metadata) for a song — read-only, no writes."""
    base_dir = app.config["BASE_DIR"]
    rel = request.args.get("path")
    if not rel:
        abort(400, description="Missing 'path' query param")
    song_path = safe_under(base_dir, base_dir / rel)
    if not song_path.exists():
        abort(404)
    try:
        raw_text = song_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        abort(500, description="Could not read song file")
    _, base_key = prepare_chopro(raw_text)
    return jsonify({"path": rel, "base_key": base_key, "name": song_path.name})


@app.route("/search")
def search_songs():
    """Filter songs by substring match against title, artist, lyrics, or path."""
    q = (request.args.get("q") or "").strip().lower()
    try:
        limit = int(request.args.get("limit", 80))
    except ValueError:
        limit = 80
    limit = max(1, min(limit, 200))

    index: List[dict] = app.config.get("SONG_SEARCH_INDEX") or []
    results = []
    for entry in index:
        if q and q not in entry.get("haystack", ""):
            continue
        results.append({
            "path": entry["path"],
            "name": entry["name"],
            "title": entry.get("title"),
            "artist": entry.get("artist"),
        })
        if len(results) >= limit:
            break
    return jsonify({"q": q, "results": results})


@app.route("/events")
def sse_events():
    base_dir = app.config["BASE_DIR"]
    rel = request.args.get("path")
    if not rel:
        abort(400, description="Missing 'path'")
    song_path = safe_under(base_dir, base_dir / rel)

    q: queue.Queue[str] = queue.Queue()
    sub = Subscriber(q=q, last_heartbeat=time.time())
    with _subscriptions_lock:
        _subscriptions.setdefault(song_path, []).append(sub)

    def gen() -> Iterable[bytes]:
        # Send an initial ping so the client knows the stream is alive
        yield b"event: ping\ndata: ok\n\n"
        try:
            while True:
                try:
                    msg = q.get(timeout=25)
                except queue.Empty:
                    # Keep-alive ping every ~25s (behind some proxies)
                    yield b"event: ping\ndata: ok\n\n"
                    continue
                yield f"data: {msg}\n\n".encode("utf-8")
        finally:
            with _subscriptions_lock:
                subs = _subscriptions.get(song_path, [])
                if sub in subs:
                    subs.remove(sub)

    return Response(gen(), mimetype="text/event-stream")


@app.route("/static/<path:filename>")
def static_files(filename: str):
    return send_from_directory(app.config["STATIC_DIR"], filename)

# --------------------------- File Watching --------------------------

class ReloadHandler(FileSystemEventHandler):
    def __init__(self, base_dir: Path):
        super().__init__()
        self.base_dir = base_dir.resolve()

    def on_modified(self, event):
        self._notify(event.src_path)

    def on_moved(self, event):
        self._notify(event.dest_path)

    def _notify(self, path: str):
        p = Path(path).resolve()
        if p.suffix.lower() not in CHORD_EXTS:
            return
        # Fan out to subscribers of this exact file
        with _subscriptions_lock:
            subs = list(_subscriptions.get(p, []))
        if not subs:
            return
        for sub in subs:
            try:
                sub.q.put_nowait("reload")
            except Exception:
                pass

# ---------------------------- HTML Template -------------------------

MAIN_TEMPLATE = r"""
<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>ChordPro/OnSong Renderer</title>
  <style>
    :root {
      --bg: #0f172a;       /* slate-900 */
      --panel: #111827;    /* gray-900 */
      --fg: #e5e7eb;       /* gray-200 */
      --muted: #94a3b8;    /* slate-400 */
      --accent: #38bdf8;   /* sky-400 */
      --accent-2: #a78bfa; /* violet-400 */
      --chorus: #0b3b57;   /* dark teal-ish */
    }
    * { box-sizing: border-box; }
    body {
      margin: 0; background: var(--bg); color: var(--fg);
      font-family: system-ui, -apple-system, Segoe UI, Roboto, Ubuntu, Cantarell, Noto Sans, Helvetica, Arial, "Apple Color Emoji", "Segoe UI Emoji";
    }
    .container { display: grid; grid-template-columns: 320px 1fr; height: 100vh; }
    aside { background: var(--panel); padding: 12px; border-right: 1px solid #1f2937; overflow: auto; display: flex; flex-direction: column; gap: 8px; }
    main { height: 100vh; overflow: auto; padding: 24px; }
    h1.title { margin: 0 0 6px 0; font-size: 28px; letter-spacing: .5px; }
    .subtitle { color: var(--muted); margin-bottom: 8px; }
    .meta { color: var(--muted); margin-bottom: 18px; font-size: 14px; }

    .song-body { white-space: pre-wrap; font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace; font-size: 18px; line-height: 1.9; }
    .line { position: relative; }
    .chord { position: relative; top: -1.15em; font-weight: 700; padding: 0 2px; }
    .comment { color: var(--accent-2); margin: 6px 0; font-style: italic; }
    .section { border-left: 3px solid var(--accent); padding-left: 10px; margin: 10px 0; background: rgba(56, 189, 248, 0.06); border-radius: 6px; }
    .section.bridge { border-color: var(--accent-2); background: rgba(167, 139, 250, 0.06); }
    .section-label { margin: 14px 0 6px; font-weight: 700; color: var(--accent); text-transform: uppercase; font-size: 12px; letter-spacing: .08em; }
    .spacer { height: 12px; }

    .file { display: flex; align-items: center; gap: 8px; padding: 8px 10px; border-radius: 10px; cursor: pointer; text-decoration: none; color: var(--fg); }
    .file:hover { background: #0b1220; }
    .file.active { background: #0e1a2f; outline: 1px solid #1f334f; }
    .header { display: flex; align-items: center; justify-content: space-between; gap: 8px; margin-bottom: 4px; }
    .brand { font-weight: 800; letter-spacing: .6px; }
    .controls { display: flex; gap: 10px; align-items: center; }
    .key-display { display: flex; flex-direction: column; font-size: 13px; color: var(--muted); }
    .key-main { font-size: 15px; color: var(--fg); font-weight: 600; }
    .transpose-buttons { display: flex; gap: 4px; }
    button { background: #0b1220; color: var(--fg); border: 1px solid #203047; border-radius: 10px; padding: 6px 10px; cursor: pointer; }
    button:hover { background: #0e1a2f; }
    button.danger { border-color: #7f1d1d; color: #fca5a5; }
    button.danger:hover { background: #450a0a; }
    button.small { padding: 3px 7px; font-size: 12px; border-radius: 8px; }
    .hint { color: var(--muted); font-size: 12px; margin-top: 6px; }
    .empty { color: var(--muted); padding: 10px; }

    .tabs { display: flex; gap: 4px; border-bottom: 1px solid #1f2937; padding-bottom: 8px; }
    .tab {
      flex: 1; text-align: center; padding: 8px 6px; border-radius: 10px 10px 0 0;
      background: transparent; border: 1px solid transparent; color: var(--muted);
      cursor: pointer; font-weight: 600; font-size: 13px; letter-spacing: .04em; text-transform: uppercase;
    }
    .tab:hover { color: var(--fg); background: #0b1220; }
    .tab.active { color: var(--fg); background: #0e1a2f; border-color: #1f334f; border-bottom-color: transparent; }
    .panel { display: none; flex-direction: column; gap: 8px; flex: 1; min-height: 0; }
    .panel.active { display: flex; }

    .create-row { display: flex; gap: 6px; }
    .create-row input, .add-row select, .setlist-item select, .add-song-picker input {
      flex: 1; min-width: 0; background: #0b1220; color: var(--fg);
      border: 1px solid #203047; border-radius: 8px; padding: 6px 8px; font-size: 13px;
    }
    .add-song-picker { display: flex; flex-direction: column; gap: 6px; }
    .add-song-picker select {
      width: 100%; min-height: 9.5em; background: #0b1220; color: var(--fg);
      border: 1px solid #203047; border-radius: 8px; padding: 4px; font-size: 13px;
    }
    .add-song-picker .add-row { align-items: stretch; }
    .add-song-picker .add-row button { align-self: flex-end; }
    .setlist-row, .setlist-item {
      display: flex; align-items: center; gap: 6px; padding: 8px 10px;
      border-radius: 10px; background: #0b1220;
    }
    .setlist-row { cursor: pointer; }
    .setlist-row:hover { background: #0e1a2f; outline: 1px solid #1f334f; }
    .setlist-row .name, .setlist-item .name { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .setlist-item { flex-wrap: wrap; }
    .setlist-item.active { outline: 1px solid #1f334f; background: #0e1a2f; }
    .setlist-item .name { cursor: pointer; }
    .setlist-item .name:hover { color: var(--accent); }
    .setlist-item .song-key { flex: 0 0 auto; font-size: 13px; font-weight: 600; color: var(--accent); min-width: 2em; text-align: center; }
    .setlist-toolbar { display: flex; align-items: center; gap: 6px; justify-content: space-between; }
    .setlist-meta { display: flex; flex-direction: column; gap: 6px; }
    .setlist-meta label { display: flex; flex-direction: column; gap: 3px; font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: .04em; }
    .setlist-meta input {
      background: #0b1220; color: var(--fg); border: 1px solid #203047;
      border-radius: 8px; padding: 6px 8px; font-size: 13px; text-transform: none; letter-spacing: normal;
    }
    .setlist-row .meta-line { display: flex; flex-direction: column; gap: 2px; flex: 1; min-width: 0; }
    .setlist-row .date { font-size: 12px; color: var(--muted); }
    .add-row { display: flex; gap: 6px; align-items: center; }
    .item-actions { display: flex; gap: 3px; }
    .songs-list, .setlists-list, .setlist-songs { display: flex; flex-direction: column; gap: 4px; }
  </style>
</head>
<body>
<div class="container">
  <aside>
    <div class="header">
      <div class="brand">🎸 Chord Viewer</div>
      <div class="controls">
        <div class="key-display" id="key-display">
          {% if base_key %}
            <span>Key</span>
            <span class="key-main" id="key-main">
              {% if transpose and current_key and current_key != base_key %}
                {{ base_key }} → {{ current_key or base_key }}
              {% else %}
                {{ base_key }}
              {% endif %}
            </span>
          {% else %}
            <span class="key-main" id="key-main">Transpose</span>
          {% endif %}
        </div>
        <div class="transpose-buttons">
          <button id="down" title="Transpose down -1">&#8722;</button>
          <button id="up" title="Transpose up +1">+</button>
        </div>
      </div>
    </div>

    <div class="tabs" role="tablist">
      <button type="button" class="tab active" data-tab="songs" role="tab" aria-selected="true">Songs</button>
      <button type="button" class="tab" data-tab="setlists" role="tab" aria-selected="false">Setlists</button>
    </div>

    <div id="panel-songs" class="panel active" role="tabpanel">
      {% if files %}
        <div class="songs-list">
          {% for rel, name in files %}
            <a class="file {% if selected == rel %}active{% endif %}" data-path="{{ rel }}" href="{{ url_for('index') }}?path={{ rel }}&transpose={{ transpose }}">{{ name }}</a>
          {% endfor %}
        </div>
        <div class="hint">Watching current file for changes…</div>
      {% else %}
        <div class="empty">No song files found in this directory. Add .cho/.chordpro/.pro/.onsong/.txt files.</div>
      {% endif %}
    </div>

    <div id="panel-setlists" class="panel" role="tabpanel">
      <div id="setlist-index-view">
        <div class="create-row">
          <input id="new-setlist-name" type="text" placeholder="New setlist name" maxlength="80" />
          <button type="button" id="create-setlist" title="Create setlist">+</button>
        </div>
        <div id="setlists-list" class="setlists-list"></div>
        <div class="hint">Setlists are stored in this browser only (YAML per setlist).</div>
      </div>
      <div id="setlist-detail-view" hidden>
        <div class="setlist-toolbar">
          <button type="button" class="small" id="back-to-setlists" title="Back">←</button>
          <button type="button" class="small danger" id="delete-setlist" title="Delete setlist">✕</button>
        </div>
        <div class="setlist-meta">
          <label>
            <span>Name</span>
            <input id="setlist-name-input" type="text" maxlength="80" />
          </label>
          <label>
            <span>Date</span>
            <input id="setlist-date-input" type="date" />
          </label>
        </div>
        <div id="setlist-songs" class="setlist-songs"></div>
        <div class="add-song-picker">
          <input id="add-song-filter" type="search" placeholder="Filter by title, artist, lyrics…" autocomplete="off" />
          <div class="add-row">
            <select id="add-song-select" size="8">
              <option value="">Add a song…</option>
            </select>
            <button type="button" id="add-song-btn" title="Add song">Add</button>
          </div>
        </div>
      </div>
    </div>
  </aside>
  <main>
    <div id="song"></div>
  </main>
</div>

<script>
(function(){
  const NOTE_INDEX = {
    "C":0,"B#":0,"C#":1,"Db":1,"D":2,"D#":3,"Eb":3,"E":4,"Fb":4,
    "F":5,"E#":5,"F#":6,"Gb":6,"G":7,"G#":8,"Ab":8,"A":9,"A#":10,"Bb":10,"B":11,"Cb":11
  };
  const NOTE_SHARP = ["C","C#","D","D#","E","F","F#","G","G#","A","A#","B"];
  const INDEX_KEY = "offsong:setlists:index";
  const FILE_PREFIX = "offsong:setlist:file:";

  const urlParams = new URLSearchParams(window.location.search);
  let sel = urlParams.get("path");
  let transpose = parseInt(urlParams.get("transpose") || "0", 10) || 0;
  let currentBaseKey = {{ (base_key or "")|tojson }};
  let activeSetlistId = null; // stable id of setlist being edited/viewed
  let activeSetlistSongIndex = null; // index within setlist when opened from there
  let lastSetlistSongIndex = null; // remembered across Songs-tab browsing
  let songLoadId = 0; // ignores stale openSong/meta callbacks
  let renderId = 0; // ignores stale /render responses

  const btnDown = document.getElementById("down");
  const btnUp = document.getElementById("up");
  const songEl = document.getElementById("song");
  const keyMain = document.getElementById("key-main");
  const fileLinks = document.querySelectorAll("#panel-songs .file");
  let es = null;

  // ---- YAML codec for setlist files: name / date / songs[{path,key}] ----
  function escapeYamlString(s) {
    if (s === "" || /[:#\n\r\t{}[\],&*!|>'"%@`]/.test(s) || /^\s|\s$/.test(s)) {
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

  function todayISO() {
    const d = new Date();
    const yyyy = d.getFullYear();
    const mm = String(d.getMonth() + 1).padStart(2, "0");
    const dd = String(d.getDate()).padStart(2, "0");
    return `${yyyy}-${mm}-${dd}`;
  }

  function emptySetlist(name, date) {
    return { name: name || "Untitled", date: date || todayISO(), songs: [] };
  }

  function setlistToYaml(doc) {
    const songs = Array.isArray(doc.songs) ? doc.songs : [];
    let out = `name: ${escapeYamlString(doc.name || "")}\n`;
    out += `date: ${escapeYamlString(doc.date || "")}\n`;
    out += "songs:\n";
    if (!songs.length) {
      out += "  []\n";
      return out;
    }
    for (const song of songs) {
      out += `  - path: ${escapeYamlString(song.path || "")}\n`;
      out += `    key: ${escapeYamlString(song.key || "")}\n`;
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
          current = { path: parseYamlScalar(item[1]), key: "" };
          continue;
        }
        const keyLine = line.match(/^\s+key\s*:\s*(.*)$/);
        if (keyLine && current) current.key = parseYamlScalar(keyLine[1]);
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
        current = { path: parseYamlScalar(item[1]), key: "" };
        continue;
      }
      const keyLine = line.match(/^\s+key\s*:\s*(.*)$/);
      if (keyLine && current) {
        current.key = parseYamlScalar(keyLine[1]);
      }
    }
    if (current) songs.push(current);
    doc.songs = songs.filter(s => s.path);
    if (!doc.date) doc.date = todayISO();
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
      songs: Array.isArray(doc.songs) ? doc.songs : []
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

  function updateKeyDisplay() {
    if (!keyMain) return;
    if (currentBaseKey) {
      const cur = applyTransposeToKey(currentBaseKey, transpose) || currentBaseKey;
      keyMain.textContent = (transpose && cur !== currentBaseKey)
        ? `${currentBaseKey} → ${cur}`
        : currentBaseKey;
    } else {
      keyMain.textContent = transpose ? `Transpose ${transpose > 0 ? "+" : ""}${transpose}` : "Transpose";
    }
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
        updateKeyDisplay();
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
    setActiveSongLink(path);
    startSSE();
    const loadId = ++songLoadId;
    fetchMeta(path).then(meta => {
      if (path !== sel) return;
      currentBaseKey = meta.base_key || null;
      // If the user already transposed (or opened another song), keep their transpose
      if (loadId !== songLoadId) {
        updateKeyDisplay();
        return;
      }
      if (desiredKey && currentBaseKey) {
        transpose = transposeAmount(currentBaseKey, desiredKey);
      } else {
        // Songs tab, or setlist song with no mappable base key
        transpose = 0;
      }
      updateLocation();
      updateKeyDisplay();
      render();
      renderSetlistDetail();
    }).catch(() => {
      if (path !== sel || loadId !== songLoadId) return;
      currentBaseKey = null;
      transpose = 0;
      updateLocation();
      updateKeyDisplay();
      render();
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
      updateKeyDisplay();
    }).catch(() => {});
  }

  function bumpTranspose(delta) {
    // Invalidate in-flight openSong so it cannot reset transpose after +/- 
    songLoadId++;
    transpose = (Number(transpose) || 0) + delta;
    updateLocation();
    updateKeyDisplay();
    render();
    persistActiveSetlistKeyFromTranspose();
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
  }

  function showSetlistDetail(id) {
    activeSetlistId = id;
    setlistIndexView.hidden = true;
    setlistDetailView.hidden = false;
    const doc = loadSetlist(id);
    setlistNameInput.value = doc.name || "";
    setlistDateInput.value = doc.date || "";
    renderSetlistDetail();
  }

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

  document.getElementById("add-song-btn").addEventListener("click", () => {
    if (!activeSetlistId) return;
    const select = document.getElementById("add-song-select");
    const path = select.value;
    if (!path) return;
    fetchMeta(path).then(meta => {
      const doc = loadSetlist(activeSetlistId);
      doc.songs.push({ path, key: meta.base_key || "C" });
      saveSetlist(activeSetlistId, doc);
      select.value = "";
      renderSetlistDetail();
    }).catch(() => {
      const doc = loadSetlist(activeSetlistId);
      doc.songs.push({ path, key: "C" });
      saveSetlist(activeSetlistId, doc);
      select.value = "";
      renderSetlistDetail();
    });
  });

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
  addSongFilter.addEventListener("input", () => {
    clearTimeout(addSongFilterTimer);
    addSongFilterTimer = setTimeout(() => {
      refreshAddSongOptions(addSongFilter.value.trim());
    }, 180);
  });
  addSongFilter.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      const select = document.getElementById("add-song-select");
      if (!select.value) {
        const first = Array.from(select.options).find(o => o.value);
        if (first) select.value = first.value;
      }
      if (select.value) document.getElementById("add-song-btn").click();
    }
  });
  document.getElementById("add-song-select").addEventListener("dblclick", () => {
    if (document.getElementById("add-song-select").value) {
      document.getElementById("add-song-btn").click();
    }
  });
  refreshAddSongOptions("");

  if (btnDown) {
    btnDown.addEventListener("click", () => bumpTranspose(-1));
  }
  if (btnUp) {
    btnUp.addEventListener("click", () => bumpTranspose(1));
  }

  fileLinks.forEach(link => {
    link.addEventListener("click", (e) => {
      e.preventDefault();
      const path = link.getAttribute("data-path");
      if (!path) return;
      activeSetlistSongIndex = null;
      openSong(path, null, null);
    });
  });

  migrateLegacyNameKeyedSetlists();
  renderSetlistIndex();
  updateKeyDisplay();
  if (sel) {
    fetchMeta(sel).then(meta => {
      currentBaseKey = meta.base_key || currentBaseKey;
      updateKeyDisplay();
    }).catch(() => {});
  }
  render();
  startSSE();
})();
</script>
</body>
</html>
"""

# ------------------------------ Main --------------------------------

def main():
    parser = argparse.ArgumentParser(description="ChordPro/OnSong live renderer")
    parser.add_argument("--dir", default=os.getcwd(), help="Base directory to serve and watch (default: cwd)")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=5000, help="Port (default 5000)")
    args = parser.parse_args()

    base_dir = Path(args.dir).expanduser().resolve()
    if not base_dir.exists() or not base_dir.is_dir():
        raise SystemExit(f"Base directory does not exist or is not a directory: {base_dir}")

    app.config["BASE_DIR"] = base_dir
    app.config["STATIC_DIR"] = base_dir  # not used, but kept for completeness
    # Cache the list of song files once at startup so navigating between songs
    # or transposing does not require rescanning large directory trees.
    song_files = list_song_files(base_dir)
    app.config["SONG_FILES"] = song_files
    app.config["SONG_SEARCH_INDEX"] = build_song_search_index(base_dir, song_files)

    # Start watchdog observer
    handler = ReloadHandler(base_dir)
    observer = Observer()
    observer.schedule(handler, str(base_dir), recursive=True)
    observer.daemon = True
    observer.start()

    print(f"Serving {base_dir} on http://{args.host}:{args.port}")
    try:
        app.run(host=args.host, port=args.port, debug=False, threaded=True)
    finally:
        observer.stop()
        observer.join()


if __name__ == "__main__":
    main()
