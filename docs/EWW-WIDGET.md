# putting the visualizer in an eww widget

`ytm --eww WxH` runs headless and streams visualizer frames as pango markup,
one frame per line — exactly what eww's `deflisten` wants. It taps the same
PipeWire monitor as the TUI, so it dances to whatever your system is playing
(any player, not just ytm). No mpv, no TTY.

```sh
ytm --eww 56x7 --eww-style drop --eww-theme ice --eww-fps 8
```

- `--eww-style` any of: drop, bars, mirror, scope, bands
- `--eww-theme` any of: ytm, synthwave, matrix, ocean, sunset, ice
- `--eww-fps` default 10 (keep it ≤10 for small CPU use; GTK re-parses the
  markup every frame)

## eww config

```lisp
(deflisten ytmviz :initial ""
  "~/.local/bin/ytm --eww 56x7 --eww-style drop --eww-theme ice --eww-fps 8")

(defwidget audio-field []
  (label :markup ytmviz :class "audio-field"))
```

```css
.audio-field {
  font-family: "JetBrainsMono Nerd Font Mono";
  font-size: 9px;
}
```

Size the `WxH` to your widget: columns × rows of characters at whatever font
size your CSS uses. 56x7 at 9px is roughly 310×90 px.

Notes:
- if nothing is playing (or the monitor can't be tapped) it switches to its
  built-in ambient animation, so the widget never looks frozen.
- `bars` and `bands` are the cheapest to render; `drop` is the prettiest.
- the stream exits cleanly when eww closes the pipe.
