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
  - Client-side setlists (YAML in localStorage): name, date, note, and songs[{path, key, note}]; no server writes

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

from flask import Flask, Response, abort, jsonify, redirect, render_template, request, url_for

import webchord

try:
    from watchdog.events import FileSystemEventHandler
    from watchdog.observers import Observer
except ImportError:  # graceful hint
    raise SystemExit("Missing dependency 'watchdog'. Install with: pip install watchdog")

# -------------------------- Configuration ---------------------------
APP_DIR = Path(__file__).resolve().parent
app = Flask(
    __name__,
    template_folder=str(APP_DIR / "templates"),
    static_folder=str(APP_DIR / "static"),
)

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

    return render_template(
        "index.html",
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
