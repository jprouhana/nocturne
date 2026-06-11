#!/bin/sh
# ytm — one-line macOS install:
#
#   curl -fsSL https://raw.githubusercontent.com/jprouhana/ytm-tui/main/install-macos.sh | sh
#
# Installs Homebrew if you don't have it, the three system deps, offers
# the BlackHole loopback driver (so the visualizer hears real audio),
# clones the repo to ~/.ytm-tui and runs the normal installer.
set -e

if [ "$(uname)" != "Darwin" ]; then
    echo "this bootstrap is for macOS — on linux see the README:"
    echo "  https://github.com/jprouhana/ytm-tui#install"
    exit 1
fi

# when piped through `curl | sh`, stdin is the script — prompts and the
# sign-in wizard need the real terminal
ask() {
    printf "%s" "$1"
    if [ -t 0 ]; then read -r ans; else read -r ans </dev/tty || ans=""; fi
}

if ! command -v brew >/dev/null 2>&1; then
    echo "→ Homebrew not found — installing it (you may be asked for"
    echo "  your password by Apple's developer-tools installer)"
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)" </dev/tty
    [ -x /opt/homebrew/bin/brew ] && eval "$(/opt/homebrew/bin/brew shellenv)"
    [ -x /usr/local/bin/brew ] && eval "$(/usr/local/bin/brew shellenv)"
fi

echo "→ installing mpv, yt-dlp, ffmpeg"
brew install mpv yt-dlp ffmpeg

ask "→ install BlackHole so the visualizer reacts to the real audio? [y/N] "
case "$ans" in
    y|Y)
        brew install blackhole-2ch
        echo
        echo "  one manual step (macOS won't let scripts do it):"
        echo "   Audio MIDI Setup → + → Create Multi-Output Device →"
        echo "   tick your speakers AND BlackHole 2ch → set as output."
        echo "  full walkthrough: docs/INSTALL-MACOS.md"
        echo
        ;;
    *) echo "  skipped — the visualizer falls back to decorative motion" ;;
esac

dir="${YTM_DIR:-$HOME/.ytm-tui}"
if [ -d "$dir/.git" ]; then
    echo "→ updating existing install in $dir"
    git -C "$dir" pull --ff-only
else
    echo "→ cloning to $dir"
    git clone --depth 1 https://github.com/jprouhana/ytm-tui "$dir"
fi

cd "$dir"
if [ -t 0 ]; then
    sh ./install.sh
else
    sh ./install.sh </dev/tty || printf 'n\n' | sh ./install.sh
fi

case ":$PATH:" in
    *":$HOME/.local/bin:"*) ;;
    *)
        echo
        echo "→ adding ~/.local/bin to PATH isn't automatic — run:"
        # shellcheck disable=SC2016
        echo '   echo '\''export PATH="$HOME/.local/bin:$PATH"'\'' >> ~/.zshrc && exec zsh'
        ;;
esac

echo
echo "all set — start with:  ytm"
echo "guided preferences:    ytm setup"
