#!/usr/bin/env python3
"""ytm — a beautiful YouTube Music terminal client.

Search, library, playlists, queue, radio. Playback via mpv + yt-dlp.
True-color half-block album art, animated visualizer, gradient UI.

Usage:
  ytm                 launch the TUI
  ytm --auth          sign in by pasting request headers from music.youtube.com
  ytm --auth-firefox  import your session from Firefox's cookie store
  ytm --doctor        check that all dependencies are healthy
"""

import argparse
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
import subprocess
import sys
import tempfile
import termios
import threading
import time
import tty
import unicodedata
import urllib.request
from dataclasses import dataclass, field

# ──────────────────────────────────────────────────────────────────────────────
# paths / config
# ──────────────────────────────────────────────────────────────────────────────

CONFIG_DIR = os.path.expanduser("~/.config/ytm-tui")
AUTH_FILE = os.path.join(CONFIG_DIR, "browser.json")
STATE_FILE = os.path.join(CONFIG_DIR, "state.json")

UA = ("Mozilla/5.0 (X11; Linux x86_64; rv:151.0) "
      "Gecko/20100101 Firefox/151.0")

# palette
RED = (255, 0, 51)
PINK = (255, 94, 125)
ORANGE = (255, 138, 41)
WHITE = (236, 236, 240)
GREY = (130, 130, 142)
DGREY = (70, 70, 80)
DARK = (24, 24, 30)
GREEN = (108, 222, 130)

RESET = "\x1b[0m"
BOLD = "\x1b[1m"
DIM = "\x1b[2m"
ITAL = "\x1b[3m"

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


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
        return cls(vid, title, artist, album, dur, thumb)


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
    CHUNK = 2048  # ~46 ms per FFT frame

    def __init__(self, source_cmd=None):
        self.alive = False
        self._spectrum = None     # raw |rfft| of latest chunk
        self._gain = 1e-6
        self._lock = threading.Lock()
        self._proc = None
        self._source_cmd = source_cmd
        self._stop = False
        try:
            import numpy  # noqa: F401  (fail early if missing)
        except ImportError:
            return
        threading.Thread(target=self._run, daemon=True).start()

    def _build_cmd(self):
        if self._source_cmd:
            return self._source_cmd
        if not shutil.which("parec"):
            return None
        try:
            sink = subprocess.run(
                ["pactl", "get-default-sink"], capture_output=True,
                text=True, timeout=3).stdout.strip()
        except Exception:
            sink = ""
        if not sink:
            return None
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
                with self._lock:
                    self._spectrum = spec
            self.alive = False
            self._proc = None
            if self._stop:
                return
            time.sleep(3)  # sink vanished (e.g. BT drop) — retry, re-resolve

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
        sapisid = cookies.get("SAPISID") or cookies.get("__Secure-3PAPISID")
        if not sapisid:
            continue
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
            print(f"✓ signed in — session imported from {db}")
            return True
        os.unlink(AUTH_FILE)
        print(f"  (cookies in {db} didn't authenticate, trying next profile)")
    print("✗ found Firefox profiles, but none had a valid YouTube Music login.")
    print("  Log in at https://music.youtube.com and retry.")
    return False


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
        self.liked_now = False
        self.input_mode = False
        self.input_buf = ""
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
                           "repeat": self.repeat}, f)
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
            self.say("library needs sign-in → quit and run: ytm --auth-firefox")
            return
        self.loading_msg = "loading liked songs…"

        def work():
            try:
                data = self.yt.get_liked_songs(limit=300)
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
            self.say("playlists need sign-in → quit and run: ytm --auth-firefox")
            return
        self.loading_msg = "loading playlists…"

        def work():
            try:
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
                data = self.yt.get_playlist(pl.playlist_id, limit=300)
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
    def play_queue(self, idx):
        if not (0 <= idx < len(self.queue)):
            return
        self.qpos = idx
        self.now = self.queue[idx]
        self.liked_now = False
        self.player.play_video(self.now.video_id)
        self.say(f"▶ {self.now.title}")

    def next_track(self):
        if self.qpos + 1 < len(self.queue):
            self.play_queue(self.qpos + 1)
        elif self.repeat and self.queue:
            self.play_queue(0)
        else:
            self.now = None
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

    def like_current(self):
        if not self.now:
            return
        if not self.authed:
            self.say("liking needs sign-in → ytm --auth-firefox")
            return

        def work():
            try:
                self.yt.rate_song(self.now.video_id, "LIKE")
                self.liked_now = True
                self.say(f"♥ liked: {self.now.title}")
            except Exception as e:
                self.say(f"like failed: {e}")
        threading.Thread(target=work, daemon=True).start()

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
                if self.input_buf.strip():
                    self.tab = 0
                    self.do_search(self.input_buf.strip())
                self.input_buf = ""
            elif k in ("\x7f", "\x08"):
                self.input_buf = self.input_buf[:-1]
            elif k and len(k) == 1 and k.isprintable():
                self.input_buf += k
            return

        lst = self.current_list()
        if k == "q":
            self.running = False
        elif k == "/":
            self.input_mode = True
            self.input_buf = ""
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

        out = ["\x1b[H"]
        for i, ln in enumerate(lines[:h]):
            out.append(f"\x1b[{i + 1};1H" + ln + "\x1b[0m\x1b[K")
        sys.stdout.write("".join(out))
        sys.stdout.flush()

    def _render_header(self, w):
        logo1 = "█▄█ ▀█▀   █▀▄▀█ █ █ █▀▀ █ █▀▀"
        logo2 = " █   █    █ ▀ █ █▄█ ▄▄█ █ █▄▄"
        badge = fg(RED) + "▶ " + RESET
        acct = (fg(GREEN) + "● signed in" if self.authed
                else fg(GREY) + "○ guest — run: ytm --auth-firefox")
        tabs = []
        for i, t in enumerate(TABS):
            if i == self.tab:
                tabs.append(bg(RED) + fg(WHITE) + BOLD + f" {i+1} {t} " + RESET)
            else:
                tabs.append(fg(GREY) + f" {i+1} {t} " + RESET)
        tab_bar = (fg(DGREY) + "·" + RESET).join(tabs)

        l1 = "  " + badge + grad_text(logo1, RED, ORANGE)
        l2 = "    " + grad_text(logo2, PINK, RED)
        pad1 = w - visible_len(l1) - visible_len(acct) - 2
        l1 += " " * max(pad1, 1) + acct + " "
        l3 = "  " + tab_bar
        rule = fg(DGREY) + "─" * w + RESET
        return [crop_pad(l1, w), crop_pad(l2, w), crop_pad(l3, w), rule]

    def _render_list(self, w, h):
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
            box = ("  " + fg(RED) + "search ❯ " + RESET + fg(WHITE) +
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
                line = bg((48, 16, 24)) + fg(WHITE) + BOLD + crop_pad(
                    " " + fg(RED) + "▌" + fg(WHITE) + plain[1:], w)
            out.append(crop_pad(line, w))
        return out

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
        art_h = max(min(h - 11, (w - 6) // 2, 22), 4)
        viz_rows = max(2, min(h - art_h - 7, 9))
        art_w = art_h * 2
        pad_l = max((w - art_w) // 2, 1)
        art = self.art.get(self.now.thumb, art_w, art_h)

        out.append(crop_pad("", w))
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
        return out[:h]

    def _viz_color(self, hfrac):
        """Vertical gradient: red base → orange mids → pink peaks."""
        if hfrac < 0.5:
            return lerp(RED, ORANGE, hfrac * 2)
        return lerp(ORANGE, PINK, (hfrac - 0.5) * 2)

    def _render_visualizer(self, w, rows):
        n = max(w - 10, 16)
        paused = bool(self.player.props.get("pause"))
        if len(self.bars) != n:
            self.bars = [0.0] * n
            self.peaks = [0.0] * n

        if self.tap and self.tap.alive:
            targets = self.tap.levels(n)            # real FFT of the audio
        elif paused or self.player.loading:
            targets = [0.0] * n
        else:                                       # decorative fallback
            t = time.time()
            targets = [max(0.0, min(1.0,
                       (math.sin(t * 2.1 + i * 0.55) +
                        math.sin(t * 3.7 + i * 0.21)) * 0.25 + 0.5 +
                       random.uniform(-0.18, 0.18))) for i in range(n)]

        for i in range(n):
            tv = targets[i]
            b = self.bars[i]
            # fast attack, gravity fall
            self.bars[i] = tv if tv > b else max(b - 0.09, tv)
            self.peaks[i] = max(self.peaks[i] - 0.030, self.bars[i])

        unit = rows * 8
        pad_l = (w - n) // 2
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
        vbar = (fg(PINK) + "▮" * vfill + fg(DGREY) + "▯" * (vol_w - vfill) + RESET)
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
        keys = [("/", "find"), ("↵", "play"), ("spc", "pause"),
                ("n/b", "skip"), ("q", "quit"), ("a", "+queue"),
                ("L", "like"), (",/.", "seek"), ("±", "vol"),
                ("s", "shuf"), ("r", "rep"), ("tab", "view")]
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
                k = self.read_key(0.08)
                if k:
                    self.handle_key(k)
                self.render()
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
            sys.stdout.write("\x1b[?25h\x1b[?1049l")
            sys.stdout.flush()
            self._save_state()
            self.tap.stop()
            self.player.quit()


# ──────────────────────────────────────────────────────────────────────────────
# entry
# ──────────────────────────────────────────────────────────────────────────────

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
    ap.add_argument("--auth", action="store_true",
                    help="sign in by pasting browser request headers")
    ap.add_argument("--auth-firefox", action="store_true",
                    help="import session from Firefox cookies")
    ap.add_argument("--doctor", action="store_true", help="check dependencies")
    ap.add_argument("--ao", default=None, help=argparse.SUPPRESS)
    args = ap.parse_args()

    if args.doctor:
        sys.exit(0 if doctor() else 1)
    if args.auth:
        sys.exit(0 if paste_headers_auth() else 1)
    if args.auth_firefox:
        sys.exit(0 if import_firefox_auth() else 1)

    if not sys.stdin.isatty():
        print("ytm needs a TTY. Run it in a terminal.")
        sys.exit(1)
    App(ao=args.ao).run()


if __name__ == "__main__":
    main()
