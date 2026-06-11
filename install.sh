#!/bin/sh
# ytm installer — sets up the venv and a launcher, then walks you
# through signing in. run from the cloned repo: ./install.sh
set -e

here="$(cd "$(dirname "$0")" && pwd)"

missing=""
for t in mpv yt-dlp ffmpeg python3; do
    command -v "$t" >/dev/null 2>&1 || missing="$missing $t"
done
if [ -n "$missing" ]; then
    echo "missing:$missing"
    echo "install them first, e.g.:"
    echo "  arch:   sudo pacman -S$missing"
    echo "  debian: sudo apt install$missing"
    echo "  fedora: sudo dnf install$missing"
    echo "  macos:  brew install$missing"
    exit 1
fi

echo "→ creating venv"
python3 -m venv "$here/.venv"
"$here/.venv/bin/pip" install --quiet --upgrade ytmusicapi pillow numpy yt-dlp secretstorage

echo "→ installing launcher to ~/.local/bin/ytm"
mkdir -p "$HOME/.local/bin"
cat > "$HOME/.local/bin/ytm" <<EOF
#!/bin/sh
exec "$here/.venv/bin/python" "$here/ytm.py" "\$@"
EOF
chmod +x "$HOME/.local/bin/ytm"

case ":$PATH:" in
    *":$HOME/.local/bin:"*) ;;
    *) echo "note: add ~/.local/bin to your PATH" ;;
esac

echo
echo "done. sign in now? (you can also do it later with: ytm --login)"
printf "  [Y/n] "
read -r ans
case "$ans" in
    n|N) echo "ok — run 'ytm' to start in guest mode" ;;
    *) "$HOME/.local/bin/ytm" --login ;;
esac
