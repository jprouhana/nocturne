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
python -m venv .venv
.venv/bin/pip install ytmusicapi pillow numpy
```

I keep a little launcher at `~/.local/bin/ytm`:

```sh
#!/bin/sh
exec "$HOME/ytm-tui/.venv/bin/python" "$HOME/ytm-tui/ytm.py" "$@"
```

## signing in

If you use Firefox, log in to music.youtube.com and run

```sh
ytm --auth-firefox
```

and it pulls the session straight out of your cookie store (handles the XDG
profile path and the sqlite WAL, which is where fresh logins hide).

Any other browser: `ytm --auth`, then paste the request headers from a
network request on music.youtube.com (F12 → Network → filter "browse" →
copy request headers). Auth lands in `~/.config/ytm-tui/browser.json`,
chmod 600, never leaves your machine.

`ytm --doctor` checks that everything is wired up.

## keys

| key | | key | |
|-----|------------|-----|------------|
| `/` | search | `space` | play/pause |
| `enter` | play | `n` / `b` | next / prev |
| `j` `k` / arrows | move | `,` / `.` | seek 10s |
| `a` | add to queue | `+` / `-` | volume |
| `L` | like song | `m` | mute |
| `s` | shuffle queue | `r` | repeat |
| `v` | cycle visualizer | `C` | cycle color theme |
| `w` | work mode (no art) | `f` | fullscreen |
| `1`-`4` / `tab` | switch view | `x` | drop from queue |
| `esc` / `h` | back out of playlist | `q` | quit |

## notes

- the spectrum is 45 Hz to 11 kHz, log-spaced, with auto gain. bars have
  gravity and peak caps like cava. if there's no pulse/pipewire monitor to
  grab it falls back to a decorative wave.
- if playback suddenly breaks it's basically always yt-dlp being out of
  date. update it and try again.
- ytmusicapi 1.12.x sometimes chokes on filtered search for signed-in
  accounts (`KeyError: musicShelfRenderer`), so search falls back to
  unfiltered results when that happens. you might see an album or video
  mixed in, they play fine.
