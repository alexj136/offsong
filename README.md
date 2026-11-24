# Offsong

A vibe-coded ChordPro renderer that runs in a web browser.

## Setup

### Ubuntu
```
sudo apt install python3-flask python3-watchdog
python3 app.py --dir /path/to/song/files # (defaults to current dir)
```
Then open `http://localhost:5000`.

### macOS

> **_Note:_** These steps should also work for linux distributions with pip and/or venv installed,
e.g. with `sudo apt install python3-venv`.

There's no system package for Flask, and Homebrew's Python refuses global
`pip install`, so use a virtualenv. The built-in `python3` is fine (3.9+),
or `brew install python` for a newer one.

```
python3 -m venv .venv
.venv/bin/pip install flask watchdog
.venv/bin/python app.py --dir /path/to/song/files --port 5173
```
Then open `http://localhost:5173`.

Port 5000 is taken by AirPlay Receiver on macOS, so pass `--port` (or turn
AirPlay Receiver off in System Settings → General → AirDrop & Handoff).
