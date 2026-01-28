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
    .container { display: grid; grid-template-columns: 280px 1fr; height: 100vh; }
    aside { background: var(--panel); padding: 12px; border-right: 1px solid #1f2937; overflow: auto; }
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
    .header { display: flex; align-items: center; justify-content: space-between; gap: 8px; margin-bottom: 8px; }
    .brand { font-weight: 800; letter-spacing: .6px; }
    .controls { display: flex; gap: 10px; align-items: center; }
    .key-display { display: flex; flex-direction: column; font-size: 13px; color: var(--muted); }
    .key-main { font-size: 15px; color: var(--fg); font-weight: 600; }
    .transpose-buttons { display: flex; gap: 4px; }
    button { background: #0b1220; color: var(--fg); border: 1px solid #203047; border-radius: 10px; padding: 6px 10px; cursor: pointer; }
    button:hover { background: #0e1a2f; }
    .hint { color: var(--muted); font-size: 12px; margin-top: 6px; }
    .empty { color: var(--muted); padding: 10px; }
  </style>
</head>
<body>
<div class="container">
  <aside>
    <div class="header">
      <div class="brand">🎸 Chord Viewer</div>
      <div class="controls">
        <div class="key-display">
          {% if base_key %}
            <span>Key</span>
            <span class="key-main">
              {% if transpose and current_key and current_key != base_key %}
                {{ base_key }} → {{ current_key or base_key }}
              {% else %}
                {{ base_key }}
              {% endif %}
            </span>
          {% else %}
            <span class="key-main">Transpose</span>
          {% endif %}
        </div>
        <div class="transpose-buttons">
          <button id="down" title="Transpose down -1">&#8722;</button>
          <button id="up" title="Transpose up +1">+</button>
        </div>
      </div>
    </div>
    {% if files %}
      {% for rel, name in files %}
        <a class="file {% if selected == rel %}active{% endif %}" data-path="{{ rel }}" href="{{ url_for('index') }}?path={{ rel }}&transpose={{ transpose }}">{{ name }}</a>
      {% endfor %}
      <div class="hint">Watching current file for changes…</div>
    {% else %}
      <div class="empty">No song files found in this directory. Add .cho/.chordpro/.pro/.onsong/.txt files.</div>
    {% endif %}
  </aside>
  <main>
    <div id="song"></div>
  </main>
</div>

<script>
(function(){
  const urlParams = new URLSearchParams(window.location.search);
  let sel = urlParams.get('path');
  let transpose = parseInt(urlParams.get('transpose') || '0', 10) || 0;
  const btnDown = document.getElementById('down');
  const btnUp = document.getElementById('up');
  const songEl = document.getElementById('song');
  const fileLinks = document.querySelectorAll('.file');
  let es = null;

  function render(){
    if(!sel){ songEl.innerHTML = '<div class="empty">Pick a file from the left.</div>'; return; }
    fetch(`/render?path=${encodeURIComponent(sel)}&transpose=${transpose}`)
      .then(r => r.text())
      .then(html => { songEl.innerHTML = html; })
      .catch(err => { songEl.innerHTML = `<div class="empty">Error: ${err}</div>`; });
  }

  function startSSE(){
    if(!sel) return;
    if (es) {
      es.close();
    }
    es = new EventSource(`/events?path=${encodeURIComponent(sel)}`);
    es.onmessage = (ev) => {
      if(ev.data === 'reload') { render(); }
    };
    es.addEventListener('ping', () => {/* keep-alive */});
    es.onerror = () => { /* browser will reconnect automatically */ };
  }

  function updateLocation() {
    const t = String(transpose || 0);
    const url = new URL(window.location);
    url.searchParams.set('transpose', t);
    if(sel) url.searchParams.set('path', sel);
    history.replaceState(null, '', url.toString());
  }

  if (btnDown) {
    btnDown.addEventListener('click', () => {
      transpose = (transpose || 0) - 1;
      updateLocation();
      render();
    });
  }
  if (btnUp) {
    btnUp.addEventListener('click', () => {
      transpose = (transpose || 0) + 1;
      updateLocation();
      render();
    });
  }

  // Intercept song link clicks to avoid full page reloads
  fileLinks.forEach(link => {
    link.addEventListener('click', (e) => {
      e.preventDefault();
      const path = link.getAttribute('data-path');
      if (!path) return;
      sel = path;
      fileLinks.forEach(l => l.classList.toggle('active', l === link));
      updateLocation();
      render();
      startSSE();
    });
  });

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
    app.config["SONG_FILES"] = list_song_files(base_dir)

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
