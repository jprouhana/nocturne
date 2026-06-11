# NOCTURNE (ytm) on macOS

Everything works on a Mac — playback, your library, the visualizers,
even the true-pixel `drop` rendering if your terminal speaks the kitty
graphics protocol. Two things need a quick setup pass.

## install

The whole thing in one line (installs Homebrew if needed, the deps,
offers BlackHole, clones to `~/.ytm-tui`, sets up the launcher and
offers sign-in):

```sh
curl -fsSL https://raw.githubusercontent.com/jprouhana/nocturne/main/install-macos.sh | sh
```

Or by hand:

```sh
brew install mpv yt-dlp ffmpeg
git clone https://github.com/jprouhana/nocturne
cd ytm-tui
./install.sh
ytm setup
```

`ytm setup` checks dependencies, signs you in (Safari isn't supported —
use Firefox or any chromium-family browser you're logged into
music.youtube.com with), and walks the taste questions.

## the visualizer needs a loopback device

macOS has no "monitor" source like PipeWire, so the FFT can't hear your
speakers out of the box (you'll get a decorative wave instead of the
real spectrum). The fix is BlackHole, a free virtual audio driver:

```sh
brew install blackhole-2ch
```

Then route audio through it while still hearing it:

1. Open **Audio MIDI Setup** (it's in /Applications/Utilities)
2. `+` in the bottom left → **Create Multi-Output Device**
3. Tick both your speakers/headphones **and** BlackHole 2ch
4. Right-click the multi-output device → **Use This Device For Sound Output**

ytm finds BlackHole through ffmpeg's avfoundation backend automatically —
restart ytm and the bars snap to the actual audio.

*(Headphone/device switching: macOS keeps the multi-output device as the
output, so unlike Linux there's nothing to re-pin.)*

## terminals

- **ghostty / kitty / WezTerm** — full experience, including the `pixel`
  quality mode (the field rendered as a true RGB bitmap).
- **iTerm2 / Terminal.app** — everything works, pixel quality tops out
  at hi-def quadrants (they don't speak the kitty graphics protocol).
