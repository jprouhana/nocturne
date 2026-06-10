# ytm

YouTube Music in the terminal. mpv handles playback, [ytmusicapi](https://github.com/sigma67/ytmusicapi)
talks to your account, and the visualizer is a real FFT of whatever is coming
out of your speakers (it taps the PipeWire monitor with parec, so it reacts to
the actual audio, not a fake animation).

Album art gets rendered as truecolor half-blocks, search results start a radio
so the music keeps going, and your library/playlists/likes work once you're
signed in. Works fine without an account too, you just lose the library stuff.

## install

You need `mpv`, `yt-dlp` and `ffmpeg` on your system. On Arch:

```sh
sudo pacman -S mpv yt-dlp ffmpeg
```

Then:

```sh
git clone https://github.com/jprouhana/ytm-tui
cd ytm-tui
./install.sh
```

That sets up a venv, drops a `ytm` launcher into `~/.local/bin`, and offers
to sign you in.

## signing in

```sh
ytm --login
```

Pick the browser you're logged into music.youtube.com with and it lifts the
session out of its cookie store — firefox, chrome, chromium, brave, edge,
vivaldi and opera all work (the chromium-family cookie decryption is handled
by yt-dlp, keyring and all).

If that somehow fails there's a manual fallback: `ytm --auth`, then paste
the request headers from any network request on music.youtube.com
(F12 → Network → filter "browse" → copy request headers).

Auth lands in `~/.config/ytm-tui/browser.json`, chmod 600, and never leaves
your machine. Skipping sign-in is fine too — search and playback work in
guest mode.

`ytm --doctor` checks that everything is wired up.

## keys

| key | | key | |
|-----|------------|-----|------------|
| `/` | search | `space` | play/pause |
| `enter` | play | `n` / `b` | next / prev |
| `j` `k` / arrows | move | `,` / `.` | seek 10s |
| `a` | add to queue | `+` / `-` | volume |
| `A` | add track to a playlist | `N` | new playlist |
| `x` | remove (queue/liked/playlist) | `D``D` | delete playlist |
| `L` | like song | `m` | mute |
| `s` | shuffle queue | `r` | repeat |
| `v` | cycle visualizer | `c` | cycle color theme |
| `w` | work mode (no art) | `f` | fullscreen |
| `p` | hi-def plasma pixels | | |
| `1`-`4` / `tab` | switch view | `esc` / `h` | back out of playlist |
| `q` | quit | | |

## notes

- the spectrum is 45 Hz to 11 kHz, log-spaced, with auto gain. bars have
  gravity and peak caps like cava. if there's no pulse/pipewire monitor to
  grab it falls back to a decorative wave.
- the `drop` visualizer cycles through 20 field equations milkdrop-style,
  crossfading every 12–24s or early when the bass hits hard. `p` switches
  it from half-block pixels to quadrant rendering (double resolution).
- if playback suddenly breaks it's basically always yt-dlp being out of
  date. update it and try again.
- ytmusicapi 1.12.x sometimes chokes on filtered search for signed-in
  accounts (`KeyError: musicShelfRenderer`), so search falls back to
  unfiltered results when that happens. you might see an album or video
  mixed in, they play fine.
