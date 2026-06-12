#!/usr/bin/env python3
"""NOCTURNE (ytm) — night music in the terminal.

YouTube Music + SoundCloud in one player. Search, library, playlists,
queue, radio. Playback via mpv + yt-dlp. True-color album art, a
beat-locked plasma visualizer, Undertale hearts.

Usage:
  ytm                 launch the TUI
  ytm setup           guided setup: deps, sign-in, theme, visualizer
  ytm update          pull the latest version + refresh yt-dlp
  ytm uninstall       remove the launcher, install and (optionally) config
  ytm --auth          sign in by pasting request headers from music.youtube.com
  ytm --oauth         sign in with a YouTube-scoped OAuth token (no cookies)
  ytm --login         interactive sign-in wizard (pick your browser)
  ytm --auth-firefox  import your session from Firefox's cookie store
  ytm --sc-login      sign in to soundcloud (likes join the library)
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
import dataclasses
from dataclasses import dataclass, field

# ──────────────────────────────────────────────────────────────────────────────
# paths / config
# ──────────────────────────────────────────────────────────────────────────────

CONFIG_DIR = os.path.expanduser("~/.config/ytm-tui")
CACHE_DIR = os.path.expanduser("~/.cache/nocturne")
ART_CACHE_DIR = os.path.join(CACHE_DIR, "art")


def shader_active():
    """True when the host ghostty runs a post-process shader (starfield
    wallpapers etc.) — dark pixels don't survive those, so nocturne
    forces its black environment automatically. No setting, no knob.
    Inside nocturne's own clean window the shader is overridden, so
    this answers False there (true blacks come back)."""
    if os.environ.get("NOCTURNE_PURE") == "1":
        return False
    if "ghostty" not in (os.environ.get("TERM", "")
                         + os.environ.get("TERM_PROGRAM", "")).lower():
        return False
    cfg = os.path.join(os.environ.get("XDG_CONFIG_HOME",
                                      os.path.expanduser("~/.config")),
                       "ghostty", "config")
    try:
        with open(cfg) as f:
            for ln in f:
                ln = ln.strip()
                if ln.startswith("custom-shader") and "=" in ln:
                    if ln.split("=", 1)[1].strip().strip('"'):
                        return True
    except OSError:
        pass
    return False


def cache_load(name, max_age=7 * 86400):
    """Stale-while-revalidate: yesterday's library beats a blank tab."""
    try:
        p = os.path.join(CACHE_DIR, name + ".json")
        if time.time() - os.path.getmtime(p) > max_age:
            return None
        with open(p) as f:
            return json.load(f)
    except Exception:
        return None


def cache_save(name, data):
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        p = os.path.join(CACHE_DIR, name + ".json")
        with open(p + ".tmp", "w") as f:
            json.dump(data, f)
        os.replace(p + ".tmp", p)
    except Exception:
        pass
AUTH_FILE = os.path.join(CONFIG_DIR, "browser.json")
OAUTH_FILE = os.path.join(CONFIG_DIR, "oauth.json")
OAUTH_CLIENT = os.path.join(CONFIG_DIR, "oauth_client.json")
SC_FILE = os.path.join(CONFIG_DIR, "soundcloud.json")
SC_COOKIES = os.path.join(CONFIG_DIR, "sc_cookies.txt")
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
    # gotham slate rising into bat-signal yellow — the drop LUT becomes
    # black → grey clouds → a yellow glow cutting through them
    ("batman",    (104, 110, 126), (255, 204, 0),   (255, 234, 130)),
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
    # zero-width: combining accents (гу́би), variation selectors (💋️),
    # zero-width (non-)joiners — terminals draw them as 0 cells; counting
    # them as 1 skews every row they appear in (drifting separators,
    # clipped durations)
    if unicodedata.combining(ch) or ch in "\u200b\u200c\u200d\ufe0e\ufe0f\u200e\u200f":
        return 0
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
    if cells[i][1] == KITTY_MARK:   # never bury the bitmap's anchor cell
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
    stream_hint: str = ""    # sc: transcoding endpoints + auth (json)

    @property
    def source(self):
        """Where this track lives: 'yt' (bare video id) or 'sc' (a full
        soundcloud permalink rides in video_id and mpv plays it via
        yt-dlp like anything else)."""
        return "sc" if self.video_id.startswith("http") else "yt"

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
# soundcloud source — no API keys, no account: yt-dlp's extractors do the
# talking (flat extraction = one HTTP round trip), and mpv streams the
# permalink through the same yt-dlp hook that plays YouTube
# ──────────────────────────────────────────────────────────────────────────────

# a TUI owns the screen — yt-dlp must never print a byte
_SC_SILENT = type("_SCLog", (), {
    "debug": lambda *a, **k: None, "info": lambda *a, **k: None,
    "warning": lambda *a, **k: None, "error": lambda *a, **k: None})()


def _sc_extract(target, n):
    try:
        import yt_dlp
    except ImportError:
        return []
    opts = {"quiet": True, "no_warnings": True, "extract_flat": True,
            "playlistend": n, "skip_download": True, "ignoreerrors": True,
            "logger": _SC_SILENT}
    tok = sc_token()
    if tok:
        opts.update({"username": "oauth", "password": tok})
    try:
        with yt_dlp.YoutubeDL(opts) as y:
            data = y.extract_info(target, download=False)
    except Exception:
        return []
    out = []
    for e in (data or {}).get("entries") or []:
        if not e:
            continue            # a blocked entry came back None
        # webpage_url = the clean permalink (plays in mpv AND grows a
        # /recommended shelf); plain url is an api.soundcloud.com handle
        url = e.get("webpage_url") or e.get("url") or ""
        if not url.startswith("http"):
            continue
        thumbs = e.get("thumbnails") or []
        out.append(Track(url, e.get("title") or "?",
                         e.get("uploader") or "SoundCloud",
                         album="SoundCloud",
                         duration=fmt_time(e.get("duration")),
                         thumb=_sc_art(thumbs[-1].get("url", "")
                                       if thumbs else "")))
    return out


def _sc_probe(tracks, keep=12, workers=10):
    """Major-label uploads are DRM-locked and indistinguishable in the
    flat search payload — fully resolve each candidate in parallel and
    keep only what will actually play. ~4-5s for a dozen, which is fine
    for results that ride in asynchronously."""
    try:
        import yt_dlp
        from concurrent.futures import ThreadPoolExecutor
    except ImportError:
        return tracks[:keep]

    tok = sc_token()

    def probe(t):
        opts = {"quiet": True, "no_warnings": True, "skip_download": True,
                "logger": _SC_SILENT}
        if tok:
            opts.update({"username": "oauth", "password": tok})
        try:
            with yt_dlp.YoutubeDL(opts) as y:
                info = y.extract_info(t.video_id, download=False)
            if any(f.get("acodec") not in (None, "none")
                   for f in (info or {}).get("formats", [])):
                return t
        except Exception:
            pass
        return None

    with ThreadPoolExecutor(workers) as ex:
        return [t for t in ex.map(probe, tracks) if t][:keep]


def sc_search(query, n=12):
    return _sc_probe(_sc_extract(f"scsearch{n}:{query}", n), keep=n)


# ── soundcloud sign-in (optional) ─────────────────────────────────────────────
# guest mode covers search/play/radio; signing in adds YOUR likes to the
# library, L on ☁ tracks, and Go+/private streams. The session is the
# browser's `oauth_token` cookie — lifted locally, stored chmod 600,
# never leaves the machine. Same privacy contract as the YT side.

def sc_token():
    try:
        with open(SC_FILE) as f:
            return json.load(f).get("oauth_token") or ""
    except Exception:
        return ""


def _sc_client_id():
    """Borrow yt-dlp's anonymous client_id (it scrapes + caches one and
    keeps it healthy across soundcloud's rotations)."""
    try:
        import yt_dlp
        y = yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True,
                              "logger": _SC_SILENT})
        ie = y.get_info_extractor("Soundcloud")
        ie._initialize_pre_login()
        return getattr(ie, "_CLIENT_ID", "") or ""
    except Exception:
        return ""


def _sc_get(url, token, method="GET"):
    import urllib.request
    headers = {"User-Agent": "Mozilla/5.0"}
    if token:
        headers["Authorization"] = f"OAuth {token}"
    req = urllib.request.Request(url, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=15) as r:
        body = r.read()
    return json.loads(body) if body.strip() else {}


def sc_me(token):
    try:
        d = _sc_get("https://api-v2.soundcloud.com/me?client_id="
                    + _sc_client_id(), token)
        return d.get("username") or "", d.get("id") or 0
    except Exception:
        return "", 0


def _sc_art(url):
    """SoundCloud hands out 100px '-large' artwork — the 500px variant
    lives at a predictable sibling URL (verified live). At 100px the
    cover visualizer painted blobs."""
    return (url or "").replace("-large.", "-t500x500.")


def _sc_hint_of(tr):
    """The plain (non-DRM) transcoding endpoints + the track's auth
    token, packed into the Track — turns play-time stream resolution
    into one ~0.3s api call instead of a 5s yt-dlp run."""
    urls = [t["url"] for t in (tr.get("media") or {}).get("transcodings", [])
            if t.get("url")
            and t.get("format", {}).get("protocol") in ("progressive", "hls")]
    if not urls:
        return ""
    return json.dumps({"a": tr.get("track_authorization") or "",
                       "u": urls[:4]})


def _sc_track_of(tr):
    art = (tr.get("artwork_url")
           or (tr.get("user") or {}).get("avatar_url") or "")
    return Track(tr.get("permalink_url") or "", tr.get("title") or "?",
                 (tr.get("user") or {}).get("username") or "SoundCloud",
                 album="SoundCloud",
                 duration=fmt_time((tr.get("duration") or 0) / 1000),
                 thumb=_sc_art(art),
                 stream_hint=_sc_hint_of(tr))


def sc_resolve_fast(track):
    """Direct stream URL via api-v2 (~0.3s). Tries the packed hint,
    then a fresh resolve of the permalink (hints go stale), and gives
    up ('') for Go+/encrypted tracks — the yt-dlp path handles those."""
    import urllib.parse
    token = sc_token()
    cid = _sc_client_id()
    if not cid:
        return ""

    def try_urls(urls, auth):
        q = f"?client_id={cid}"
        if auth:
            q += "&track_authorization=" + urllib.parse.quote(auth)
        for u in urls:
            try:
                got = _sc_get(u + q, token).get("url") or ""
                if got:
                    return got
            except Exception:
                continue
        return ""

    if track.stream_hint:
        try:
            h = json.loads(track.stream_hint)
            got = try_urls(h.get("u") or [], h.get("a") or "")
            if got:
                return got
        except Exception:
            pass
    try:
        tr = _sc_get("https://api-v2.soundcloud.com/resolve?url="
                     + urllib.parse.quote(track.video_id, safe="")
                     + f"&client_id={cid}", token)
        urls = [t["url"] for t in (tr.get("media") or {}).get("transcodings", [])
                if t.get("format", {}).get("protocol") in ("progressive", "hls")]
        return try_urls(urls, tr.get("track_authorization") or "")
    except Exception:
        return ""


def _sc_uid(token):
    try:
        with open(SC_FILE) as f:
            uid = json.load(f).get("user_id") or 0
    except Exception:
        uid = 0
    if not uid:
        _, uid = sc_me(token)
    return uid


def sc_likes(token, n=200):
    """Your soundcloud likes, newest first. NOTE: the endpoint is
    /users/{id}/track_likes — /me/track_likes 404s on api-v2."""
    cid = _sc_client_id()
    uid = _sc_uid(token)
    if not uid:
        return []
    url = (f"https://api-v2.soundcloud.com/users/{uid}/track_likes"
           f"?client_id={cid}&limit=100")
    out = []
    try:
        while url and len(out) < n:
            d = _sc_get(url, token)
            for it in d.get("collection") or []:
                t = _sc_track_of(it.get("track") or {})
                if t.video_id:
                    out.append(t)
            nxt = d.get("next_href")
            url = (nxt + ("&" if "?" in nxt else "?")
                   + f"client_id={cid}") if nxt else None
    except Exception:
        pass
    return out[:n]


def sc_playlists(token, n=100):
    """Everything on your soundcloud library shelf — your sets and the
    ones you've liked. /me/library/all is the endpoint that exists;
    /me/playlists 404s."""
    cid = _sc_client_id()
    url = (f"https://api-v2.soundcloud.com/me/library/all"
           f"?client_id={cid}&limit=50")
    out = []
    try:
        while url and len(out) < n:
            d = _sc_get(url, token)
            for it in d.get("collection") or []:
                pl = it.get("playlist") or it.get("system_playlist") or {}
                u = pl.get("permalink_url") or ""
                if u:
                    out.append(Playlist(u, pl.get("title") or "?",
                                        str(pl.get("track_count") or "")))
            nxt = d.get("next_href")
            url = (nxt + ("&" if "?" in nxt else "?")
                   + f"client_id={cid}") if nxt else None
    except Exception:
        pass
    return out[:n]


def sc_playlist_tracks(pl, n=500):
    """Open a soundcloud set via api-v2: resolve hydrates the first few
    tracks, the rest arrive as id-stubs that /tracks?ids= fills in (50
    per call). Full metadata, 2-3 round trips for a 100-track set. No
    DRM probe — a locked track just auto-skips at play time."""
    import urllib.parse
    token = sc_token()
    cid = _sc_client_id()
    try:
        d = _sc_get("https://api-v2.soundcloud.com/resolve?url="
                    + urllib.parse.quote(pl.playlist_id, safe="")
                    + f"&client_id={cid}", token)
        raw = (d.get("tracks") or [])[:n]
        full = {t["id"]: t for t in raw if t.get("permalink_url")}
        missing = [str(t["id"]) for t in raw if not t.get("permalink_url")]
        for i in range(0, len(missing), 50):
            chunk = ",".join(missing[i:i + 50])
            for t in _sc_get("https://api-v2.soundcloud.com/tracks"
                             f"?ids={chunk}&client_id={cid}", token):
                full[t["id"]] = t
        return [_sc_track_of(full[t["id"]]) for t in raw
                if full.get(t["id"], {}).get("permalink_url")]
    except Exception:
        return []


def weave(a, b):
    """Merge two newest-first lists so each keeps its order and both
    spread evenly through the result — the closest thing to a date
    merge available, since YT Music never exposes liked-at timestamps
    (soundcloud does, but a one-sided date is no date)."""
    out, i, j = [], 0, 0
    la, lb = max(len(a), 1), max(len(b), 1)
    while i < len(a) or j < len(b):
        if j >= len(b) or (i < len(a) and i / la <= j / lb):
            out.append(a[i])
            i += 1
        else:
            out.append(b[j])
            j += 1
    return out


def sc_like(track, on=True):
    """Like/unlike on the soundcloud side: resolve the permalink to a
    track id, then PUT/DELETE the like."""
    token = sc_token()
    if not token:
        return False
    try:
        import urllib.parse
        cid = _sc_client_id()
        uid = _sc_uid(token)
        tr = _sc_get("https://api-v2.soundcloud.com/resolve?url="
                     + urllib.parse.quote(track.video_id, safe="")
                     + f"&client_id={cid}", token)
        _sc_get(f"https://api-v2.soundcloud.com/users/{uid}"
                f"/track_likes/{tr['id']}?client_id={cid}", token,
                method="PUT" if on else "DELETE")
        return True
    except Exception:
        return False


def sc_login():
    """ytm --sc-login: lift the soundcloud session out of a browser the
    same way --login does for YT. Nothing is typed, nothing is sent
    anywhere but soundcloud itself."""
    print("→ looking for a soundcloud session in your browsers…")
    found, src = "", ""
    for db in firefox_cookie_dbs():                 # firefox family
        try:
            with tempfile.TemporaryDirectory() as td:
                tmp = os.path.join(td, "cookies.sqlite")
                shutil.copyfile(db, tmp)
                for ext in ("-wal", "-shm"):
                    if os.path.isfile(db + ext):
                        shutil.copyfile(db + ext, tmp + ext)
                con = sqlite3.connect(tmp)
                rows = con.execute(
                    "SELECT value FROM moz_cookies WHERE host LIKE "
                    "'%soundcloud.com' AND name='oauth_token' "
                    "ORDER BY lastAccessed DESC").fetchall()
                con.close()
            if rows and rows[0][0]:
                found, src = rows[0][0], db
                break
        except Exception:
            continue
    if not found:                                   # chromium family
        try:
            from yt_dlp.cookies import extract_cookies_from_browser
            for b in ("chrome", "chromium", "brave", "edge",
                      "vivaldi", "opera"):
                try:
                    jar = extract_cookies_from_browser(b)
                except Exception:
                    continue
                for c in jar:
                    if c.domain.endswith("soundcloud.com") and \
                            c.name == "oauth_token" and c.value:
                        found, src = c.value, b
                        break
                if found:
                    break
        except ImportError:
            pass
    if not found:
        print("✗ no soundcloud login found.")
        print("  Log in at https://soundcloud.com in your browser,")
        print("  then run:  ytm --sc-login")
        return False
    who, uid = sc_me(found)
    if not who:
        print("✗ found a token but soundcloud rejected it —")
        print("  log in again in the browser and retry")
        return False
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(SC_FILE, "w") as f:
        json.dump({"oauth_token": found, "user_id": uid}, f)
    os.chmod(SC_FILE, 0o600)
    # a cookies.txt twin lets mpv's yt-dlp hook log in too (Go+/private
    # streams) — yt-dlp picks the oauth_token cookie up on its own
    with open(SC_COOKIES, "w") as f:
        f.write("# Netscape HTTP Cookie File\n"
                ".soundcloud.com\tTRUE\t/\tTRUE\t2147483647\t"
                f"oauth_token\t{found}\n")
    os.chmod(SC_COOKIES, 0o600)
    print(f"✓ soundcloud: signed in as {who}  (session from {src})")
    print(f"  token lives in {SC_FILE} (chmod 600) and never leaves")
    return True


def sc_related(track):
    """SoundCloud's own 'recommended' shelf for a track — the radio."""
    fresh = [t for t in _sc_extract(track.video_id.rstrip("/") +
                                    "/recommended", 25)
             if t.video_id != track.video_id]
    return _sc_probe(fresh, keep=15)


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
        if os.path.isfile(SC_COOKIES):
            # signed-in soundcloud: the hook's yt-dlp logs in via the
            # cookie jar (Go+/private streams). a path in ps is fine —
            # the token itself never appears on the command line
            args.append("--ytdl-raw-options-append=cookies=" + SC_COOKIES)
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

    def play_video(self, video_id, direct=None):
        self._loading = True
        self.props["time-pos"] = None
        self.props["duration"] = None
        # soundcloud (and any other yt-dlp-able source) tracks carry a
        # full URL where YT tracks carry a bare video id; a prefetched
        # direct stream URL bypasses the ytdl hook entirely
        url = direct or (video_id if video_id.startswith("http")
                         else f"https://music.youtube.com/watch?v={video_id}")
        self.cmd("loadfile", url)
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

    @staticmethod
    def _coreaudio_loopback():
        """macOS: find a loopback device (BlackHole et al) in ffmpeg's
        avfoundation list — returns its audio index, or None."""
        try:
            out = subprocess.run(
                ["ffmpeg", "-hide_banner", "-f", "avfoundation",
                 "-list_devices", "true", "-i", ""],
                capture_output=True, text=True, timeout=5).stderr
        except Exception:
            return None
        audio = False
        for ln in out.splitlines():
            low = ln.lower()
            if "avfoundation audio devices" in low:
                audio = True
                continue
            if "avfoundation video devices" in low:
                audio = False
                continue
            if audio and ("blackhole" in low or "loopback" in low
                          or "soundflower" in low):
                m = re.findall(r"\[(\d+)\]", ln)
                if m:
                    return int(m[-1])
        return None

    def _build_cmd(self):
        if self._source_cmd:
            return self._source_cmd
        if shutil.which("parec"):
            sink = self._default_sink()
            if sink:
                self._sink = sink
                return ["parec", "--raw", "--format=float32le",
                        f"--rate={self.RATE}", "--channels=1",
                        "--latency-msec=30", "-d", f"{sink}.monitor"]
        if sys.platform == "darwin" and shutil.which("ffmpeg"):
            # no monitor sources on coreaudio — tap a loopback device
            # (brew install blackhole-2ch + a multi-output device)
            idx = self._coreaudio_loopback()
            if idx is not None:
                self._sink = ""
                return ["ffmpeg", "-hide_banner", "-loglevel", "quiet",
                        "-f", "avfoundation", "-i", f":{idx}",
                        "-ac", "1", "-ar", str(self.RATE),
                        "-f", "f32le", "-"]
        return None

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
        self._cache = {}   # (url, w, h, floor) -> list[str]
        self._fetching = set()
        self._lock = threading.Lock()
        # when set (shader guard), art pixels are floored above the
        # luminance cutoff of background-keying terminal shaders so
        # covers render solid instead of dissolving into the wallpaper
        self.floor = None

    def get(self, url, w, h):
        """Return rendered art lines, or None and fetch in background."""
        if not url:
            return None
        key = (url, w, h, self.floor)
        with self._lock:
            if key in self._cache:
                return self._cache[key]
            if key in self._fetching:
                return None
            self._fetching.add(key)
        threading.Thread(target=self._fetch, args=(url, w, h, self.floor),
                         daemon=True).start()
        return None

    def palette(self, url):
        """64-color gradient pulled from the artwork (dark → bright),
        ready to drive the drop renderer. None while it's still loading."""
        if not url:
            return None
        with self._lock:
            if url in getattr(self, "_pal", {}):
                return self._pal[url]
            if not hasattr(self, "_pal"):
                self._pal = {}
            pkey = ("pal", url)
            if pkey in self._fetching:
                return None
            self._fetching.add(pkey)
        threading.Thread(target=self._fetch_pal, args=(url,),
                         daemon=True).start()
        return None

    def rgb(self, url):
        """Square 512px RGB array of the artwork (uint8), for pasting
        straight into pixel-mode bitmaps. None while loading."""
        if not url:
            return None
        with self._lock:
            if url in getattr(self, "_rgb", {}):
                return self._rgb[url]
            if not hasattr(self, "_rgb"):
                self._rgb = {}
            rkey = ("rgb", url)
            if rkey in self._fetching:
                return None
            self._fetching.add(rkey)
        threading.Thread(target=self._fetch_rgb, args=(url,),
                         daemon=True).start()
        return None

    @staticmethod
    def _raw(url):
        """Artwork bytes, disk-cached — three different renderers want
        the same image, and relaunches want it instantly."""
        import hashlib
        p = os.path.join(ART_CACHE_DIR, hashlib.sha1(url.encode()).hexdigest())
        try:
            with open(p, "rb") as f:
                return f.read()
        except OSError:
            pass
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        raw = urllib.request.urlopen(req, timeout=10).read()
        try:
            os.makedirs(ART_CACHE_DIR, exist_ok=True)
            with open(p + ".tmp", "wb") as f:
                f.write(raw)
            os.replace(p + ".tmp", p)
        except OSError:
            pass
        return raw

    def _fetch_rgb(self, url):
        try:
            from PIL import Image
            import io
            import numpy as np
            img = Image.open(io.BytesIO(self._raw(url))).convert("RGB")
            side = min(img.size)
            left = (img.width - side) // 2
            top = (img.height - side) // 2
            img = img.crop((left, top, left + side, top + side))
            img = img.resize((512, 512), Image.LANCZOS)
            with self._lock:
                self._rgb[url] = np.asarray(img, dtype=np.uint8)
        except Exception:
            with self._lock:
                self._fetching.discard(("rgb", url))

    def _fetch_pal(self, url):
        try:
            from PIL import Image
            import io
            import numpy as np
            img = Image.open(io.BytesIO(self._raw(url))) \
                .convert("RGB").resize((32, 32))
            arr = np.asarray(img).reshape(-1, 3).astype(np.float32)
            lum = arr @ np.float32([0.299, 0.587, 0.114])
            order = np.argsort(lum)
            # anchors at luminance ranks, window-averaged so one weird
            # pixel can't hijack a stop; floor the dark end for contrast
            stops, n = [], len(order)
            for q in (0.04, 0.30, 0.58, 0.82, 0.97):
                i = int(q * (n - 1))
                wnd = arr[order[max(0, i - 16):i + 16]]
                stops.append(wnd.mean(axis=0))
            stops[0] = stops[0] * 0.35
            pos = [0.0, 0.34, 0.60, 0.82, 1.0]
            lut = np.zeros((64, 3), np.float32)
            for i in range(64):
                t = i / 63
                a = max(j for j in range(5) if pos[j] <= t or j == 0)
                a = min(a, 3)
                f = (t - pos[a]) / (pos[a + 1] - pos[a])
                lut[i] = stops[a] + (stops[a + 1] - stops[a]) * min(1.0, f)
            with self._lock:
                self._pal[url] = lut
        except Exception:
            with self._lock:
                self._fetching.discard(("pal", url))

    def _fetch(self, url, w, h, floor=None):
        key = (url, w, h, floor)
        try:
            from PIL import Image
            import io
            img = Image.open(io.BytesIO(self._raw(url))).convert("RGB")
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
                    if floor:
                        t = tuple(max(v, f) for v, f in zip(t, floor))
                        b = tuple(max(v, f) for v, f in zip(b, floor))
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
        os.path.expanduser("~/Library/Application Support/Firefox/Profiles"),
        os.path.expanduser("~/Library/Application Support/librewolf/Profiles"),
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


def detected_browsers():
    """Browsers with a profile on this machine, mac + linux paths."""
    home = os.path.expanduser("~")
    asup = os.path.join(home, "Library", "Application Support")
    paths = {
        "firefox": [f"{home}/.mozilla/firefox",
                    f"{home}/.config/mozilla/firefox",
                    f"{asup}/Firefox/Profiles"],
        "chrome": [f"{asup}/Google/Chrome", f"{home}/.config/google-chrome"],
        "brave": [f"{asup}/BraveSoftware/Brave-Browser",
                  f"{home}/.config/BraveSoftware/Brave-Browser"],
        "edge": [f"{asup}/Microsoft Edge", f"{home}/.config/microsoft-edge"],
        "chromium": [f"{asup}/Chromium", f"{home}/.config/chromium"],
        "vivaldi": [f"{asup}/Vivaldi", f"{home}/.config/vivaldi"],
        "opera": [f"{asup}/com.operasoftware.Opera", f"{home}/.config/opera"],
    }
    return [b for b, ps in paths.items()
            if any(os.path.isdir(p) for p in ps)]


def login_wizard():
    print()
    print("  ── sign in to YouTube Music ──────────────────────────────")
    print()
    print("  Any method gets you your library, likes and playlists.")
    print("  Nothing ever leaves this machine.")
    print()
    print("   1) from your browser" + " " * 12 + "← easiest, ~10 seconds")
    print("      lifts your music.youtube.com session straight out of the")
    print("      browser you're already logged in with.")
    print("      + zero setup")
    print("      − the saved cookies are your whole Google session")
    print("        (kept local, chmod 600 — just know what the file is)")
    print()
    print("   2) paste request headers" + " " * 8 + "~1 minute")
    print("      you copy one request from devtools yourself.")
    print("      + nothing automated ever reads your browser files")
    print("      − fiddly (F12 → Network → copy request headers)")
    print()
    print("   3) Google OAuth" + " " * 17 + "most private")
    print("      approve a code on any device; the token is scoped to")
    print("      YouTube ONLY and revocable from your Google account.")
    print("      + can't touch Gmail/Drive/anything else")
    print("      − one-time ~5 min Google Cloud setup (guided)")
    print()
    print("   privacy tip: keep a separate browser profile (or spare")
    print("   account) logged into only music.youtube.com and use 1.")
    print()
    try:
        choice = input("  choice [1-3, enter = 1]: ").strip() or "1"
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    if choice == "3":
        return oauth_login()
    if choice == "2":
        return paste_headers_auth()
    if choice != "1":
        print("  ✗ not a valid choice")
        return False

    found = detected_browsers()
    order = found + [b for b in ("firefox", "chrome", "chromium", "brave",
                                 "edge", "vivaldi", "opera")
                     if b not in found]
    if sys.platform == "darwin":
        print()
        print("  (Safari isn't supported — its cookie store is sealed off;")
        print("   use any of these you're logged in with)")
    print()
    for i, b in enumerate(order, 1):
        mark = "  ← found on this machine" if b in found else ""
        print(f"    {i}) {b}{mark}")
    print()
    try:
        pick = input(f"  browser [1-{len(order)}, enter = 1]: ").strip() or "1"
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    if not pick.isdigit() or not 1 <= int(pick) <= len(order):
        print("  ✗ not a valid choice")
        return False
    browser = order[int(pick) - 1]
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


def auth_present():
    """Any saved sign-in — browser cookies or an OAuth token."""
    return os.path.isfile(AUTH_FILE) or (
        os.path.isfile(OAUTH_FILE) and os.path.isfile(OAUTH_CLIENT))


def make_ytmusic():
    """Build the right YTMusic client for whatever auth is on disk.
    OAuth wins when both exist — it's scoped and self-refreshing."""
    from ytmusicapi import YTMusic
    if os.path.isfile(OAUTH_FILE) and os.path.isfile(OAUTH_CLIENT):
        from ytmusicapi import OAuthCredentials
        with open(OAUTH_CLIENT) as f:
            cl = json.load(f)
        return YTMusic(OAUTH_FILE, oauth_credentials=OAuthCredentials(
            cl["client_id"], cl["client_secret"]))
    if os.path.isfile(AUTH_FILE):
        return YTMusic(AUTH_FILE)
    return YTMusic()


def verify_auth():
    try:
        yt = make_ytmusic()
        yt.get_library_playlists(limit=1)
        return True
    except Exception:
        return False


def oauth_login():
    """Device-flow OAuth: a token scoped to YouTube only — no browser
    cookies, revocable from your Google account's security page."""
    print()
    print("  ── Google OAuth sign-in (no browser cookies) ────────")
    print()
    print("  One-time setup — Google requires your own (free) client:")
    print("   1. console.cloud.google.com → create a project")
    print("   2. APIs & Services → enable 'YouTube Data API v3'")
    print("   3. OAuth consent screen → External → add yourself as a")
    print("      test user")
    print("   4. Credentials → Create OAuth client ID →")
    print("      type: 'TVs and Limited Input devices'")
    print()
    try:
        cid = input("  client id: ").strip()
        csec = input("  client secret: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    if not cid or not csec:
        print("  ✗ need both")
        return False
    try:
        from ytmusicapi import setup_oauth
        os.makedirs(CONFIG_DIR, exist_ok=True)
        setup_oauth(cid, csec, filepath=OAUTH_FILE, open_browser=False)
        with open(OAUTH_CLIENT, "w") as f:
            json.dump({"client_id": cid, "client_secret": csec}, f)
        os.chmod(OAUTH_FILE, 0o600)
        os.chmod(OAUTH_CLIENT, 0o600)
    except Exception as e:
        print(f"  ✗ oauth flow failed: {e}")
        return False
    if verify_auth():
        print("  ✓ signed in — token saved to", OAUTH_FILE)
        print("    revoke any time: myaccount.google.com → Security →")
        print("    Third-party apps & services")
        return True
    print("  ✗ token saved but the library check failed")
    return False


# ──────────────────────────────────────────────────────────────────────────────
# the app
# ──────────────────────────────────────────────────────────────────────────────

TABS = ["Search", "Library", "Playlists", "Queue"]
BLOCKS = " ▁▂▃▄▅▆▇█"
VIZ_STYLES = ["bars", "mirror", "scope", "bands", "drop", "cover"]

# the blackspace family — calm interference waves glowing out of darkness,
# cheap to compute (a few sines, no log/atan stacks); they join the app's
# normal preset pool
WAVE_PRESETS = list(range(36, 42))
# what the eww desktop widget rotates through: a curated mix that reads
# well as chunky pixels at 5fps — hexagons, fractal plasma, raindrop
# ripples, kaleidoscope, chevrons, a spinning box, the two best
# blackspace waves, and a smiley that winks at you
WIDGET_PRESETS = [3, 24, 25, 29, 30, 34, 36, 39, 42]
BADGE = ["█▙  ", "███▙", "█▛  "]
APP_NAME = "NOCTURNE"
LOGO = [
    "█▖ █ ▗█▀█▖ ▗█▀▀ ▀▀█▀▀ █  █ █▀▀▙ █▖ █ █▀▀▀",
    "█▚ █ ▐▌ ▐▌ ▐▌     █   █  █ █▄▄▛ █▚ █ █▀▀ ",
    "█ ▚█ ▝█▄█▘ ▝█▄▄   █   ▜▄▄▛ █ ▝▙ █ ▚█ █▄▄▄",
]
# the sign: NOCTURNE set in JetBrains Mono Bold Italic, rasterized
# to half-block pixels — solid letterforms that stay legible over
# any wallpaper/shader, unlike line-art figlet strokes
BIG_BITS = [
    "  ##    #      ####        ###     ########    ##   ##    #####      ###   #     #######",
    "  ###   #     ######     ######    ########    #    ##    #######    ###   #     #######",
    "  ###   #    ##   ##     ##  ##       ##       #    ##    ##   ##    ###   #     #",
    "  ###  ##    ##   ##    ##    #       ##       #    ##    #    ##    ###   #    ##",
    "  # #  ##    #    ##    ##            ##      ##    ##    #    ##    # #  ##    ##",
    " ## #  ##    #    ##    ##            #       ##    #     #    ##    # #  ##    ##",
    " ## #  ##   ##    ##    #             #       ##    #    ### ###    ## ## ##    ######",
    " ## ## #    ##    ##    #             #       ##   ##    ######     ## ## ##    ######",
    " ## ## #    ##    #    ##            ##       #    ##    ##  ##     ##  # #     #",
    " #   # #    ##    #    ##            ##       #    ##    ##  ##     ##  # #     #",
    " #   ###    ##   ##    ##    #       ##       #    ##    #   ##     #   ###    ##",
    " #   ###    ##   ##    ##   ##       ##       ##  ##     #    #     #   ###    ##",
    "##   ###    ######      ######       ##       ######     #    ##    #   ###    #######",
    "##   ##      ####        ###         #         ###      ##    ##    #    ##    #######",
]
_LOGO_CACHE = {}


def fancy_logo():
    """The NOCTURNE wordmark: a real italic typeface as half-block
    pixels — diagonal gradient, a lit bevel along each letter's top
    edge. Rebuilt per theme, cached after."""
    key = (RED, ORANGE, PINK)
    if key in _LOGO_CACHE:
        return _LOGO_CACHE[key]
    H2 = len(BIG_BITS)
    Wc = max(len(r) for r in BIG_BITS)
    g = [[1 if x < len(r) and r[x] == "#" else 0 for x in range(Wc)]
         for r in BIG_BITS]
    out = []
    for r in range(H2 // 2):
        seg = []
        for x in range(Wc):
            t, b = g[2 * r][x], g[2 * r + 1][x]
            if not t and not b:
                seg.append(" ")
                continue
            gx = x / (Wc - 1)

            def col(y, lit):
                c = lerp(lerp(RED, PINK, y / (H2 - 1)),
                         lerp(ORANGE, RED, y / (H2 - 1)), gx)
                return lerp(c, WHITE, 0.40) if lit else c
            lit_t = t and (r == 0 or not g[2 * r - 1][x])
            if t and b:
                seg.append(fg(col(2 * r, lit_t)) + bg(col(2 * r + 1, False))
                           + "▀" + RESET)
            elif t:
                seg.append(fg(col(2 * r, lit_t)) + "▀" + RESET)
            else:
                seg.append(fg(col(2 * r + 1, False)) + "▄" + RESET)
        out.append("".join(seg))
    _LOGO_CACHE.clear()           # one theme at a time is plenty
    _LOGO_CACHE[key] = out
    return out


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


# ──────────────────────────────────────────────────────────────────────────────
# blackspace — the hidden floor (type "bhop" in the search bar)
# ──────────────────────────────────────────────────────────────────────────────

WOLF_BEST = os.path.join(CONFIG_DIR, "wolf.json")


class Blackspace:
    """A bhop parkour runner wearing the drop visualizer's skin.

    Easter egg, on purpose: typing "bhop" into the search bar
    drops you in (nothing in the help screen mentions it). An endless
    corridor over the void — the floor develops gaps, chained hops
    build speed, and the like-splash heart rides at the bottom of the
    screen burning hotter the faster you fly. Music keeps playing: the
    same groove signals that drive the drop viz surge the world here.
    Esc wakes you up, back in the player where you left it.

    Deliberately self-contained: it reads the App's audio drive signals
    and nothing else, and only _wolf_start / handle_key / render / run
    know it exists. Looking dead from the feature list is the point —
    it is not dead code."""

    HOLD = 0.24          # key-held window refreshed by terminal autorepeat

    def __init__(self, deep=False):
        import numpy as np
        self.np = np
        # deep = running in a dedicated shader-free window (ytm --wolf):
        # true blacks are safe there, so the palette goes all the way down
        self.deep = deep
        self.best = 0
        try:
            with open(WOLF_BEST) as f:
                self.best = int(json.load(f).get("best", 0))
        except Exception:
            pass
        self.lut = None
        self._reset()

    def _reset(self):
        self.cells = {}          # x → (lo, hi, gap): one corridor slice
        self._c, self._hw = 12.0, 2
        self._gen_to = -1
        self._gap_left = 0
        self._safe = 14          # the first stretch has no gaps
        self.px, self.py, self.ang = 1.5, 12.5, 0.0
        self.vx = self.vy = 0.0
        self.z = self.vz = 0.0
        self.grounded = True
        self.airtime = self.ground_t = 0.0
        self.chain = 0
        self.dead = False
        self.death_t = 0.0
        self.held = {}
        self.tt = 0.0            # floor time — rides the bass
        self.wtt = 0.0           # wall time — rides the mids
        self.pitch = 0.0         # mouse: vertical look
        self.mturn = 0.0         # mouse: edge-assist turn rate
        self._mlast = None       # last mouse pixel position
        self.audio_live = False  # is the tap actually hearing music
        self.pulse = self.pulse_s = self.heat = 0.0
        self.recoil = 0.0
        self.shocks = []         # landing shockwaves rippling the floor
        self.bob_t = self.bob_amt = 0.0
        self.score = 0
        self.hearts = []         # pickups floating over the track
        self.msg_text, self.msg_t = "", 0.0
        self.fcur, self.fnxt = self._roll(), self._roll()
        self.morph_t, self.hold_t = 99.0, random.uniform(12, 24)
        self.zbuf = None
        self.lut = None
        self._cam = (1.0, 0.0, 0.0, 0.66)
        self.last = time.perf_counter()
        self._gen(44)
        self._say("the void hums below. aim with the mouse — hold "
                  "space (or the button) to bhop.")

    # ── tiny helpers ─────────────────────────────────────────────────────
    def _say(self, t, dur=4.5):
        self.msg_text, self.msg_t = t, dur

    def _gen(self, upto):
        # endless corridor marching +x: the center wanders, the width
        # breathes, and past the safe stretch the floor starts missing.
        # gap length scales with distance — the run teaches the chain.
        while self._gen_to < upto:
            x = self._gen_to = self._gen_to + 1
            if x % 3 == 0:
                self._c = min(20.0, max(4.0,
                                        self._c + random.choice((-1, 0, 0, 1))))
            if x % 7 == 0:
                self._hw = random.choice((1, 2, 2, 3))
            gap = False
            if x > self._safe:
                if self._gap_left > 0:
                    self._gap_left -= 1
                    gap = True
                elif random.random() < min(0.16, 0.05 + x / 2500):
                    self._gap_left = random.randint(0, min(3, 1 + x // 150))
                    gap = True
            lo, hi = int(self._c) - self._hw, int(self._c) + self._hw
            self.cells[x] = (lo, hi, gap)
            if not gap and x > 6 and random.random() < 0.12:
                self.hearts.append({
                    "x": x + 0.5, "y": random.uniform(lo + 0.6, hi + 0.4),
                    "phase": random.uniform(0, 6.28), "got": False})
        # the void eats the path behind you — no backtracking
        for x in [k for k in self.cells if k < self.px - 14]:
            del self.cells[x]
        self.hearts = [h for h in self.hearts
                       if h["x"] > self.px - 6 and not h["got"]]

    def _cell(self, x, y):
        c = self.cells.get(int(x))
        if c is None:
            return 1
        return 0 if c[0] <= int(y) <= c[1] else 1

    def _floor(self, x, y):
        c = self.cells.get(int(x))
        return bool(c) and c[0] <= int(y) <= c[1] and not c[2]

    def _solid_area(self, x, y, r):
        return (self._cell(x - r, y - r) or self._cell(x + r, y - r) or
                self._cell(x - r, y + r) or self._cell(x + r, y + r))

    def _roll(self):
        # floor params: caustic-beat wavenumber offset, origin knocked
        # off-center 45% of the time
        off = (lambda: random.uniform(-5, 5) if random.random() < 0.45
               else 0.0)
        k1 = random.uniform(2.2, 3.2)
        return {"ox": 12 + off(), "oy": 12 + off(),
                "r1": random.uniform(3, 5.5), "r2": random.uniform(2, 4.5),
                "o1": random.uniform(0.17, 0.26),
                "o2": random.uniform(0.15, 0.25),
                "k1": k1, "k2": k1 + 0.6,
                "a1": random.uniform(2.5, 2.9), "a2": random.uniform(1.5, 2.0),
                "bias": -random.uniform(1.3, 1.8)}

    def _eff(self):
        if self.morph_t >= 2.5:
            return self.fcur
        m = self.morph_t / 2.5
        m = m * m * (3 - 2 * m)
        return {k: self.fcur[k] + (self.fnxt[k] - self.fcur[k]) * m
                for k in self.fcur}

    # ── input ────────────────────────────────────────────────────────────
    def key(self, k):
        now = time.time()
        if k in ("w", "a", "s", "d", "UP", "DOWN", "LEFT", "RIGHT"):
            self.held[k] = now + self.HOLD
        elif k == " ":
            self.held["jump"] = now + 0.22       # held space = auto-bhop
        elif k in ("r", "R"):
            self._reset()                        # instant run restart

    def mouse(self, x, y, fx, fy, pressed):
        # csgo-style raw aim: pixel deltas drive the yaw 1:1 — sharp,
        # to the hand. no pointer lock in a tty, so when the cursor pins
        # in the outer edge band a steady assist turn keeps you going.
        if self._mlast is not None:
            dx = x - self._mlast[0]
            dy = y - self._mlast[1]
            if abs(dx) < 500 and abs(dy) < 500:    # teleports aren't aim
                self.ang += dx * 0.0040
                self.pitch = max(-0.8, min(0.8, self.pitch - dy * 0.0033))
        self._mlast = (x, y)
        if fx < 0.03:
            self.mturn = -1.0
        elif fx > 0.97:
            self.mturn = 1.0
        else:
            self.mturn = 0.0
        if pressed:
            self.held["jump"] = time.time() + 0.22

    def _held(self, k):
        return self.held.get(k, 0.0) > time.time()

    # ── simulation ───────────────────────────────────────────────────────
    def _airturn(self, a):
        # the strafe-sync: turning WITH the matching strafe key carries
        # the velocity around the corner and feeds it a little
        c, s = math.cos(a), math.sin(a)
        vx = self.vx * c - self.vy * s
        vy = self.vx * s + self.vy * c
        self.vx, self.vy = vx * 1.0015, vy * 1.0015

    def _die(self):
        self.dead, self.death_t = True, 0.0
        dist = int(self.px - 1.5)
        if dist > self.best:
            self.best = dist
            try:
                with open(WOLF_BEST, "w") as f:
                    json.dump({"best": self.best}, f)
            except Exception:
                pass
            self._say(f"a new horizon — {dist}m. run again.   [r]", 9999)
        else:
            self._say("the void caught you. "
                      "you cannot give up just yet.   [r]", 9999)

    def _update(self, dt):
        self.recoil *= math.exp(-dt * 9)
        if self.msg_t > 0:
            self.msg_t -= dt
        if self.morph_t < 2.5:
            self.morph_t += dt
            if self.morph_t >= 2.5:
                self.fcur, self.hold_t = self.fnxt, random.uniform(12, 24)
        else:
            self.hold_t -= dt
            if self.hold_t <= 0:
                self.fnxt, self.morph_t = self._roll(), 0.0

        if self.dead:
            self.death_t += dt
            return

        # turning — arrows or the mouse stick; in the air with the
        # matching strafe held, the velocity vector turns too (that's
        # the whole sport)
        turn = 0.0
        if self._held("LEFT"):
            turn -= 2.9
        if self._held("RIGHT"):
            turn += 2.9
        turn += self.mturn * 3.2                     # edge assist
        if turn:
            self.ang += turn * dt
            if not self.grounded:
                if turn < 0 and self._held("a"):
                    self._airturn(turn * 0.76 * dt)
                elif turn > 0 and self._held("d"):
                    self._airturn(turn * 0.76 * dt)

        ca, sa = math.cos(self.ang), math.sin(self.ang)
        wx = wy = 0.0
        if self._held("w") or self._held("UP"):
            wx += ca; wy += sa
        if self._held("s") or self._held("DOWN"):
            wx -= ca; wy -= sa
        if self._held("a"):
            wx += sa; wy -= ca
        if self._held("d"):
            wx -= sa; wy += ca
        wl = math.hypot(wx, wy)
        if wl:
            wx, wy = wx / wl, wy / wl
        speed = math.hypot(self.vx, self.vy)

        if self.grounded:
            self.ground_t += dt
            if wl:
                # ground accel caps at run speed — the chain carries more
                cap = max(4.2, speed)
                self.vx += wx * 16 * dt
                self.vy += wy * 16 * dt
                s2 = math.hypot(self.vx, self.vy)
                if s2 > cap:
                    self.vx *= cap / s2
                    self.vy *= cap / s2
            else:
                f = math.exp(-dt * 7)
                self.vx *= f
                self.vy *= f
        else:
            self.airtime += dt
            if wl:
                self.vx += wx * 2.2 * dt
                self.vy += wy * 2.2 * dt

        # the hop, and the chain: re-jump within a tenth of landing and
        # the speed compounds; dawdle and it's just a jump again
        if self.grounded and self._held("jump"):
            if self.ground_t <= 0.10 and self.airtime > 0.15:
                self.chain += 1
                self.shocks = (self.shocks
                               + [[self.px, self.py, 0.0]])[-4:]
                boost = 1.05 + 0.02 * min(6, self.chain)
                s2 = min(11.5, speed * boost)
                if speed > 0.5:
                    n = s2 / speed
                    self.vx *= n
                    self.vy *= n
                self.pulse = min(1.0, self.pulse + 0.45)
                if self.chain == 3:
                    self._say("the soul skips like a stone.")
                elif self.chain == 6:
                    self._say("momentum is a kind of mercy.")
                elif self.chain == 10:
                    self._say("you are the wind now.")
            elif self.ground_t > 0.10:
                self.chain = 0
            self.vz = 4.7
            self.grounded = False
            self.airtime = self.ground_t = 0.0

        # move with wall sliding
        r = 0.25
        nx, ny = self.px + self.vx * dt, self.py + self.vy * dt
        if not self._solid_area(nx, self.py, r):
            self.px = nx
        else:
            self.vx *= -0.1
        if not self._solid_area(self.px, ny, r):
            self.py = ny
        else:
            self.vy *= -0.1

        # vertical: gravity, landings, and the void
        if self.grounded:
            if not self._floor(self.px, self.py):
                self.grounded = False        # ran off an edge
                self.airtime = self.vz = 0.0
        else:
            self.vz -= 12.5 * dt
            self.z += self.vz * dt
            if self.z <= 0 and self.vz <= 0:
                # landing only counts from just above the surface — once
                # you're properly below it, the floor is a ceiling and
                # the void has you. (the flip side: enough speed skims
                # short gaps without jumping at all.)
                if self.z > -0.25 and self._floor(self.px, self.py):
                    land = -self.vz
                    self.shocks = (self.shocks
                                   + [[self.px, self.py, 0.0]])[-4:]
                    self.z = self.vz = 0.0
                    self.grounded = True
                    self.ground_t = 0.0
                    self.recoil = min(1.5, 0.2 + land * 0.1)
                elif self.z < -2.5:
                    self._die()

        for s in self.shocks:
            s[2] += dt
        self.shocks = [s for s in self.shocks if s[2] < 1.1]

        # speed is energy — the world wakes up when you fly
        sp = math.hypot(self.vx, self.vy)
        self.heat = min(1.0, max(self.heat, sp / 11.5))
        if self.grounded and sp > 0.5:
            self.bob_t += dt * (4 + sp * 1.6)
            self.bob_amt = min(1.0, self.bob_amt + dt * 6)
        else:
            self.bob_amt = max(0.0, self.bob_amt - dt * 4)

        # pickups
        for hp_ in self.hearts:
            if (not hp_["got"] and abs(hp_["x"] - self.px) < 0.55
                    and abs(hp_["y"] - self.py) < 0.55 and self.z < 1.2):
                hp_["got"] = True
                self.score += 1
                self.pulse = 1.0
                self.heat = min(1.0, self.heat + 0.3)
        self._gen(int(self.px) + 42)

    # ── field math (the blackspace plane-wave family — no atan2) ─────────
    def _wall_raw(self, wt, u, vy, t):
        np = self.np
        if wt == 1:          # ember curtain
            return (2.7 * np.sin(u * 2.6 + np.sin(vy * 1.4 + 0.5 * t) * 2.2)
                    + 1.7 * np.sin(vy * 2.2 - 0.7 * t) - 1.5)
        if wt == 2:          # deep swell
            return (2.6 * np.sin(u * 2.9 - 0.55 * t + 2 * math.sin(0.2 * t))
                    + 1.6 * np.sin(u * 1.05 + vy * 0.7 + 0.4 * t)
                    + 0.8 * np.sin(vy * 2.8 + 0.31 * t) - 1.7)
        return (2.8 * np.sin((0.8 * u + 0.55 * vy) * 1.6 - 0.42 * t)
                + 1.8 * np.sin((0.8 * u - 0.6 * vy) * 1.35 + 0.36 * t) - 1.5)

    def _ensure_lut(self):
        # 64 entries, post curve (contrast → gamma → cosine S) baked in,
        # built from the live theme so matrix gets a green canyon.
        # deep (own shader-free window): the true blackspace anchor.
        # in-place fallback: a lifted slate — luminance-keyed terminal
        # shaders (scifi-space.glsl) replace dark pixels with a starfield,
        # and ghostty feeds them LINEAR-space colors, so anything below
        # ~42% sRGB grey is "background" to them. the slate keeps the
        # in-place mode at least partially solid; the dedicated window is
        # the real fix. the void ducks under every cutoff on purpose
        # (see frame()) — the track floats in space.
        if self.lut is not None:
            return
        dark = (8, 8, 14) if self.deep else (40, 44, 62)
        pos = [0.0, 0.34, 0.60, 0.80, 0.94, 1.0]

        def ramp(stops):
            out = []
            for i in range(64):
                v = i / 63
                v = min(1.0, max(0.0, (v - 0.5) * 1.25 + 0.5)) ** 1.2
                v = 0.5 - 0.5 * math.cos(v * math.pi)
                a = max(j for j in range(len(pos) - 1) if pos[j] <= v)
                f = (v - pos[a]) / (pos[a + 1] - pos[a])
                out.append(lerp(stops[a], stops[a + 1], f))
            return out

        # four canvases: the floor wears the theme; each wall family gets
        # its own hue so surfaces read against the pure-black void
        violet = (122, 62, 255)
        teal = (0, 205, 185)
        lut = ramp([dark, lerp(dark, RED, 0.45), RED,
                    ORANGE, PINK, lerp(PINK, WHITE, 0.55)])          # floor
        lut += ramp([dark, lerp(dark, RED, 0.5), RED, ORANGE,
                     lerp(ORANGE, WHITE, 0.3), lerp(ORANGE, WHITE, 0.6)])
        lut += ramp([dark, lerp(dark, violet, 0.5), violet, PINK,
                     lerp(PINK, WHITE, 0.3), lerp(PINK, WHITE, 0.6)])
        lut += ramp([dark, lerp(dark, teal, 0.5), teal, (140, 255, 230),
                     lerp(teal, WHITE, 0.5), lerp(teal, WHITE, 0.75)])
        self.lut = lut
        self._fgs = [f"\x1b[38;2;{c[0]};{c[1]};{c[2]}m" for c in lut]
        self._bgs = [f"\x1b[48;2;{c[0]};{c[1]};{c[2]}m" for c in lut]

    # ── the heart: silhouette math stamped into the index grid ──────────
    def _draw_heart(self, idx, W, H, cx, cy, scale, o, depth, ex):
        np = self.np
        x0 = max(0, int(cx - 1.45 * scale))
        x1 = min(W - 1, int(cx + 1.45 * scale))
        ytop = 1.2 + 0.95 * o.get("boost", 1.0) if o.get("flame") else 1.2
        y0 = max(0, int(cy - ytop * scale))
        y1 = min(H - 1, int(cy + 1.12 * scale))
        if x1 <= x0 or y1 <= y0:
            return
        hx = (np.arange(x0, x1 + 1) - cx) / scale
        hy = (cy - np.arange(y0, y1 + 1)) / scale
        x, y = np.meshgrid(hx, hy)
        ok = np.ones(x.shape, bool)
        if depth > 0 and self.zbuf is not None:
            ok &= (self.zbuf[x0:x1 + 1] > depth)[None, :]
        rate, desat = 9.0, 0.0
        if o.get("dying"):
            dT = o["dt"]
            rate = 34.0                                   # panic flutter
            split = max(0.0, dT - 0.3) * 0.55
            y = y + max(0.0, dT - 0.3) ** 2 * 1.1         # halves sink
            desat = min(1.0, max(0.0, (dT - 0.2))) * 0.85
            c = 0.07 * np.sin(y * 8.5 + o["seed"])        # jagged crack
            ok &= np.abs(x - c) >= 0.045 + split
            x = x - np.sign(x - c) * split
        ax = np.abs(x)
        dxc, dyc = ax - 0.58, y - 0.52
        # two circles + a straight-edged wedge tapering to a tip at y=-1
        inside = ok & ((dxc * dxc + dyc * dyc < 0.39) |
                       ((y <= 0.52) & (y >= -1.0) &
                        (ax <= 1.2 * (y + 1.0) / 1.52)))
        pl = 0.5 + 0.5 * math.sin(rate * self.tt + o.get("phase", 0.0))
        col = lerp(RED, PINK, pl)
        if desat > 0:
            g = sum(col) // 3
            col = lerp(col, (g, g, g), desat)
        sub = idx[y0:y1 + 1, x0:x1 + 1]
        sub[inside] = ex(col)
        if not o.get("flame") or (o.get("dying") and o["dt"] > 0.35):
            return
        # fire in the complement, off the crown
        crown = 0.52 + np.sqrt(np.maximum(0.0, 0.39 - dxc * dxc))
        rel = y - crown
        fl = ok & ~inside & (ax < 1.18) & (y >= 0.52) & (rel > 0)
        t = self.tt
        ph = o.get("phase", 0.0)
        n1 = np.sin(x * 7.3 + t * 9.1 + ph)
        n2 = np.sin(x * 3.1 - t * 6.7)
        n3 = np.sin(x * 11 + t * 13 + ph)
        tongue = (np.maximum(0.0, n1 * n2 * 0.7 + n3 * 0.3)
                  * 0.85 * o.get("boost", 1.0))
        comp = (255 - col[0], 255 - col[1], 255 - col[2])
        core = lerp(comp, (255, 255, 255), 0.55)
        burn = fl & (rel < tongue)
        burn &= ~((rel > tongue * 0.78) &
                  (np.sin(t * 21 + x * 5) < 0))           # tips blink off
        sub[burn] = ex(comp)
        sub[burn & (rel < tongue * 0.45)] = ex(core)
        sparks = (fl & (n3 > 0.9) & (rel >= tongue) & (rel < tongue + 0.3) &
                  (np.abs(np.sin(x * 43 + t * 27)) > 0.93))
        sub[sparks] = ex(comp)                            # detached sparks

    # ── one frame: returns h terminal lines, last one is the dialogue ───
    def frame(self, w, h, app=None):
        np = self.np
        now = time.perf_counter()
        dt = min(0.05, now - self.last)
        self.last = now

        # drink the same groove signals as the drop viz — the world
        # surges on the music's kicks on top of the gameplay pulses
        mpulse = en = tre = eb = em = 0.0
        if app is not None:
            try:
                if (bool(app.player.props.get("pause"))
                        or app.player.loading):
                    raw = (0.0, 0.0, 0.0)
                elif app.tap and app.tap.producing:
                    lv = app.tap.levels(18)
                    raw = (sum(lv[:5]) / 5, sum(lv[5:12]) / 7,
                           sum(lv[12:]) / 6)
                else:
                    raw = (0.0, 0.0, 0.0)
                for i, v in enumerate(raw):
                    e = app._drop_e[i]
                    kk = min(1.0, (10.0 if v > e else 2.4) * dt)
                    app._drop_e[i] = e + (v - e) * kk
                mpulse, en, eb, em, _ = app._drop_groove(
                    raw, dt, time.time())
                tre = getattr(app, "_tre_pulse", 0.0)
                self.audio_live = bool(app.tap and app.tap.producing)
            except Exception:
                pass

        # motion rules: eased pulse attack, energy bed, musical time
        self.pulse *= math.exp(-dt * 3.5)
        p = min(1.2, max(self.pulse, mpulse))
        self.pulse_s += (p - self.pulse_s) * min(1.0, dt * 16)
        self.heat = min(1.0, self.heat * math.exp(-dt * 0.45))
        energy = min(1.0, 0.15 + max(self.heat, en * 0.8))
        # multiband canvas: the floor breathes with the bass, the walls
        # flow with the mids — two clocks, one song
        self.tt += dt * (0.55 + 1.1 * energy + 1.6 * self.pulse_s
                         + 1.2 * min(1.0, eb))
        self.wtt += dt * (0.6 + 2.2 * min(1.0, em) + 0.8 * self.pulse_s)
        self._update(dt)

        W, rows = w, h - 1
        H = rows * 2
        self._ensure_lut()
        idx = np.zeros((H, W), np.int16)
        extras, emap = [], {}

        def ex(c):
            c = (int(c[0]), int(c[1]), int(c[2]))
            i = emap.get(c)
            if i is None:
                i = 256 + len(extras)
                emap[c] = i
                extras.append(c)
            return i

        # camera: FOV swells on the pulse AND stretches with speed;
        # the eye rides z, so hops lift the whole world
        spd = math.hypot(self.vx, self.vy)
        aspect = min(1.6, max(0.5, (W / max(H, 1)) / 1.78))
        plane = 0.66 * (1 + 0.10 * self.pulse_s + 0.018 * spd) * aspect
        dirx, diry = math.cos(self.ang), math.sin(self.ang)
        plx, ply = -diry * plane, dirx * plane
        self._cam = (dirx, diry, plx, ply)
        horizon = int(H / 2 + self.pitch * H * 0.5
                      + math.sin(self.bob_t) * 2.2 * self.bob_amt
                      - self.recoil * 4)
        horizon = min(H - 10, max(8, horizon))
        bright = 1.0 + 0.45 * self.pulse_s + 0.25 * tre
        eyez = 0.55 + self.z * 0.45

        # the sky: true void, deliberately BELOW the luminance cutoff —
        # on a shader-backed terminal it becomes its starfield, on
        # anything else it reads near-black. either way: blackspace.
        void_ix = ex((8, 8, 14))
        idx[:] = void_ix
        # sparse stars parallax the SKY (the pits get none — that black
        # is the one that kills you)
        star_ix = ex((96, 104, 142))
        star_hi = ex((150, 158, 196))
        for sx_ in range(0, W, 2):
            a = (self.ang + (2 * sx_ / W - 1) * plane) % 6.28318
            q = int(a * 60)
            h1 = math.sin(q * 12.9898) * 43758.5453
            f1 = h1 - math.floor(h1)
            if f1 < 0.30:
                h2 = math.sin(q * 78.233) * 24634.6345
                f2 = h2 - math.floor(h2)
                ry_ = int(horizon * (0.1 + 0.8 * f2))
                if 0 <= ry_ < horizon:
                    idx[ry_, sx_] = star_hi if f1 < 0.08 else star_ix

        # visible corridor slices as arrays for the vectorized floor
        x0w = int(self.px) - 14
        n = max(1, self._gen_to - x0w + 1)
        loA = np.full(n, 99, np.int32)
        hiA = np.full(n, -99, np.int32)
        gpA = np.ones(n, bool)
        for xx in range(x0w, self._gen_to + 1):
            c = self.cells.get(xx)
            if c:
                loA[xx - x0w], hiA[xx - x0w], gpA[xx - x0w] = c
        # which cells border a gap — the void announces itself
        nx_gap = np.concatenate([gpA[1:], [True]])
        pv_gap = np.concatenate([[True], gpA[:-1]])

        # floor: twin orbiting ripple sources; gap cells fall to void
        if eyez > 0.05:
            P = self._eff()
            t = self.tt
            s1x = P["ox"] + P["r1"] * math.sin(P["o1"] * t)
            s1y = P["oy"] + P["r1"] * math.cos(P["o1"] * t * 0.77)
            s2x = P["ox"] + P["r2"] * math.sin(-P["o2"] * t + 2.1)
            s2y = P["oy"] + P["r2"] * math.cos(P["o2"] * t * 0.6 + 1.0)
            ys = np.arange(horizon + 1, H, dtype=np.float32)
            if len(ys):
                rowd = (eyez * H) / (ys - horizon)
                xs = np.arange(W, dtype=np.float32) / W
                rdx0, rdy0 = dirx - plx, diry - ply
                rdx1, rdy1 = dirx + plx, diry + ply
                fx = self.px + rowd[:, None] * (rdx0 + (rdx1 - rdx0)
                                                * xs[None, :])
                fy = self.py + rowd[:, None] * (rdy0 + (rdy1 - rdy0)
                                                * xs[None, :])
                d1 = np.sqrt((fx - s1x) ** 2 + (fy - s1y) ** 2)
                d2 = np.sqrt((fx - s2x) ** 2 + (fy - s2y) ** 2)
                af = 1.0 + 0.85 * min(1.0, eb) + 0.3 * self.pulse_s
                raw = (P["a1"] * af * np.sin(d1 * P["k1"] - 1.15 * t)
                       + P["a2"] * af * np.sin(d2 * P["k2"] + 0.85 * t)
                       + P["bias"])
                # every landing slams a ring of light into the floor
                for sx_, sy_, age in self.shocks:
                    ds = np.sqrt((fx - sx_) ** 2 + (fy - sy_) ** 2)
                    ring = ds - (0.6 + 5.5 * age)
                    raw += (2.2 * (1.0 - age / 1.1)
                            * np.exp(-ring * ring * 3.2))
                # gap edges glow hot before the drop
                xig = np.clip(fx.astype(np.int32) - x0w, 0, n - 1)
                frac = fx - np.floor(fx)
                throb = 0.7 + 0.3 * math.sin(self.tt * 3.1)
                raw += 2.4 * throb * (nx_gap[xig] * frac ** 3
                                      + pv_gap[xig] * (1.0 - frac) ** 3)
                vn = np.clip(0.5 + raw / 5.5, 0.0, 1.0)
                fogf = bright / (1 + 0.06 * rowd * rowd)
                fi = np.clip(vn * 63 * fogf[:, None], 0, 63).astype(np.int16)
                xi = np.clip(fx.astype(np.int32) - x0w, 0, n - 1)
                yi = fy.astype(np.int32)
                has = ((~gpA[xi]) & (yi >= loA[xi]) & (yi <= hiA[xi])
                       & (fx >= x0w))
                idx[horizon + 1:] = np.where(has, fi, void_ix)

        # walls: per-column DDA (python), per-pixel field (numpy)
        perp = np.empty(W, np.float32)
        uarr = np.empty(W, np.float32)
        sides = np.empty(W, np.int8)
        wts = np.empty(W, np.int8)
        cells = self.cells
        for cx in range(W):
            camx = 2 * cx / W - 1
            rx, ry = dirx + plx * camx, diry + ply * camx
            mx, my = int(self.px), int(self.py)
            ddx = abs(1 / rx) if rx else 1e30
            ddy = abs(1 / ry) if ry else 1e30
            stx, sdx = ((-1, (self.px - mx) * ddx) if rx < 0
                        else (1, (mx + 1 - self.px) * ddx))
            sty, sdy = ((-1, (self.py - my) * ddy) if ry < 0
                        else (1, (my + 1 - self.py) * ddy))
            side, wt = 0, 1
            for _ in range(96):
                if sdx < sdy:
                    sdx += ddx
                    mx += stx
                    side = 0
                else:
                    sdy += ddy
                    my += sty
                    side = 1
                cc = cells.get(mx)
                if cc is None or not (cc[0] <= my <= cc[1]):
                    wt = 1 + (mx // 40) % 3   # the zone picks the field
                    break
            d = max(0.05, (sdx - ddx) if side == 0 else (sdy - ddy))
            perp[cx] = d
            sides[cx] = side
            wts[cx] = wt
            uarr[cx] = (self.py + d * ry) if side == 0 else (self.px + d * rx)
        self.zbuf = perp

        Hp = H / perp
        wall_top = horizon - (1.0 - eyez) * Hp
        Y = np.arange(H, dtype=np.float32)[:, None]
        vv = (Y - wall_top[None, :]) / Hp[None, :]
        wmask = (vv >= 0) & (vv < 1)
        vy = (vv - 0.5) * 3
        fog = (bright * (1 + 0.35 * min(1.0, em))
               / (1 + 0.035 * perp * perp)
               * np.where(sides == 1, 0.82, 1.0))
        rawW = np.zeros((H, W), np.float32)
        UU = np.broadcast_to(uarr[None, :], (H, W))
        base = np.zeros(W, np.int16)
        for wt_ in (1, 2, 3):
            m = wmask & (wts[None, :] == wt_)
            if m.any():
                rawW[m] = self._wall_raw(wt_, UU[m], vy[m], self.wtt)
            base[wts == wt_] = wt_ * 64
        vnW = np.clip(0.5 + rawW / 5.5, 0.0, 1.0)
        wi = np.clip(vnW * 63 * np.broadcast_to(fog[None, :], (H, W)),
                     0, 63).astype(np.int16)
        # walls keep a dim tint even at full fog — only the void is void
        wi = np.maximum(wi, 12) + base[None, :]
        idx[wmask] = wi[wmask]
        # crest line: the wall announces its top edge against the sky
        cols = np.arange(W)
        rim = wall_top.astype(np.int32)
        ok_rim = (rim >= 0) & (rim < H - 1)
        idx[np.clip(rim, 0, H - 1)[ok_rim], cols[ok_rim]] = \
            base[ok_rim] + 52
        idx[np.clip(rim + 1, 0, H - 1)[ok_rim], cols[ok_rim]] = \
            base[ok_rim] + 34

        # pickup hearts, far to near, occluded per column by the zbuffer
        inv = 1.0 / (plx * diry - dirx * ply)
        for hp_ in sorted(self.hearts,
                          key=lambda e: -((e["x"] - self.px) ** 2 +
                                          (e["y"] - self.py) ** 2)):
            if hp_["got"]:
                continue
            rx, ry = hp_["x"] - self.px, hp_["y"] - self.py
            trx = inv * (diry * rx - dirx * ry)
            try_ = inv * (-ply * rx + plx * ry)
            if try_ < 0.15 or try_ > 26:
                continue
            scale = (0.30 * H / try_) / 2.14
            if scale < 1.0:
                continue
            sx = (W / 2) * (1 + trx / try_)
            bobz = 0.45 + 0.12 * math.sin(self.tt * 2 + hp_["phase"])
            cy = horizon - (bobz - eyez) * (H / try_)
            self._draw_heart(idx, W, H, sx, cy, scale,
                             {"phase": hp_["phase"], "seed": 0.0,
                              "flame": True}, try_, ex)

        # the soul rides the bottom of the screen and burns with speed
        sway = math.sin(self.bob_t * 0.5) * 4 * self.bob_amt
        self._draw_heart(idx, W, H, W / 2 + sway,
                         H - 8 + self.recoil * 7, max(8, H * 0.16),
                         {"dying": self.dead,
                          "dt": max(0.0, self.death_t),
                          "phase": 0.0, "seed": 1.7, "flame": True,
                          "boost": 0.8 + spd / 7.0}, -1, ex)

        # the chain, as a row of tiny hearts
        for i in range(min(10, self.chain)):
            self._draw_heart(idx, W, H, 8 + i * 9, 8, 3.2,
                             {"phase": float(i), "seed": float(i)}, -1, ex)

        # half-block cells: 64 cached palette strings + frame extras
        fgs = self._fgs + [f"\x1b[38;2;{r};{g_};{b}m" for r, g_, b in extras]
        bgs = self._bgs + [f"\x1b[48;2;{r};{g_};{b}m" for r, g_, b in extras]
        ti, bi = idx[0::2], idx[1::2]
        lines = []
        for rr in range(rows):
            tl, bl = ti[rr].tolist(), bi[rr].tolist()
            lines.append("".join(fgs[a] + bgs[b] + "▀"
                                 for a, b in zip(tl, bl)) + RESET)
        lines.append(self._status(w))
        return lines

    def _status(self, w):
        left = (BOLD + fg(WHITE) + "* " + self.msg_text + RESET
                if self.msg_t > 0 else "")
        spd = math.hypot(self.vx, self.vy)
        note = "♪ " if self.audio_live else "∅ "
        right = (fg(DGREY) + note
                 + f"{spd:4.1f} u/s · {max(0, int(self.px - 1.5))}m"
                 f" · ♥{self.score} · best {self.best}m · esc wakes up"
                 + RESET)
        gap = w - visible_len(left) - visible_len(right)
        if gap < 1:
            return crop_pad(left, w)
        return left + " " * gap + right


def wolf_main():
    """Hidden: ytm --wolf — the blackspace in its own window.

    Spawned by the search-bar easter egg into a shader-free, opaque
    ghostty so the dark canvas survives luminance-keyed terminal
    shaders. Taps the system audio monitor directly, so music playing
    in the main ytm still drives the world."""
    if not sys.stdin.isatty():
        print("the blackspace needs a TTY.")
        return 1
    shim = None
    try:
        # a minimal stand-in for App: just enough for frame()'s groove
        shim = type("WolfAudio", (), {})()
        shim.tap = SpectrumTap()
        shim.player = type("P", (), {"props": {}, "loading": False})()
        shim._drop_e = [0.0, 0.0, 0.0]
        shim._drop_bass_avg = 0.12
        shim._drop_groove = App._drop_groove.__get__(shim)
    except Exception:
        shim = None
    game = Blackspace(deep=True)
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    sys.stdout.write("\x1b[?1049h\x1b[?25l\x1b[2J")
    try:
        tty.setraw(fd, termios.TCSANOW)
        attrs = termios.tcgetattr(fd)
        attrs[1] |= termios.OPOST | termios.ONLCR
        termios.tcsetattr(fd, termios.TCSANOW, attrs)
        # any-motion mouse tracking, SGR encoded, PIXEL coords —
        # raw deltas are the aim hand (csgo-style, not a stick)
        sys.stdout.write("\x1b[?1003h\x1b[?1006h\x1b[?1016h")
        sys.stdout.flush()

        def win_px():
            try:
                ws = struct.unpack(
                    "HHHH", fcntl.ioctl(fd, termios.TIOCGWINSZ,
                                        b"\0" * 8))
                if ws[2] and ws[3]:
                    return ws[2], ws[3]
            except OSError:
                pass
            return lastsz[0] * 10, lastsz[1] * 20
        pxsz = (1, 1)
        mouse_re = re.compile(rb"\x1b\[<(\d+);(\d+);(\d+)([Mm])")
        running, cost, lastsz = True, 0.0, (1, 1)
        ibuf = b""
        while running:
            r, _, _ = select.select([sys.stdin], [], [],
                                    max(0.002, 0.012 - cost))
            if r:
                try:
                    ibuf += os.read(fd, 2048)
                except OSError:
                    break
            while ibuf and running:
                if ibuf[0:1] == b"\x1b":
                    m = mouse_re.match(ibuf)
                    if m:
                        bt, cx, cy = (int(m.group(1)), int(m.group(2)),
                                      int(m.group(3)))
                        if bt < 64:    # clicks + moves; wheel ignored
                            game.mouse(
                                cx, cy,
                                cx / max(1, pxsz[0]),
                                cy / max(1, pxsz[1]),
                                m.group(4) == b"M" and (bt & 0x43) == 0)
                        ibuf = ibuf[m.end():]
                        continue
                    seq = ibuf[1:3].decode("ascii", "ignore")
                    code = {"[A": "UP", "[B": "DOWN", "[C": "RIGHT",
                            "[D": "LEFT"}.get(seq)
                    if code:
                        game.key(code)
                        ibuf = ibuf[3:]
                        continue
                    if len(ibuf) < 4:
                        r2, _, _ = select.select([sys.stdin], [], [], 0.01)
                        if r2:
                            break          # partial sequence — wait for it
                        running = False    # a lone esc: wake up
                        break
                    ibuf = ibuf[1:]        # unknown sequence: shed a byte
                    continue
                ch = chr(ibuf[0])
                ibuf = ibuf[1:]
                if ch in ("q", "\x03"):
                    running = False
                    break
                game.key(ch)
            if not running:
                break
            t0 = time.perf_counter()
            size = os.get_terminal_size()
            w, h = size.columns, size.lines
            if (w, h) != lastsz:
                lastsz = (w, h)
                pxsz = win_px()
                sys.stdout.write("\x1b[2J")   # no stale rows after resize
            if w < 40 or h < 12:
                sys.stdout.write("\x1b[H\x1b[2Jthe door is too small.")
                sys.stdout.flush()
                time.sleep(0.2)
                continue
            try:
                lines = game.frame(w, h, shim)
            except Exception:
                # leave a trail instead of a silently vanished window
                import traceback
                try:
                    with open(os.path.join(CONFIG_DIR, "wolf-crash.log"),
                              "w") as f:
                        f.write(traceback.format_exc())
                except Exception:
                    pass
                return 1
            out = ["\x1b[H"]
            for i, ln in enumerate(lines[:h]):
                out.append(f"\x1b[{i + 1};1H" + ln + "\x1b[0m\x1b[K")
            sys.stdout.write("".join(out))
            sys.stdout.flush()
            cost = time.perf_counter() - t0
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
        sys.stdout.write("\x1b[?1003l\x1b[?1006l\x1b[?1016l"
                         "\x1b[?25h\x1b[?1049l")
        sys.stdout.flush()
        if shim is not None:
            try:
                shim.tap.stop()
            except Exception:
                pass
    return 0


def wolf_pulse_main():
    """Hidden: ytm --pulse — localhost groove feed for the browser game.

    Serves the drop visualizer's drive signals as JSON on
    127.0.0.1:8763 with CORS * (file:// pages can read it). Taps the
    system monitor, so it hears whatever is playing. Auto-exits when
    nobody has polled for ~90 s, or instantly if the port is taken
    (someone is already serving the groove)."""
    import http.server
    state = {"pulse": 0.0, "energy": 0.0, "bass": 0.0, "mid": 0.0,
             "treble": 0.0, "live": False, "theme": None}
    last_req = [time.time()]

    def read_theme():
        try:
            with open(STATE_FILE) as f:
                ti = int(json.load(f).get("theme_i", 0))
        except Exception:
            ti = 0
        name, p1, p2, p3 = THEMES[ti % len(THEMES)]
        return {"name": name, "colors": [list(p1), list(p2), list(p3)]}
    shim = type("WolfAudio", (), {})()
    shim.tap = SpectrumTap()
    shim._drop_e = [0.0, 0.0, 0.0]
    shim._drop_bass_avg = 0.12
    shim._drop_groove = App._drop_groove.__get__(shim)

    def pump():
        last = time.perf_counter()
        beat = 0
        while time.time() - last_req[0] < 90:
            beat += 1
            if beat % 60 == 1:          # every ~2s: follow theme changes
                state["theme"] = read_theme()
            now = time.perf_counter()
            dt = min(0.25, now - last)
            last = now
            try:
                if shim.tap.producing:
                    lv = shim.tap.levels(18)
                    raw = (sum(lv[:5]) / 5, sum(lv[5:12]) / 7,
                           sum(lv[12:]) / 6)
                else:
                    raw = (0.0, 0.0, 0.0)
                for i, v in enumerate(raw):
                    e = shim._drop_e[i]
                    k = min(1.0, (10.0 if v > e else 2.4) * dt)
                    shim._drop_e[i] = e + (v - e) * k
                pulse, en, eb, em, et = shim._drop_groove(
                    raw, dt, time.time())
                state.update(pulse=round(min(1.2, pulse), 4),
                             energy=round(en, 4),
                             bass=round(min(1.2, eb), 4),
                             mid=round(min(1.2, em), 4),
                             treble=round(min(1.2, et), 4),
                             live=bool(shim.tap.producing))
            except Exception:
                pass
            time.sleep(0.033)
        try:
            shim.tap.stop()
        except Exception:
            pass
        os._exit(0)

    class Feed(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            last_req[0] = time.time()
            body = json.dumps(state).encode()
            self.send_response(200)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    try:
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 8763), Feed)
    except OSError:
        return 0
    threading.Thread(target=pump, daemon=True).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


class App:
    def __init__(self, ao=None):
        from ytmusicapi import YTMusic
        self.authed = auth_present()
        try:
            self.yt = make_ytmusic()
        except Exception:
            self.authed = False
            self.yt = YTMusic()

        state = self._load_state()
        self.player = Player(self._on_eof, ao=ao,
                             volume=state.get("volume", 70))
        self.art = ArtCache()
        self.tap = SpectrumTap()
        self._url_cache = {}      # video_id → (direct stream url, ts)
        self._art_dims = (28, 14)  # last art-panel size, for prefetching
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

        # 0 = off, 1 = repeat the queue, 2 = loop the current track
        # (older state files stored a bool — int() maps True → 1)
        self.repeat = int(state.get("repeat", 0) or 0)
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
        self._drop_pa = self._drop_new_params(self._drop_preset)
        self._drop_pb = self._drop_pa
        self._drop_mix = 1.0
        self._drop_switch_at = time.time() + random.uniform(12, 24)
        # 0 = chunky half-blocks, 1 = hi-def quadrants, 2 = pixel: the
        # field blitted as a real bitmap via the kitty graphics protocol
        self._kitty_ok = self._kitty_sniff()
        self._kitty_payload = None
        self._kitty_live = False
        self._kitty_art_key = None    # url of the cover currently shipped
        self._kitty_art_live = False
        self._kitty_id = 0
        self.drop_px = int(state.get(
            "drop_px", 1 if state.get("drop_hd") else 0))
        if self.drop_px == 3:      # migrate: pixel moved from 3 to 2
            self.drop_px = 2
        if self.drop_px > (2 if self._kitty_ok else 1):
            self.drop_px = 1
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
        self.help = False         # ?: the full command box
        self.rich_search = int(state.get("rich_search", 1))
        self.viz_art = int(state.get("viz_art", 0))
        # lifts the visualizer's blacks above luminance-keyed terminal
        # shaders (ghostty starfields etc.) so the plasma and the cover
        # don't dissolve into the wallpaper
        # forced black environment when a wallpaper shader is detected —
        # not a setting (he tried settings; they were ugly)
        self.shader_guard = 2 if shader_active() else 0
        self.guard_floor = 108
        if self.shader_guard:
            self.art.floor = self.SHADER_FLOOR
        self.keep_awake = int(state.get("keep_awake", 1))
        self._inhibit_p = None    # the sleep/lock inhibitor child
        self.sc_on = int(state.get("sc_on", 1))  # ☁ merged soundcloud
        self.full = False
        self.viz_max = False      # F: visualizer owns the whole terminal
        self.liked_now = False
        self.input_mode = False
        self.input_buf = ""
        self.input_purpose = "search"
        self.wolf = None          # the hidden floor (search: "bhop")
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
        self._update_check()

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
                           "rich_search": self.rich_search,
                           "viz_art": self.viz_art,
                           "keep_awake": self.keep_awake,
                           "sc_on": self.sc_on,
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
            return self._src_view(self.results)
        if self.tab == 1:
            return self._src_view(self.lib)
        if self.tab == 2:
            return self.pl_tracks if self.pl_open else self.playlists
        return self.queue

    def _src_view(self, lst):
        """S swaps the lens on search/library: everything, YT only, or
        ☁ only. A view, never a mutation — the merged list stays whole."""
        f = getattr(self, "src_filter", "all")
        if f == "all":
            return lst
        return [t for t in lst if getattr(t, "source", "yt") == f]

    # ── data fetching (background threads) ───────────────────────────────────
    def _update_check(self):
        """Quietly look for new commits on GitHub once per launch; the
        header grows an ↑ badge and the status line says how to update."""
        def work():
            try:
                here = os.path.dirname(os.path.abspath(__file__))
                if not os.path.isdir(os.path.join(here, ".git")):
                    return
                subprocess.run(["git", "-C", here, "fetch", "--quiet"],
                               capture_output=True, timeout=20)
                r = subprocess.run(
                    ["git", "-C", here, "rev-list", "--count", "HEAD..@{u}"],
                    capture_output=True, text=True, timeout=5)
                n = int(r.stdout.strip() or 0)
                if n > 0:
                    self._update_n = n
                    self.say(f"ytm update available ({n} new commit"
                             f"{'s' if n > 1 else ''}) — quit and run: "
                             "ytm update")
            except Exception:
                pass
        threading.Thread(target=work, daemon=True).start()

    def _auth_refresh(self):
        """Google rotates session cookies; quietly re-import a fresh set
        from the browser and rebuild the client. OAuth tokens refresh
        themselves, so there a rebuild is all that's needed."""
        try:
            import io
            import contextlib
            if os.path.isfile(OAUTH_FILE) and os.path.isfile(OAUTH_CLIENT):
                self.yt = make_ytmusic()
                self.authed = True
                return True
            with contextlib.redirect_stdout(io.StringIO()):
                ok = import_firefox_auth()
            if ok:
                self.yt = make_ytmusic()
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
    def _wolf_start(self):
        # the easter egg door: searching "bhop" lands here instead
        # of the API. music keeps playing — the game drinks the groove.
        # preferred engine: the browser build (true pointer lock — click
        # captures the mouse, esc releases it — and smooth 60fps canvas).
        # falls back to the terminal build, then to in-place mode.
        gui = (os.environ.get("WAYLAND_DISPLAY")
               or os.environ.get("DISPLAY"))
        web = os.path.expanduser("~/Projects/blackspace-wolf/index.html")
        if gui and os.path.exists(web) and shutil.which("firefox"):
            try:
                subprocess.Popen(
                    [sys.executable, os.path.abspath(__file__), "--pulse"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    start_new_session=True)
                subprocess.Popen(
                    ["firefox", "--new-window", "file://" + web],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    start_new_session=True)
                self.say("the blackspace opens a door. click to lock in.")
                return
            except Exception:
                pass
        if shutil.which("ghostty") and gui:
            try:
                subprocess.Popen(
                    ["ghostty", "--gtk-single-instance=false",
                     "--custom-shader=",
                     "--background=#050507", "--background-opacity=1",
                     "--background-blur=0", "--title=the blackspace",
                     "-e", sys.executable, os.path.abspath(__file__),
                     "--wolf"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    start_new_session=True)
                self.say("the blackspace opens a door.")
                return
            except Exception:
                pass
        try:
            self.wolf = Blackspace()
        except Exception as e:
            self.say(f"the blackspace stays shut: {e}")

    def do_search(self, query):
        self.searching = True
        self._search_q = query     # newer searches invalidate older ones

        def work():
            try:
                try:
                    items = self.yt.search(query, filter="songs", limit=30)
                except KeyError:
                    # ytmusicapi can't parse some authed filtered responses
                    # (KeyError: 'musicShelfRenderer') — unfiltered works
                    items = self.yt.search(query, limit=30)
                if self._search_q != query:
                    return
                self.results = [Track.from_item(i) for i in items
                                if i.get("videoId")]
                self.sel[0] = 0
                self.scroll[0] = 0
                self.say(f"{len(self.results)} results for “{query}”")
            except Exception as e:
                self.say(f"search failed: {e}")
            # the merger: soundcloud results ride in after, tagged ☁ —
            # the YT results are already on screen, this only extends
            if getattr(self, "sc_on", 1):
                try:
                    sc = sc_search(query)
                    if sc and self._search_q == query:
                        self.results = self.results + sc
                        self.say(f"{len(self.results)} results "
                                 f"(+{len(sc)} ☁ soundcloud)")
                except Exception:
                    pass
            self.searching = False
        threading.Thread(target=work, daemon=True).start()

    def fetch_library(self):
        if self._lib_fetched:
            return
        self._lib_fetched = True
        sct = sc_token()
        if not self.authed and not sct:
            self.say("library needs sign-in → quit and run: ytm --login")
            return
        # last session's library appears INSTANTLY; the network refresh
        # quietly replaces it when it lands
        cached = cache_load("library")
        if cached and not self.lib:
            try:
                self.lib = [Track(**t) for t in cached]
            except TypeError:
                pass
        self.loading_msg = "loading liked songs…"

        def work():
            try:
                yt_part = []
                if self.authed:
                    data = self._lib_call(
                        lambda: self.yt.get_liked_songs(limit=300))
                    yt_part = [Track.from_item(t)
                               for t in data.get("tracks", [])
                               if t.get("videoId")]
                sc_part = sc_likes(sct) if sct else []
                self.lib = weave(yt_part, sc_part)
                cache_save("library", [dataclasses.asdict(t)
                                       for t in self.lib])
                msg = f"{len(yt_part)} liked songs"
                if sc_part:
                    msg += f" + {len(sc_part)} ☁"
                self.say(msg)
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
        sct = sc_token()
        if not self.authed and not sct:
            self.say("playlists need sign-in → quit and run: ytm --login")
            return
        cached = cache_load("playlists")
        if cached and not self.playlists:
            try:
                self.playlists = [Playlist(**p) for p in cached]
            except TypeError:
                pass
        self.loading_msg = "loading playlists…"

        def work():
            try:
                yt_part = []
                if self.authed:
                    items = self._lib_call(
                        lambda: self.yt.get_library_playlists(limit=50))
                    if not items and self._auth_refresh():
                        # signed-out responses come back empty, not errors
                        items = self.yt.get_library_playlists(limit=50)
                    yt_part = [
                        Playlist(p["playlistId"], p.get("title", "?"),
                                 str(p.get("count", "")))
                        for p in items]
                sc_part = sc_playlists(sct) if sct else []
                self.playlists = yt_part + sc_part
                cache_save("playlists", [dataclasses.asdict(p)
                                         for p in self.playlists])
                if sc_part:
                    self.say(f"{len(yt_part)} playlists "
                             f"+ {len(sc_part)} ☁")
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
                if pl.playlist_id.startswith("http"):   # ☁ set
                    self.pl_tracks = sc_playlist_tracks(pl)
                else:
                    data = self._lib_call(
                        lambda: self.yt.get_playlist(pl.playlist_id,
                                                     limit=300))
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
        """Fill the queue with the source's radio for a track — YT Music's
        watch playlist, or SoundCloud's recommended shelf."""
        def work():
            try:
                if track.source == "sc":
                    fresh = sc_related(track)
                else:
                    data = self.yt.get_watch_playlist(
                        videoId=track.video_id, radio=True)
                    fresh = [Track.from_item(t)
                             for t in data.get("tracks", [])
                             if t.get("videoId")]
                    fresh = [t for t in fresh
                             if t.video_id != track.video_id]
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
        if self.authed or self.now.source == "sc":
            self._fetch_like_state(self.now.video_id)
        # a prefetched direct stream skips mpv's yt-dlp resolve (~1-3s)
        hit = self._url_cache.get(self.now.video_id)
        direct = hit[0] if hit and time.time() - hit[1] < 3000 else None
        if direct is None and self.now.source == "sc":
            # soundcloud resolves in ~0.3s through api-v2 — do that in a
            # thread and start playback the moment it lands, instead of
            # handing mpv's yt-dlp hook a 5-second job
            trk = self.now

            def work():
                try:
                    u = sc_resolve_fast(trk)
                except Exception:
                    u = ""
                if self.now is trk:          # user didn't skip meanwhile
                    self.player.play_video(trk.video_id, direct=u or None)
            self.player._loading = True
            threading.Thread(target=work, daemon=True).start()
        else:
            self.player.play_video(self.now.video_id, direct=direct)
        self._write_now()
        self.say(f"▶ {self.now.title}")
        self._prefetch()

    def _resolve_stream(self, vid):
        """Resolve a track to its direct audio URL ourselves — the same
        thing mpv's hook does, done ahead of time. soundcloud goes
        through api-v2 first (~0.3s); yt-dlp is the fallback."""
        if vid.startswith("http"):
            trk = next((t for t in self.queue if t.video_id == vid), None)
            u = sc_resolve_fast(trk or Track(vid, "", ""))
            if u:
                return u
        import yt_dlp
        target = vid if vid.startswith("http") \
            else f"https://music.youtube.com/watch?v={vid}"
        opts = {"quiet": True, "no_warnings": True, "logger": _SC_SILENT,
                "format": "bestaudio[acodec^=opus]/bestaudio/best",
                "skip_download": True}
        tok = sc_token()
        if vid.startswith("http") and tok:
            opts.update({"username": "oauth", "password": tok})
        with yt_dlp.YoutubeDL(opts) as y:
            info = y.extract_info(target, download=False)
        return (info or {}).get("url") or ""

    def _prefetch(self):
        """While this track plays, get the NEXT one ready: its stream
        URL (instant skip) and its artwork (no blank art panel) — plus
        the current track's art in every size the UI uses."""
        cur, nxt = self.now, None
        if 0 <= self.qpos + 1 < len(self.queue):
            nxt = self.queue[self.qpos + 1]

        def work():
            for t in (cur, nxt):
                if not t:
                    continue
                self.art.rgb(t.thumb)
                self.art.palette(t.thumb)
                w, h = self._art_dims
                self.art.get(t.thumb, w, h)
            if nxt and nxt.video_id not in self._url_cache:
                try:
                    u = self._resolve_stream(nxt.video_id)
                    if u:
                        self._url_cache[nxt.video_id] = (u, time.time())
                        if len(self._url_cache) > 24:   # oldest out
                            old = min(self._url_cache,
                                      key=lambda k: self._url_cache[k][1])
                            del self._url_cache[old]
                except Exception:
                    pass
        threading.Thread(target=work, daemon=True).start()

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

    def start_mix(self):
        """R = make a mix out of the selected track, wherever it is —
        play it and let its radio fill the queue, then show the queue."""
        lst = self.current_list()
        i = self.sel[self.tab]
        in_tracks = self.tab in (0, 1, 3) or (self.tab == 2 and self.pl_open)
        if not (in_tracks and lst and 0 <= i < len(lst)) or \
                isinstance(lst[i], Playlist):
            return
        track = lst[i]
        self.queue = [track]
        self.play_queue(0)
        self.start_radio(track)
        self.tab = 3              # watch the mix build, hearts and all
        self.sel[3] = 0
        self.say(f"✦ mix from: {track.title}")

    def _fetch_like_state(self, vid):
        """The header ♥ (and the L toggle) should know whether the track
        is already liked, not just whether it was liked this session."""
        if vid.startswith("http"):
            # soundcloud — liked iff it's in the library's ☁ section
            self.liked_now = any(t.video_id == vid for t in self.lib)
            return

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
        if self.now.source == "sc":
            self._sc_like_toggle(self.now)
            return
        if not self.authed:
            self.say("liking needs sign-in → ytm --login")
            return
        trk = self.now
        if self.liked_now:           # L on a liked song = unlike, heartbreak
            self._like_t = time.time()
            self._like_mode = "break"
            self.liked_now = False
            # the library reflects it instantly; the quiet refresh after
            # the network call is just the confirm
            self.lib = [x for x in self.lib if x.video_id != trk.video_id]
            self.sel[1] = min(self.sel[1], max(len(self.lib) - 1, 0))

            def work():
                try:
                    self.yt.rate_song(trk.video_id, "INDIFFERENT")
                    self.say(f"♡ unliked: {trk.title}")
                    if self._lib_fetched:
                        self._lib_refresh()
                except Exception as e:
                    self.say(f"unlike failed: {e}")
            threading.Thread(target=work, daemon=True).start()
            return
        self._like_t = time.time()   # heart splash, optimistic
        self._like_mode = "like"
        self.liked_now = True
        if self._lib_fetched and \
                all(x.video_id != trk.video_id for x in self.lib):
            self.lib.insert(0, trk)  # likes land newest-first

        def work():
            try:
                self.yt.rate_song(trk.video_id, "LIKE")
                self.say(f"♥ liked: {trk.title}")
                if self._lib_fetched:
                    self._lib_refresh()
            except Exception as e:
                self.say(f"like failed: {e}")
                if self.now and self.now.video_id == trk.video_id:
                    self.liked_now = False
        threading.Thread(target=work, daemon=True).start()

    def _sc_like_toggle(self, trk):
        """L on a ☁ track: same heart, soundcloud-side like — optimistic
        like everything else, the api call lands behind the splash."""
        if not sc_token():
            self.say("☁ run: ytm --sc-login  to like soundcloud tracks")
            return
        if self.liked_now:
            self._like_t = time.time()
            self._like_mode = "break"
            self.liked_now = False
            self.lib = [x for x in self.lib if x.video_id != trk.video_id]
            self.sel[1] = min(self.sel[1], max(len(self.lib) - 1, 0))

            def work():
                self.say(f"♡ unliked on ☁: {trk.title}"
                         if sc_like(trk, on=False) else "☁ unlike failed")
            threading.Thread(target=work, daemon=True).start()
            return
        self._like_t = time.time()
        self._like_mode = "like"
        self.liked_now = True
        if all(x.video_id != trk.video_id for x in self.lib):
            self.lib.insert(0, trk)      # newest likes live at the top

        def work():
            if sc_like(trk, on=True):
                self.say(f"♥ liked on ☁: {trk.title}")
            else:
                self.say("☁ like failed")
                if self.now and self.now.video_id == trk.video_id:
                    self.liked_now = False
        threading.Thread(target=work, daemon=True).start()

    # ── library write ops ────────────────────────────────────────────────────
    def _pl_refresh(self):
        """Quietly re-pull the open playlist so it tracks reality."""
        pl = self.pl_open
        if not (self.authed and pl) or pl.playlist_id.startswith("http"):
            return                       # ☁ sets refresh on reopen only

        def work():
            try:
                data = self._lib_call(
                    lambda: self.yt.get_playlist(pl.playlist_id, limit=300))
                tracks = [Track.from_item(t)
                          for t in data.get("tracks", [])
                          if t and t.get("videoId")]
                if self.pl_open and \
                        self.pl_open.playlist_id == pl.playlist_id:
                    self.pl_tracks = tracks
                    self.sel[2] = min(self.sel[2], max(len(tracks) - 1, 0))
            except Exception:
                pass
        threading.Thread(target=work, daemon=True).start()

    def _lib_refresh(self):
        """Quietly re-pull liked songs (likes land here live)."""
        if not self.authed:
            return

        def work():
            try:
                data = self._lib_call(
                    lambda: self.yt.get_liked_songs(limit=300))
                # the ☁ half survives the YT-side refresh, re-woven
                self.lib = weave(
                    [Track.from_item(t)
                     for t in data.get("tracks", [])
                     if t.get("videoId")],
                    [t for t in self.lib if t.source == "sc"])
                self.sel[1] = min(self.sel[1], max(len(self.lib) - 1, 0))
            except Exception:
                pass
        threading.Thread(target=work, daemon=True).start()

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
        if not isinstance(lst[i], Playlist) and lst[i].source == "sc":
            self.say("☁ soundcloud track — your YT playlists can't hold it")
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
        if pl.playlist_id.startswith("http"):
            self.say("☁ soundcloud sets are read-only here (for now)")
            return
        # the visible list (and the splash) reacts NOW — the network call
        # and the quiet refresh land behind it
        if pl.playlist_id == "LM":
            self._like_t = time.time()
            self._like_mode = "like"
            if self.now and self.now.video_id == track.video_id:
                self.liked_now = True
            if self._lib_fetched and \
                    all(x.video_id != track.video_id for x in self.lib):
                self.lib.insert(0, track)
        elif self.pl_open and self.pl_open.playlist_id == pl.playlist_id:
            self.pl_tracks.append(track)

        def work():
            try:
                if pl.playlist_id == "LM":      # Liked Music is rate-based
                    self.yt.rate_song(track.video_id, "LIKE")
                else:
                    self.yt.add_playlist_items(pl.playlist_id,
                                               [track.video_id])
                self.say(f"✚ {track.title} → {pl.title}")
                self._pls_fetched = False        # counts changed
                if pl.playlist_id == "LM":
                    self._lib_refresh()
                elif self.pl_open and \
                        self.pl_open.playlist_id == pl.playlist_id:
                    self._pl_refresh()
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
        if track in self.lib:                    # gone from the list NOW
            self.lib.remove(track)
        self.sel[1] = min(self.sel[1], max(len(self.lib) - 1, 0))
        if track.source == "sc":

            def work():
                self.say(f"♡ unliked on ☁: {track.title}"
                         if sc_like(track, on=False) else "☁ unlike failed")
            threading.Thread(target=work, daemon=True).start()
            return

        def work():
            try:
                self.yt.rate_song(track.video_id, "INDIFFERENT")
                self.say(f"♡ unliked: {track.title}")
                self._lib_refresh()
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
        if track in self.pl_tracks:              # gone from the list NOW
            self.pl_tracks.remove(track)
        self.sel[2] = min(self.sel[2], max(len(self.pl_tracks) - 1, 0))

        def work():
            try:
                self.yt.remove_playlist_items(
                    pl.playlist_id,
                    [{"videoId": track.video_id,
                      "setVideoId": track.set_video_id}])
                self.say(f"✗ removed from {pl.title}: {track.title}")
                self._pls_fetched = False        # counts changed
                self._pl_refresh()
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
        if pl.playlist_id.startswith("http"):
            self.say("☁ soundcloud sets are read-only here (for now)")
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
        if self.wolf is not None:
            # the hidden floor swallows every key; esc/q wakes you up
            if k in ("ESC", "q"):
                self.wolf = None
                self.say("the blackspace lets you go.")
            else:
                self.wolf.key(k)
            return

        if self.input_mode:
            if k == "ESC":
                self.input_mode = False
                self.input_buf = ""
            elif k in ("\r", "\n"):
                self.input_mode = False
                text = self.input_buf.strip()
                if text and self.input_purpose == "search":
                    if text.lower() == "bhop":
                        # the search bar is the door (see Blackspace)
                        self.input_buf = ""
                        self._wolf_start()
                        return
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

        if self.help:
            self.help = False      # any key returns to the adventure
            return
        if k == "?":
            self.help = True
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
        elif k == "S":
            cyc = ("all", "yt", "sc")
            self.src_filter = cyc[
                (cyc.index(getattr(self, "src_filter", "all")) + 1) % 3]
            self.sel[self.tab] = 0
            self.scroll[self.tab] = 0
            self.say({"all": "sources: all", "yt": "♪ yt music only",
                      "sc": "☁ soundcloud only"}[self.src_filter])
        elif k == "R":
            self.start_mix()
        elif k == "s":
            self.shuffle_queue()
        elif k == "r":
            self.repeat = (self.repeat + 1) % 3
            self.say(("repeat off", "repeat all",
                      "loop track")[self.repeat])
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
        elif k == "t":
            self.viz_art = 1 - getattr(self, "viz_art", 0)
            self.say("album art over the visualizer"
                     if self.viz_art else "art overlay off")
        elif k == "p":
            self.drop_px = (self.drop_px + 1) % (3 if self._kitty_ok else 2)
            self.say(["drop: chunky pixels", "drop: hi-def pixels",
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
        if self.wolf is not None:
            try:
                lines = self.wolf.frame(w, h, self)
            except Exception:
                # never let the easter egg take the player down with it
                self.wolf = None
                self.say("the blackspace collapsed. it happens.")
                return
            out = ["\x1b[H"]
            for i, ln in enumerate(lines[:h]):
                out.append(f"\x1b[{i + 1};1H" + ln + "\x1b[0m\x1b[K")
            sys.stdout.write("".join(out))
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
        if self.help:
            lines = self._render_help_overlay(lines, w)

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
        if getattr(self, "_update_n", 0):
            acct = fg(ORANGE) + BOLD + "↑ ytm update" + RESET + "  " + acct
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
        big = w >= max(len(r) for r in BIG_BITS) + 6 and self.size.lines >= 26
        if big:
            for r, row in enumerate(fancy_logo()):
                ln = "  " + row
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
        elif self.tab in (0, 1):
            f = getattr(self, "src_filter", "all")
            if f != "all":
                title += "  ·  " + ("♪ yt only" if f == "yt"
                                    else "☁ soundcloud only")
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
        if self.tab == 0 and getattr(self, "rich_search", 0) and lst and \
                not isinstance(lst[0], Playlist):
            out.extend(self._render_rich_rows(lst, w, rows))
            return out
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
                3: "queue is empty — R makes a mix of any song",
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
                pic = ("☁ " if it.playlist_id.startswith("http") else "▤ ")
                line = (f" {fg(ORANGE)}{pic}{RESET}{fg(WHITE)}{it.title}"
                        f"{RESET} {fg(GREY)}{DIM}{it.count}{RESET}")
            else:
                playing = bool(self.now) and (
                    (self.tab == 3 and i == self.qpos)
                    or (self.tab == 1
                        and it.video_id == self.now.video_id))
                if self.tab in (1, 3):
                    # the queue AND the library speak Undertale: the
                    # playing track is a beating soul, queue history is
                    # hollow and faded, everything else waits as dim
                    # hearts — a liked song IS a heart, after all
                    if playing:
                        hc = lerp(RED, PINK,
                                  0.5 + 0.5 * math.sin(time.time() * 6))
                        mark = fg(hc) + "♥ " + RESET
                    elif self.tab == 3 and i < self.qpos:
                        mark = fg(DGREY) + "♡ " + RESET
                    else:
                        mark = fg(lerp(DARK, RED, 0.55)) + "♡ " + RESET
                else:
                    mark = fg(RED) + "▶ " + RESET if playing else "  "
                faded = self.tab == 3 and i < self.qpos and not playing
                tcol = DGREY if faded else WHITE
                src = ""
                if self.tab in (0, 1, 3):    # the mixed-source views
                    src = ((fg(ORANGE) + "☁ " if it.source == "sc"
                            else fg(RED) + "▶ ") + RESET)
                dur = it.duration or ""
                tw = w - 4 - len(dur) - 2 - (2 if src else 0)
                t = it.title[:max(tw - len(it.artist) - 3, 8)]
                line = (f" {mark}{src}{fg(tcol)}{t}{RESET} "
                        f"{fg(GREY)}{DIM}{it.artist}{RESET}")
                pad = w - visible_len(line) - len(dur) - 2
                line += " " * max(pad, 1) + fg(DGREY) + dur + RESET
            if is_sel:
                plain = ANSI_RE.sub("", line)
                if self.tab in (1, 3) and not isinstance(it, Playlist):
                    # the selection cursor IS the soul, like the menus
                    hc = lerp(RED, PINK,
                              0.5 + 0.5 * math.sin(time.time() * 6))
                    line = bg(lerp(DARK, RED, 0.18)) + fg(WHITE) + BOLD + \
                        crop_pad(" " + fg(hc) + "♥" + fg(WHITE)
                                 + plain[2:], w)
                else:
                    line = bg(lerp(DARK, RED, 0.18)) + fg(WHITE) + BOLD + \
                        crop_pad(" " + fg(RED) + "▌" + fg(WHITE)
                                 + plain[1:], w)
            out.append(crop_pad(line, w))
        return out

    def _render_rich_rows(self, lst, w, rows):
        """Search results as two-row cards: a small album thumb, bold
        title with duration, then artist · album underneath."""
        out = []
        n_vis = max(1, rows // 2)
        sel = self.sel[self.tab]
        scr = self.scroll[self.tab]
        if sel < scr:
            scr = sel
        if sel >= scr + n_vis:
            scr = sel - n_vis + 1
        self.scroll[self.tab] = scr
        for v in range(n_vis):
            i = scr + v
            if i >= len(lst):
                out.append(crop_pad("", w))
                out.append(crop_pad("", w))
                continue
            it = lst[i]
            is_sel = (i == sel)
            art = self.art.get(getattr(it, "thumb", ""), 5, 2)
            a0 = art[0] if art else fg(DGREY) + "▒" * 5 + RESET
            a1 = art[1] if art else fg(DGREY) + "▒" * 5 + RESET
            bar = fg(RED) + "▌" + RESET if is_sel else " "
            hl = bg(lerp(DARK, RED, 0.18)) if is_sel else ""
            dur = it.duration or ""
            cloud = ((fg(ORANGE) + "☁ " if getattr(it, "source", "yt") == "sc"
                      else fg(RED) + "▶ ") + RESET)
            tw = max(8, w - 9 - len(dur) - 2 - 2)
            t1 = (cloud + hl + BOLD + fg(WHITE) + it.title[:tw] + RESET)
            sub = it.artist + (f" · {it.album}" if it.album else "")
            t2 = (hl + fg(GREY) + DIM + sub[:tw] + RESET)
            l1 = bar + a0 + " " + t1
            pad = w - visible_len(l1) - len(dur) - 2
            l1 += hl + " " * max(pad, 1) + fg(DGREY) + dur + RESET
            out.append(crop_pad(l1, w))
            out.append(crop_pad(bar + a1 + " " + t2, w))
        if rows % 2:
            out.append(crop_pad("", w))
        return out

    def _menu_items(self):
        """(label, attr, lo, hi, step, fmt) — fmt is a suffix string for
        continuous sliders or a tuple of names for discrete ones."""
        return [
            ("beat punch", "viz_react", 0.2, 2.4, 0.2, "×"),
            ("flow speed", "viz_speed", 0.4, 2.4, 0.2, "×"),
            ("morph speed", "viz_morph", 0.4, 2.4, 0.2, "×"),
            ("pixel quality", "drop_px",
             0, 2 if self._kitty_ok else 1, 1,
             ("chunky", "hi-def", "pixel")),
            ("rich search", "rich_search", 0, 1, 1, ("off", "on")),
            ("art overlay", "viz_art", 0, 1, 1, ("off", "on")),
            ("keep awake", "keep_awake", 0, 1, 1, ("off", "on")),
            ("☁ soundcloud", "sc_on", 0, 1, 1, ("off", "on")),
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
        # under a wallpaper shader the box's empty cells get keyed out —
        # give every cell an opaque floored backdrop so the menu stays
        # one solid panel
        bgc = bg(self.SHADER_FLOOR) if getattr(self, "shader_guard", 0) \
            else ""
        for i, rrow in enumerate(rows):
            if y0 + i >= len(lines):
                break
            cells = ansi_cells(lines[y0 + i], w)
            for j, (sgr, ch) in enumerate(ansi_cells(rrow, bw)):
                if ch:
                    put_cell(cells, x0 + j, bgc + sgr, ch)
            lines[y0 + i] = cells_to_str(cells)
        return lines

    HELP_SECTIONS = [
        ("PLAY", [("enter", "play"), ("spc", "pause"),
                  ("n / b", "next / prev"), (", / .", "seek 10s"),
                  ("+ / -", "volume"), ("m", "mute"),
                  ("s", "shuffle"), ("r", "repeat / loop"),
                  ("R", "mix from song")]),
        ("SOUL", [("L", "like / unlike"), ("/", "search"),
                  ("a", "add to queue"), ("A", "→ playlist"),
                  ("N", "new playlist"), ("x", "remove"),
                  ("D D", "delete playlist")]),
        ("VISUALS", [("v", "visualizer"), ("c", "theme"),
                     ("p", "pixel quality"), ("t", "art overlay"),
                     ("F", "max visualizer"), ("f", "fullscreen"),
                     ("M", "tuning menu"), ("[ ]", "beat punch"),
                     ("{ }", "flow speed"), ("w", "work mode")]),
        ("WORLD", [("tab 1-4", "switch view"), ("j k", "move"),
                   ("S", "all / yt / ☁"), ("esc / h", "back out"),
                   ("?", "this box"), ("q", "quit")]),
    ]

    def _render_help_overlay(self, lines, w):
        """?: every command in one Undertale dialogue box — chunky white
        border, the soul as section bullet, asterisk sign-off. Any key
        closes it. Exists because the footer hints truncate on narrow
        terminals and people couldn't find the rest."""
        hc = lerp(RED, PINK, 0.5 + 0.5 * math.sin(time.time() * 6))
        bw = min(w - 2, 72)
        two = bw >= 58
        ncols = 2 if two else 1
        colw = (bw - 4 - 2 * ncols) // ncols
        split = [self.HELP_SECTIONS[:2], self.HELP_SECTIONS[2:]] if two \
            else [self.HELP_SECTIONS]
        cols = []
        for secs in split:
            col = []
            for title, keys in secs:
                if col:
                    col.append("")
                col.append(fg(hc) + "♥ " + RESET + BOLD + fg(WHITE)
                           + title + RESET)
                for key, lab in keys:
                    col.append("  " + fg(ORANGE) + f"{key:<9}" + RESET
                               + fg(GREY) + lab + RESET)
            cols.append(col)
        n = max(len(c) for c in cols)
        for c in cols:
            c += [""] * (n - len(c))

        def fit(s, width):
            return s + " " * max(0, width - visible_len(s))

        bord = BOLD + fg(WHITE)
        t = " * COMMANDS * "
        lpad = (bw - 2 - len(t)) // 2
        rows = [bord + "╔" + "═" * lpad + RESET + BOLD + fg(hc) + t + RESET
                + bord + "═" * (bw - 2 - len(t) - lpad) + "╗" + RESET,
                bord + "║" + " " * (bw - 2) + "║" + RESET]
        for i in range(n):
            body = "  ".join(fit(c[i], colw) for c in cols)
            rows.append(bord + "║" + RESET + " " + fit(body, bw - 4) + " "
                        + bord + "║" + RESET)
        sign = "* press any key to continue your adventure"
        rows += [bord + "║" + " " * (bw - 2) + "║" + RESET,
                 bord + "║" + RESET + fg(DGREY) + ITAL
                 + f"{sign:^{bw - 2}}" + RESET + bord + "║" + RESET,
                 bord + "╚" + "═" * (bw - 2) + "╝" + RESET]
        x0 = (w - bw) // 2
        y0 = max(4, (len(lines) - len(rows)) // 2)
        bgc = bg(self.SHADER_FLOOR) if getattr(self, "shader_guard", 0) \
            else ""
        for i, rrow in enumerate(rows):
            if y0 + i >= len(lines):
                break
            cells = ansi_cells(lines[y0 + i], w)
            for j, (sgr, ch) in enumerate(ansi_cells(rrow, bw)):
                if ch:
                    put_cell(cells, x0 + j, bgc + sgr, ch)
            lines[y0 + i] = cells_to_str(cells)
        # the box is on fire — tongues lick up off the top border with
        # the heart splash's multi-sine flicker, transparent around the
        # flames so the screen keeps living behind them
        tt = time.time()
        col_f, col_n2, col_n3 = [], [], []
        for j in range(bw):
            # low spatial frequency: tongues span several cells instead
            # of strobing per column (cell res is much coarser than the
            # splash's half-block pixels)
            n1 = 0.5 + 0.5 * math.sin(j * 0.73 - tt * 9)
            n2 = 0.5 + 0.5 * math.sin(j * 0.31 + tt * 5.7)
            n3 = 0.5 + 0.5 * math.sin(j * 1.7 + tt * 15)
            col_f.append(n1 * n2 * 0.7 + n3 * 0.3)
            col_n2.append(n2)
            col_n3.append(n3)
        for dy in range(1, 5):
            y = y0 - dy
            if y < 0:
                continue
            cells = ansi_cells(lines[y], w)
            lit = False
            for j in range(bw):
                fh = 1 + int(col_f[j] * 3.2)       # 1..4 — an unbroken crown
                if dy < fh:
                    c = lerp(ORANGE, RED, dy / 4)
                    put_cell(cells, x0 + j, fg(c), "█" if dy == 1 else "▓")
                    lit = True
                elif dy == fh:
                    c = lerp(lerp(ORANGE, RED, dy / 4), PINK, 0.45)
                    tip = "▂▃▄▅"[int(col_n2[j] * 3.99)]
                    put_cell(cells, x0 + j, fg(c), tip)
                    lit = True
                elif dy == fh + 1 and col_n3[j] > 0.93:
                    put_cell(cells, x0 + j, fg(PINK), "✦")  # a spark
                    lit = True
            if lit:
                lines[y] = cells_to_str(cells)
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
            self._art_dims = (art_w, art_h)
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
        # undertale proportions: slim lobes, then straight sides pulling
        # in immediately below them to a clean point
        lobes = (ax - 0.58) ** 2 + (ys - 0.52) ** 2 < 0.39
        wedge = (ys <= 0.52) & (ys >= -1.0) & \
                (ax < 1.2 * (ys + 1.0) / 1.52)
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
        import numpy as np
        rows, cols, pad_l, band0 = self._splash_geom(out, w)
        s = min(1.0, 0.35 + t * 3.5)             # pop-in scale
        c = lerp(RED, PINK, 0.5 + 0.5 * math.sin(t * 9))
        pix = self._heart_mask(rows, cols, s)
        # the heart burns: flame tongues lick up from the crown in the
        # complement of whatever color the heart is pulsing through.
        # the grid grows upward (stealing rows above the heart) so the
        # fire has headroom, with a hot inner core and stray sparks
        comp = (255 - c[0], 255 - c[1], 255 - c[2])
        up = max(0, min(3, band0 - 1))           # cell rows of headroom
        hr = up * 2
        grid = np.vstack([np.zeros((hr, pix.shape[1]), bool), pix])
        b0 = band0 - up
        crown = grid.argmax(axis=0)              # first lit pixel per col
        ci = np.arange(grid.shape[1])
        n1 = 0.5 + 0.5 * np.sin(ci * 1.9 - t * 13)
        n2 = 0.5 + 0.5 * np.sin(ci * 0.57 + t * 7.3)
        n3 = 0.5 + 0.5 * np.sin(ci * 3.1 + t * 21)
        flick = n1 * n2 * 0.7 + n3 * 0.3
        fh = (2 + flick * 9 * s).astype(int)     # tongue heights
        outer = np.zeros_like(grid)
        inner = np.zeros_like(grid)
        for x in np.where(grid.any(axis=0))[0]:
            top = crown[x]
            if top <= 0:
                continue
            f = min(int(fh[x]), top)
            outer[top - f:top, x] = True
            # ragged tips: the topmost pixel of a tall tongue blinks
            if f > 2 and math.sin(x * 5.3 + t * 27) < -0.25:
                outer[top - f, x] = False
            inner[top - max(1, int(f * 0.45)):top, x] = True
            # a spark breaks free when the flicker peaks
            if n3[x] > 0.9 and top - f - 2 >= 0:
                outer[top - f - 2, x] = True
        self._stamp_pixels(out, w, outer, b0, pad_l, fg(comp))
        self._stamp_pixels(out, w, inner, b0, pad_l,
                           fg(lerp(comp, WHITE, 0.55)))
        self._stamp_pixels(out, w, grid, b0, pad_l, fg(c))
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

    def _viz_pulse(self):
        """Run the groove engine for the discrete styles too, so bars and
        scopes get the same kick/mid/hat signals the plasma dances to.
        Returns the smoothed kick pulse; mid/treble land on attributes."""
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
        dt = min(now - getattr(self, "_drop_last", now - 0.04), 0.25)
        self._drop_last = now
        for i, x in enumerate(raw):
            e = self._drop_e[i]
            k = min(1.0, (10.0 if x > e else 2.4) * dt)
            self._drop_e[i] = e + (x - e) * k
        pulse, _, _, _, _ = self._drop_groove(raw, dt, now)
        ps = getattr(self, "_pulse_s", 0.0)
        ps += (pulse - ps) * min(1.0, dt * 16.0)
        self._pulse_s = ps
        return ps

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
        # every style renders fresh every tick now — the bar physics are
        # time-based, so high fps just means smoother, not twitchier
        self._kitty_payload = None
        lines = self._render_visualizer_now(w, rows)
        if (getattr(self, "viz_art", 0)
                and self.viz_style in ("drop", "cover")
                and self.now and getattr(self.now, "thumb", "")
                and rows >= 8 and not self._kitty_payload):
            # pixel mode bakes the art into the bitmap itself; the text
            # overlay is for the block-character modes
            lines = self._overlay_art(lines, w, rows)
        return lines

    def _overlay_art(self, lines, w, rows):
        """t: the album cover floats over the plasma — the field keeps
        dancing around it as a generative backdrop."""
        ah = min(max(4, int(rows * 0.62)), rows - 2)
        aw = min(ah * 2, w - 10)
        ah = max(4, aw // 2)
        art = self.art.get(self.now.thumb, aw, ah)
        if not art:
            return lines                  # still downloading — next frame
        top = max(1, (rows - ah) // 2)
        left = (w - aw) // 2
        for r, al in enumerate(art[:ah]):
            if top + r >= len(lines):
                break
            cells = ansi_cells(lines[top + r], w)
            for i, (sgr, ch) in enumerate(ansi_cells(al, aw)):
                if ch:
                    put_cell(cells, left + i, sgr, ch)
            lines[top + r] = cells_to_str(cells)
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
        if self.viz_style in ("drop", "cover"):
            return self._viz_drop(w, rows, n, pad_l)

        if self.viz_style == "mirror":
            m = n // 2 + 1
            lv = self._viz_targets(m)
            c = (n - 1) / 2
            targets = [lv[min(int(abs(i - c)), m - 1)] for i in range(n)]
        else:
            targets = self._viz_targets(n)
        self._viz_physics(targets)
        ps = self._viz_pulse()
        md = getattr(self, "_mid_pulse", 0.0)
        tp = getattr(self, "_tre_pulse", 0.0)

        unit = rows * 8
        # one color per row, groove-shaped: the kick flashes the whole
        # gradient toward white, mids warm it toward pink
        glow = min(0.45, 0.34 * ps)
        rowc = []
        for r in range(rows):
            hfrac = (rows - 1 - r) / max(rows - 1, 1)
            col = lerp(self._viz_color(hfrac), PINK, min(0.5, 0.35 * md))
            rowc.append((fg(lerp(col, WHITE, glow)),
                         fg(lerp(col, WHITE, min(0.8, glow + 0.25
                                                 + 0.35 * tp)))))
        # hats strobe the falling peak caps
        cap_c = fg(lerp(lerp(WHITE, PINK, 0.35), WHITE, min(0.9, tp)))
        lines = []
        for r in range(rows):
            base = (rows - 1 - r) * 8
            body, tip = rowc[r]
            cells = []
            for i in range(n):
                filled = int(max(0, min(8, self.bars[i] * unit - base)))
                if filled == 8:
                    cells.append(body + "█")
                elif filled:        # the bar's tip burns brighter
                    cells.append(tip + BLOCKS[filled])
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
        ps = self._viz_pulse()
        md = getattr(self, "_mid_pulse", 0.0)
        grid = [[0] * n for _ in range(rows)]
        prev = None
        for x, v in enumerate(s):
            y = int((0.5 - v * 0.48) * (hd - 1))
            y = max(0, min(hd - 1, y))
            lo, hi = (y, y) if prev is None else (min(prev, y), max(prev, y))
            for yy in range(lo, hi + 1):
                grid[yy // 4][x // 2] |= BRAILLE[x % 2][yy % 4]
            prev = y
        # phosphor: the last ~120ms of trace lingers dimly underneath,
        # like a CRT; the live beam flashes white on the kick
        old, gt = getattr(self, "_scope_gh", (None, 0.0))
        nowt = time.time()
        if not old or len(old) != rows or len(old[0]) != n:
            old = [[0] * n for _ in range(rows)]
            self._scope_gh = (old, nowt)
        if nowt - gt > 0.12:
            self._scope_gh = ([row[:] for row in grid], nowt)
        else:
            for rr in range(rows):
                gr, cr = old[rr], grid[rr]
                for ii in range(n):
                    if cr[ii]:
                        gr[ii] |= cr[ii]
        glow = min(0.55, 0.45 * ps)
        lines = []
        for r in range(rows):
            cells = []
            for i in range(n):
                m = grid[r][i]
                g = old[r][i] & ~m
                if m:
                    c = lerp(lerp(RED, ORANGE, i / max(n - 1, 1)),
                             WHITE, glow)
                    cells.append(fg(c) + chr(0x2800 + m))
                elif g:
                    c = lerp(lerp(RED, ORANGE, i / max(n - 1, 1)),
                             DARK, 0.62 - min(0.25, md * 0.25))
                    cells.append(fg(c) + chr(0x2800 + g))
                else:
                    cells.append(" ")
            lines.append(crop_pad(" " * pad_l + "".join(cells) + RESET, w))
        return lines

    def _drop_new_params(self, preset=None):
        # ~half the rolls knock the origin off-center so radial presets
        # stop staring at the exact middle of the panel every time
        off = random.random() < 0.45
        p = {"k1": random.uniform(2.0, 5.5),
             "k2": random.uniform(2.0, 8.0),
             "arms": random.choice([2, 3, 3, 4, 5, 6]),
             "ph": random.uniform(0, math.tau),
             "cx": random.uniform(-1.1, 1.1) if off else 0.0,
             "cy": random.uniform(-0.6, 0.6) if off else 0.0,
             # post-processing personality: 0 = smooth, n = n sharp
             # palette bands (milkdrop-style colorful level sets)
             "band": random.choice([0, 0, 0, 2, 2, 3, 3, 4]),
             "gam": random.uniform(0.95, 1.45)}
        if preset is not None and preset in WAVE_PRESETS:
            # blackspace family: smooth gradients only — palette banding
            # would slice the glow into rings — and deeper shadows
            p["band"] = 0
            p["gam"] = random.uniform(1.1, 1.35)
        elif preset == 42:
            # the smiley reads as a face, not as level-set rings
            p["band"] = 0
            p["gam"] = random.uniform(1.0, 1.2)
        return p

    def _drop_post(self, v, P):
        """Contrast shaping — this is what makes presets pop instead of
        rendering as the same mid-palette mush."""
        import numpy as np
        v = np.clip(0.5 + v / 5.5, 0.0, 1.0)
        v = np.clip((v - 0.5) * 1.25 + 0.5, 0.0, 1.0) ** P["gam"]
        if P["band"]:
            return 0.5 - 0.5 * np.cos(v * math.tau * P["band"])
        return 0.5 - 0.5 * np.cos(v * math.pi)

    N_PRESETS = 43

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
            # the slow overtone needs an INTEGER angular frequency —
            # sw*0.5 with odd arms tears along the arctan2 branch cut
            sw2 = ang * ((A + 1) // 2) + np.log(r + 0.25) * (k2 * 0.5 + em)
            return (sin(sw - t * 1.8)
                    + sin(r * (4 + 9 * eb) - t * 2.2)
                    + sin(sw2 + t * 0.7)
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
        if preset == 27:   # dunes — warped ridges rolling across the panel
            wx = xs + 1.1 * sin(ys * 1.3 + t * 0.5)
            return (sin(wx * (k1 * 0.8 + 2 * eb) + ys * 1.5 - t * 0.9)
                    + sin(ys * k2 * 0.5 + sin(wx * 1.7 - t * 0.4) * 2)
                    + em * sin((wx + ys) * 3 + t * 0.7)
                    + et * 0.9 * sin(wx * 9 - t * 2.5))
        if preset == 28:   # rotor lattice — a grid seen through a slow spin
            co, si = math.cos(t * 0.18 + ph), math.sin(t * 0.18)
            xr = xs * co - ys * si
            yr = xs * si + ys * co
            return (sin(xr * (k1 * 1.6 + 2 * eb) + t * 0.4)
                    + sin(yr * k2 * 0.9 - t * 0.6)
                    + eb * 2 * sin(r * (5 + 5 * eb) - t * 2)
                    + et * sin((xr + yr) * 7 - t * 3))
        if preset == 29:   # ripple pond — three drifting raindrops
            acc = 0.0
            for i in range(3):
                px = 1.2 * math.sin(t * (0.21 + 0.09 * i) + ph + i * 2.1)
                py = 0.7 * math.cos(t * (0.17 + 0.07 * i) + i * 1.3)
                di = np.sqrt((xs - px) ** 2 + (ys - py) ** 2)
                acc = acc + sin(di * (k1 * 2 + 5 * eb) - t * (1.6 + 0.3 * i))
            return acc + em * sin(r * 3 - t) + et * sin(r * 14 - t * 4)
        if preset == 30:   # chevron flow — zigzag wavefronts marching
            z1 = np.abs(((xs * k1 * 0.8 + ys * 0.6 + t * 0.5) % 2) - 1)
            z2 = np.abs(((ys * k2 * 0.5 - xs * 0.4 - t * 0.35) % 2) - 1)
            return ((z1 * 2 - 1) * (1.2 + 1.5 * eb)
                    + (z2 * 2 - 1) * 1.2
                    + em * sin((xs + ys) * 3 + t)
                    + et * sin(xs * 9 - t * 3))
        if preset == 31:   # ribbons — horizontal silk bands breathing
            wv = sin(xs * 2.2 + t * 0.8) * (1.8 + 2.2 * eb)
            return (sin(ys * k1 * 1.5 + wv)
                    + sin(ys * k2 * 0.7 - wv * 0.6 + t * 0.5)
                    + em * sin(xs * 3 - t * 0.7)
                    + et * sin(ys * 11 + t * 2.5))
        if preset == 32:   # counterspiral moiré — arms braiding past each other
            s1 = ang * A + r * (k1 * 2.5 + 4 * eb) - t * 1.6
            s2 = ang * A - r * (k2 * 1.5) + t * 1.1
            return (sin(s1) + sin(s2)
                    + em * sin(r * 4 - t)
                    + et * 1.2 * sin(r * 13 + ang * 2 - t * 4))
        if preset == 33:   # speed lines — radial fan rushing outward
            return (sin(ang * (A * 3)) * (1.0 + 1.2 * em)
                    + sin(r * (6 + 8 * eb) - t * (3 + 2 * em)) * 1.4
                    + sin(np.log(r + 0.3) * 4 - t * 2) * 0.8
                    + et * sin(r * 18 - t * 6))
        if preset == 34:   # spinning box tunnel — nested squares in a twist
            co, si = math.cos(t * 0.25), math.sin(t * 0.25)
            xr = xs * co - ys * si
            yr = xs * si + ys * co
            q = np.maximum(np.abs(xr), np.abs(yr))
            return (sin(q * (k1 * 2.5 + 7 * eb) - t * 2)
                    + sin(q * 3 + t * 0.6)
                    + em * sin(ang * A + t)
                    + et * sin(q * 15 - t * 5))
        if preset == 35:   # aurora — curtains of light rippling sideways
            cur = sin(xs * k1 * 1.1
                      + sin(ys * 1.6 + t * 0.6) * (2.2 + 2 * eb))
            return (cur * (1.5 + eb) + sin(ys * 1.8 - t * 0.4)
                    + em * sin(xs * 5 + t * 1.2)
                    + et * 0.9 * sin((xs - ys) * 8 + t * 2.8))
        # ── the blackspace family (WAVE_PRESETS) ──────────────────────────
        # calm interference glowing out of darkness: every field here is a
        # couple of plane/distance waves biased negative, so most of the
        # panel rests at the bottom of the palette and only the crests
        # glow. no `ang` anywhere — immune to the branch-cut seam — and
        # cheap enough for the 5fps desktop widget
        if preset == 36:   # twin ripples — two soft sources circling slowly
            ox = 0.8 * math.sin(t * 0.22 + ph)
            oy = 0.45 * math.cos(t * 0.17)
            d1 = np.sqrt((xs - ox) ** 2 + (ys - oy) ** 2)
            d2 = np.sqrt((xs + ox) ** 2 + (ys + oy) ** 2)
            return (sin(d1 * (3.2 + 3 * eb) - t * 0.9) * 2.5
                    + sin(d2 * (2.6 + 2 * em) + t * 0.7) * 1.7
                    - 1.3 + et * 0.5 * sin(d1 * 9 - t * 2))
        if preset == 37:   # deep swell — slow rollers crossing a dark sea
            return (sin(xs * (1.8 + eb) - t * 0.6) * 2.4
                    + sin(xs * 0.9 + ys * 1.6 + t * 0.45) * 1.6
                    + sin(ys * 2.4 - t * 0.3 + math.sin(t * 0.2) * 2) * 0.9
                    - 1.25 + em * 0.6 * sin(xs * 4 - t))
        if preset == 38:   # breathing pond — one ripple source fading out
            env = 1.0 / (1.0 + r * (1.0 + 0.5 * em))
            return (sin(r * (4.5 + 4 * eb) - t * 1.1) * 5.5 * env
                    - 1.2 + et * 0.5 * sin(r * 11 - t * 3))
        if preset == 39:   # caustic beats — twin ring sets, slow moiré
            d1 = np.sqrt((xs - 0.55) ** 2 + ys * ys)
            d2 = np.sqrt((xs + 0.55) ** 2 + ys * ys)
            return (sin(d1 * (k1 * 0.8 + 2.2) - t * 0.8) * 2.9
                    + sin(d2 * (k1 * 0.8 + 2.8) + t * 0.65) * 2.0
                    - 1.8 + eb * 1.2 * sin(r * 2.5 - t)
                    + et * 0.4 * sin(d2 * 10 - t * 2.5))
        if preset == 40:   # dune drift — diagonal bands sliding in the dark
            return (sin((xs * 0.8 + ys * 0.55) * (k1 * 0.6 + 1.2 + eb)
                        - t * 0.5) * 2.8
                    + sin((xs - ys) * 1.3 + t * 0.27) * 1.5
                    - 1.8 + em * 0.5 * sin(xs * 3 + t * 0.8))
        if preset == 41:   # ember curtain — aurora out of blackness
            cur = sin(xs * (k1 * 0.4 + 1.0)
                      + sin(ys * 1.4 + t * 0.5) * (1.8 + 1.5 * eb))
            return (cur * 2.9 + sin(ys * 1.5 - t * 0.35) * 1.2
                    - 1.8 + et * 0.5 * sin(xs * 6 - t * 1.6))
        # 42: smiley — a grinning face beaming over a gentle ripple.
        # pure distance fields (ring + two dots + an arc), so it's as
        # cheap as any wave; one eye winks on a slow clock
        bob_x = 0.30 * math.sin(t * 0.31)
        bob_y = 0.18 * math.sin(t * 0.23)
        fx, fy = xs - bob_x, ys - bob_y
        fr2 = np.sqrt(fx * fx + fy * fy)
        face = 1.0 / (1.0 + np.abs(fr2 - 0.62) * (14 + 8 * eb))
        wink = 1.0 if math.sin(t * 0.7) > -0.6 else 0.22   # ;)
        e1 = (fx + 0.24) ** 2 + (fy + 0.22) ** 2
        e2 = (fx - 0.24) ** 2 + ((fy + 0.22) / wink) ** 2
        eyes = 1.0 / (1.0 + e1 * 120) + 1.0 / (1.0 + e2 * 120)
        arc = np.abs(np.sqrt(fx * fx + (fy + 0.06) ** 2) - 0.40)
        smile = (1.0 / (1.0 + arc * (26 + 20 * em))
                 * np.clip((fy - 0.04) * 9, 0.0, 1.0))   # soft lower-arc mask
        ripple = (0.8 * sin(r * 3 - t)
                  * np.clip((fr2 - 0.62) * 1.6, 0.15, 1.0))  # calm on the face
        return ((face * 2.6 + eyes * 3.4 + smile * 2.8)
                * (1.0 + 0.5 * eb)
                + ripple - 1.45
                - 1.1 * np.clip((0.62 - fr2) * 2.2, 0.0, 1.0)  # dark face
                + et * 0.4 * sin(r * 9 - t * 2.5))

    @staticmethod
    def _kitty_sniff():
        """True if the terminal speaks the kitty graphics protocol."""
        term = os.environ.get("TERM", "")
        prog = os.environ.get("TERM_PROGRAM", "")
        return any(t in (term + " " + prog).lower()
                   for t in ("kitty", "ghostty", "wezterm"))

    def _cell_px(self):
        """Cell size in real screen pixels (for pixel-perfect rendering).
        Prefers the terminal's own escape replies; without them, macOS
        reports zeros or Retina *points* through TIOCGWINSZ — so there we
        assume Retina and double, because oversampling just gets scaled
        down cleanly while undersampling renders soft and blocky."""
        q = getattr(self, "_cellpx_q", None)
        if q:
            return q
        mac = sys.platform == "darwin"
        try:
            r, c, xp, yp = struct.unpack(
                "HHHH", fcntl.ioctl(1, termios.TIOCGWINSZ, b"\0" * 8))
            if xp and yp and r and c:
                cw, ch = max(2, xp // c), max(4, yp // r)
                return (cw * 2, ch * 2) if mac else (cw, ch)
        except OSError:
            pass
        return (20, 40) if mac else (10, 20)

    def _query_cellpx(self, fd):
        """Ask the terminal for its cell pixel size: CSI 16t directly,
        else CSI 14t (window pixels) divided by the cell grid. Runs once
        at startup, inside raw mode."""
        try:
            sys.stdout.write("\x1b[16t\x1b[14t")
            sys.stdout.flush()
            buf = ""
            end = time.time() + 0.30
            win = None
            while time.time() < end:
                r, _, _ = select.select([sys.stdin], [], [], 0.05)
                if not r:
                    continue
                buf += os.read(fd, 128).decode("ascii", "ignore")
                m = re.search(r"\x1b\[6;(\d+);(\d+)t", buf)
                if m:
                    hh, ww = int(m.group(1)), int(m.group(2))
                    if ww > 1 and hh > 2:
                        self._cellpx_q = (ww, hh)
                        return
                m = re.search(r"\x1b\[4;(\d+);(\d+)t", buf)
                if m:
                    win = (int(m.group(2)), int(m.group(1)))
            if win:
                try:
                    r, c, _, _ = struct.unpack(
                        "HHHH",
                        fcntl.ioctl(1, termios.TIOCGWINSZ, b"\0" * 8))
                    if r and c and win[0] > c and win[1] > r:
                        self._cellpx_q = (max(2, win[0] // c),
                                          max(4, win[1] // r))
                except OSError:
                    pass
        except Exception:
            pass

    def _drop_kitty(self, idxf, Wpx, Hpx, W, rows, pad, bright, w):
        """Blit the field as a true RGB bitmap (kitty graphics protocol).
        The image is transmitted after the text frame; here we just leave
        a blank panel with a marker cell so render() knows where it goes."""
        import numpy as np
        lut = np.clip(self._drop_lut() * bright, 0, 255).astype(np.uint8)
        rgb = lut[idxf.astype(np.intp)]              # (Hpx, Wpx, 3)
        was_live = getattr(self, "_kitty_live", False)
        data = base64.standard_b64encode(
            zlib.compress(rgb.tobytes(), 1)).decode("ascii")
        new = 91 + (self._kitty_id ^ 1)              # double-buffer ids
        old = 91 + self._kitty_id
        self._kitty_id ^= 1
        head = (f"a=T,i={new},q=2,f=24,o=z,s={Wpx},v={Hpx},"
                f"c={W},r={rows},z=-1,C=1")
        out = []
        for o in range(0, len(data), 4096):
            chunk = data[o:o + 4096]
            m = 1 if o + 4096 < len(data) else 0
            out.append(f"\x1b_G{head},m={m};{chunk}\x1b\\" if o == 0
                       else f"\x1b_Gm={m};{chunk}\x1b\\")
        out.append(f"\x1b_Ga=d,d=i,i={old},q=2\x1b\\")
        out += self._kitty_art(W, rows, was_live)
        self._kitty_payload = "".join(out)
        self._kitty_live = True
        blank = " " * w
        first = " " * pad + KITTY_MARK + " " * (w - pad - 1)
        return [first] + [blank] * (rows - 1)

    def _kitty_art(self, W, rows, was_live):
        """The t-mode cover in pixel mode: its own kitty image at the
        art's native resolution, placed over the field. Baking it into
        the budget-scaled field bitmap smeared it into mush on big
        panels — this way the terminal scales the 512px source exactly
        once. Transmitted per track (id 93), re-placed per frame."""
        on = getattr(self, "viz_art", 0) and getattr(self, "now", None)
        # step aside while a heart splash plays — the cover sits at z≥0,
        # which draws over text, and the heart IS text
        splash = time.time() - getattr(self, "_like_t", 0) < 1.9
        a = self.art.rgb(getattr(self.now, "thumb", "")) \
            if on and not splash else None
        if a is None:
            if getattr(self, "_kitty_art_live", False):
                self._kitty_art_live = False
                if not splash:      # keep the shipped pixels for after
                    self._kitty_art_key = None
                return ["\x1b_Ga=d,d=i,i=93,q=2\x1b\\"]
            return []
        cw, ch = self._cell_px()
        side = max(1.0, 0.62 * min(W * cw, rows * ch))
        ac = max(1, min(W, round(side / cw)))
        ar = max(1, min(rows, round(side / ch)))
        out = []
        if getattr(self, "shader_guard", 0):
            import numpy as np
            a = np.maximum(a, np.array(self.SHADER_FLOOR, np.uint8))
        url = (self.now.thumb,
               self.SHADER_FLOOR if getattr(self, "shader_guard", 0) else None)
        if getattr(self, "_kitty_art_key", None) != url or not was_live:
            # new track (or the screen was wiped): ship the pixels once
            data = base64.standard_b64encode(
                zlib.compress(a.tobytes(), 1)).decode("ascii")
            out.append("\x1b_Ga=d,d=i,i=93,q=2\x1b\\")
            head = "a=t,i=93,q=2,f=24,o=z,s=512,v=512"
            for o in range(0, len(data), 4096):
                chunk = data[o:o + 4096]
                m = 1 if o + 4096 < len(data) else 0
                out.append(f"\x1b_G{head},m={m};{chunk}\x1b\\" if o == 0
                           else f"\x1b_Gm={m};{chunk}\x1b\\")
            self._kitty_art_key = url
        # place it centered — relative cursor moves from the panel
        # origin (the C=1 on the field transmit left the cursor there),
        # fixed placement id so each frame replaces, never stacks
        ro, co = (rows - ar) // 2, (W - ac) // 2
        mv = (f"\x1b[{ro}B" if ro else "") + (f"\x1b[{co}C" if co else "")
        out.append(mv + f"\x1b_Ga=p,i=93,p=1,q=2,c={ac},r={ar},z=0,C=1\x1b\\")
        self._kitty_art_live = True
        return out

    @property
    def SHADER_FLOOR(self):
        """The darkest color the guard lets through — tunable, because
        every wallpaper shader keys at a different cutoff (his new one
        eats anything under ~42% grey; the old fixed slab vanished)."""
        v = int(getattr(self, "guard_floor", 108))
        return (v, v + 4, v + 20)

    def _drop_lut(self):
        """64-entry palette: dark → primary → secondary → accent → white.
        In cover mode the gradient comes from the album art instead.
        shader guard scopes: 'art' leaves this transparent over the
        wallpaper (the look); 'full' ALSO lifts this LUT — for shaders
        whose cutoff swallows whole themes (red is luminance-poor, the
        ytm theme can vanish entirely)."""
        full = getattr(self, "shader_guard", 0) >= 2

        def g(lut):
            if not full:
                return lut
            import numpy as np
            return np.maximum(lut, np.array(self.SHADER_FLOOR, float))
        if getattr(self, "viz_style", "") == "cover":
            now = getattr(self, "now", None)
            art = getattr(self, "art", None)
            if now is not None and art is not None:
                pal = art.palette(getattr(now, "thumb", ""))
                if pal is not None:
                    return g(pal)
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
        return g(self._drop_lut_cache)

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
        # mids and highs get their own transient detectors, so vocal
        # swells and hi-hats hit visibly instead of everything hanging
        # off the kick drum: mids surge the flow, hats spark the detail
        rm, rt = raw[1], raw[2]
        mavg = getattr(self, "_drop_mid_avg", 0.15) * 0.985 + rm * 0.015
        tavg = getattr(self, "_drop_tre_avg", 0.12) * 0.985 + rt * 0.015
        self._drop_mid_avg, self._drop_tre_avg = mavg, tavg
        mlast = getattr(self, "_mid_t", 0.0)
        if rm > mavg * 1.5 + 0.05 and now - mlast > 0.18:
            self._mid_t = mlast = now
            self._mid_amp = min(1.0, 0.4 + (rm - mavg) * 2.2)
        mpul = getattr(self, "_mid_amp", 0.0) * math.exp(-(now - mlast) / 0.30)
        tlast = getattr(self, "_tre_t", 0.0)
        if rt > tavg * 1.55 + 0.04 and now - tlast > 0.09:
            self._tre_t = tlast = now
            self._tre_amp = min(1.0, 0.35 + (rt - tavg) * 2.5)
        tpul = getattr(self, "_tre_amp", 0.0) * math.exp(-(now - tlast) / 0.15)
        eb, em, et = self._drop_e
        en = getattr(self, "_drop_energy", 0.2)
        en += ((eb + em + et) / 3 - en) * min(1.0, dt * 1.2)
        self._drop_energy = en
        react = getattr(self, "viz_react", 1.0)
        self._mid_pulse = mpul * react
        self._tre_pulse = tpul * react
        b = min(1.4, 0.25 * eb + (0.85 * pulse + 0.18 * groove) * react)
        m = min(1.3, 0.45 * em + (0.25 * pulse + 0.50 * mpul) * react)
        tr = min(1.2, 0.35 * et + (0.60 * tpul + 0.12 * groove) * react)
        return pulse * react, en, b, m, tr

    def _viz_drop(self, w, rows, n, pad_l):
        """Milkdrop-ish plasma: interference field warped by bass/mid/treble,
        rendered as half-block pixels."""
        import numpy as np
        W = w if getattr(self, "eww_flush", False) else max(w - 4, 20)
        H = rows * 2
        mode = getattr(self, "drop_px", 0)
        if mode == 2 and (not getattr(self, "_kitty_ok", False)
                          or getattr(self, "menu", False)
                          or getattr(self, "help", False)):
            mode = 1     # overlays composite into text cells, not bitmaps
        # the heart splash does NOT kick pixel mode out: it stamps
        # fg-only half-blocks onto the blank marker lines — the empty
        # halves show the live bitmap through, and parsing blank lines
        # costs nothing (parsing a fullscreen of SGR-dense chunky text
        # every frame was what made the splash stutter)
        # chunky is supersampled 2× because its grid is so coarse that
        # radial cores alias into staircases — averaging 4 subsamples
        # per pixel costs nothing at that size
        ss = 2 if mode == 0 else 1
        if mode == 2:
            # pixel: real screen pixels, with an adaptive budget — spend
            # resolution until the frame cost eats the tick, back off when
            # the encode (or a slow terminal) pushes back. the terminal
            # scales the bitmap to the panel either way
            ss = 1
            cw, chh = self._cell_px()
            nat_w, nat_h = W * cw, rows * chh
            b = getattr(self, "_px_budget", 280_000.0)
            cost = getattr(self, "_frame_cost", 0.0)
            if cost > 0.020:
                b = max(140_000.0, b * 0.92)
            elif cost < 0.014:
                b = min(520_000.0, b * 1.04)
            self._px_budget = b
            sc = min(1.0, math.sqrt(b / max(1, nat_w * nat_h)))
            Wpx, Hpx = max(64, int(nat_w * sc)), max(64, int(nat_h * sc))
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
            (0.55 + 1.1 * en + 1.6 * ps
             + 0.7 * getattr(self, "_mid_pulse", 0.0))
        t = self._drop_t

        # aspect-correct coordinates: half-block pixels are ~square, so the
        # field stops stretching into smears on wide panels (rings stay round)
        aspect = min(W / max(H, 1), 4.5)   # cap so wide strips stay coherent
        zoom = 1.15 / (1.0 + 0.16 * ps)    # camera swells in on the kick
        # sample budget: maximized terminals ask for ~10× the pixels of the
        # side panel — compute the field at a capped resolution and stretch
        if mode == 2:
            # field res pinned to the panel's native size, NOT the
            # adaptive output budget — otherwise every budget step would
            # change the field shape and reset the temporal ease
            fs = max(1.0, math.sqrt(nat_w * nat_h / 140_000))
            fw, fh = max(8, int(nat_w / fs)), max(8, int(nat_h / fs))
        else:
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
            pool = getattr(self, "_drop_pool", None) or range(self.N_PRESETS)
            self._drop_preset = random.choice(
                [i for i in pool if i != self._drop_preset])
            self._drop_pa = self._drop_new_params(self._drop_preset)
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
        # brightness: slow mood bed + a lift on the kick, plus a quick
        # sparkle when the hats tick — alive in the highs, no strobing
        bright = min(1.05, 0.30 + 0.50 * min(1.0, en * 1.5) + 0.26 * ps
                     + 0.14 * getattr(self, "_tre_pulse", 0.0))
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
        if mode == 2:
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

        # hi-def: pack each 2×2 block into a quadrant glyph split on
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
        """Centered horizontal bars, one frequency band per row, bass low.
        Falling peak ticks ride the ends, the kick flashes the stack."""
        self._viz_physics(self._viz_targets(rows))
        ps = self._viz_pulse()
        md = getattr(self, "_mid_pulse", 0.0)
        tp = getattr(self, "_tre_pulse", 0.0)
        glow = min(0.45, 0.34 * ps)
        cap_c = fg(lerp(lerp(WHITE, PINK, 0.35), WHITE, min(0.9, tp)))
        lines = []
        for r in range(rows):
            i = rows - 1 - r                        # bass at the bottom
            v = self.bars[i]
            width = int(v * n) or (1 if v > 0.02 else 0)
            col = lerp(self._viz_color(i / max(rows - 1, 1)),
                       PINK, min(0.5, 0.35 * md))
            col = lerp(col, WHITE, glow)
            pk = int(self.peaks[i] * n)
            cells = [" "] * n
            lp = (n - width) // 2
            for x in range(lp, lp + width):
                cells[x] = fg(col) + "▆"
            if pk > width + 1:                      # peak ticks, both ends
                pl = (n - pk) // 2
                if 0 <= pl < n and cells[pl] == " ":
                    cells[pl] = cap_c + "▏"
                pr = pl + pk - 1
                if 0 <= pr < n and cells[pr] == " ":
                    cells[pr] = cap_c + "▕"
            lines.append(crop_pad(
                " " * pad_l + "".join(cells) + RESET, w))
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
            flags.append(fg(ORANGE)
                         + ("⟲ repeat" if self.repeat == 1 else "⟳ loop")
                         + RESET)
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
            keys = [("?", "keys"), ("F", "exit"), ("M", "tune"), ("v", "viz"),
                    ("c", "theme"), ("p", "px"), ("t", "art"), ("spc", "pause"),
                    ("n/b", "skip"), ("L", "like"), ("±", "vol"),
                    ("q", "quit")]
        elif self.full:
            keys = [("?", "keys"), ("f", "exit full"), ("spc", "pause"),
                    ("n/b", "skip"),
                    ("v", "viz"), ("c", "theme"), ("p", "hd"),
                    ("w", "work"), ("±", "vol"), ("q", "quit")]
        else:
            keys = [("?", "keys"), ("/", "find"), ("↵", "play"),
                    ("spc", "pause"),
                    ("n/b", "skip"), ("R", "mix"), ("q", "quit"), ("f", "full"),
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
            if used + len(piece) + (2 if k == "?" else 0) > w - 1:
                break
            if k == "?":
                # the door to every other command — render it as a chip
                # so it can't fade into the hint soup
                hint = (bg(RED) + BOLD + fg(WHITE) + " ? " + RESET +
                        fg(GREY) + " " + v)
                used += 2
            else:
                hint = fg(RED) + k + fg(DGREY) + " " + v
            line += ("" if i == 0 else fg(DGREY) + " · ") + hint
            used += len(piece)
        return crop_pad(line + RESET, w)

    def _inhibit_sync(self):
        """While music plays (and keep awake is on), hold a sleep/lock
        inhibitor: systemd-inhibit on linux (shows up in inhibitor
        widgets, blocks suspend + idle lock), caffeinate on macOS.
        Released the moment playback pauses or stops."""
        playing = bool(self.now) and self.keep_awake \
            and not self.player.props.get("pause")
        p = self._inhibit_p
        if p is not None and p.poll() is not None:
            self._inhibit_p = p = None       # child died on its own
        if playing and p is None:
            cmd = None
            if sys.platform == "darwin":
                cmd = ["caffeinate", "-d", "-i"]
            elif shutil.which("systemd-inhibit"):
                cmd = ["systemd-inhibit", "--what=sleep:idle",
                       "--who=NOCTURNE", "--why=music is playing",
                       "--mode=block", "sleep", "infinity"]
            if cmd:
                try:
                    self._inhibit_p = subprocess.Popen(
                        cmd, stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL)
                except OSError:
                    self.keep_awake = 0      # don't retry every frame
        elif not playing and p is not None:
            try:
                p.kill()
            except OSError:
                pass
            self._inhibit_p = None

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
            self._query_cellpx(fd)
            while self.running:
                if self.eof_flag.is_set():
                    self.eof_flag.clear()
                    # loop track replays on the NATURAL end only —
                    # pressing n still skips ahead
                    if self.repeat == 2 and self.now:
                        self.play_queue(self.qpos)
                    else:
                        self.next_track()
                if self.now and time.time() - self._now_wt > 2:
                    self._now_wt = time.time()
                    self._write_now()
                # live lists: the visible playlist/library quietly
                # re-pulls so edits from other devices appear on their own
                self._inhibit_sync()
                if self.authed and \
                        time.time() - getattr(self, "_live_t", 0.0) > 45:
                    self._live_t = time.time()
                    if self.tab == 2 and self.pl_open:
                        self._pl_refresh()
                    elif self.tab == 1 and self._lib_fetched:
                        self._lib_refresh()
                # the plasma runs flat out (~125 fps tick, render cost is
                # the real ceiling), every other style at 60; idle screen
                # is lazier
                if self.wolf is not None:
                    tick = 0.012   # the hidden floor is a game — keep it hot
                elif self.now or self.viz_max:
                    tick = (0.008 if self.viz_style in ("drop", "cover")
                            else 0.016)
                elif self.input_mode or self.help:
                    tick = 0.04   # the help box's flames keep dancing
                else:
                    tick = 0.12
                # a heart splash is alive: full frame rate no matter what
                # was on screen (unliking from a list while idle would
                # otherwise animate at the lazy 8fps idle tick)
                if time.time() - self._like_t < 1.9:
                    tick = min(tick, 0.008)
                # while keys are streaming (held scroll), outpace the
                # keyboard repeat rate so every frame handles exactly one
                # key — smooth motion AND nothing left to backlog
                if time.time() - getattr(self, "_key_t", 0.0) < 0.4:
                    tick = min(tick, 0.012)
                # constant cadence: sleep what's left of the tick after
                # the last render, so frame intervals don't see-saw
                # between tick and tick+render — that wobble reads as
                # stutter on fast monitors
                k = self.read_key(
                    max(0.002, tick - getattr(self, "_frame_cost", 0.0)))
                drained = 0
                while k and self.running:
                    self.handle_key(k)
                    self._key_t = time.time()
                    # drain anything already queued before drawing —
                    # held keys repeating faster than a frame renders
                    # would otherwise replay as ghost scrolling later
                    drained += 1
                    k = self.read_key(0) if drained < 64 else None
                t0 = time.perf_counter()
                self.render()
                self._frame_cost = time.perf_counter() - t0
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
            if self._kitty_ok:
                sys.stdout.write("\x1b_Ga=d,d=A,q=2\x1b\\")
            sys.stdout.write("\x1b[?25h\x1b[?1049l")
            sys.stdout.flush()
            self._save_state()
            self.now = None
            if self._inhibit_p is not None:
                try:
                    self._inhibit_p.kill()
                except OSError:
                    pass
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
    # the widget plays a curated pixel-friendly mix, livelier than the
    # app default: faster flow and quicker morphs suit a 5fps canvas
    v._drop_pool = WIDGET_PRESETS
    v.viz_speed = 1.6
    v.viz_morph = 1.4
    v._drop_preset = random.choice(WIDGET_PRESETS)
    v._drop_prev = v._drop_preset
    v._drop_pa = v._drop_new_params(v._drop_preset)
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


def _mac_default_output(raw=None):
    """Name of the device macOS is currently sending output to."""
    try:
        if raw is None:
            raw = subprocess.run(
                ["system_profiler", "SPAudioDataType", "-json"],
                capture_output=True, text=True, timeout=10).stdout
        data = json.loads(raw)
        for grp in data.get("SPAudioDataType", []):
            for it in grp.get("_items", []):
                if it.get("coreaudio_default_audio_output_device") \
                        == "spaudio_yes":
                    return it.get("_name", "")
    except Exception:
        pass
    return ""


def _mac_capture_err(idx, err=None):
    """Try a half-second capture and read the refusal, if any."""
    try:
        if err is None:
            p = subprocess.run(
                ["ffmpeg", "-hide_banner", "-f", "avfoundation",
                 "-i", f":{idx}", "-t", "0.5", "-f", "null", "-"],
                capture_output=True, text=True, timeout=12)
            err = p.stderr or ""
        low = err.lower()
        if "not permitted" in low or "permission" in low \
                or "declined" in low or "tcc" in low:
            return ("microphone permission denied — System Settings → "
                    "Privacy & Security → Microphone → enable your terminal")
    except Exception:
        pass
    return ""


def _mac_audio_report():
    """macOS: don't guess — test each link of the BlackHole chain and
    name the one that's broken."""
    idx = SpectrumTap._coreaudio_loopback()
    if idx is None:
        print("    ✗ no loopback device — brew install blackhole-2ch, "
              "then make a Multi-Output Device (docs/INSTALL-MACOS.md)")
        return
    print(f"    ✓ loopback device installed (avfoundation :{idx})")
    out_dev = _mac_default_output()
    if out_dev:
        low = out_dev.lower()
        if "blackhole" in low or "soundflower" in low or "loopback" in low:
            print(f"    ✗ output is “{out_dev}” DIRECTLY — you'd hear "
                  "nothing; select the Multi-Output Device instead")
        elif "multi" in low or "aggregate" in low:
            print(f"    ✓ output routed through “{out_dev}”")
        else:
            print(f"    ✗ output is “{out_dev}” — BlackHole isn't being "
                  "fed; pick the Multi-Output Device (Audio MIDI Setup)")
    perm = _mac_capture_err(idx)
    if perm:
        print("    ✗ " + perm)
    elif out_dev and ("multi" in out_dev.lower()
                      or "aggregate" in out_dev.lower()):
        print("    → chain looks right — if it still reads silent, "
              "is music actually playing?")


def doctor():
    ok = True
    for tool in ("mpv", "yt-dlp", "ffmpeg"):
        path = shutil.which(tool)
        print(f"  {'✓' if path else '✗'} {tool:8s} {path or 'MISSING'}")
        ok = ok and bool(path)
    for mod in ("ytmusicapi", "PIL", "yt_dlp"):
        try:
            __import__(mod)
            print(f"  ✓ python:{mod}")
        except ImportError:
            print(f"  ✗ python:{mod} MISSING")
            ok = False
    print(f"  {'✓' if sc_token() else '○'} soundcloud "
          + ("signed in" if sc_token()
             else "guest (search/play works; ytm --sc-login for likes)"))
    # is the visualizer hearing REAL audio, or freewheeling?
    print("  … listening for 3s — have music playing NOW to test reactivity")
    tap = SpectrumTap()
    t0 = time.time()
    heard = False
    while time.time() - t0 < 3.0:
        time.sleep(0.2)
        if tap.producing:
            heard = True
            break
    lv = max(tap.levels(8) or [0.0])
    tap.stop()
    if heard:
        print(f"  ✓ audio capture LIVE (level {lv:.2f}) — the visualizers "
              "are locked to the real audio")
    elif tap.alive:
        print("  ○ capture is open but hears SILENCE — the visualizers are "
              "freewheeling on synthetic motion")
        if sys.platform == "darwin":
            _mac_audio_report()
        else:
            print("    → is music actually playing right now?")
    else:
        print("  ○ no audio capture — the visualizers freewheel on "
              "synthetic motion")
        if sys.platform == "darwin":
            _mac_audio_report()
        else:
            print("    → needs pulseaudio/pipewire (parec) on PATH")
    term = os.environ.get("TERM", "?")
    prog = os.environ.get("TERM_PROGRAM", "")
    gfx = App._kitty_sniff()
    print(f"  {'✓' if gfx else '○'} terminal {term}"
          + (f" ({prog})" if prog else "")
          + ("" if gfx else " — no kitty graphics, pixel mode off"))
    if not gfx:
        hint = ("brew install --cask ghostty"
                if sys.platform == "darwin" else "ghostty, kitty or wezterm")
        print(f"    → for the true-pixel renderer: {hint}, then run ytm "
              "inside it")
    try:
        r, c, xp, yp = struct.unpack(
            "HHHH", fcntl.ioctl(1, termios.TIOCGWINSZ, b"\0" * 8))
        cell = f"{xp // c}x{yp // r}px/cell" if (xp and yp) else "unreported"
        print(f"  · TIOCGWINSZ: {c}x{r} cells, {cell}")
    except OSError:
        pass
    if sys.stdin.isatty() and sys.stdout.isatty():
        fdd = sys.stdin.fileno()
        old_t = termios.tcgetattr(fdd)
        try:
            tty.setcbreak(fdd)
            sys.stdout.write("\x1b[16t\x1b[14t")
            sys.stdout.flush()
            buf, end = "", time.time() + 0.35
            got = False
            while time.time() < end:
                rr, _, _ = select.select([sys.stdin], [], [], 0.05)
                if rr:
                    buf += os.read(fdd, 128).decode("ascii", "ignore")
                    m = re.search(r"\x1b\[6;(\d+);(\d+)t", buf)
                    if m:
                        print(f"  · terminal reports {m.group(2)}x"
                              f"{m.group(1)}px/cell (CSI 16t) — "
                              "pixel mode uses this")
                        got = True
                        break
            if not got:
                m = re.search(r"\x1b\[4;(\d+);(\d+)t", buf)
                if m:
                    print(f"  · window is {m.group(2)}x{m.group(1)}px "
                          "(CSI 14t) — pixel mode divides by the grid")
                elif sys.platform == "darwin":
                    print("  · no size replies — assuming Retina (2x "
                          "TIOCGWINSZ)")
                else:
                    print("  · no size replies — pixel mode falls back "
                          "to TIOCGWINSZ")
        finally:
            termios.tcsetattr(fdd, termios.TCSADRAIN, old_t)
    if os.path.isfile(OAUTH_FILE):
        print(f"  {'✓' if verify_auth() else '✗'} oauth ({OAUTH_FILE})")
    elif os.path.isfile(AUTH_FILE):
        print(f"  {'✓' if verify_auth() else '✗'} auth ({AUTH_FILE})")
    else:
        print("  ○ no auth file — guest mode (search/play only)")
    return ok


def version_string():
    here = os.path.dirname(os.path.abspath(__file__))
    try:
        r = subprocess.run(
            ["git", "-C", here, "log", "-1", "--format=%h %cs"],
            capture_output=True, text=True, timeout=5)
        if r.returncode == 0 and r.stdout.strip():
            sha, date = r.stdout.split()
            return f"ytm {sha} ({date})"
    except Exception:
        pass
    return "ytm (unknown build — not a git checkout)"


def self_update():
    """ytm update — pull the latest code and refresh the fragile deps."""
    here = os.path.dirname(os.path.abspath(__file__))
    if not os.path.isdir(os.path.join(here, ".git")):
        print("✗ this install isn't a git checkout — reinstall via the")
        print("  README (or the macOS one-liner) to get updates")
        return False
    print("→ pulling latest ytm")
    r = subprocess.run(["git", "-C", here, "pull", "--ff-only"],
                       capture_output=True, text=True)
    out = (r.stdout + r.stderr).strip()
    print("  " + out.replace("\n", "\n  "))
    if r.returncode != 0:
        return False
    print("→ refreshing yt-dlp + ytmusicapi (stale yt-dlp = broken playback)")
    subprocess.run([sys.executable, "-m", "pip", "install", "-q",
                    "--upgrade", "yt-dlp", "ytmusicapi"], check=False)
    # updaters from the YT MUSIC era get the new command too
    ytm_l = os.path.join(os.path.expanduser("~"), ".local", "bin", "ytm")
    noct = os.path.join(os.path.dirname(ytm_l), "nocturne")
    if os.path.isfile(ytm_l) and not os.path.exists(noct):
        try:
            os.symlink(ytm_l, noct)
            print("→ new command installed: nocturne")
        except OSError:
            pass
    print("✓ up to date — restart   [" + version_string() + "]")
    return True


def respawn_pure():
    """Launched inside a shader-rigged ghostty? Open nocturne in its own
    clean window — shader cleared, opaque true-black background, the
    rest of the user's config untouched — and give this shell its
    prompt back. NOTHING is killed: the new window is an independent
    process and every existing terminal stays exactly as it was."""
    if os.environ.get("NOCTURNE_PURE") == "1":
        return False                      # already in the clean window
    if not shader_active() or not shutil.which("ghostty"):
        return False
    if not (os.environ.get("WAYLAND_DISPLAY")
            or os.environ.get("DISPLAY")):
        return False
    if not sys.stdout.isatty():
        return False
    try:
        subprocess.Popen(
            ["ghostty", "--gtk-single-instance=false",
             "--custom-shader=",
             "--background=#0a0a10", "--background-opacity=1",
             "--background-blur=0", "--title=NOCTURNE",
             "-e", sys.executable, os.path.abspath(__file__)],
            env=dict(os.environ, NOCTURNE_PURE="1"),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True)
    except Exception:
        return False
    print("☾ nocturne opens its own window — no shader, true black.")
    print("  (NOCTURNE_PURE=1 nocturne runs it right here instead)")
    return True


def rename_notice():
    """One-time, for everyone arriving from the YT MUSIC era: the new
    name announced in their own terminal, in the house gradient."""
    marker = os.path.join(CONFIG_DIR, ".nocturne")
    if os.path.isfile(marker) or not sys.stdout.isatty() \
            or not sys.stdin.isatty():
        return
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(marker, "w") as f:
            f.write("the night knows its name\n")
    except OSError:
        return
    dim = fg((110, 110, 145))
    print()
    print(dim + "    ✦ ·  ˚      *           ˚     ·      ✦      ˚" + RESET)
    print(dim + "          this player has grown into a name:" + RESET)
    print()
    for i, row in enumerate(LOGO):
        print("    " + grad_text(row, lerp(RED, PINK, i / 2),
                                 lerp(ORANGE, RED, i / 2)))
    print()
    print(f"    {BOLD}{fg(WHITE)}NOCTURNE{RESET}"
          f"{fg(GREY)} — night music in the terminal{RESET}")
    print(f"    {fg(GREY)}yt music + soundcloud · same app, same config,"
          f" same keys{RESET}")
    print(f"    new command: {fg(ORANGE)}{BOLD}nocturne{RESET}"
          f"{fg(GREY)}   (ytm works forever){RESET}")
    print(dim + "      ˚     ·       ✦         ·        ˚    ✦" + RESET)
    print()
    try:
        input(dim + "    press enter to head into the night " + RESET)
    except (EOFError, KeyboardInterrupt):
        print()


def uninstall():
    """ytm uninstall — remove the launcher, this checkout, and (if you
    say so) your sign-in and settings."""
    here = os.path.dirname(os.path.abspath(__file__))
    launcher = os.path.join(os.path.expanduser("~"), ".local", "bin", "ytm")
    # only ever delete something that actually looks like a ytm checkout
    sane = (os.path.isfile(os.path.join(here, "ytm.py"))
            and (os.path.isdir(os.path.join(here, ".git"))
                 or os.path.isfile(os.path.join(here, "install.sh")))
            and here not in ("/", os.path.expanduser("~")))
    print("this removes:")
    if sane:
        print(f"  • the install: {here}")
    print(f"  • the launcher: {launcher}")
    print(f"  • NOT your config/sign-in ({CONFIG_DIR}) unless you say so")
    try:
        if input("\nremove ytm? [y/N] ").strip().lower() != "y":
            print("kept everything.")
            return True
        wipe_cfg = input(
            f"also delete config + sign-in ({CONFIG_DIR})? [y/N] "
        ).strip().lower() == "y"
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    if os.path.isfile(launcher):
        try:
            with open(launcher) as f:
                if "ytm" in f.read():
                    os.unlink(launcher)
                    print(f"✓ removed {launcher}")
        except OSError as e:
            print(f"✗ launcher: {e}")
    twin = os.path.join(os.path.dirname(launcher), "nocturne")
    if os.path.islink(twin) or os.path.isfile(twin):
        try:
            os.unlink(twin)
            print(f"✓ removed {twin}")
        except OSError as e:
            print(f"✗ nocturne launcher: {e}")
    if wipe_cfg and os.path.isdir(CONFIG_DIR):
        shutil.rmtree(CONFIG_DIR, ignore_errors=True)
        shutil.rmtree(CACHE_DIR, ignore_errors=True)
        print(f"✓ removed {CONFIG_DIR} (sign-in included)")
    elif os.path.isdir(CONFIG_DIR):
        print(f"○ kept {CONFIG_DIR} — sign-in survives a reinstall")
    if sane:
        shutil.rmtree(here, ignore_errors=True)
        print(f"✓ removed {here}")
    print("gone. thanks for listening ♪")
    return True


def setup_wizard():
    """ytm setup — the whole rig in one pass: dependency check, sign-in,
    then the taste questions (theme, visualizer, search style)."""
    print(BOLD + "ytm setup" + RESET + "\n")
    print("dependencies:")
    if not doctor():
        hint = ("brew install mpv yt-dlp ffmpeg"
                if sys.platform == "darwin"
                else "sudo pacman -S mpv yt-dlp ffmpeg")
        print(f"\n  fix the ✗ lines first ({hint}), then rerun ytm setup.")
        return False
    if sys.platform == "darwin" and not SpectrumTap._coreaudio_loopback():
        print("  ○ no loopback audio device — the visualizer will run on")
        print("    decorative motion. for the real FFT: brew install")
        print("    blackhole-2ch, then docs/INSTALL-MACOS.md")
    print()
    if auth_present() and verify_auth():
        print("✓ already signed in")
        if input("  sign in again anyway? [y/N] ").strip().lower() == "y":
            login_wizard()
    elif input("sign in to your account now? [Y/n] "
               ).strip().lower() != "n":
        login_wizard()
    state = {}
    try:
        with open(STATE_FILE) as f:
            state = json.load(f)
    except Exception:
        pass
    names = [t[0] for t in THEMES]
    print()
    for i, (name, p, s, a) in enumerate(THEMES):
        sw = []
        for x in range(26):
            f = x / 25
            c = lerp(p, s, f * 2) if f < 0.5 else lerp(s, a, (f - 0.5) * 2)
            sw.append(fg(c) + "█")
        print(f"  {BOLD}{i + 1}{RESET}  {name:<10s} " + "".join(sw) + RESET)
    pick = input(f"\npick a theme [1-{len(names)}, enter keeps "
                 f"'{state.get('theme', names[0])}']: ").strip()
    if pick.isdigit() and 1 <= int(pick) <= len(names):
        state["theme"] = names[int(pick) - 1]
    state["viz"] = ("cover" if input(
        "visualizer palette — theme colors [Y] or album-art colors [c]? "
    ).strip().lower() == "c" else "drop")
    kitty = App._kitty_sniff()
    state["drop_px"] = 2 if kitty else 1
    print("  pixel quality auto-set: " +
          ("pixel — true bitmaps, your terminal speaks kitty graphics"
           if kitty else "hi-def quadrants"))
    state["rich_search"] = 0 if input(
        "rich search results with thumbnails? [Y/n] "
    ).strip().lower() == "n" else 1
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)
    print("\n✓ all set — run: " + BOLD + "ytm" + RESET)
    return True


def main():
    ap = argparse.ArgumentParser(prog="ytm", description=__doc__)
    ap.add_argument("cmd", nargs="?",
                    choices=["setup", "update", "uninstall"],
                    help="ytm setup — guided first-run setup; "
                         "ytm update — pull the latest version; "
                         "ytm uninstall — remove ytm cleanly")
    ap.add_argument("--login", action="store_true",
                    help="interactive sign-in wizard (pick your browser)")
    ap.add_argument("--setup", action="store_true",
                    help="same as: ytm setup")
    ap.add_argument("--auth", action="store_true",
                    help="sign in by pasting browser request headers")
    ap.add_argument("--oauth", action="store_true",
                    help="sign in via Google OAuth device flow — no "
                         "browser cookies, YouTube-scoped, revocable")
    ap.add_argument("--sc-login", action="store_true",
                    help="sign in to soundcloud (lifts the session from "
                         "your browser — likes land in the library)")
    ap.add_argument("--auth-firefox", action="store_true",
                    help="import session from Firefox cookies")
    ap.add_argument("--auth-browser", metavar="NAME",
                    help="import session from a browser non-interactively "
                         "(chrome, brave, edge, chromium, vivaldi, opera)")
    ap.add_argument("--version", action="store_true",
                    help="print the installed version (git commit) and exit")
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
    ap.add_argument("--wolf", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--pulse", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args()

    if args.version:
        print(version_string())
        sys.exit(0)
    if args.wolf:
        sys.exit(wolf_main())
    if args.pulse:
        sys.exit(wolf_pulse_main())
    if args.cmd == "setup" or args.setup:
        sys.exit(0 if setup_wizard() else 1)
    if args.cmd == "update":
        sys.exit(0 if self_update() else 1)
    if args.cmd == "uninstall":
        sys.exit(0 if uninstall() else 1)
    if args.doctor:
        sys.exit(0 if doctor() else 1)
    if args.eww:
        sys.exit(0 if eww_stream(args.eww, args.eww_style, args.eww_fps,
                                 args.eww_theme, args.eww_frame) else 1)
    if args.login:
        sys.exit(0 if login_wizard() else 1)
    if args.sc_login:
        sys.exit(0 if sc_login() else 1)
    if args.oauth:
        sys.exit(0 if oauth_login() else 1)
    if args.auth:
        sys.exit(0 if paste_headers_auth() else 1)
    if args.auth_firefox:
        sys.exit(0 if import_firefox_auth() else 1)
    if args.auth_browser:
        sys.exit(0 if import_browser_auth(args.auth_browser) else 1)

    if not sys.stdin.isatty():
        print("ytm needs a TTY. Run it in a terminal.")
        sys.exit(1)
    if not auth_present() and sys.stdout.isatty():
        print("  no account linked yet — search and playback still work.")
        print("  to get your library/likes/playlists:  ytm --login")
        print("  starting in guest mode in 3s…")
        time.sleep(3)
    if respawn_pure():
        sys.exit(0)
    rename_notice()
    App(ao=args.ao).run()


if __name__ == "__main__":
    main()
