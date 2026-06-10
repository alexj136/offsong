#!/usr/bin/env python3
"""
Web Chord - Python port of webchord.pl
--------------------------------------

CGI script to convert a ChordPro file to HTML, closely mirroring the original
Perl implementation of Webchord (https://sourceforge.net/projects/webchord/).

Usage (as CGI):
  - Install this script under your web server's CGI directory.
  - Ensure it is executable (chmod +x webchord.py).
  - Configure the server to run it as a CGI script.

Input:
  - CGI parameter "chordpro" (text field or file upload) containing ChordPro.

Output:
  - Complete HTML document with inline CSS styling and chords/lyrics layout.
"""

import datetime
import html
import os
import re
import sys
from email import message_from_bytes
from email.policy import HTTP
from typing import List, Optional, Tuple
from urllib.parse import parse_qs


LOG_PATH = "/var/log/webchord.log"


def _open_log():
    try:
        return open(LOG_PATH, "a", encoding="utf-8", errors="ignore")
    except OSError:
        # Fall back to stderr if log file cannot be opened
        return sys.stderr


LOG = _open_log()


def log(msg: str) -> None:
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    host = os.environ.get("REMOTE_HOST") or os.environ.get("REMOTE_ADDR") or "-"
    LOG.write(f"---\n{timestamp}: from host {host}\n{msg}\n")
    LOG.flush()


def bailout(msg: str) -> None:
    """Log an error and emit a small HTML error response, then exit."""
    log(msg)
    # HTTP header
    sys.stdout.write("Content-Type: text/html; charset=utf-8\n\n")
    # Body
    safe_msg = html.escape(msg)
    sys.stdout.write(
        "<HTML><HEAD><TITLE>Web Chord: Error</TITLE></HEAD>"
        "<BODY><H1>Error</H1><P>\n"
        f"{safe_msg}\n"
        "</P></BODY></HTML>"
    )
    sys.exit(0)


def parse_chopro_line(line: str, mode: int) -> str:
    """
    Parse a single ChordPro content line (not a directive).

    Returns HTML representing either a <BR>, a <DIV> with lyrics only,
    or a <TABLE> with chords above lyrics, using the same CSS class names
    as the original Perl script.
    """
    # mode = 0 normal, 1 chorus, 2 normal+tab, 3 chorus+tab
    l_classes = ["lyrics", "lyrics_chorus", "lyrics_tab", "lyrics_chorus_tab"]
    c_classes = ["chords", "chords_chorus", "chords_tab", "chords_chorus_tab"]

    # Replace spaces with &nbsp; to preserve alignment (like Perl version)
    line = line.replace(" ", "&nbsp;")

    chords: List[str] = [""]
    lyrics: List[str] = []

    rest = line
    while True:
        # Find next [chord]
        start = rest.find("[")
        if start == -1:
            break
        end = rest.find("]", start + 1)
        if end == -1:
            break

        before = rest[:start]
        chord = rest[start + 1 : end]
        after = rest[end + 1 :]

        lyrics.append(before)

        # In the Perl version there is a special-case for '\|', but that
        # is an edge case; here we just pass the chord through unchanged.
        chords.append(chord)

        rest = after

    # Remaining lyrics after last chord
    lyrics.append(rest)

    # If line began with a chord, first lyrics and chord entries are empty
    if lyrics and lyrics[0] == "":
        chords = chords[1:]
        lyrics = lyrics[1:]

    # Empty line?
    if not lyrics or all(part == "" for part in lyrics):
        return "<BR>\n"

    # Line without chords
    if len(lyrics) == 1 and (not chords or chords[0] == ""):
        return f'<DIV class="{l_classes[mode]}">{lyrics[0]}</DIV>\n'

    # Line with chords -> two-row table
    out = []
    out.append('<TABLE cellpadding="0" cellspacing="0">')
    # Chords row
    out.append("<TR>")
    for c in chords:
        out.append(f'<TD class="{c_classes[mode]}">{c}</TD>')
    out.append("</TR>")
    # Lyrics row
    out.append("<TR>")
    for l in lyrics:
        out.append(f'<TD class="{l_classes[mode]}">{l}</TD>')
    out.append("</TR></TABLE>\n")
    return "".join(out)


def chopro2html(chopro: str) -> str:
    """
    Convert a ChordPro document to a full HTML page, following the
    behavior of the original webchord.pl script.
    """
    # Escape HTML special characters
    chopro = html.escape(chopro)

    # Extract title
    title = "ChordPro song"
    for raw_line in chopro.splitlines():
        line = raw_line.strip()
        if line.lower().startswith("{title:") and line.endswith("}"):
            title = line[7:-1].strip()
            break
        if line.lower().startswith("{t:") and line.endswith("}"):
            title = line[3:-1].strip()
            break

    # Start building HTML
    out: List[str] = []
    out.append(f"<HTML><HEAD><TITLE>{title}</TITLE>")
    out.append(
        "<STYLE TYPE=\"text/css\"><!--\n"
        "H1 {\n"
        "font-family: \"Arial\", Helvetica;\n"
        "font-size: 24pt;\n"
        "}\n"
        "H2 {\n"
        "font-family: \"Arial\", Helvetica;\n"
        "font-size: 16pt;\n"
        "}\n"
        ".lyrics, .lyrics_chorus { font-size: 12pt; }\n"
        ".lyrics_tab, .lyrics_chorus_tab { font-family: \"Courier New\", Courier; font-size: 10pt; }\n"
        ".lyrics_chorus, .lyrics_chorus_tab, .chords_chorus, .chords_chorus_tab { font-weight: bold; }\n"
        ".chords, .chords_chorus, .chords_tab, .chords_chorus_tab { font-size: 10pt; color: blue; padding-right: 4pt;}\n"
        ".comment, .comment_italic, .comment_box { background-color: #ffbbaa; }\n"
        ".comment_italic { font-style: italic; }\n"
        ".comment_box { border: solid; }\n"
        "--></STYLE>\n"
        "</HEAD><BODY>\n"
        "<!--\nConverted from ChordPro format with Web Chord (Python port)\n-->\n"
    )

    mode = 0  # 0 normal, 1 chorus, 2 normal+tab, 3 chorus+tab

    for raw_line in chopro.splitlines():
        line = raw_line.rstrip("\n")

        # Comment line starting with #
        if line.startswith("#"):
            out.append(f"<!--{line[1:]}-->\n")
            continue

        # Command line enclosed in { }
        if line.startswith("{") and line.endswith("}"):
            inner = line[1:-1]
            lower = inner.lower()

            # Title
            if lower.startswith("title:") or lower.startswith("t:"):
                value = inner.split(":", 1)[1]
                out.append(f"<H1>{value}</H1>\n")
            # Artist (rendered similar to subtitle, under the title)
            elif lower.startswith("artist:") or lower.startswith("a:"):
                value = inner.split(":", 1)[1]
                out.append(f"<H2>{value}</H2>\n")
            # Subtitle
            elif lower.startswith("subtitle:") or lower.startswith("st:"):
                value = inner.split(":", 1)[1]
                out.append(f"<H2>{value}</H2>\n")
            # Chorus markers
            elif lower.startswith("start_of_chorus") or lower.startswith("soc"):
                mode |= 1
            elif lower.startswith("end_of_chorus") or lower.startswith("eoc"):
                mode &= ~1
            # Comments
            elif lower.startswith("comment_italic:") or lower.startswith("ci:"):
                value = inner.split(":", 1)[1]
                out.append(f'<P class="comment_italic">{value}</P>\n')
            elif lower.startswith("comment_box:") or lower.startswith("cb:"):
                value = inner.split(":", 1)[1]
                out.append(f'<P class="comment_box">{value}</P>\n')
            elif lower.startswith("comment:") or lower.startswith("c:"):
                value = inner.split(":", 1)[1]
                out.append(f'<P class="comment">{value}</P>\n')
            # Tab markers
            elif lower.startswith("start_of_tab") or lower.startswith("sot"):
                mode |= 2
            elif lower.startswith("end_of_tab") or lower.startswith("eot"):
                mode &= ~2
            else:
                out.append(f"<!--Unsupported command: {inner}-->\n")
            continue

        # Standalone section headings like "Chorus:", "Chorus 1:", "Verse 2:", "Bridge:", "Pre-Chorus:", etc.
        # Matches any word (optionally with a number or hyphenated) followed by a colon, as a complete line.
        heading_match = re.match(r"^\s*([A-Za-z][A-Za-z0-9\- ]*?)\s*:\s*$", line)
        if heading_match:
            # Strip trailing colon and whitespace for display
            heading_text = heading_match.group(1).strip()
            out.append(f"<H4>{heading_text}</H4>\n")
            continue

        # Regular line with chords/lyrics
        out.append(parse_chopro_line(line, mode))

    out.append("</BODY></HTML>")
    return "".join(out)


def _read_cgi_body() -> bytes:
    content_length = int(os.environ.get("CONTENT_LENGTH") or 0)
    if content_length <= 0:
        return b""
    return sys.stdin.buffer.read(content_length)


def _parse_multipart_chordpro(body: bytes, content_type: str) -> Tuple[Optional[str], Optional[str], Optional[bytes]]:
    """Return (text_value, upload_filename, upload_bytes) for the chordpro field."""
    header = f"Content-Type: {content_type}\r\n\r\n".encode("latin-1", errors="replace")
    msg = message_from_bytes(header + body, policy=HTTP)
    for part in msg.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if name != "chordpro":
            continue
        payload = part.get_payload(decode=True)
        if payload is None:
            payload = b""
        filename = part.get_filename()
        if filename:
            return None, filename, payload
        return payload.decode("utf-8", errors="replace"), None, None
    return None, None, None


def _parse_cgi_chordpro() -> Optional[str]:
    """Parse CGI GET/POST input and return the chordpro field value."""
    method = os.environ.get("REQUEST_METHOD", "GET").upper()
    if method == "GET":
        values = parse_qs(os.environ.get("QUERY_STRING", ""), keep_blank_values=True).get("chordpro", [])
        if values:
            log("Text box used.")
        return values[0] if values else None

    content_type = os.environ.get("CONTENT_TYPE", "")
    body = _read_cgi_body()
    if not body:
        return None

    if content_type.startswith("multipart/form-data"):
        text_value, filename, upload_bytes = _parse_multipart_chordpro(body, content_type)
        if upload_bytes is not None:
            log(f"Upload: file name={filename}")
            return upload_bytes.decode("utf-8", errors="replace")
        if text_value is not None:
            log("Text box used.")
        return text_value

    values = parse_qs(body.decode("utf-8", errors="replace"), keep_blank_values=True).get("chordpro", [])
    if values:
        log("Text box used.")
    return values[0] if values else None


def main() -> None:
    chopro = _parse_cgi_chordpro()
    if chopro is None:
        bailout("No chordpro parameter")
        return

    # Emit header and HTML body
    sys.stdout.write("Content-Type: text/html; charset=utf-8\n\n")
    sys.stdout.write(chopro2html(chopro))
    sys.stdout.flush()


if __name__ == "__main__":
    main()

