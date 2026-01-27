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

TODO: look into rendering with Webchord (https://sourceforge.net/projects/webchord/)
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

# --------------------- ChordPro → HTML renderer ---------------------

def render_song_to_html(text: str, *, transpose: int = 0, key_hint: Optional[str] = None) -> str:
    """Render a subset of ChordPro/OnSong to HTML. Returns an HTML fragment (no <html> wrapper)."""
    prefer = ACCIDENTAL_PREFS.get((key_hint or "C").strip(), "sharp")
    chor_stack: List[str] = []

    title = None
    subtitle = None
    meta_lines: List[str] = []
    body_html: List[str] = []

    lines = text.splitlines()
    for raw in lines:
        line = raw.rstrip("\n")
        # Directives
        d = DIRECTIVE_RE.match(line)
        if d:
            key, val = d.group(1).lower(), d.group(2)
            if key in ("title", "t"):
                title = val
            elif key in ("subtitle", "st"):
                subtitle = val
            elif key in ("artist", "a"):
                meta_lines.append(f"Artist: {html.escape(val)}")
            elif key in ("key", "k"):
                meta_lines.append(f"Key: {html.escape(val)}")
                key_hint = val
                prefer = ACCIDENTAL_PREFS.get(val.strip(), prefer)
            elif key in ("capo", "c"):
                meta_lines.append(f"Capo: {html.escape(val)}")
            elif key in ("comment", "c"):
                body_html.append(f'<div class="comment">{html.escape(val)}</div>')
            elif key in START_DIRECTIVES:
                cls = "chorus" if "chorus" in key or key == "soc" else "bridge"
                body_html.append(f'<div class="section {cls}">')
                chor_stack.append(cls)
            elif key in END_DIRECTIVES:
                if chor_stack:
                    body_html.append("</div>")
                    chor_stack.pop()
            # Ignore unknown directives gracefully
            continue

        # OnSong style section labels like "Verse:" or "Chorus:" on their own line
        if re.match(r"^[A-Za-z][A-Za-z0-9 ]+:\s*$", line):
            hdr = line.rstrip(": ")
            body_html.append(f'<div class="section-label">{html.escape(hdr)}</div>')
            continue

        # Empty line => paragraph spacing
        if not line.strip():
            body_html.append('<div class="spacer"></div>')
            continue

        # Optionally transpose chord tags
        if transpose:
            line = transpose_line(line, transpose, prefer)

        # Convert [Chord] tokens to HTML spans; keep the lyrics intact.
        pos = 0
        out: List[str] = []
        for m in CHORD_RE.finditer(line):
            # preceding lyrics
            lyrics = line[pos:m.start()]
            if lyrics:
                out.append(html.escape(lyrics))
            chord_txt = m.group(1)
            # If the chord is immediately before a space or end, add a non‑breaking space anchor
            following = line[m.end():m.end()+1]
            anchor = "&nbsp;" if (not following or following.isspace()) else ""
            out.append(f'<span class="chord">{html.escape(chord_txt)}</span>{anchor}')
            pos = m.end()
        # tail lyrics
        out.append(html.escape(line[pos:]))
        body_html.append(f'<div class="line">{"".join(out)}</div>')

    # Assemble the card
    title_html = f'<h1 class="title">{html.escape(title) if title else "Untitled"}</h1>'
    subtitle_html = f'<div class="subtitle">{html.escape(subtitle)}</div>' if subtitle else ""
    meta_html = ("<div class=\"meta\">" + " · ".join(meta_lines) + "</div>") if meta_lines else ""

    return f"""
    <div class="song">
      {title_html}
      {subtitle_html}
      {meta_html}
      <div class="song-body">{''.join(body_html)}</div>
    </div>
    """

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
    files = list_song_files(base_dir)
    selected = request.args.get("path")
    transpose = int(request.args.get("transpose", 0))

    # Ensure selected is safe and default to first file
    if selected:
        selected_path = safe_under(base_dir, base_dir / selected)
    else:
        selected_path = files[0] if files else None
        if selected_path:
            return redirect(url_for("index", path=str(selected_path.relative_to(base_dir))))

    page = MAIN_TEMPLATE
    return render_template_string(page,
                                  files=[(str(p.relative_to(base_dir)), p.name) for p in files],
                                  selected=str(selected_path.relative_to(base_dir)) if selected_path else None,
                                  transpose=transpose)


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

    text = song_path.read_text(encoding="utf-8", errors="replace")
    html_fragment = render_song_to_html(text, transpose=transpose)
    return html_fragment


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
    .controls { display: flex; gap: 8px; align-items: center; }
    input[type="number"] { width: 68px; background: #0b1220; color: var(--fg); border: 1px solid #203047; border-radius: 8px; padding: 6px 8px; }
    button { background: #0b1220; color: var(--fg); border: 1px solid #203047; border-radius: 10px; padding: 8px 10px; cursor: pointer; }
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
        <label>Transpose <input id="transpose" type="number" step="1" value="{{ transpose }}"/></label>
        <button id="apply">Apply</button>
      </div>
    </div>
    {% if files %}
      {% for rel, name in files %}
        <a class="file {% if selected == rel %}active{% endif %}" href="{{ url_for('index') }}?path={{ rel }}&transpose={{ transpose }}">{{ name }}</a>
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
  const sel = urlParams.get('path');
  const transposeInput = document.getElementById('transpose');
  const applyBtn = document.getElementById('apply');
  const songEl = document.getElementById('song');

  function render(){
    if(!sel){ songEl.innerHTML = '<div class="empty">Pick a file from the left.</div>'; return; }
    const t = parseInt(transposeInput.value || '0', 10) || 0;
    fetch(`/render?path=${encodeURIComponent(sel)}&transpose=${t}`)
      .then(r => r.text())
      .then(html => { songEl.innerHTML = html; })
      .catch(err => { songEl.innerHTML = `<div class="empty">Error: ${err}</div>`; });
  }

  function startSSE(){
    if(!sel) return;
    const t = parseInt(transposeInput.value || '0', 10) || 0;
    const es = new EventSource(`/events?path=${encodeURIComponent(sel)}`);
    es.onmessage = (ev) => {
      if(ev.data === 'reload') { render(); }
    };
    es.addEventListener('ping', () => {/* keep-alive */});
    es.onerror = () => { /* browser will reconnect automatically */ };
  }

  applyBtn.addEventListener('click', () => {
    const t = transposeInput.value || '0';
    const url = new URL(window.location);
    url.searchParams.set('transpose', t);
    if(sel) url.searchParams.set('path', sel);
    window.location = url.toString();
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
