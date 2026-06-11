#!/usr/bin/env python3
"""ytm — a beautiful YouTube Music terminal client.

Search, library, playlists, queue, radio. Playback via mpv + yt-dlp.
True-color half-block album art, animated visualizer, gradient UI.

Usage:
  ytm                 launch the TUI
  ytm --auth          sign in by pasting request headers from music.youtube.com
  ytm --login         interactive sign-in wizard (pick your browser)
  ytm --auth-firefox  import your session from Firefox's cookie store
  ytm --doctor        check that all dependencies are healthy
"""

import argparse
import base64
import fcntl
import json
import math
import os
import random
import re
import select
import shutil
import signal
import socket
import sqlite3
import struct
import subprocess
import sys
import tempfile
import termios
import threading
import time
import tty
import unicodedata
import urllib.request
import zlib
from dataclasses import dataclass, field

# ──────────────────────────────────────────────────────────────────────────────
# paths / config
# ──────────────────────────────────────────────────────────────────────────────

CONFIG_DIR = os.path.expanduser("~/.config/ytm-tui")
AUTH_FILE = os.path.join(CONFIG_DIR, "browser.json")
STATE_FILE = os.path.join(CONFIG_DIR, "state.json")

UA = ("Mozilla/5.0 (X11; Linux x86_64; rv:151.0) "
      "Gecko/20100101 Firefox/151.0")

# palette — RED/ORANGE/PINK are theme slots, swapped by set_theme()
RED = (255, 0, 51)
PINK = (255, 94, 125)
ORANGE = (255, 138, 41)
WHITE = (236, 236, 240)
GREY = (130, 130, 142)
DGREY = (70, 70, 80)
DARK = (24, 24, 30)
GREEN = (108, 222, 130)

# name, primary, secondary, accent
THEMES = [
    ("ytm",       (255, 0, 51),    (255, 138, 41),  (255, 94, 125)),
    ("synthwave", (255, 56, 222),  (110, 80, 255),  (0, 229, 255)),
    ("matrix",    (0, 210, 70),    (110, 255, 130), (200, 255, 210)),
    ("ocean",     (0, 130, 255),   (0, 215, 215),   (160, 235, 255)),
    ("sunset",    (255, 80, 0),    (255, 195, 40),  (255, 120, 145)),
    ("ice",       (120, 165, 255), (195, 220, 255), (245, 250, 255)),
]


def set_theme(i):
    global RED, ORANGE, PINK
    _, RED, ORANGE, PINK = THEMES[i % len(THEMES)]

RESET = "\x1b[0m"
BOLD = "\x1b[1m"
DIM = "\x1b[2m"
ITAL = "\x1b[3m"

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
KITTY_MARK = "\x10"   # placeholder cell marking where a kitty image lands


def fg(c):
    return f"\x1b[38;2;{c[0]};{c[1]};{c[2]}m"


def bg(c):
    return f"\x1b[48;2;{c[0]};{c[1]};{c[2]}m"


def lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def grad_text(text, c1, c2):
    """Color text with a horizontal gradient."""
    n = max(len(text) - 1, 1)
    out = []
    for i, ch in enumerate(text):
        out.append(fg(lerp(c1, c2, i / n)) + ch)
    return "".join(out) + RESET


def char_w(ch):
    return 2 if unicodedata.east_asian_width(ch) in "WF" else 1


def visible_len(s):
    return sum(char_w(ch) for ch in ANSI_RE.sub("", s))


def crop_pad(s, w):
    """Crop a string with ANSI codes to visible width w, then pad with spaces."""
    out, vis, i = [], 0, 0
    while i < len(s) and vis < w:
        m = ANSI_RE.match(s, i)
        if m:
            out.append(m.group())
            i = m.end()
            continue
        cw = char_w(s[i])
        if vis + cw > w:
            break
        out.append(s[i])
        vis += cw
        i += 1
    out.append(RESET)
    return "".join(out) + " " * (w - vis)


def ansi_cells(s, w):
    """Explode an ANSI string into w (sgr_prefix, char) cells so overlays
    can replace individual columns without nuking the colors around them.
    Wide chars take two cells; the second is a "" continuation."""
    cells, cur, i = [], "", 0
    while i < len(s) and len(cells) < w:
        m = ANSI_RE.match(s, i)
        if m:
            g = m.group()
            cur = "" if g == RESET else cur + g
            i = m.end()
            continue
        ch = s[i]
        cells.append((cur, ch))
        if char_w(ch) == 2 and len(cells) < w:
            cells.append((cur, ""))
        i += 1
    while len(cells) < w:
        cells.append(("", " "))
    return cells


def put_cell(cells, i, sgr, ch):
    if i < 0 or i >= len(cells):
        return
    if cells[i][1] == "":                       # tail of a wide char
        cells[i - 1] = ("", " ")
    elif i + 1 < len(cells) and cells[i + 1][1] == "":
        cells[i + 1] = ("", " ")                # head of a wide char
    cells[i] = (sgr, ch)


def sgr_to_bg(sgr):
    """Best-guess background code for a cell's SGR prefix — its own bg if
    set, else its fg recolored as bg. Lets half-block overlays blend with
    whatever they're stamped on instead of flashing terminal-black."""
    mb = re.search(r"\x1b\[48;2;([0-9;]+)m", sgr)
    if mb:
        return "\x1b[48;2;" + mb.group(1) + "m"
    mf = re.search(r"\x1b\[38;2;([0-9;]+)m", sgr)
    if mf:
        return "\x1b[48;2;" + mf.group(1) + "m"
    return ""


def cells_to_str(cells):
    out, cur = [], None
    for sgr, ch in cells:
        if ch == "":
            continue
        if sgr != cur:
            out.append(RESET + sgr)
            cur = sgr
        out.append(ch)
    out.append(RESET)
    return "".join(out)


def fmt_time(secs):
    if secs is None or secs < 0:
        return "-:--"
    secs = int(secs)
    if secs >= 3600:
        return f"{secs // 3600}:{secs % 3600 // 60:02d}:{secs % 60:02d}"
    return f"{secs // 60}:{secs % 60:02d}"


# ──────────────────────────────────────────────────────────────────────────────
# data
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class Track:
    video_id: str
    title: str
    artist: str
    album: str = ""
    duration: str = ""
    thumb: str = ""
    set_video_id: str = ""   # playlist membership id, needed for removal

    @classmethod
    def from_item(cls, it):
        """Parse a track out of any ytmusicapi result shape."""
        vid = it.get("videoId") or ""
        title = it.get("title") or "?"
        artists = it.get("artists") or []
        artist = ", ".join(a.get("name", "") for a in artists if a.get("name")) or "Unknown"
        album = it.get("album")
        album = (album.get("name") if isinstance(album, dict) else album) or ""
        dur = it.get("duration") or it.get("length") or ""
        thumbs = it.get("thumbnails") or it.get("thumbnail") or []
        if isinstance(thumbs, dict):
            thumbs = thumbs.get("thumbnails", [])
        thumb = thumbs[-1]["url"] if thumbs else ""
        return cls(vid, title, artist, album, dur, thumb,
                   it.get("setVideoId") or "")


@dataclass
class Playlist:
    playlist_id: str
    title: str
    count: str = ""


# ──────────────────────────────────────────────────────────────────────────────
# mpv player over JSON IPC
# ──────────────────────────────────────────────────────────────────────────────

class Player:
    def __init__(self, on_track_end, ao=None, volume=70):
        self.sock_path = os.path.join(
            tempfile.gettempdir(), f"ytm-mpv-{os.getpid()}.sock")
        self.on_track_end = on_track_end
        self.props = {"pause": False, "volume": float(volume), "mute": False,
                      "time-pos": None, "duration": None}
        self._lock = threading.Lock()
        self._loading = False
        args = [
            "mpv", "--idle=yes", "--no-video", "--no-terminal",
            f"--input-ipc-server={self.sock_path}",
            f"--volume={volume}",
            "--ytdl-format=bestaudio[acodec^=opus]/bestaudio/best",
            "--cache=yes", "--cache-secs=30",
        ]
        if ao:
            args.append(f"--ao={ao}")
        self.proc = subprocess.Popen(
            args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.sock = None
        deadline = time.time() + 8
        while time.time() < deadline:
            try:
                s = socket.socket(socket.AF_UNIX)
                s.connect(self.sock_path)
                self.sock = s
                break
            except OSError:
                time.sleep(0.1)
        if not self.sock:
            raise RuntimeError("could not connect to mpv IPC socket")
        threading.Thread(target=self._reader, daemon=True).start()
        for i, prop in enumerate(
                ["time-pos", "duration", "pause", "volume", "mute"], 1):
            self._send({"command": ["observe_property", i, prop]})

    def _send(self, obj):
        with self._lock:
            try:
                self.sock.sendall(json.dumps(obj).encode() + b"\n")
            except OSError:
                pass

    def _reader(self):
        buf = b""
        while True:
            try:
                data = self.sock.recv(8192)
            except OSError:
                return
            if not data:
                return
            buf += data
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                ev = msg.get("event")
                if ev == "property-change":
                    self.props[msg.get("name")] = msg.get("data")
                elif ev == "end-file" and msg.get("reason") == "eof":
                    self.on_track_end()
                elif ev == "file-loaded":
                    self._loading = False

    def cmd(self, *args):
        self._send({"command": list(args)})

    def play_video(self, video_id):
        self._loading = True
        self.props["time-pos"] = None
        self.props["duration"] = None
        self.cmd("loadfile", f"https://music.youtube.com/watch?v={video_id}")
        self.cmd("set_property", "pause", False)

    @property
    def loading(self):
        return self._loading

    def toggle_pause(self):
        self.cmd("cycle", "pause")

    def seek(self, secs):
        self.cmd("seek", secs, "relative")

    def add_volume(self, d):
        v = max(0, min(130, (self.props.get("volume") or 70) + d))
        self.cmd("set_property", "volume", v)
        self.props["volume"] = v

    def toggle_mute(self):
        self.cmd("cycle", "mute")

    def stop(self):
        self.cmd("stop")

    def quit(self):
        self.cmd("quit")
        try:
            self.proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        try:
            os.unlink(self.sock_path)
        except OSError:
            pass


# ──────────────────────────────────────────────────────────────────────────────
# real-time spectrum tap (PipeWire/Pulse monitor → FFT)
# ──────────────────────────────────────────────────────────────────────────────

class SpectrumTap:
    """Captures the default sink's monitor with parec and serves FFT levels.

    levels(n) returns n floats in 0..1, log-spaced 45 Hz – 11 kHz, auto-gained.
    Falls back gracefully: .alive is False if capture isn't possible.
    """

    RATE = 44100
    CHUNK = 2048  # ~46 ms per FFT frame — steadier spectrum for the bars

    def __init__(self, source_cmd=None):
        self.alive = False
        self._spectrum = None     # raw |rfft| of latest chunk
        self._samples = None      # raw samples of latest chunk
        self._gain = 1e-6
        self._amp = 1e-4          # running amplitude for waveform autogain
        self._sig_t = 0.0         # last time we actually heard something
        self._lock = threading.Lock()
        self._proc = None
        self._source_cmd = source_cmd
        self._stop = False
        try:
            import numpy  # noqa: F401  (fail early if missing)
        except ImportError:
            return
        threading.Thread(target=self._run, daemon=True).start()

    @staticmethod
    def _default_sink():
        try:
            return subprocess.run(
                ["pactl", "get-default-sink"], capture_output=True,
                text=True, timeout=3).stdout.strip()
        except Exception:
            return ""

    def _build_cmd(self):
        if self._source_cmd:
            return self._source_cmd
        if not shutil.which("parec"):
            return None
        sink = self._default_sink()
        if not sink:
            return None
        self._sink = sink
        return ["parec", "--raw", "--format=float32le",
                f"--rate={self.RATE}", "--channels=1",
                "--latency-msec=30", "-d", f"{sink}.monitor"]

    def _run(self):
        import numpy as np
        win = np.hanning(self.CHUNK).astype(np.float32)
        nbytes = self.CHUNK * 4
        while not self._stop:
            cmd = self._build_cmd()
            if not cmd:
                return
            try:
                self._proc = subprocess.Popen(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            except OSError:
                return
            self.alive = True
            buf = b""
            check_t = time.time()
            switched = False
            while not self._stop:
                data = self._proc.stdout.read(nbytes - len(buf))
                if not data:
                    break
                buf += data
                if len(buf) < nbytes:
                    continue
                samples = np.frombuffer(buf, dtype=np.float32)
                buf = b""
                spec = np.abs(np.fft.rfft(samples * win))
                if float(np.abs(samples).max()) > 1e-3:
                    self._sig_t = time.time()
                with self._lock:
                    self._spectrum = spec
                    self._samples = samples
                # follow the default sink: plugging in headphones moves it,
                # but parec stays pinned to the old monitor — re-pin
                if (not self._source_cmd and getattr(self, "_sink", "")
                        and time.time() - check_t > 2.0):
                    check_t = time.time()
                    now = self._default_sink()
                    if now and now != self._sink:
                        switched = True
                        try:
                            self._proc.kill()
                        except OSError:
                            pass
                        break
            self.alive = False
            self._proc = None
            if self._stop:
                return
            if not switched:
                time.sleep(3)  # sink vanished (e.g. BT drop) — retry

    def levels(self, n):
        import numpy as np
        with self._lock:
            spec = self._spectrum
        if spec is None:
            return [0.0] * n
        freqs = np.fft.rfftfreq(self.CHUNK, 1 / self.RATE)
        edges = np.logspace(math.log10(45), math.log10(11000), n + 1)
        idx = np.searchsorted(freqs, edges)
        vals = np.zeros(n)
        for i in range(n):
            lo, hi = idx[i], max(idx[i + 1], idx[i] + 1)
            vals[i] = spec[lo:hi].mean() if lo < len(spec) else 0.0
        # gentle high-frequency tilt so the right side isn't always dead
        vals *= 1.0 + 1.6 * np.linspace(0, 1, n)
        peak = float(vals.max())
        self._gain = max(self._gain * 0.996, peak, 1e-6)
        out = np.sqrt(np.clip(vals / self._gain, 0, 1))
        return out.tolist()

    @property
    def producing(self):
        """True only if capture is up AND has heard real audio recently.
        Lets the visualizer fall back to animation when the monitor is
        silent (e.g. WSL can't loop back the sink)."""
        return self.alive and (time.time() - self._sig_t) < 1.5

    def samples(self, n):
        """n waveform points in -1..1, autogained to fill the scope."""
        import numpy as np
        with self._lock:
            s = self._samples
        if s is None:
            return [0.0] * n
        self._amp = max(self._amp * 0.995, float(np.abs(s).max()), 1e-4)
        idx = np.linspace(0, len(s) - 1, n).astype(int)
        return np.clip(s[idx] / self._amp, -1, 1).tolist()

    def stop(self):
        self._stop = True
        if self._proc:
            try:
                self._proc.kill()
            except OSError:
                pass


# ──────────────────────────────────────────────────────────────────────────────
# album art → ANSI half-blocks
# ──────────────────────────────────────────────────────────────────────────────

class ArtCache:
    def __init__(self):
        self._cache = {}   # (url, w, h) -> list[str]
        self._fetching = set()
        self._lock = threading.Lock()

    def get(self, url, w, h):
        """Return rendered art lines, or None and fetch in background."""
        if not url:
            return None
        key = (url, w, h)
        with self._lock:
            if key in self._cache:
                return self._cache[key]
            if key in self._fetching:
                return None
            self._fetching.add(key)
        threading.Thread(target=self._fetch, args=(url, w, h),
                         daemon=True).start()
        return None

    def _fetch(self, url, w, h):
        key = (url, w, h)
        try:
            from PIL import Image
            import io
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            raw = urllib.request.urlopen(req, timeout=10).read()
            img = Image.open(io.BytesIO(raw)).convert("RGB")
            # crop to square, then sample w × h*2 pixels (half-block doubling)
            side = min(img.size)
            left = (img.width - side) // 2
            top = (img.height - side) // 2
            img = img.crop((left, top, left + side, top + side))
            img = img.resize((w, h * 2), Image.LANCZOS)
            px = img.load()
            lines = []
            for row in range(h):
                parts = []
                for col in range(w):
                    t = px[col, row * 2]
                    b = px[col, row * 2 + 1]
                    parts.append(fg(t) + bg(b) + "▀")
                lines.append("".join(parts) + RESET)
            with self._lock:
                self._cache[key] = lines
        except Exception:
            with self._lock:
                self._fetching.discard(key)


# ──────────────────────────────────────────────────────────────────────────────
# auth helpers
# ──────────────────────────────────────────────────────────────────────────────

def firefox_cookie_dbs():
    roots = [
        os.path.expanduser("~/.mozilla/firefox"),
        os.path.expanduser("~/.config/mozilla/firefox"),
        os.path.expanduser("~/.var/app/org.mozilla.firefox/.mozilla/firefox"),
        os.path.expanduser("~/.var/app/io.gitlab.librewolf-community/.librewolf"),
        os.path.expanduser("~/.librewolf"),
    ]
    dbs = []
    for root in roots:
        if not os.path.isdir(root):
            continue
        for prof in sorted(os.listdir(root)):
            db = os.path.join(root, prof, "cookies.sqlite")
            if os.path.isfile(db):
                dbs.append(db)
    # WSL: the friend's browser lives on the Windows side. Firefox cookies
    # aren't encrypted, so we can read them straight off the mounted drive.
    import glob
    for pat in ("/mnt/*/Users/*/AppData/Roaming/Mozilla/Firefox/Profiles/"
                "*/cookies.sqlite",
                "/mnt/*/Users/*/AppData/Roaming/librewolf/Profiles/"
                "*/cookies.sqlite"):
        dbs.extend(sorted(glob.glob(pat)))
    return dbs


def import_firefox_auth():
    dbs = firefox_cookie_dbs()
    if not dbs:
        print("✗ no Firefox/LibreWolf profile with cookies found.")
        print("  Log in to https://music.youtube.com in Firefox first,")
        print("  then run:  ytm --auth-firefox")
        return False
    for db in dbs:
        cookies = {}
        try:
            # copy first: Firefox holds a lock on the live DB.
            # Bring the WAL/SHM along or fresh logins are invisible.
            with tempfile.TemporaryDirectory() as td:
                tmp = os.path.join(td, "cookies.sqlite")
                shutil.copyfile(db, tmp)
                for ext in ("-wal", "-shm"):
                    if os.path.isfile(db + ext):
                        shutil.copyfile(db + ext, tmp + ext)
                con = sqlite3.connect(tmp)
                # skip ST-* session-tab junk; ASC order so dupes resolve
                # to the most recently touched cookie
                rows = con.execute(
                    "SELECT name, value FROM moz_cookies "
                    "WHERE host IN ('.youtube.com', 'music.youtube.com', "
                    "'.music.youtube.com') AND name NOT LIKE 'ST-%' "
                    "ORDER BY lastAccessed ASC").fetchall()
                con.close()
            cookies = dict(rows)
        except Exception as e:
            print(f"  (skipping {db}: {e})")
            continue
        if write_auth_from_cookies(cookies):
            print(f"✓ signed in — session imported from {db}")
            return True
        print(f"  (cookies in {db} didn't authenticate, trying next profile)")
    print("✗ found Firefox profiles, but none had a valid YouTube Music login.")
    print("  Log in at https://music.youtube.com and retry.")
    return False


def write_auth_from_cookies(cookies):
    """Build browser.json from a {name: value} cookie dict and verify it."""
    sapisid = cookies.get("SAPISID") or cookies.get("__Secure-3PAPISID")
    if not sapisid:
        return False
    cookie_str = "; ".join(f"{k}={v}" for k, v in cookies.items())
    # ytmusicapi requires a SAPISIDHASH authorization header to detect
    # browser auth (it re-derives a fresh one per request)
    import hashlib
    origin = "https://music.youtube.com"
    ts = str(int(time.time()))
    sha = hashlib.sha1(f"{ts} {sapisid} {origin}".encode()).hexdigest()
    os.makedirs(CONFIG_DIR, exist_ok=True)
    headers = {
        "user-agent": UA,
        "accept": "*/*",
        "accept-language": "en-US,en;q=0.5",
        "content-type": "application/json",
        "x-goog-authuser": "0",
        "x-origin": origin,
        "origin": origin,
        "authorization": f"SAPISIDHASH {ts}_{sha}",
        "cookie": cookie_str,
    }
    with open(AUTH_FILE, "w") as f:
        json.dump(headers, f, indent=2)
    os.chmod(AUTH_FILE, 0o600)
    if verify_auth():
        return True
    os.unlink(AUTH_FILE)
    return False


def import_browser_auth(browser):
    """Pull the YT Music session from any browser yt-dlp can read
    (handles Chromium keyring decryption too)."""
    try:
        from yt_dlp.cookies import extract_cookies_from_browser
    except ImportError:
        print("✗ yt-dlp python module missing — pip install yt-dlp")
        return False
    try:
        jar = extract_cookies_from_browser(browser)
    except Exception as e:
        print(f"✗ couldn't read {browser} cookies: {e}")
        return False
    cookies = {c.name: c.value for c in jar
               if c.domain in (".youtube.com", "music.youtube.com",
                               ".music.youtube.com")
               and not c.name.startswith("ST-")}
    if write_auth_from_cookies(cookies):
        print(f"✓ signed in — session imported from {browser}")
        return True
    print(f"✗ {browser} had no valid YouTube Music login.")
    print("  Log in at https://music.youtube.com there and retry.")
    return False


def login_wizard():
    print()
    print("  ── sign in to YouTube Music ─────────────────────────")
    print()
    print("  Make sure you're logged in at https://music.youtube.com")
    print("  in your browser, then pick it:")
    print()
    browsers = ["firefox", "chrome", "chromium", "brave",
                "edge", "vivaldi", "opera"]
    for i, b in enumerate(browsers, 1):
        print(f"    {i}) {b}")
    print(f"    {len(browsers) + 1}) paste request headers manually")
    print()
    try:
        choice = input("  choice: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    if not choice.isdigit() or not 1 <= int(choice) <= len(browsers) + 1:
        print("  ✗ not a valid choice")
        return False
    n = int(choice)
    if n == len(browsers) + 1:
        return paste_headers_auth()
    browser = browsers[n - 1]
    ok = (import_firefox_auth() if browser == "firefox"
          else import_browser_auth(browser))
    if ok:
        print("  You're all set — run: ytm")
    return ok


def paste_headers_auth():
    print("── ytm sign-in ─────────────────────────────────────────────")
    print(" 1. Open https://music.youtube.com in your browser (logged in)")
    print(" 2. F12 → Network tab → filter “browse” → click any request")
    print(" 3. Copy ALL request headers (Firefox: right-click → Copy Request Headers)")
    print(" 4. Paste below, then press Ctrl-D on an empty line:")
    print("────────────────────────────────────────────────────────────")
    raw = sys.stdin.read()
    if "cookie" not in raw.lower():
        print("✗ that didn't look like request headers (no Cookie line).")
        return False
    import ytmusicapi
    os.makedirs(CONFIG_DIR, exist_ok=True)
    ytmusicapi.setup(filepath=AUTH_FILE, headers_raw=raw)
    os.chmod(AUTH_FILE, 0o600)
    if verify_auth():
        print("✓ signed in — auth saved to", AUTH_FILE)
        return True
    print("✗ headers saved but authentication failed; try copying them again.")
    return False


def verify_auth():
    try:
        from ytmusicapi import YTMusic
        yt = YTMusic(AUTH_FILE)
        yt.get_library_playlists(limit=1)
        return True
    except Exception:
        return False


# ──────────────────────────────────────────────────────────────────────────────
# the app
# ──────────────────────────────────────────────────────────────────────────────

TABS = ["Search", "Library", "Playlists", "Queue"]
BLOCKS = " ▁▂▃▄▅▆▇█"
VIZ_STYLES = ["bars", "mirror", "scope", "bands", "drop"]
BADGE = ["█▙  ", "███▙", "█▛  "]
LOGO = [
    "█▖ ▗█ ▀▀█▀▀   █▀▄▀█ █   █ ▗█▀▀▀ ▀█▀ ▗█▀▀",
    "▝█▄█▘   █     █ ▀ █ █   █ ▝▀▀█▖  █  ▐▌  ",
    "  █     █     █   █ ▜▄▄▄▛ ▄▄▄█▘ ▄█▄ ▝█▄▄",
]
_GLYPHS = {
    "Y": ["██╗   ██╗", "╚██╗ ██╔╝", " ╚████╔╝ ",
          "  ╚██╔╝  ", "   ██║   ", "   ╚═╝   "],
    "T": ["████████╗", "╚══██╔══╝", "   ██║   ",
          "   ██║   ", "   ██║   ", "   ╚═╝   "],
    "M": ["███╗   ███╗", "████╗ ████║", "██╔████╔██║",
          "██║╚██╔╝██║", "██║ ╚═╝ ██║", "╚═╝     ╚═╝"],
    "U": ["██╗   ██╗", "██║   ██║", "██║   ██║",
          "██║   ██║", "╚██████╔╝", " ╚═════╝ "],
    "S": ["███████╗", "██╔════╝", "███████╗",
          "╚════██║", "███████║", "╚══════╝"],
    "I": ["██╗", "██║", "██║", "██║", "██║", "╚═╝"],
    "C": [" ██████╗", "██╔════╝", "██║     ",
          "██║     ", "╚██████╗", " ╚═════╝"],
    " ": ["  "] * 6,
}
BIG_LOGO = [" ".join(_GLYPHS[ch][r] for ch in "YT MUSIC") for r in range(6)]


def shadow_text(line, c1, c2):
    """ANSI-shadow lettering: blocks get the gradient, shadows go dim."""
    n = max(len(line) - 1, 1)
    out = []
    for i, ch in enumerate(line):
        if ch == "█":
            out.append(fg(lerp(c1, c2, i / n)) + ch)
        elif ch == " ":
            out.append(ch)
        else:
            out.append(fg(DGREY) + ch)
    return "".join(out) + RESET
# braille dot bits, [x][y] within a 2×4 cell
BRAILLE = [[0x01, 0x02, 0x04, 0x40], [0x08, 0x10, 0x20, 0x80]]
# quadrant blocks indexed by TL*8 + TR*4 + BL*2 + BR
QUADS = " ▗▖▄▝▐▞▟▘▚▌▙▀▜▛█"


class App:
    def __init__(self, ao=None):
        from ytmusicapi import YTMusic
        self.authed = os.path.isfile(AUTH_FILE)
        try:
            self.yt = YTMusic(AUTH_FILE) if self.authed else YTMusic()
        except Exception:
            self.authed = False
            self.yt = YTMusic()

        state = self._load_state()
        self.player = Player(self._on_eof, ao=ao,
                             volume=state.get("volume", 70))
        self.art = ArtCache()
        self.tap = SpectrumTap()
        self.peaks: list[float] = []

        self.tab = 0
        self.sel = [0, 0, 0, 0]
        self.scroll = [0, 0, 0, 0]
        self.results: list[Track] = []
        self.lib: list[Track] = []
        self.playlists: list[Playlist] = []
        self.pl_tracks: list[Track] = []
        self.pl_open: Playlist | None = None
        self.queue: list[Track] = []
        self.qpos = -1
        self.now: Track | None = None

        self.repeat = state.get("repeat", False)
        self.work = state.get("work", False)
        self.viz_style = state.get("viz", "bars")
        if self.viz_style not in VIZ_STYLES:
            self.viz_style = "bars"
        names = [t[0] for t in THEMES]
        self.theme_i = names.index(state["theme"]) \
            if state.get("theme") in names else 0
        set_theme(self.theme_i)
        self._drop_t = random.uniform(0, 90)   # never open on the same look
        self._viz_cache = None
        self._phys_t = time.time()
        self._drop_last = time.time()
        self._drop_e = [0.0, 0.0, 0.0]   # smoothed bass/mid/treble
        self._drop_lut_cache = None
        # milkdrop-style preset morphing
        self._drop_preset = random.randrange(self.N_PRESETS)
        self._drop_prev = self._drop_preset
        self._drop_pa = self._drop_new_params()
        self._drop_pb = self._drop_pa
        self._drop_mix = 1.0
        self._drop_switch_at = time.time() + random.uniform(12, 24)
        # 0 = chunky half-blocks, 1 = hi-def quadrants, 2 = silk
        # (supersampled 4× then averaged), 3 = pixel: the field blitted
        # as a real bitmap via the kitty graphics protocol
        self._kitty_ok = self._kitty_sniff()
        self._kitty_payload = None
        self._kitty_live = False
        self._kitty_id = 0
        self.drop_px = int(state.get(
            "drop_px", 1 if state.get("drop_hd") else 0))
        if self.drop_px > (3 if self._kitty_ok else 2):
            self.drop_px = 2
        self._drop_last_switch = 0.0
        self._drop_bass_avg = 0.15
        self._beat_t = 0.0        # beat tracker: last kick + recent gaps
        self._beat_amp = 0.0
        self._beat_gaps = []
        self._drop_energy = 0.2   # slow mood bed, immune to frame jitter
        self.viz_react = float(state.get("viz_react", 1.0))   # [ ]
        self.viz_speed = float(state.get("viz_speed", 1.0))   # { }
        self.viz_morph = float(state.get("viz_morph", 1.0))   # preset churn
        self.menu = False         # M: slider overlay for all of the above
        self.menu_sel = 0
        self.full = False
        self.viz_max = False      # F: visualizer owns the whole terminal
        self.liked_now = False
        self.input_mode = False
        self.input_buf = ""
        self.input_purpose = "search"
        self.picker = None            # playlist-picker modal state
        self.picker_sel = 0
        self.picker_track = None
        self._confirm_del = ("", 0.0)
        self._now_wt = 0.0
        self._like_t = 0.0    # timestamp of last like → heart splash
        self._like_mode = "like"   # "like" = heart pop, "break" = heartbreak
        self.status = ""
        self.status_t = 0.0
        self.searching = False
        self.loading_msg = ""
        self.running = True
        self.eof_flag = threading.Event()
        self.bars: list[float] = []
        self._lib_fetched = False
        self._pls_fetched = False
        self.size = shutil.get_terminal_size()
        signal.signal(signal.SIGWINCH, self._winch)

    # ── persistence ──────────────────────────────────────────────────────────
    def _load_state(self):
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except Exception:
            return {}

    def _save_state(self):
        try:
            os.makedirs(CONFIG_DIR, exist_ok=True)
            with open(STATE_FILE, "w") as f:
                json.dump({"volume": self.player.props.get("volume", 70),
                           "repeat": self.repeat, "work": self.work,
                           "viz": self.viz_style,
                           "drop_px": self.drop_px,
                           "viz_react": self.viz_react,
                           "viz_speed": self.viz_speed,
                           "viz_morph": self.viz_morph,
                           "theme": THEMES[self.theme_i][0]}, f)
        except Exception:
            pass

    # ── helpers ──────────────────────────────────────────────────────────────
    def _winch(self, *_):
        self.size = shutil.get_terminal_size()

    def say(self, msg):
        self.status = msg
        self.status_t = time.time()

    def _on_eof(self):
        self.eof_flag.set()

    def current_list(self):
        if self.tab == 0:
            return self.results
        if self.tab == 1:
            return self.lib
        if self.tab == 2:
            return self.pl_tracks if self.pl_open else self.playlists
        return self.queue

    # ── data fetching (background threads) ───────────────────────────────────
    def _auth_refresh(self):
        """Google rotates session cookies; quietly re-import a fresh set
        from the browser and rebuild the client."""
        try:
            import io
            import contextlib
            from ytmusicapi import YTMusic
            with contextlib.redirect_stdout(io.StringIO()):
                ok = import_firefox_auth()
            if ok:
                self.yt = YTMusic(AUTH_FILE)
                self.authed = True
                return True
        except Exception:
            pass
        return False

    def _lib_call(self, fn):
        """Run a library call; on failure refresh auth from the browser
        once and retry (stale-cookie sessions look signed-out)."""
        try:
            return fn()
        except Exception:
            if self._auth_refresh():
                self.say("session went stale — refreshed from your browser")
                return fn()
            raise
    def do_search(self, query):
        self.searching = True

        def work():
            try:
                try:
                    items = self.yt.search(query, filter="songs", limit=30)
                except KeyError:
                    # ytmusicapi can't parse some authed filtered responses
                    # (KeyError: 'musicShelfRenderer') — unfiltered works
                    items = self.yt.search(query, limit=30)
                self.results = [Track.from_item(i) for i in items
                                if i.get("videoId")]
                self.sel[0] = 0
                self.scroll[0] = 0
                self.say(f"{len(self.results)} results for “{query}”")
            except Exception as e:
                self.say(f"search failed: {e}")
            finally:
                self.searching = False
        threading.Thread(target=work, daemon=True).start()

    def fetch_library(self):
        if self._lib_fetched:
            return
        self._lib_fetched = True
        if not self.authed:
            self.say("library needs sign-in → quit and run: ytm --login")
            return
        self.loading_msg = "loading liked songs…"

        def work():
            try:
                data = self._lib_call(
                    lambda: self.yt.get_liked_songs(limit=300))
                self.lib = [Track.from_item(t) for t in data.get("tracks", [])
                            if t.get("videoId")]
                self.say(f"{len(self.lib)} liked songs")
            except Exception as e:
                self._lib_fetched = False
                self.say(f"library failed: {e}")
            finally:
                self.loading_msg = ""
        threading.Thread(target=work, daemon=True).start()

    def fetch_playlists(self):
        if self._pls_fetched:
            return
        self._pls_fetched = True
        if not self.authed:
            self.say("playlists need sign-in → quit and run: ytm --login")
            return
        self.loading_msg = "loading playlists…"

        def work():
            try:
                items = self._lib_call(
                    lambda: self.yt.get_library_playlists(limit=50))
                if not items and self._auth_refresh():
                    # signed-out responses come back empty, not as errors
                    items = self.yt.get_library_playlists(limit=50)
                self.playlists = [
                    Playlist(p["playlistId"], p.get("title", "?"),
                             str(p.get("count", "")))
                    for p in items]
            except Exception as e:
                self._pls_fetched = False
                self.say(f"playlists failed: {e}")
            finally:
                self.loading_msg = ""
        threading.Thread(target=work, daemon=True).start()

    def open_playlist(self, pl):
        self.loading_msg = f"opening “{pl.title}”…"

        def work():
            try:
                data = self._lib_call(
                    lambda: self.yt.get_playlist(pl.playlist_id, limit=300))
                self.pl_tracks = [Track.from_item(t)
                                  for t in data.get("tracks", [])
                                  if t and t.get("videoId")]
                self.pl_open = pl
                self.sel[2] = 0
                self.scroll[2] = 0
            except Exception as e:
                self.say(f"couldn't open playlist: {e}")
            finally:
                self.loading_msg = ""
        threading.Thread(target=work, daemon=True).start()

    def start_radio(self, track):
        """Fill the queue with YT Music's radio for a track."""
        def work():
            try:
                data = self.yt.get_watch_playlist(videoId=track.video_id,
                                                  radio=True)
                fresh = [Track.from_item(t) for t in data.get("tracks", [])
                         if t.get("videoId")]
                fresh = [t for t in fresh if t.video_id != track.video_id]
                if self.queue and self.queue[self.qpos].video_id == track.video_id:
                    self.queue[self.qpos + 1:] = fresh
                    self.say(f"radio: +{len(fresh)} tracks queued")
            except Exception:
                pass
        threading.Thread(target=work, daemon=True).start()

    # ── playback ─────────────────────────────────────────────────────────────
    def _write_now(self):
        """Drop now-playing info where widgets/scripts can read it.
        Lines: title, artist, album, pos secs, dur secs, state,
        queue idx/len, next-up title."""
        try:
            os.makedirs(CONFIG_DIR, exist_ok=True)
            with open(os.path.join(CONFIG_DIR, "now.txt"), "w") as f:
                if self.now:
                    pos = int(self.player.props.get("time-pos") or 0)
                    dur = int(self.player.props.get("duration") or 0)
                    state = ("paused" if self.player.props.get("pause")
                             else "playing")
                    nxt = (self.queue[self.qpos + 1].title
                           if 0 <= self.qpos + 1 < len(self.queue) else "")
                    f.write(f"{self.now.title}\n{self.now.artist}\n"
                            f"{self.now.album}\n{pos}\n{dur}\n{state}\n"
                            f"{self.qpos + 1}/{len(self.queue)}\n{nxt}\n")
        except Exception:
            pass

    def play_queue(self, idx):
        if not (0 <= idx < len(self.queue)):
            return
        self.qpos = idx
        self.now = self.queue[idx]
        self.liked_now = False
        if self.authed:
            self._fetch_like_state(self.now.video_id)
        self.player.play_video(self.now.video_id)
        self._write_now()
        self.say(f"▶ {self.now.title}")

    def next_track(self):
        if self.qpos + 1 < len(self.queue):
            self.play_queue(self.qpos + 1)
        elif self.repeat and self.queue:
            self.play_queue(0)
        else:
            self.now = None
            self._write_now()
            self.player.stop()

    def prev_track(self):
        pos = self.player.props.get("time-pos") or 0
        if pos > 5:
            self.player.seek(-pos)
        elif self.qpos > 0:
            self.play_queue(self.qpos - 1)

    def activate(self):
        lst = self.current_list()
        i = self.sel[self.tab]
        if not lst or not (0 <= i < len(lst)):
            return
        if self.tab == 0:                      # search → play + radio
            self.queue = [lst[i]]
            self.play_queue(0)
            self.start_radio(lst[i])
        elif self.tab == 1:                    # library → play from here
            self.queue = list(lst)
            self.play_queue(i)
        elif self.tab == 2:
            if self.pl_open:                   # inside playlist
                self.queue = list(lst)
                self.play_queue(i)
            else:                              # playlist list → open
                self.open_playlist(lst[i])
        else:                                  # queue
            self.play_queue(i)

    def add_to_queue(self):
        lst = self.current_list()
        i = self.sel[self.tab]
        if self.tab in (0, 1) or (self.tab == 2 and self.pl_open):
            if lst and 0 <= i < len(lst):
                self.queue.append(lst[i])
                self.say(f"+ queued: {lst[i].title}")

    def _fetch_like_state(self, vid):
        """The header ♥ (and the L toggle) should know whether the track
        is already liked, not just whether it was liked this session."""
        def work():
            try:
                data = self.yt.get_watch_playlist(videoId=vid, limit=1)
                tr = (data.get("tracks") or [{}])[0]
                if self.now and self.now.video_id == vid:
                    self.liked_now = tr.get("likeStatus") == "LIKE"
            except Exception:
                pass
        threading.Thread(target=work, daemon=True).start()

    def like_current(self):
        if not self.now:
            return
        if not self.authed:
            self.say("liking needs sign-in → ytm --login")
            return
        if self.liked_now:           # L on a liked song = unlike, heartbreak
            self._like_t = time.time()
            self._like_mode = "break"
            self.liked_now = False

            def work():
                try:
                    self.yt.rate_song(self.now.video_id, "INDIFFERENT")
                    self.say(f"♡ unliked: {self.now.title}")
                except Exception as e:
                    self.say(f"unlike failed: {e}")
            threading.Thread(target=work, daemon=True).start()
            return
        self._like_t = time.time()   # heart splash, optimistic
        self._like_mode = "like"

        def work():
            try:
                self.yt.rate_song(self.now.video_id, "LIKE")
                self.liked_now = True
                self.say(f"♥ liked: {self.now.title}")
            except Exception as e:
                self.say(f"like failed: {e}")
        threading.Thread(target=work, daemon=True).start()

    # ── library write ops ────────────────────────────────────────────────────
    def create_playlist(self, name):
        def work():
            try:
                self.yt.create_playlist(name, "", "PRIVATE")
                self.say(f"✚ playlist created: {name}")
                self._pls_fetched = False
                self.fetch_playlists()
            except Exception as e:
                self.say(f"create failed: {e}")
        threading.Thread(target=work, daemon=True).start()

    def open_picker(self):
        """A = add the selected track to one of your playlists."""
        if not self.authed:
            self.say("playlists need sign-in → ytm --login")
            return
        lst = self.current_list()
        i = self.sel[self.tab]
        in_tracks = self.tab in (0, 1, 3) or (self.tab == 2 and self.pl_open)
        if not (in_tracks and lst and 0 <= i < len(lst)):
            return
        if not self.playlists:
            self.fetch_playlists()
            self.say("loading playlists — press A again in a second")
            return
        self.full = False
        self.viz_max = False
        self.picker = self.playlists
        self.picker_sel = 0
        self.picker_track = lst[i]

    def add_to_playlist(self, pl, track):
        def work():
            try:
                if pl.playlist_id == "LM":      # Liked Music is rate-based
                    self.yt.rate_song(track.video_id, "LIKE")
                    self._like_t = time.time()
                    self._like_mode = "like"
                    if self.now and self.now.video_id == track.video_id:
                        self.liked_now = True
                else:
                    self.yt.add_playlist_items(pl.playlist_id,
                                               [track.video_id])
                self.say(f"✚ {track.title} → {pl.title}")
            except Exception as e:
                self.say(f"add failed: {e}")
        threading.Thread(target=work, daemon=True).start()

    def unlike_at(self, i):
        """x in Library = remove from liked songs."""
        if not (0 <= i < len(self.lib)):
            return
        track = self.lib[i]
        self._like_t = time.time()
        self._like_mode = "break"
        if self.now and self.now.video_id == track.video_id:
            self.liked_now = False

        def work():
            try:
                self.yt.rate_song(track.video_id, "INDIFFERENT")
                if track in self.lib:
                    self.lib.remove(track)
                self.sel[1] = min(self.sel[1], max(len(self.lib) - 1, 0))
                self.say(f"♡ unliked: {track.title}")
            except Exception as e:
                self.say(f"unlike failed: {e}")
        threading.Thread(target=work, daemon=True).start()

    def remove_from_playlist(self, i):
        """x inside an open playlist = remove that track from it."""
        if not (self.pl_open and 0 <= i < len(self.pl_tracks)):
            return
        track = self.pl_tracks[i]
        if not track.set_video_id:
            self.say("can't remove this one (not an editable playlist item)")
            return
        pl = self.pl_open

        def work():
            try:
                self.yt.remove_playlist_items(
                    pl.playlist_id,
                    [{"videoId": track.video_id,
                      "setVideoId": track.set_video_id}])
                if track in self.pl_tracks:
                    self.pl_tracks.remove(track)
                self.sel[2] = min(self.sel[2],
                                  max(len(self.pl_tracks) - 1, 0))
                self.say(f"✗ removed from {pl.title}: {track.title}")
            except Exception as e:
                self.say(f"remove failed: {e}")
        threading.Thread(target=work, daemon=True).start()

    def delete_playlist(self, pl):
        """D twice on a playlist deletes it (with a confirm window)."""
        if not self.authed:
            self.say("playlists need sign-in → ytm --login")
            return
        if pl.playlist_id == "LM":
            self.say("can't delete Liked Music")
            return
        name, t0 = self._confirm_del
        if name == pl.playlist_id and time.time() - t0 < 3:
            self._confirm_del = ("", 0.0)

            def work():
                try:
                    self.yt.delete_playlist(pl.playlist_id)
                    self.playlists = [p for p in self.playlists
                                      if p.playlist_id != pl.playlist_id]
                    self.sel[2] = min(self.sel[2],
                                      max(len(self.playlists) - 1, 0))
                    self.say(f"✗ deleted playlist: {pl.title}")
                except Exception as e:
                    self.say(f"delete failed: {e}")
            threading.Thread(target=work, daemon=True).start()
        else:
            self._confirm_del = (pl.playlist_id, time.time())
            self.say(f"press D again to delete “{pl.title}”")

    def shuffle_queue(self):
        if len(self.queue) <= 1:
            return
        cur = self.queue[self.qpos] if 0 <= self.qpos < len(self.queue) else None
        random.shuffle(self.queue)
        if cur:
            self.queue.remove(cur)
            self.queue.insert(0, cur)
            self.qpos = 0
        self.say("⤨ queue shuffled")

    # ── input ────────────────────────────────────────────────────────────────
    def read_key(self, timeout):
        r, _, _ = select.select([sys.stdin], [], [], timeout)
        if not r:
            return None
        ch = os.read(sys.stdin.fileno(), 1).decode(errors="ignore")
        if ch == "\x1b":
            r, _, _ = select.select([sys.stdin], [], [], 0.01)
            if not r:
                return "ESC"
            seq = os.read(sys.stdin.fileno(), 2).decode(errors="ignore")
            return {"[A": "UP", "[B": "DOWN", "[C": "RIGHT",
                    "[D": "LEFT", "[Z": "SHIFT-TAB"}.get(seq, "ESC")
        return ch

    def handle_key(self, k):
        if self.input_mode:
            if k == "ESC":
                self.input_mode = False
                self.input_buf = ""
            elif k in ("\r", "\n"):
                self.input_mode = False
                text = self.input_buf.strip()
                if text and self.input_purpose == "search":
                    self.tab = 0
                    self.do_search(text)
                elif text and self.input_purpose == "newpl":
                    self.create_playlist(text)
                self.input_buf = ""
            elif k in ("\x7f", "\x08"):
                self.input_buf = self.input_buf[:-1]
            elif k and len(k) == 1 and k.isprintable():
                self.input_buf += k
            return

        if self.picker is not None:
            if k == "ESC":
                self.picker = None
            elif k in ("UP", "k"):
                self.picker_sel = max(0, self.picker_sel - 1)
            elif k in ("DOWN", "j"):
                self.picker_sel = min(len(self.picker) - 1,
                                      self.picker_sel + 1)
            elif k in ("\r", "\n"):
                self.add_to_playlist(self.picker[self.picker_sel],
                                     self.picker_track)
                self.picker = None
            return

        if self.menu:
            items = self._menu_items()
            if k in ("ESC", "M", "q"):
                self.menu = False
            elif k in ("UP", "k"):
                self.menu_sel = (self.menu_sel - 1) % len(items)
            elif k in ("DOWN", "j"):
                self.menu_sel = (self.menu_sel + 1) % len(items)
            elif k in ("LEFT", "h", "RIGHT", "l"):
                _, attr, lo, hi, step, _ = items[self.menu_sel]
                cur = getattr(self, attr) + (step if k in ("RIGHT", "l")
                                             else -step)
                cur = min(hi, max(lo, round(cur, 2)))
                setattr(self, attr, int(cur) if isinstance(step, int) else cur)
            return

        lst = self.current_list()
        if k == "q":
            self.running = False
        elif k == "/":
            self.full = False          # can't see the search box otherwise
            self.viz_max = False
            self.input_mode = True
            self.input_purpose = "search"
            self.input_buf = ""
        elif k == "N":
            if not self.authed:
                self.say("playlists need sign-in → ytm --login")
            else:
                self.full = False
                self.viz_max = False
                self.input_mode = True
                self.input_purpose = "newpl"
                self.input_buf = ""
        elif k == "A":
            self.open_picker()
        elif k == "D" and self.tab == 2 and not self.pl_open and lst:
            self.delete_playlist(lst[self.sel[2]])
        elif k in ("UP", "k"):
            self.sel[self.tab] = max(0, self.sel[self.tab] - 1)
        elif k in ("DOWN", "j"):
            self.sel[self.tab] = min(max(len(lst) - 1, 0),
                                     self.sel[self.tab] + 1)
        elif k in ("\r", "\n"):
            self.activate()
        elif k == " ":
            self.player.toggle_pause()
        elif k == "n":
            self.next_track()
        elif k == "b":
            self.prev_track()
        elif k in ("RIGHT", "."):
            self.player.seek(10)
        elif k in ("LEFT", ","):
            self.player.seek(-10)
        elif k in ("+", "="):
            self.player.add_volume(5)
        elif k == "-":
            self.player.add_volume(-5)
        elif k == "m":
            self.player.toggle_mute()
        elif k == "a":
            self.add_to_queue()
        elif k == "L":
            self.like_current()
        elif k == "s":
            self.shuffle_queue()
        elif k == "r":
            self.repeat = not self.repeat
            self.say(f"repeat {'on' if self.repeat else 'off'}")
        elif k == "v":
            i = VIZ_STYLES.index(self.viz_style)
            self.viz_style = VIZ_STYLES[(i + 1) % len(VIZ_STYLES)]
            self.bars = []          # reset physics for the new style
            self.say(f"visualizer: {self.viz_style}")
        elif k == "w":
            self.work = not self.work
            self.say("work mode — art hidden" if self.work
                     else "work mode off")
        elif k in ("c", "C"):
            self.theme_i = (self.theme_i + 1) % len(THEMES)
            set_theme(self.theme_i)
            self._drop_lut_cache = None
            self.say(f"theme: {THEMES[self.theme_i][0]}")
        elif k == "f":
            self.full = not self.full
            self.viz_max = False
        elif k == "F":
            self.viz_max = not self.viz_max
            if self.viz_max:
                self.say("visualizer maximized — F or esc brings it back")
        elif k == "M":
            self.menu = True
            self.menu_sel = 0
        elif k == "ESC" and self.viz_max:
            self.viz_max = False
        elif k == "p":
            self.drop_px = (self.drop_px + 1) % (4 if self._kitty_ok else 3)
            self.say(["drop: chunky pixels", "drop: hi-def pixels",
                      "drop: silk — supersampled smooth",
                      "drop: pixel — true pixels"][self.drop_px])
        elif k in ("[", "]"):
            step = 0.2 if k == "]" else -0.2
            self.viz_react = min(2.4, max(0.2, round(self.viz_react + step, 2)))
            self.say(f"viz beat punch: {self.viz_react:.1f}×")
        elif k in ("{", "}"):
            step = 0.2 if k == "}" else -0.2
            self.viz_speed = min(2.4, max(0.4, round(self.viz_speed + step, 2)))
            self.say(f"viz flow speed: {self.viz_speed:.1f}×")
        elif k == "\t":
            self.tab = (self.tab + 1) % len(TABS)
        elif k == "SHIFT-TAB":
            self.tab = (self.tab - 1) % len(TABS)
        elif k in "1234":
            self.tab = int(k) - 1
        elif k in ("ESC", "h") and self.tab == 2 and self.pl_open:
            self.pl_open = None
            self.pl_tracks = []
        elif k == "x" and self.tab == 3 and lst:
            i = self.sel[3]
            if 0 <= i < len(self.queue) and i != self.qpos:
                del self.queue[i]
                if i < self.qpos:
                    self.qpos -= 1
                self.sel[3] = min(self.sel[3], max(len(self.queue) - 1, 0))
        elif k == "x" and self.tab == 1 and lst:
            self.unlike_at(self.sel[1])
        elif k == "x" and self.tab == 2 and self.pl_open and lst:
            self.remove_from_playlist(self.sel[2])

        if self.tab == 1:
            self.fetch_library()
        elif self.tab == 2:
            self.fetch_playlists()

    # ── rendering ────────────────────────────────────────────────────────────
    def render(self):
        w, h = self.size.columns, self.size.lines
        if w < 60 or h < 16:
            sys.stdout.write("\x1b[H\x1b[2J" + fg(RED) +
                             "terminal too small (need ≥ 60×16)" + RESET)
            sys.stdout.flush()
            return
        if self.viz_max:
            lines = self._render_viz_max(w, h)
        elif self.full:
            lines = self._render_now(w, h - 1)
            lines.append(self._render_footer(w))
        else:
            left_w = max(34, int(w * 0.42))
            right_w = w - left_w - 1
            lines = []
            lines.extend(self._render_header(w))
            body_h = h - len(lines) - 1
            left = self._render_list(left_w, body_h)
            right = self._render_now(right_w, body_h)
            sep = fg(DGREY) + "│" + RESET
            for i in range(body_h):
                lines.append(left[i] + sep + right[i])
            lines.append(self._render_footer(w))
        if self.menu:
            lines = self._render_menu_overlay(lines, w)

        blob = self._kitty_payload
        self._kitty_payload = None
        place = None
        if blob:
            for i, ln in enumerate(lines[:h]):
                j = ln.find(KITTY_MARK)
                if j >= 0:
                    place = (i + 1, visible_len(ln[:j]) + 1)
                    lines[i] = ln.replace(KITTY_MARK, " ")
                    break
        out = ["\x1b[H"]
        for i, ln in enumerate(lines[:h]):
            out.append(f"\x1b[{i + 1};1H" + ln + "\x1b[0m\x1b[K")
        if place:
            out.append(f"\x1b[{place[0]};{place[1]}H" + blob)
        elif self._kitty_live:
            # the bitmap isn't on screen this frame — clear it
            out.append("\x1b_Ga=d,d=A,q=2\x1b\\")
            self._kitty_live = False
        sys.stdout.write("".join(out))
        sys.stdout.flush()

    def _render_header(self, w):
        acct = (fg(GREEN) + "● signed in" if self.authed
                else fg(GREY) + "○ guest — run: ytm --login")
        if self.work:
            acct = fg(GREY) + "▪ work  " + acct
        tabs = []
        for i, t in enumerate(TABS):
            if i == self.tab:
                tabs.append(bg(RED) + fg(WHITE) + BOLD + f" {i+1} {t} " + RESET)
            else:
                tabs.append(fg(GREY) + f" {i+1} {t} " + RESET)
        tab_bar = (fg(DGREY) + "·" + RESET).join(tabs)

        lines = []
        big = w >= 76 and self.size.lines >= 26
        if big:
            for r, row in enumerate(BIG_LOGO):
                cl = lerp(RED, PINK, r / 5)
                cr = lerp(ORANGE, RED, r / 5)
                ln = "  " + shadow_text(row, cl, cr)
                if r == 0:
                    pad = w - visible_len(ln) - visible_len(acct) - 2
                    ln += " " * max(pad, 1) + acct + " "
                lines.append(crop_pad(ln, w))
        else:
            for r, row in enumerate(LOGO):
                # gradient drifts down-left as it descends the rows
                cl = lerp(RED, PINK, r / 2)
                cr = lerp(ORANGE, RED, r / 2)
                ln = ("  " + fg(lerp(RED, DARK, r * 0.18)) + BADGE[r] + RESET +
                      "  " + grad_text(row, cl, cr))
                if r == 0:
                    pad = w - visible_len(ln) - visible_len(acct) - 2
                    ln += " " * max(pad, 1) + acct + " "
                lines.append(crop_pad(ln, w))
        lines.append(crop_pad("  " + tab_bar, w))
        lines.append(fg(DGREY) + "─" * w + RESET)
        return lines

    def _render_picker(self, w, h):
        """Modal: pick a playlist to add the chosen track to."""
        out = []
        out.append(crop_pad(
            "  " + BOLD + fg(WHITE) + "add to playlist" + RESET +
            fg(GREY) + f"  ({self.picker_track.title})" + RESET, w))
        out.append(crop_pad("  " + fg(DGREY) + ITAL +
                            "↵ add · esc cancel" + RESET, w))
        out.append(crop_pad("", w))
        rows = h - len(out)
        scr = max(0, self.picker_sel - rows + 1)
        for r in range(rows):
            i = scr + r
            if i >= len(self.picker):
                out.append(crop_pad("", w))
                continue
            pl = self.picker[i]
            mark = "♥ " if pl.playlist_id == "LM" else "▤ "
            line = (f"   {fg(ORANGE)}{mark}{RESET}{fg(WHITE)}{pl.title}"
                    f"{RESET} {fg(GREY)}{DIM}{pl.count}{RESET}")
            if i == self.picker_sel:
                plain = ANSI_RE.sub("", line)
                line = bg(lerp(DARK, RED, 0.18)) + fg(WHITE) + BOLD + \
                    crop_pad(" " + fg(RED) + "▌" + fg(WHITE) + plain[1:], w)
            out.append(crop_pad(line, w))
        return out

    def _render_list(self, w, h):
        if self.picker is not None:
            return self._render_picker(w, h)
        lst = self.current_list()
        title = TABS[self.tab]
        if self.tab == 2 and self.pl_open:
            title = f"Playlists ▸ {self.pl_open.title}"
        out = []
        head = "  " + BOLD + fg(WHITE) + title + RESET
        if self.searching or self.loading_msg:
            head += fg(ORANGE) + f"  ⟳ {self.loading_msg or 'searching…'}" + RESET
        out.append(crop_pad(head, w))
        out.append(crop_pad("", w))

        if self.input_mode:
            label = ("search ❯ " if self.input_purpose == "search"
                     else "new playlist ❯ ")
            box = ("  " + fg(RED) + label + RESET + fg(WHITE) +
                   self.input_buf + bg(RED) + " " + RESET)
            out.append(crop_pad(box, w))
            out.append(crop_pad("", w))

        rows = h - len(out)
        sel = self.sel[self.tab]
        # keep selection in viewport
        scr = self.scroll[self.tab]
        if sel < scr:
            scr = sel
        if sel >= scr + rows:
            scr = sel - rows + 1
        self.scroll[self.tab] = scr

        if not lst:
            hint = {
                0: "press / to search",
                1: "your liked songs live here",
                2: "your playlists live here",
                3: "queue is empty — play something",
            }[self.tab if not (self.tab == 2 and self.pl_open) else 2]
            for i in range(rows):
                out.append(crop_pad(
                    f"   {fg(DGREY)}{ITAL}{hint}{RESET}" if i == rows // 2
                    else "", w))
            return out

        for r in range(rows):
            i = scr + r
            if i >= len(lst):
                out.append(crop_pad("", w))
                continue
            it = lst[i]
            is_sel = (i == sel)
            if isinstance(it, Playlist):
                line = (f" {fg(ORANGE)}▤ {RESET}{fg(WHITE)}{it.title}"
                        f"{RESET} {fg(GREY)}{DIM}{it.count}{RESET}")
            else:
                playing = (self.now and self.tab == 3 and i == self.qpos)
                mark = fg(RED) + "▶ " + RESET if playing else "  "
                dur = it.duration or ""
                tw = w - 4 - len(dur) - 2
                t = it.title[:max(tw - len(it.artist) - 3, 8)]
                line = (f" {mark}{fg(WHITE)}{t}{RESET} "
                        f"{fg(GREY)}{DIM}{it.artist}{RESET}")
                pad = w - visible_len(line) - len(dur) - 2
                line += " " * max(pad, 1) + fg(DGREY) + dur + RESET
            if is_sel:
                plain = ANSI_RE.sub("", line)
                line = bg(lerp(DARK, RED, 0.18)) + fg(WHITE) + BOLD + crop_pad(
                    " " + fg(RED) + "▌" + fg(WHITE) + plain[1:], w)
            out.append(crop_pad(line, w))
        return out

    def _menu_items(self):
        """(label, attr, lo, hi, step, fmt) — fmt is a suffix string for
        continuous sliders or a tuple of names for discrete ones."""
        return [
            ("beat punch", "viz_react", 0.2, 2.4, 0.2, "×"),
            ("flow speed", "viz_speed", 0.4, 2.4, 0.2, "×"),
            ("morph speed", "viz_morph", 0.4, 2.4, 0.2, "×"),
            ("pixel quality", "drop_px",
             0, 3 if self._kitty_ok else 2, 1,
             ("chunky", "hi-def", "silk", "pixel")),
        ]

    def _render_menu_overlay(self, lines, w):
        """M: tuning sliders composited over whatever is on screen — the
        visualizer keeps dancing behind the panel while you tweak it."""
        items = self._menu_items()
        bw = min(w - 6, 50)
        barw = bw - 26
        x0 = (w - bw) // 2
        bord = fg(DGREY)
        t = " visualizer tuning "
        lpad = (bw - 2 - len(t)) // 2
        rows = [bord + "╭" + "─" * lpad + RESET + BOLD + fg(ORANGE) + t +
                RESET + bord + "─" * (bw - 2 - len(t) - lpad) + "╮" + RESET,
                bord + "│" + " " * (bw - 2) + "│" + RESET]
        for i, (label, attr, lo, hi, step, fmt) in enumerate(items):
            cur = getattr(self, attr)
            fill = int(round((cur - lo) / (hi - lo) * barw))
            sel = i == self.menu_sel
            pre = (fg(PINK) + "▸ " + RESET) if sel else "  "
            lab = ((BOLD + fg(WHITE) if sel else fg(GREY)) +
                   f"{label:<14}" + RESET)
            bar = (fg(RED) + "█" * fill +
                   fg(DGREY) + "░" * (barw - fill) + RESET)
            val = fmt[int(cur)] if isinstance(fmt, tuple) else f"{cur:.1f}{fmt}"
            val = ((fg(ORANGE) if sel else fg(GREY)) + f" {val:>7}" + RESET)
            row = pre + lab + bar + val
            row += " " * max(0, bw - 2 - visible_len(row))
            rows.append(bord + "│" + RESET + row + bord + "│" + RESET)
        hint = "←/→ adjust · ↑/↓ pick · esc close"
        rows += [bord + "│" + " " * (bw - 2) + "│" + RESET,
                 bord + "│" + RESET + fg(DGREY) + ITAL +
                 f"{hint:^{bw - 2}}" + RESET + bord + "│" + RESET,
                 bord + "╰" + "─" * (bw - 2) + "╯" + RESET]
        y0 = max(1, (len(lines) - len(rows)) // 2)
        for i, rrow in enumerate(rows):
            if y0 + i >= len(lines):
                break
            cells = ansi_cells(lines[y0 + i], w)
            for j, (sgr, ch) in enumerate(ansi_cells(rrow, bw)):
                if ch:
                    put_cell(cells, x0 + j, sgr, ch)
            lines[y0 + i] = cells_to_str(cells)
        return lines

    def _render_viz_max(self, w, h):
        """F: the visualizer owns the whole terminal, border to border,
        with one status line keeping the track and time in reach."""
        self.eww_flush = True            # flush rendering, no side padding
        try:
            lines = self._render_visualizer(w, h - 2)
        finally:
            self.eww_flush = False
        t = time.time() - self._like_t   # splashes still land on top
        if t < (1.4 if self._like_mode == "like" else 1.8):
            splash = (self._like_splash if self._like_mode == "like"
                      else self._break_splash)
            lines = splash(lines, w, t)

        pos = self.player.props.get("time-pos")
        dur = self.player.props.get("duration")
        if self.now:
            times = f"{fmt_time(pos)} / {fmt_time(dur)}"
            left = f" ♪ {self.now.title}"
            sub = f" · {self.now.artist}"
            room = w - len(times) - 2
            if len(left) + len(sub) > room:
                sub = ""
                left = left[:room]
            gap = " " * max(1, w - len(left) - len(sub) - len(times) - 1)
            lines.append(crop_pad(
                fg(RED) + " ♪" + BOLD + fg(WHITE) + left[2:] + RESET +
                fg(GREY) + sub + gap + fg(GREY) + times + RESET, w))
        else:
            lines.append(crop_pad(
                fg(DGREY) + f"{'♪ nothing playing':^{w}}" + RESET, w))
        lines.append(self._render_footer(w))
        return lines

    def _render_now(self, w, h):
        out = []
        if not self.now:
            art_side = min(w - 6, h - 6, 24)
            for i in range(h):
                if i == h // 2 - 1:
                    out.append(crop_pad(
                        f"{fg(DGREY)}{'♪ nothing playing':^{w}}{RESET}", w))
                elif i == h // 2:
                    out.append(crop_pad(
                        f"{fg(DGREY)}{ITAL}{'search & hit enter':^{w}}{RESET}", w))
                else:
                    out.append(crop_pad("", w))
            return out

        # geometry: art on top, then meta, visualizer (grabs leftover), progress
        if self.work:
            art_h = 0                      # no thumbnails in work mode
            viz_rows = max(2, min(h - 7, 26))
        elif self.full:
            # fullscreen: split the room — art stays prominent but the
            # visualizer gets real height instead of a thin strip
            art_h = max(min(h - 18, (w - 6) // 2, 18), 4)
            viz_rows = max(4, min(h - art_h - 7, 22))
        else:
            art_h = max(min(h - 11, (w - 6) // 2, 22), 4)
            viz_rows = max(2, min(h - art_h - 7, 9))

        out.append(crop_pad("", w))
        if not self.work:
            art_w = art_h * 2
            pad_l = max((w - art_w) // 2, 1)
            art = self.art.get(self.now.thumb, art_w, art_h)
            for row in range(art_h):
                if art:
                    out.append(crop_pad(" " * pad_l + art[row], w))
                else:
                    fill = fg(DGREY) + ("·" * art_w)
                    out.append(crop_pad(" " * pad_l + fill + RESET, w))

        out.append(crop_pad("", w))
        title = self.now.title
        heart = fg(RED) + " ♥" + RESET if self.liked_now else ""
        out.append(crop_pad(
            BOLD + fg(WHITE) + f"{title:^{w}}" + RESET + heart, w))
        sub = self.now.artist + (f"  ·  {self.now.album}" if self.now.album else "")
        out.append(crop_pad(fg(GREY) + f"{sub:^{w}}" + RESET, w))
        out.append(crop_pad("", w))

        out.extend(self._render_visualizer(w, viz_rows))
        out.extend(self._render_progress(w))

        while len(out) < h:
            out.append(crop_pad("", w))
        out = out[:h]
        t = time.time() - self._like_t
        if t < (1.4 if self._like_mode == "like" else 1.8):
            splash = (self._like_splash if self._like_mode == "like"
                      else self._break_splash)
            out = splash(out, w, t)
        return out

    def _splash_geom(self, out, w):
        """Shared layout for the like/heartbreak splashes."""
        h = len(out)
        rows = min(14, max(6, h - 6))
        cols = min(rows * 2, w - 6)
        pad_l = (w - cols) // 2
        band0 = max(1, (h - rows - 2) // 2)
        return rows, cols, pad_l, band0

    def _heart_mask(self, rows, cols, s=1.0):
        """Heart as a boolean pixel grid (two pixel rows per cell row,
        scaled by s for the pop-in). Emoji silhouette — two circles and
        a wedge — because the classic (x²+y²−1)³ = x²y³ curve has lobes
        so shallow they render as a flat top at cell resolution."""
        import numpy as np
        xs = np.linspace(-1.45, 1.45, cols)[None, :] / s
        ys = np.linspace(1.35, -1.45, rows * 2)[:, None] / s
        ax = np.abs(xs)
        lobes = (ax - 0.62) ** 2 + (ys - 0.45) ** 2 < 0.49
        wedge = (ys <= 0.45) & (ys >= -1.35) & \
                (ax < 1.32 * (ys + 1.35) / 1.8)
        return lobes | wedge

    def _stamp_pixels(self, out, w, pix, band0, pad_l, col):
        """Half-block render of a boolean pixel grid composited onto the
        frame — transparent where empty, and edge halves borrow the color
        underneath so the shape has no black fringe."""
        h = len(out)
        for r in range(len(pix) // 2):
            if band0 + r >= h - 1:
                break
            top, bot = pix[r * 2], pix[r * 2 + 1]
            if not top.any() and not bot.any():
                continue
            cells = ansi_cells(out[band0 + r], w)
            for i in range(len(top)):
                if top[i] and bot[i]:
                    put_cell(cells, pad_l + i, col, "█")
                elif top[i] or bot[i]:
                    under = sgr_to_bg(cells[pad_l + i][0])
                    put_cell(cells, pad_l + i, under + col,
                             "▀" if top[i] else "▄")
            out[band0 + r] = cells_to_str(cells)

    def _splash_label(self, out, w, row0, label, t, colors, fall):
        """Spaced-out word under the splash — every letter flashes through
        `colors` and shakes; `fall` is the chance a letter drops a row."""
        h = len(out)
        if row0 >= h - 1:
            return
        x0 = (w - (len(label) * 4 - 3)) // 2
        two_rows = row0 + 1 < h - 1
        lines = {0: ansi_cells(out[row0], w)}
        if two_rows:
            lines[1] = ansi_cells(out[row0 + 1], w)
        for i, ch in enumerate(label):
            cc = colors[int(t * 12 + i) % len(colors)]
            jx = random.randint(-1, 1)
            jy = 1 if two_rows and random.random() < fall else 0
            x = x0 + i * 4 + jx
            under = sgr_to_bg(lines[jy][x][0]) if 0 <= x < w else ""
            put_cell(lines[jy], x, under + BOLD + fg(cc), ch)
        out[row0] = cells_to_str(lines[0])
        if two_rows:
            out[row0 + 1] = cells_to_str(lines[1])

    def _like_splash(self, out, w, t):
        """Big pulsing heart composited over the panel right after a like,
        popping in with a theme pulse. Everything around the curve stays
        transparent so the panel keeps living underneath."""
        rows, cols, pad_l, band0 = self._splash_geom(out, w)
        s = min(1.0, 0.35 + t * 3.5)             # pop-in scale
        c = lerp(RED, PINK, 0.5 + 0.5 * math.sin(t * 9))
        pix = self._heart_mask(rows, cols, s)
        self._stamp_pixels(out, w, pix, band0, pad_l, fg(c))
        if t > 0.25:
            self._splash_label(out, w, band0 + rows, "LIKED", t,
                               (RED, ORANGE, PINK), 0.3)
        return out

    def _break_splash(self, out, w, t):
        """The same heart, breaking: a panicked flutter, then a jagged
        crack opens and the two halves drift apart and sink while the
        color drains out of them."""
        import numpy as np
        rows, cols, pad_l, band0 = self._splash_geom(out, w)
        inside = self._heart_mask(rows, cols)
        u = max(0.0, (t - 0.35) / 1.45)          # 0 = crack opens, 1 = gone
        if t < 0.35:                             # panic heartbeat
            c = lerp(RED, PINK, 0.5 + 0.5 * math.sin(t * 26))
        else:
            c = lerp(RED, (115, 115, 130), min(1.0, u * 1.5))

        ph, pw = rows * 2, cols
        pix = np.zeros((ph + int(u * u * 12) + 2, pw), dtype=bool)
        jx = random.randint(-1, 1) if t < 0.35 else 0
        dx = int(u * u * cols * 0.22)            # halves drift apart...
        dyl = int(u * u * 11)                    # ...and sink, left faster
        dyr = int(u * u * 7)
        gap = 0 if t < 0.35 else 1 + int(u * 2.5)
        mid = pw // 2
        for yy in range(ph):
            crack = mid + ((yy * 5 // 3) % 3) - 1    # jagged, deterministic
            row = inside[yy]
            for i in range(pw):
                if not row[i]:
                    continue
                if gap and abs(i - crack) < gap:
                    continue                     # splinters off at the crack
                if i < crack:
                    x, y = i - dx + jx, yy + dyl
                else:
                    x, y = i + dx + jx, yy + dyr
                if 0 <= x < pw and y < len(pix):
                    pix[y, x] = True
        self._stamp_pixels(out, w, pix, band0, pad_l, fg(c))
        if t > 0.45:
            grief = ((205, 205, 215), (140, 140, 155), (95, 95, 110))
            self._splash_label(out, w, band0 + rows, "UNLIKED", t,
                               grief, min(0.8, u + 0.2))
        return out

    def _viz_color(self, hfrac):
        """Vertical gradient: red base → orange mids → pink peaks."""
        if hfrac < 0.5:
            return lerp(RED, ORANGE, hfrac * 2)
        return lerp(ORANGE, PINK, (hfrac - 0.5) * 2)

    def _viz_targets(self, n):
        """n spectrum levels 0..1 — real FFT when audio is flowing, a gentle
        animation when we can't tap it, flat when paused."""
        if bool(self.player.props.get("pause")) or self.player.loading:
            return [0.0] * n
        if self.tap and self.tap.producing:
            return self.tap.levels(n)
        t = time.time()
        return [max(0.0, min(1.0,
                (math.sin(t * 2.1 + i * 0.55) +
                 math.sin(t * 3.7 + i * 0.21)) * 0.25 + 0.5 +
                random.uniform(-0.18, 0.18))) for i in range(n)]

    def _viz_physics(self, targets):
        """Fast attack, gravity fall, falling peak caps. Time-based rates
        so the feel is identical at any framerate."""
        now = time.time()
        dt = min(max(now - self._phys_t, 0.005), 0.2)
        self._phys_t = now
        fall = 1.1 * dt
        pfall = 0.38 * dt
        n = len(targets)
        if len(self.bars) != n:
            self.bars = [0.0] * n
            self.peaks = [0.0] * n
        for i in range(n):
            tv, b = targets[i], self.bars[i]
            self.bars[i] = tv if tv > b else max(b - fall, tv)
            self.peaks[i] = max(self.peaks[i] - pfall, self.bars[i])

    def _render_visualizer(self, w, rows):
        # the UI ticks ~25fps while playing, but discrete styles look
        # steadier updating at ~12 — serve a cached frame in between
        if self.viz_style != "drop":
            c = self._viz_cache
            if c and c[1:4] == (self.viz_style, w, rows) and \
                    time.time() - c[0] < 0.075:
                return c[4]
        lines = self._render_visualizer_now(w, rows)
        if self.viz_style != "drop":
            self._viz_cache = (time.time(), self.viz_style, w, rows, lines)
        return lines

    def _render_visualizer_now(self, w, rows):
        if getattr(self, "eww_flush", False):
            n, pad_l = w, 0          # eww frames: fill border to border
        else:
            n = max(w - 10, 16)
            pad_l = (w - n) // 2
        if self.viz_style == "scope":
            return self._viz_scope(w, rows, n, pad_l)
        if self.viz_style == "bands":
            return self._viz_bands(w, rows, n, pad_l)
        if self.viz_style == "drop":
            return self._viz_drop(w, rows, n, pad_l)

        if self.viz_style == "mirror":
            m = n // 2 + 1
            lv = self._viz_targets(m)
            c = (n - 1) / 2
            targets = [lv[min(int(abs(i - c)), m - 1)] for i in range(n)]
        else:
            targets = self._viz_targets(n)
        self._viz_physics(targets)

        unit = rows * 8
        cap_c = fg(lerp(WHITE, PINK, 0.35))
        lines = []
        for r in range(rows):
            base = (rows - 1 - r) * 8
            cells = []
            for i in range(n):
                filled = int(max(0, min(8, self.bars[i] * unit - base)))
                if filled:
                    hfrac = (rows - 1 - r) / max(rows - 1, 1)
                    cells.append(fg(self._viz_color(hfrac)) + BLOCKS[filled])
                else:
                    pk = int(self.peaks[i] * unit)
                    if base <= pk < base + 8 and pk > self.bars[i] * unit + 1:
                        cells.append(cap_c + "▔")
                    else:
                        cells.append(" ")
            lines.append(crop_pad(" " * pad_l + "".join(cells) + RESET, w))
        return lines

    def _viz_scope(self, w, rows, n, pad_l):
        """Braille oscilloscope of the raw waveform."""
        wd, hd = n * 2, rows * 4                    # dot resolution
        if bool(self.player.props.get("pause")) or self.player.loading:
            s = [0.0] * wd
        elif self.tap and self.tap.producing:
            s = self.tap.samples(wd)
        else:
            t = time.time()
            s = [math.sin(t * 3.0 + x * 0.11) *
                 (0.5 + 0.3 * math.sin(t * 0.9)) for x in range(wd)]
        grid = [[0] * n for _ in range(rows)]
        prev = None
        for x, v in enumerate(s):
            y = int((0.5 - v * 0.48) * (hd - 1))
            y = max(0, min(hd - 1, y))
            lo, hi = (y, y) if prev is None else (min(prev, y), max(prev, y))
            for yy in range(lo, hi + 1):
                grid[yy // 4][x // 2] |= BRAILLE[x % 2][yy % 4]
            prev = y
        lines = []
        for r in range(rows):
            cells = []
            for i in range(n):
                m = grid[r][i]
                if m:
                    c = lerp(RED, ORANGE, i / max(n - 1, 1))
                    cells.append(fg(c) + chr(0x2800 + m))
                else:
                    cells.append(" ")
            lines.append(crop_pad(" " * pad_l + "".join(cells) + RESET, w))
        return lines

    def _drop_new_params(self):
        # ~half the rolls knock the origin off-center so radial presets
        # stop staring at the exact middle of the panel every time
        off = random.random() < 0.45
        return {"k1": random.uniform(2.0, 5.5),
                "k2": random.uniform(2.0, 8.0),
                "arms": random.choice([2, 3, 3, 4, 5, 6]),
                "ph": random.uniform(0, math.tau),
                "cx": random.uniform(-1.1, 1.1) if off else 0.0,
                "cy": random.uniform(-0.6, 0.6) if off else 0.0,
                # post-processing personality: 0 = smooth, n = n sharp
                # palette bands (milkdrop-style colorful level sets)
                "band": random.choice([0, 0, 0, 2, 2, 3, 3, 4]),
                "gam": random.uniform(0.95, 1.45)}

    def _drop_post(self, v, P):
        """Contrast shaping — this is what makes presets pop instead of
        rendering as the same mid-palette mush."""
        import numpy as np
        v = np.clip(0.5 + v / 5.5, 0.0, 1.0)
        v = np.clip((v - 0.5) * 1.25 + 0.5, 0.0, 1.0) ** P["gam"]
        if P["band"]:
            return 0.5 - 0.5 * np.cos(v * math.tau * P["band"])
        return 0.5 - 0.5 * np.cos(v * math.pi)

    N_PRESETS = 28

    def _drop_field(self, preset, P, xs, ys, r, ang, t, eb, em, et):
        """One milkdrop-ish interference field. Each preset is a different
        equation; bass=rings/zoom, mid=swirl/speed, treble=fine ripple."""
        import numpy as np
        sin, cos = np.sin, np.cos
        k1, k2, A, ph = P["k1"], P["k2"], P["arms"], P["ph"]
        if preset == 0:    # pulse rings + swirl
            return (sin(r * (k1 * 1.5 + 9 * eb) - t * 2.0)
                    + sin(xs * (2.5 + 4 * et) + t)
                    + sin(ys * 3.0 - t * 0.8)
                    + sin(ang * A + t * (0.4 + 1.6 * em)))
        if preset == 1:    # tunnel
            return (sin(k1 * 2.2 / (r + 0.35) - t * (1.5 + 2 * em))
                    + sin(ang * A + t * 0.7)
                    + sin(r * (4 + 6 * eb) - t * 2.0)
                    + et * 2.0 * sin(xs * 8 + t * 3))
        if preset == 2:    # two orbiting sources
            ox = 0.7 * math.cos(t * 0.6 + ph)
            oy = 0.55 * math.sin(t * 0.43)
            d1 = np.sqrt((xs - ox) ** 2 + (ys - oy) ** 2)
            d2 = np.sqrt((xs + ox) ** 2 + (ys + oy) ** 2)
            return (sin(d1 * (k1 * 2 + 6 * eb) - t * 2.0)
                    + sin(d2 * (k2 + 4 * em) + t * 1.5)
                    + sin(ang * 2 + t * 0.5)
                    + et * 1.5 * sin(r * 12 - t * 4))
        if preset == 3:    # kaleidoscope
            m = np.abs(((ang * A / math.pi) % 2) - 1)
            return (sin(r * (k1 * 2 + 8 * eb) - t * 1.8)
                    + sin(m * math.pi * k2 + t * (0.5 + 1.5 * em))
                    + sin(r * 3 + m * 4 - t)
                    + et * 1.5 * sin(r * 14 - t * 5))
        if preset == 4:    # weave
            return (sin(xs * k1 * 2 + t * (0.8 + em))
                    + sin(ys * k2 - t)
                    + sin((xs + ys) * 2.5 + t * 1.3 + 3 * eb * math.sin(t))
                    + sin(r * (5 + 7 * eb) - t * 2))
        if preset == 5:    # spiral galaxy
            return (sin(ang * A + r * (k1 * 3 + 6 * eb) - t * (2 + 2 * em))
                    + sin(r * 5 - t * 1.5)
                    + sin(xs * 3 + t * 0.7)
                    + et * 1.5 * sin(r * 16 - t * 5))
        if preset == 6:    # moiré ring interference
            return (sin(r * (k1 * 5 + 4 * eb) - t)
                    + sin(r * (k1 * 5 + 0.8) + t * 0.4)
                    + sin(ang * A - t * (0.5 + em))
                    + et * 1.5 * sin(r * 18 - t * 5))
        if preset == 7:    # rose petals
            return (sin(A * ang + t) * cos(r * (4 + 6 * eb) - t * 1.5) * 2
                    + sin(r * 3 - t)
                    + em * 1.5 * sin(ang * 2 + t * 1.3))
        if preset == 8:    # lissajous drift
            return (sin(xs * k1 * 2 + math.sin(t * 0.8) * 3)
                    + sin(ys * k2 + math.cos(t * 0.6) * 3)
                    + sin(r * (5 + 8 * eb) - t)
                    + et * 1.5 * sin((xs - ys) * 7 + t * 2))
        if preset == 9:    # diamond pulse
            d = np.abs(xs) + np.abs(ys)
            return (sin(d * (4 + 8 * eb) - t * 2)
                    + sin(ang * 2 + t * (0.4 + em))
                    + sin(d * 2 + t * 0.6)
                    + et * 1.5 * sin(r * 15 - t * 4))
        if preset == 10:   # square tunnel
            q = np.maximum(np.abs(xs), np.abs(ys))
            return (sin(q * (5 + 8 * eb) - t * 2)
                    + sin(q * k1 + t * 0.7)
                    + sin(ang * A + t * em * 2)
                    + et * sin(xs * 9 + t * 3))
        if preset == 11:   # vortex twist
            tw = ang + r * (1.0 + 2.0 * em)
            return (sin(tw * A - t * 2)
                    + sin(r * (6 + 6 * eb) - t)
                    + sin(tw * 2 + t * 0.5)
                    + et * 1.5 * sin(r * 13 + t * 3))
        if preset == 12:   # ripple grid
            return (sin(xs * k1 * 3 - t) * sin(ys * k1 * 3 + t) * 2
                    + sin(r * (4 + 8 * eb) - t * 2)
                    + em * sin((xs + ys) * 4 + t))
        if preset == 13:   # starburst fan
            return (sin(ang * A * 2 + t)
                    + sin(r * (3 + 6 * eb) - t * 2)
                    + em * 2 * sin(ang * 3 - r * 4 + t)
                    + et * sin(r * 17 - t * 5))
        if preset == 14:   # log zoom rush
            return (sin(np.log(r + 0.2) * (6 + 4 * em) - t * (2 + 3 * eb))
                    + sin(ang * A + t * 0.5)
                    + sin(r * 4 - t)
                    + et * 1.5 * sin(ang * 8 + t * 2))
        if preset == 15:   # colliding wavefronts
            d1 = np.sqrt((xs - 1.0) ** 2 + (ys - 0.7) ** 2)
            d2 = np.sqrt((xs + 1.0) ** 2 + (ys + 0.7) ** 2)
            return (sin(d1 * (k1 * 2 + 5 * eb) - t * 2)
                    + sin(d2 * (k2 + 5 * eb) - t * 2)
                    + sin(r * 3 + t * em)
                    + et * sin((d1 - d2) * 8 + t * 3))
        if preset == 16:   # warped silk
            return (sin(xs * 4 + sin(ys * 3 + t) * (1.5 + 2 * eb))
                    + sin(ys * 4 + sin(xs * 3 - t) * 1.5)
                    + sin(r * 5 - t)
                    + em * sin(ang * A + t))
        if preset == 17:   # wandering eye
            cx = 0.5 * math.cos(t * 0.5 + ph)
            cy = 0.4 * math.sin(t * 0.37)
            rr = np.sqrt((xs - cx) ** 2 + (ys - cy) ** 2)
            return (sin(rr * (6 + 9 * eb) - t * 2)
                    + sin(ang * 2 + t * em * 2)
                    + sin(rr * 2.5 + t * 0.5)
                    + et * 1.5 * sin(rr * 16 - t * 5))
        if preset == 18:   # checker pulse
            return (sin(xs * k1 * 2 + t) * sin(ys * k2 * 2 - t)
                    + sin((xs + ys) * 3 + t)
                    + eb * 2 * sin(r * 8 - t * 3)
                    + et * sin((xs - ys) * 9 - t * 2))
        if preset == 19:   # braided radial waves
            return (sin(r * 8 - ang * A - t * 2)
                    + sin(r * 5 + ang * A + t)
                    + em * sin(ang * 4 + t)
                    + et * 2 * sin(r * 14 - t * 4))
        if preset == 20:   # silk caustics — domain-warped shimmer
            u = xs + 0.7 * sin(ys * 2.1 + t * 0.8)
            vv = ys + 0.7 * sin(xs * 1.9 - t * 0.7)
            return (sin(u * (k1 * 1.6 + 3 * eb)) + sin(vv * k2)
                    + sin((u + vv) * 2.2 - t * 1.4)
                    + et * 1.5 * sin((u - vv) * 6 + t * 2))
        if preset == 21:   # petal tunnel — the walls flex on the kick
            rr = r * (1.0 + 0.30 * sin(ang * A + t * 0.6))
            return (sin(k1 * 1.8 / (rr + 0.3) - t * (1.2 + 1.5 * em))
                    + sin(rr * (5 + 8 * eb) - t * 2)
                    + sin(ang * A - t * 0.5)
                    + et * sin(rr * 13 - t * 4))
        if preset == 22:   # tri-lattice shimmer
            a1 = xs * k1
            a2 = (xs * 0.5 + ys * 0.866) * k1
            a3 = (xs * 0.5 - ys * 0.866) * k1
            return (sin(a1 + t) + sin(a2 - t * 0.8) + sin(a3 + t * 0.6)
                    + eb * 2.2 * sin(r * (4 + 5 * eb) - t * 2)
                    + et * sin(r * 15 - t * 4))
        if preset == 23:   # comet swirl — log-spiral arms breathing
            sw = ang * A + np.log(r + 0.25) * (k2 + 2 * em)
            return (sin(sw - t * 1.8)
                    + sin(r * (4 + 9 * eb) - t * 2.2)
                    + sin(sw * 0.5 + t * 0.7)
                    + et * 1.3 * sin(r * 12 + ang * 2 - t * 3))
        # the centerless family: lattices and fractal fields with no focal
        # point — the whole panel is the subject, not a bullseye
        if preset == 24:   # honeycomb — hex cell walls, drifting sideways
            u = xs * (k1 * 0.9 + 1.5 * eb) + t * 0.35
            vy = ys * (k1 * 0.9 + 1.5 * eb)
            a1 = sin(u + t * 0.3)
            a2 = sin(u * 0.5 + vy * 0.866 - t * 0.4)
            a3 = sin(u * 0.5 - vy * 0.866 + t * 0.25)
            hexg = np.abs(a1) + np.abs(a2) + np.abs(a3)
            return (hexg * (1.4 + 1.6 * eb) - 2.2
                    + em * sin((xs + ys) * 2 + t)
                    + et * 0.8 * sin(xs * 7 - t * 2))
        if preset == 25:   # fractal plasma — three octaves, no anchor
            f, amp, acc = k1 * 0.7, 1.0, 0.0
            for o in range(3):
                acc = acc + amp * (sin(xs * f + t * (0.4 + 0.2 * o))
                                   * sin(ys * f - t * (0.3 + 0.25 * o)))
                f, amp = f * 1.9, amp * (0.55 + 0.3 * eb)
            return acc * 2.2 + em * sin((xs - ys) * 3 + t * 0.8)
        if preset == 26:   # drifting cells — voronoi-ish membranes
            d = None
            for i in range(5):
                px = 1.6 * math.sin(t * (0.13 + 0.07 * i) + ph + i * 2.4)
                py = 0.9 * math.cos(t * (0.11 + 0.06 * i) + i * 1.7)
                di = np.sqrt((xs - px) ** 2 + (ys - py) ** 2)
                d = di if d is None else np.minimum(d, di)
            return (sin(d * (k2 + 6 * eb) - t * 1.4)
                    + sin(d * 3.5 + t * 0.5)
                    + em * sin((xs + ys) * 2.5 - t)
                    + et * sin(d * 14 - t * 3))
        # 27: dunes — warped ridges rolling across the panel
        wx = xs + 1.1 * sin(ys * 1.3 + t * 0.5)
        return (sin(wx * (k1 * 0.8 + 2 * eb) + ys * 1.5 - t * 0.9)
                + sin(ys * k2 * 0.5 + sin(wx * 1.7 - t * 0.4) * 2)
                + em * sin((wx + ys) * 3 + t * 0.7)
                + et * 0.9 * sin(wx * 9 - t * 2.5))

    @staticmethod
    def _kitty_sniff():
        """True if the terminal speaks the kitty graphics protocol."""
        term = os.environ.get("TERM", "")
        prog = os.environ.get("TERM_PROGRAM", "")
        return any(t in (term + " " + prog).lower()
                   for t in ("kitty", "ghostty", "wezterm"))

    @staticmethod
    def _cell_px():
        """Cell size in screen pixels (for pixel-perfect rendering)."""
        try:
            r, c, xp, yp = struct.unpack(
                "HHHH", fcntl.ioctl(1, termios.TIOCGWINSZ, b"\0" * 8))
            if xp and yp and r and c:
                return max(2, xp // c), max(4, yp // r)
        except OSError:
            pass
        return 10, 20

    def _drop_kitty(self, idxf, Wpx, Hpx, W, rows, pad, bright, w):
        """Blit the field as a true RGB bitmap (kitty graphics protocol).
        The image is transmitted after the text frame; here we just leave
        a blank panel with a marker cell so render() knows where it goes."""
        import numpy as np
        lut = np.clip(self._drop_lut() * bright, 0, 255).astype(np.uint8)
        rgb = lut[idxf.astype(np.intp)]              # (Hpx, Wpx, 3)
        data = base64.standard_b64encode(
            zlib.compress(rgb.tobytes(), 1)).decode("ascii")
        new = 91 + (self._kitty_id ^ 1)              # double-buffer ids
        old = 91 + self._kitty_id
        self._kitty_id ^= 1
        head = (f"a=T,i={new},q=2,f=24,o=z,s={Wpx},v={Hpx},"
                f"c={W},r={rows},z=-1")
        out = []
        for o in range(0, len(data), 4096):
            chunk = data[o:o + 4096]
            m = 1 if o + 4096 < len(data) else 0
            out.append(f"\x1b_G{head},m={m};{chunk}\x1b\\" if o == 0
                       else f"\x1b_Gm={m};{chunk}\x1b\\")
        out.append(f"\x1b_Ga=d,d=i,i={old},q=2\x1b\\")
        self._kitty_payload = "".join(out)
        self._kitty_live = True
        blank = " " * w
        first = " " * pad + KITTY_MARK + " " * (w - pad - 1)
        return [first] + [blank] * (rows - 1)

    def _drop_lut(self):
        """64-entry palette: dark → primary → secondary → accent → white."""
        if self._drop_lut_cache is None:
            import numpy as np
            # anchored dark lows so light themes (ice…) still have contrast
            stops = [(8, 8, 14), lerp((8, 8, 14), RED, 0.45), RED,
                     ORANGE, PINK, lerp(PINK, WHITE, 0.55)]
            pos = [0.0, 0.34, 0.60, 0.80, 0.94, 1.0]
            lut = []
            for i in range(64):
                t = i / 63
                a = max(j for j in range(len(pos) - 1) if pos[j] <= t)
                f = (t - pos[a]) / (pos[a + 1] - pos[a])
                lut.append(lerp(stops[a], stops[a + 1], f))
            self._drop_lut_cache = np.array(lut, dtype=float)
        return self._drop_lut_cache

    def _drop_groove(self, raw, dt, now):
        """Beat-locked drive signals. Raw spectrum jitter is flattened into
        a slow energy bed; the punch comes from detected bass kicks, and a
        tempo estimate lets the field bob *between* beats too — so it
        breathes with the song instead of flickering at it."""
        rb = raw[0]
        self._drop_bass_avg = self._drop_bass_avg * 0.985 + rb * 0.015
        avg = self._drop_bass_avg
        last = getattr(self, "_beat_t", 0.0)
        if rb > avg * 1.45 + 0.06 and now - last > 0.22:
            gaps = getattr(self, "_beat_gaps", [])
            if last and 0.25 < now - last < 2.0:
                gaps = (gaps + [now - last])[-8:]
            self._beat_gaps = gaps
            self._beat_t = now
            self._beat_amp = min(1.0, 0.45 + (rb - avg) * 2.0)
            last = now
        pulse = getattr(self, "_beat_amp", 0.0) * math.exp(-(now - last) / 0.42)
        groove = 0.0
        gaps = getattr(self, "_beat_gaps", [])
        if len(gaps) >= 4:                     # tempo locked — keep bobbing
            period = sorted(gaps)[len(gaps) // 2]
            groove = 0.5 + 0.5 * math.cos((now - last) / period * math.tau)
        eb, em, et = self._drop_e
        en = getattr(self, "_drop_energy", 0.2)
        en += ((eb + em + et) / 3 - en) * min(1.0, dt * 1.2)
        self._drop_energy = en
        react = getattr(self, "viz_react", 1.0)
        b = min(1.4, 0.25 * eb + (0.85 * pulse + 0.18 * groove) * react)
        m = min(1.2, 0.50 * em + 0.30 * pulse * react)
        tr = min(1.0, 0.45 * et + 0.20 * groove)
        return pulse * react, en, b, m, tr

    def _viz_drop(self, w, rows, n, pad_l):
        """Milkdrop-ish plasma: interference field warped by bass/mid/treble,
        rendered as half-block pixels."""
        import numpy as np
        W = w if getattr(self, "eww_flush", False) else max(w - 4, 20)
        H = rows * 2
        mode = getattr(self, "drop_px", 0)
        if mode == 3 and (not getattr(self, "_kitty_ok", False)
                          or getattr(self, "menu", False)
                          or time.time() - getattr(self, "_like_t", 0) < 1.9):
            mode = 2     # overlays composite into text cells, not bitmaps
        # silk is 4× supersampled; chunky gets the same treatment because
        # its grid is so coarse that radial cores alias into staircases —
        # averaging 4 subsamples per pixel costs nothing at that size
        ss = 1 if mode == 1 else 2
        if mode == 3:
            # pixel: real screen pixels, capped so encode+transport stays
            # fast — the terminal scales the bitmap to the panel anyway
            ss = 1
            cw, chh = self._cell_px()
            Wpx, Hpx = W * cw, rows * chh
            sc = min(1.0, math.sqrt(400_000 / max(1, Wpx * Hpx)))
            Wpx, Hpx = max(64, int(Wpx * sc)), max(64, int(Hpx * sc))
        else:
            Wpx = (W * 2 if mode else W) * ss   # hi-def: 2×2 px per cell
            Hpx = H * ss
        pad = (w - W) // 2

        if bool(self.player.props.get("pause")) or self.player.loading:
            raw = (0.0, 0.0, 0.0)
        elif self.tap and self.tap.producing:
            lv = self.tap.levels(18)
            raw = (sum(lv[:5]) / 5, sum(lv[5:12]) / 7, sum(lv[12:]) / 6)
        else:
            t0 = time.time()
            raw = (0.4 + 0.3 * math.sin(t0 * 1.9),
                   0.4 + 0.3 * math.sin(t0 * 1.3 + 2),
                   0.3 + 0.2 * math.sin(t0 * 2.7 + 4))
        now = time.time()
        dt = min(now - self._drop_last, 0.25)
        self._drop_last = now
        for i, x in enumerate(raw):
            e = self._drop_e[i]
            k = min(1.0, (10.0 if x > e else 2.4) * dt)
            self._drop_e[i] = e + (x - e) * k
        pulse, en, eb, em, et = self._drop_groove(raw, dt, now)
        # ease the pulse with a short attack so the kick lands as a swell,
        # not a single-frame jolt — keeps the punch, loses the jitter
        ps = getattr(self, "_pulse_s", 0.0)
        ps += (pulse - ps) * min(1.0, dt * 16.0)
        self._pulse_s = ps
        self._drop_t += dt * getattr(self, "viz_speed", 1.0) * \
            (0.55 + 1.1 * en + 1.6 * ps)
        t = self._drop_t

        # aspect-correct coordinates: half-block pixels are ~square, so the
        # field stops stretching into smears on wide panels (rings stay round)
        aspect = min(W / max(H, 1), 4.5)   # cap so wide strips stay coherent
        zoom = 1.15 / (1.0 + 0.16 * ps)    # camera swells in on the kick
        # sample budget: maximized terminals ask for ~10× the pixels of the
        # side panel — compute the field at a capped resolution and stretch
        fs = max(1.0, math.sqrt(Wpx * Hpx / 140_000))
        fw, fh = max(8, int(Wpx / fs)), max(8, int(Hpx / fs))
        # float32 end to end: the trig-heavy field math and the upsample
        # run 2-4× faster, and 24-bit color can't show the difference
        ys = np.linspace(-zoom, zoom, fh, dtype=np.float32)[:, None]
        xs = np.linspace(-aspect * zoom, aspect * zoom, fw,
                         dtype=np.float32)[None, :]

        def coords(P):
            # each preset roll carries its own origin — radial fields can
            # live off-center instead of always orbiting the middle
            x2 = xs - P.get("cx", 0.0)
            y2 = ys - P.get("cy", 0.0)
            return (x2, y2, np.sqrt(x2 * x2 + y2 * y2) + 1e-6,
                    np.arctan2(y2, x2))

        # milkdrop-style preset switching: on a timer, or on a hard kick
        beat = pulse > 0.8 and now - self._drop_last_switch > 7
        if (now >= self._drop_switch_at or beat) and self._drop_mix >= 1.0:
            self._drop_prev = self._drop_preset
            self._drop_pb = self._drop_pa
            self._drop_preset = random.choice(
                [i for i in range(self.N_PRESETS) if i != self._drop_preset])
            self._drop_pa = self._drop_new_params()
            self._drop_mix = 0.0
            self._drop_last_switch = now
            self._drop_switch_at = now + random.uniform(12, 24) / \
                max(0.4, getattr(self, "viz_morph", 1.0))

        if self._drop_mix < 1.0:
            self._drop_mix = min(1.0, self._drop_mix + dt / 2.5)
            mx = self._drop_mix
            mx = mx * mx * (3 - 2 * mx)                # smoothstep crossfade
            va = self._drop_post(self._drop_field(
                self._drop_prev, self._drop_pb,
                *coords(self._drop_pb), t, eb, em, et), self._drop_pb)
            vb = self._drop_post(self._drop_field(
                self._drop_preset, self._drop_pa,
                *coords(self._drop_pa), t, eb, em, et), self._drop_pa)
            v = va * (1 - mx) + vb * mx
        else:
            v = self._drop_post(self._drop_field(
                self._drop_preset, self._drop_pa,
                *coords(self._drop_pa), t, eb, em, et), self._drop_pa)
        # brightness: slow mood bed + a lift on the kick — no strobing
        bright = min(1.0, 0.30 + 0.50 * min(1.0, en * 1.5) + 0.30 * ps)
        idxf = np.clip(v * 63, 0, 63)          # palette space, float
        # ~70ms ease on the field: values glide between frames instead
        # of snapping, which is most of what reads as "choppy". applied
        # at field res (cheap); brightness rides the palette, so the
        # kick still lands instantly
        prev = getattr(self, "_drop_idxp", None)
        if prev is not None and prev.shape == idxf.shape:
            idxf = prev + (idxf - prev) * min(1.0, dt * 14.0)
        self._drop_idxp = idxf
        if (fw, fh) != (Wpx, Hpx):
            # maximized panels: the field was computed under a sample
            # budget — stretch it back up bilinearly, so dense regions
            # (a swirl core) ramp smoothly instead of snapping in blocks
            yf = np.linspace(0, fh - 1, Hpx)
            xf = np.linspace(0, fw - 1, Wpx)
            y0 = yf.astype(int)
            x0 = xf.astype(int)
            y1 = np.minimum(y0 + 1, fh - 1)
            x1 = np.minimum(x0 + 1, fw - 1)
            wy = (yf - y0)[:, None].astype(np.float32)
            wx = (xf - x0)[None, :].astype(np.float32)
            top = idxf[y0][:, x0] * (1 - wx) + idxf[y0][:, x1] * wx
            bot = idxf[y1][:, x0] * (1 - wx) + idxf[y1][:, x1] * wx
            idxf = top * (1 - wy) + bot * wy
        if mode == 3:
            return self._drop_kitty(idxf, Wpx, Hpx, W, rows, pad, bright, w)
        if ss > 1:
            # average the oversampled field down — banding edges melt
            # into gradients instead of stair-stepping
            idxf = idxf.reshape(H, ss, Wpx // ss, ss).mean(axis=(1, 3))

        # 64 palette escape strings per frame — cells become table lookups
        # instead of formatting six ints each (this is the fps)
        cols = np.clip(self._drop_lut() * bright, 0, 255).astype(int)
        fgs = [f"\x1b[38;2;{c[0]};{c[1]};{c[2]}m" for c in cols]
        bgs = [f"\x1b[48;2;{c[0]};{c[1]};{c[2]}m" for c in cols]
        left, right = " " * pad, " " * (w - pad - W)

        lines = []
        if not mode:
            ti = idxf[0::2].astype(int)
            bi = idxf[1::2].astype(int)
            for rr in range(rows):
                tl, bl = ti[rr].tolist(), bi[rr].tolist()
                lines.append(left + "".join(
                    fgs[a] + bgs[b] + "▀" for a, b in zip(tl, bl)) +
                    RESET + right)
            return lines

        # hi-def/silk: pack each 2×2 block into a quadrant glyph split on
        # the brighter half; colors average in palette space (the LUT is
        # luminance-monotonic, so the mean index is the mean color)
        q = idxf.reshape(rows, 2, W, 2)
        mean = q.mean(axis=(1, 3), keepdims=True)
        mask = q >= mean
        cnt = mask.sum(axis=(1, 3))
        fgi = ((q * mask).sum(axis=(1, 3)) /
               np.clip(cnt, 1, 4)).astype(int)
        bgi = ((q * ~mask).sum(axis=(1, 3)) /
               np.clip(4 - cnt, 1, 4)).astype(int)
        flat = cnt == 4                        # uniform block: bg = fg
        bgi[flat] = fgi[flat]
        bits = (mask[:, 0, :, 0].astype(int) * 8 + mask[:, 0, :, 1] * 4 +
                mask[:, 1, :, 0] * 2 + mask[:, 1, :, 1])
        for rr in range(rows):
            fl, bl = fgi[rr].tolist(), bgi[rr].tolist()
            btl = bits[rr].tolist()
            lines.append(left + "".join(
                fgs[f] + bgs[b] + QUADS[bt]
                for f, b, bt in zip(fl, bl, btl)) + RESET + right)
        return lines

    def _viz_bands(self, w, rows, n, pad_l):
        """Centered horizontal bars, one frequency band per row, bass low."""
        self._viz_physics(self._viz_targets(rows))
        lines = []
        for r in range(rows):
            i = rows - 1 - r                        # bass at the bottom
            v = self.bars[i]
            width = int(v * n) or (1 if v > 0.02 else 0)
            c = self._viz_color(i / max(rows - 1, 1))
            lp = (n - width) // 2
            lines.append(crop_pad(
                " " * (pad_l + lp) + fg(c) + "▆" * width + RESET, w))
        return lines

    def _render_progress(self, w):
        pos = self.player.props.get("time-pos")
        dur = self.player.props.get("duration")
        vol = int(self.player.props.get("volume") or 0)
        mute = self.player.props.get("mute")
        paused = bool(self.player.props.get("pause"))

        bar_w = max(w - 22, 10)
        frac = (pos / dur) if (pos and dur) else 0.0
        frac = max(0.0, min(1.0, frac))
        filled = int(bar_w * frac)
        bar = []
        for i in range(bar_w):
            if i < filled:
                bar.append(fg(lerp(RED, ORANGE, i / max(bar_w - 1, 1))) + "━")
            elif i == filled:
                bar.append(fg(WHITE) + BOLD + "●" + RESET)
            else:
                bar.append(fg(DGREY) + "─")
        state = ("⏸" if paused else "▶") if not self.player.loading else "⟳"
        times = f"{fmt_time(pos)} / {fmt_time(dur)}"
        line1 = (f"  {fg(RED)}{state}{RESET} " + "".join(bar) + RESET +
                 f" {fg(GREY)}{times}{RESET}")

        vol_w = 12
        vfill = int(vol / 130 * vol_w)
        vbar = (fg(PINK) + "█" * vfill + fg(DGREY) + "░" * (vol_w - vfill) + RESET)
        flags = []
        if self.repeat:
            flags.append(fg(ORANGE) + "⟲ repeat" + RESET)
        if mute:
            flags.append(fg(RED) + "muted" + RESET)
        qinfo = (f"{self.qpos + 1}/{len(self.queue)}"
                 if self.queue else "–")
        line2 = (f"  {fg(GREY)}vol{RESET} {vbar} {fg(GREY)}{vol:>3}%"
                 f"   queue {qinfo}{RESET}"
                 + ("   " + "  ".join(flags) if flags else ""))
        return [crop_pad(line1, w), crop_pad(line2, w)]

    def _render_footer(self, w):
        if self.status and time.time() - self.status_t < 4:
            return crop_pad("  " + fg(ORANGE) + self.status + RESET, w)
        if self.viz_max:
            keys = [("F", "exit"), ("M", "tune"), ("v", "viz"),
                    ("c", "theme"), ("p", "px"), ("spc", "pause"),
                    ("n/b", "skip"), ("L", "like"), ("±", "vol"),
                    ("q", "quit")]
        elif self.full:
            keys = [("f", "exit full"), ("spc", "pause"), ("n/b", "skip"),
                    ("v", "viz"), ("c", "theme"), ("p", "hd"),
                    ("w", "work"), ("±", "vol"), ("q", "quit")]
        else:
            keys = [("/", "find"), ("↵", "play"), ("spc", "pause"),
                    ("n/b", "skip"), ("q", "quit"), ("f", "full"),
                    ("F", "max viz"), ("M", "tune"), ("v", "viz"),
                    ("c", "theme"), ("w", "work"),
                    ("a", "+queue"), ("A", "→playlist"), ("N", "new pl"),
                    ("x", "remove"), ("L", "like"), (",/.", "seek"),
                    ("±", "vol"), ("s", "shuf"), ("r", "rep"),
                    ("tab", "view")]
        # add hints while they fit, most important first
        line, used = "  ", 2
        for i, (k, v) in enumerate(keys):
            piece = (("" if i == 0 else " · ") + f"{k} {v}")
            if used + len(piece) > w - 1:
                break
            line += (("" if i == 0 else fg(DGREY) + " · ") +
                     fg(RED) + k + fg(DGREY) + " " + v)
            used += len(piece)
        return crop_pad(line + RESET, w)

    # ── main loop ────────────────────────────────────────────────────────────
    def run(self):
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        sys.stdout.write("\x1b[?1049h\x1b[?25l\x1b[2J")
        try:
            tty.setraw(fd, termios.TCSANOW)
            # keep output post-processing (raw mode disables ONLCR)
            attrs = termios.tcgetattr(fd)
            attrs[1] |= termios.OPOST | termios.ONLCR
            termios.tcsetattr(fd, termios.TCSANOW, attrs)
            while self.running:
                if self.eof_flag.is_set():
                    self.eof_flag.clear()
                    self.next_track()
                if self.now and time.time() - self._now_wt > 2:
                    self._now_wt = time.time()
                    self._write_now()
                # drop runs at ~60 fps; other styles tick the UI at ~25
                # (smooth progress bar) with the visualizer itself cached
                # down to ~12; idle screen is lazier
                if self.viz_style == "drop" and (self.now or self.viz_max):
                    tick = 0.016
                elif self.now or self.input_mode or self.viz_max:
                    tick = 0.04
                else:
                    tick = 0.12
                k = self.read_key(tick)
                if k:
                    self.handle_key(k)
                self.render()
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
            if self._kitty_ok:
                sys.stdout.write("\x1b_Ga=d,d=A,q=2\x1b\\")
            sys.stdout.write("\x1b[?25h\x1b[?1049l")
            sys.stdout.flush()
            self._save_state()
            self.now = None
            self._write_now()
            self.tap.stop()
            self.player.quit()


# ──────────────────────────────────────────────────────────────────────────────
# entry
# ──────────────────────────────────────────────────────────────────────────────

def ansi_line_to_pango(s):
    """Translate one truecolor-ANSI line into pango markup for eww/GTK."""
    out, buf = [], []
    fgc = bgc = None

    def flush():
        if not buf:
            return
        text = ("".join(buf).replace("&", "&amp;")
                .replace("<", "&lt;").replace(">", "&gt;"))
        attrs = []
        if fgc:
            attrs.append(f"foreground='#{fgc[0]:02x}{fgc[1]:02x}{fgc[2]:02x}'")
        if bgc:
            attrs.append(f"background='#{bgc[0]:02x}{bgc[1]:02x}{bgc[2]:02x}'")
        if attrs:
            out.append(f"<span {' '.join(attrs)}>{text}</span>")
        else:
            out.append(text)
        buf.clear()

    i = 0
    while i < len(s):
        m = ANSI_RE.match(s, i)
        if m:
            codes = [int(x) for x in
                     (m.group(0)[2:-1] or "0").split(";") if x != ""] or [0]
            j = 0
            nf, nb = fgc, bgc
            while j < len(codes):
                if codes[j] == 0:
                    nf = nb = None
                elif codes[j] == 38 and j + 4 < len(codes) and codes[j+1] == 2:
                    nf = (codes[j+2], codes[j+3], codes[j+4]); j += 4
                elif codes[j] == 48 and j + 4 < len(codes) and codes[j+1] == 2:
                    nb = (codes[j+2], codes[j+3], codes[j+4]); j += 4
                j += 1
            if (nf, nb) != (fgc, bgc):
                flush()
                fgc, bgc = nf, nb
            i = m.end()
            continue
        buf.append(s[i])
        i += 1
    flush()
    return "".join(out)


def eww_stream(spec, style, fps, theme_name, frame_title=None):
    """Headless visualizer feed for an eww widget (deflisten).

    Prints one pango-markup line per frame, rows joined with &#10;.
    Visualizes whatever the system is playing — no TUI, no mpv."""
    try:
        cols, rows = (int(x) for x in spec.lower().split("x"))
    except ValueError:
        print("--eww expects COLSxROWS, e.g. 64x9", file=sys.stderr)
        return False
    names = [t[0] for t in THEMES]
    set_theme(names.index(theme_name) if theme_name in names else 0)
    if style not in VIZ_STYLES:
        style = "drop"

    import types
    v = types.SimpleNamespace()
    for name in dir(App):
        if name.startswith(("_viz", "_drop")) or name == "_render_visualizer_now":
            v.__dict__[name] = getattr(App, name).__get__(v)
    v.N_PRESETS = App.N_PRESETS
    v.viz_style = style
    v.drop_px = 1
    v.eww_flush = True
    v.bars, v.peaks = [], []
    v._phys_t = time.time()
    v.player = types.SimpleNamespace(props={}, loading=False)
    v.tap = SpectrumTap()
    v._viz_cache = None
    v._drop_t = random.uniform(0, 90)
    v._drop_last = time.time()
    v._drop_e = [0.0, 0.0, 0.0]
    v._drop_lut_cache = None
    v._drop_preset = random.randrange(App.N_PRESETS)
    v._drop_prev = v._drop_preset
    v._drop_pa = v._drop_new_params()
    v._drop_pb = v._drop_pa
    v._drop_mix = 1.0
    v._drop_switch_at = time.time() + random.uniform(12, 24)
    v._drop_last_switch = 0.0
    v._drop_bass_avg = 0.15

    top = bot = None
    if frame_title:
        # ascii box matching the classic eww hud style:
        # +-- TITLE ------+ / |rows| / +-----------+
        top = ("+-- " + frame_title + " ").ljust(cols + 1, "-") + "+"
        bot = "+" + "-" * cols + "+"

    period = 1.0 / max(min(fps, 30), 1)
    try:
        while True:
            t0 = time.time()
            lines = v._render_visualizer_now(cols, rows)
            rows_md = [ansi_line_to_pango(ln) for ln in lines]
            if frame_title:
                rows_md = [top] + ["|" + r + "|" for r in rows_md] + [bot]
            print("&#10;".join(rows_md), flush=True)
            time.sleep(max(0.0, period - (time.time() - t0)))
    except (BrokenPipeError, KeyboardInterrupt):
        pass
    finally:
        v.tap.stop()
    return True


def doctor():
    ok = True
    for tool in ("mpv", "yt-dlp", "ffmpeg"):
        path = shutil.which(tool)
        print(f"  {'✓' if path else '✗'} {tool:8s} {path or 'MISSING'}")
        ok = ok and bool(path)
    for mod in ("ytmusicapi", "PIL"):
        try:
            __import__(mod)
            print(f"  ✓ python:{mod}")
        except ImportError:
            print(f"  ✗ python:{mod} MISSING")
            ok = False
    if os.path.isfile(AUTH_FILE):
        print(f"  {'✓' if verify_auth() else '✗'} auth ({AUTH_FILE})")
    else:
        print("  ○ no auth file — guest mode (search/play only)")
    return ok


def main():
    ap = argparse.ArgumentParser(prog="ytm", description=__doc__)
    ap.add_argument("--login", "--setup", action="store_true", dest="login",
                    help="interactive sign-in wizard (pick your browser)")
    ap.add_argument("--auth", action="store_true",
                    help="sign in by pasting browser request headers")
    ap.add_argument("--auth-firefox", action="store_true",
                    help="import session from Firefox cookies")
    ap.add_argument("--auth-browser", metavar="NAME",
                    help="import session from a browser non-interactively "
                         "(chrome, brave, edge, chromium, vivaldi, opera)")
    ap.add_argument("--doctor", action="store_true", help="check dependencies")
    ap.add_argument("--eww", metavar="WxH",
                    help="stream visualizer frames as pango markup for an "
                         "eww widget, e.g. --eww 64x9")
    ap.add_argument("--eww-style", default="drop", choices=VIZ_STYLES,
                    help="visualizer style for --eww (default: drop)")
    ap.add_argument("--eww-fps", type=int, default=10,
                    help="frame rate for --eww (default: 10)")
    ap.add_argument("--eww-theme", default="ytm",
                    help="color theme for --eww (default: ytm)")
    ap.add_argument("--eww-frame", metavar="TITLE", default=None,
                    help="wrap --eww frames in an ascii box with this title")
    ap.add_argument("--ao", default=None, help=argparse.SUPPRESS)
    args = ap.parse_args()

    if args.doctor:
        sys.exit(0 if doctor() else 1)
    if args.eww:
        sys.exit(0 if eww_stream(args.eww, args.eww_style, args.eww_fps,
                                 args.eww_theme, args.eww_frame) else 1)
    if args.login:
        sys.exit(0 if login_wizard() else 1)
    if args.auth:
        sys.exit(0 if paste_headers_auth() else 1)
    if args.auth_firefox:
        sys.exit(0 if import_firefox_auth() else 1)
    if args.auth_browser:
        sys.exit(0 if import_browser_auth(args.auth_browser) else 1)

    if not sys.stdin.isatty():
        print("ytm needs a TTY. Run it in a terminal.")
        sys.exit(1)
    if not os.path.isfile(AUTH_FILE) and sys.stdout.isatty():
        print("  no account linked yet — search and playback still work.")
        print("  to get your library/likes/playlists:  ytm --login")
        print("  starting in guest mode in 3s…")
        time.sleep(3)
    App(ao=args.ao).run()


if __name__ == "__main__":
    main()
