#!/usr/bin/env python3
"""Force-align known lyrics to a track's ACTUAL audio → an LRC timed to that
exact recording. Transcribes the audio with faster-whisper (word timestamps),
then snaps the correct lyric lines onto the transcription's timing — so even a
non-official upload (slowed, extended, padded) syncs perfectly.

Invoked by nocturne's `G` key via the nocturne-align wrapper (which puts the
voice-type venv's CUDA libs on LD_LIBRARY_PATH). Stages print to stderr so the
caller can surface progress; the finished LRC is written to --out.
"""
import argparse
import difflib
import os
import re
import subprocess
import sys
import tempfile


def log(msg):
    print(msg, file=sys.stderr, flush=True)


def fetch_audio(src, dst_dir):
    """yt-dlp + ffmpeg → a real 16kHz mono WAV at <dst_dir>/audio.wav.
    Returns the wav path or None. Extracting to a FIXED .wav avoids the
    'yt-dlp picks the container extension' trap that fed Whisper a path
    that didn't exist."""
    url = src if src.startswith("http") else \
        f"https://www.youtube.com/watch?v={src}"
    tmpl = os.path.join(dst_dir, "audio.%(ext)s")
    wav = os.path.join(dst_dir, "audio.wav")
    base = ["yt-dlp", "-x", "--audio-format", "wav", "--no-playlist",
            "--postprocessor-args", "ffmpeg:-ac 1 -ar 16000", "-o", tmpl]
    log("⬇ fetching audio…")
    r = subprocess.run(base + [url], capture_output=True, text=True)
    if not os.path.exists(wav):
        # retry pulling cookies from firefox (age-gated / members-only)
        r = subprocess.run(
            base + ["--cookies-from-browser", "firefox", url],
            capture_output=True, text=True)
    if not os.path.exists(wav) or os.path.getsize(wav) < 10000:
        log("yt-dlp/ffmpeg failed:\n" + (r.stderr or "")[-600:])
        return None
    return wav


def transcribe(audio):
    """faster-whisper → [(word, start_sec)] for the whole track."""
    from faster_whisper import WhisperModel
    model_name = os.environ.get("VT_MODEL", "large-v3-turbo")
    device = os.environ.get("VT_DEVICE", "cuda")
    compute = os.environ.get("VT_COMPUTE", "float16")
    log(f"◌ loading {model_name} ({device}/{compute})…")
    try:
        model = WhisperModel(model_name, device=device, compute_type=compute)
    except Exception as e:
        log(f"cuda load failed ({e}); falling back to cpu/int8")
        model = WhisperModel(model_name, device="cpu", compute_type="int8")
    log("◍ transcribing audio (this is the slow part)…")
    # NO vad_filter: the Silero VAD treats sung-over-instrumental as
    # non-speech and drops the WHOLE track (0 segments). Music needs it off.
    segments, _ = model.transcribe(
        audio, language="en", word_timestamps=True, vad_filter=False,
        beam_size=1, condition_on_previous_text=False)
    words = []
    for seg in segments:
        for w in (seg.words or []):
            words.append((w.word, float(w.start)))
    return words


_NORM = re.compile(r"[^\w]")


def align(lyric_lines, asr_words):
    """Correct lyric lines + ASR (word,time) → [(sec, line)] for this audio."""
    known, owner = [], []
    for li, line in enumerate(lyric_lines):
        for w in re.findall(r"\w+", line.lower()):
            known.append(w)
            owner.append(li)
    asr_norm = [_NORM.sub("", w.lower()) for w, _ in asr_words]
    if not known or not asr_norm:
        return []
    sm = difflib.SequenceMatcher(None, known, asr_norm, autojunk=False)
    line_t = {}
    for a, b, size in sm.get_matching_blocks():
        for k in range(size):
            li = owner[a + k]
            t = asr_words[b + k][1]
            if li not in line_t or t < line_t[li]:
                line_t[li] = t
    n = len(lyric_lines)
    times = [line_t.get(i) for i in range(n)]
    anchors = [(i, t) for i, t in enumerate(times) if t is not None]
    if not anchors:
        return []
    out = []
    for i in range(n):
        if times[i] is not None:
            out.append(times[i])
            continue
        prev = max([a for a in anchors if a[0] < i], default=None,
                   key=lambda x: x[0])
        nxt = min([a for a in anchors if a[0] > i], default=None,
                  key=lambda x: x[0])
        if prev and nxt:
            f = (i - prev[0]) / (nxt[0] - prev[0])
            out.append(prev[1] + (nxt[1] - prev[1]) * f)
        elif prev:
            out.append(prev[1] + (i - prev[0]) * 2.0)
        else:
            out.append(max(0.0, nxt[1] - (nxt[0] - i) * 2.0))
    for i in range(1, n):
        out[i] = max(out[i], out[i - 1] + 0.05)
    return [(out[i], lyric_lines[i]) for i in range(n)]


def to_lrc(timed):
    lines = []
    for t, txt in timed:
        lines.append(f"[{int(t // 60):02d}:{t % 60:05.2f}]{txt}")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--audio", required=True)      # YT id or SC url
    ap.add_argument("--lyrics-file", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()

    with open(a.lyrics_file, encoding="utf-8") as f:
        lyric_lines = [ln.strip() for ln in f if ln.strip()]
    if len(lyric_lines) < 2:
        log("no lyrics text to align")
        return 2

    tmpdir = tempfile.mkdtemp(prefix="nocturne-align-")
    try:
        audio = fetch_audio(a.audio, tmpdir)
        if not audio:
            return 3
        words = transcribe(audio)
        if len(words) < 5:
            log("transcription too sparse to align")
            return 4
        log(f"⟁ aligning {len(lyric_lines)} lines to {len(words)} words…")
        timed = align(lyric_lines, words)
        if not timed:
            log("alignment found no anchors")
            return 5
        os.makedirs(os.path.dirname(a.out), exist_ok=True)
        with open(a.out, "w", encoding="utf-8") as f:
            f.write(to_lrc(timed))
        log(f"✓ wrote {len(timed)} synced lines → {a.out}")
        return 0
    finally:
        try:
            for fn in os.listdir(tmpdir):
                os.remove(os.path.join(tmpdir, fn))
            os.rmdir(tmpdir)
        except OSError:
            pass


if __name__ == "__main__":
    sys.exit(main())
