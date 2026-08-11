#!/usr/bin/env python3
"""
audiotap.py — Capture the system audio output on Linux (PipeWire /
PulseAudio monitor source) and automatically split it into one file per
track using the player's MPRIS metadata.

Requires: parec (libpulse), ffmpeg, playerctl
    sudo pacman -S libpulse ffmpeg playerctl        # Arch / Manjaro
    sudo apt install pulseaudio-utils ffmpeg playerctl   # Debian / Ubuntu
"""

import argparse
import array
import atexit
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time

# ----------------------------------------------------------------------------
# Audio parameters. 48 kHz matches the native rate of most sinks, avoiding
# an unnecessary resampling step in the server.
# ----------------------------------------------------------------------------
RATE = 48000
CHANNELS = 2
SAMPLE_BYTES = 2                      # s16le
FRAMES = 1024                         # ~21 ms per block
CHUNK = FRAMES * CHANNELS * SAMPLE_BYTES
BYTES_SEC = RATE * CHANNELS * SAMPLE_BYTES   # 192000

# ----------------------------------------------------------------------------
# Encoder settings per format. Each entry: (extension, ffmpeg codec flags).
# ----------------------------------------------------------------------------
FORMATS = {
    "flac": (".flac", ["-c:a", "flac", "-compression_level", "8"]),
    "mp3":  (".mp3",  ["-c:a", "libmp3lame", "-b:a", "192k"]),
    "opus": (".opus", ["-c:a", "libopus", "-b:a", "128k"]),
    "wav":  (".wav",  ["-c:a", "pcm_s16le"]),
}


# ============================================================================
# Helpers
# ============================================================================

def sanitize(name):
    """Clean a string so it becomes a valid filename.

    Titles from streaming services often contain /, |, :, emoji, etc. We
    keep letters (in any alphabet — \\w is Unicode-aware in Python 3),
    digits, spaces and a few safe punctuation marks.
    """
    clean = re.sub(r"[^\w\s\-.()\[\]']", "_", name, flags=re.UNICODE)
    clean = re.sub(r"\s+", " ", clean).strip(" .")
    return clean[:150] or "unknown"


def unique_path(path):
    """If the file already exists, append (2), (3)... so nothing is overwritten."""
    if not os.path.exists(path):
        return path
    stem, ext = os.path.splitext(path)
    n = 2
    while os.path.exists(f"{stem} ({n}){ext}"):
        n += 1
    return f"{stem} ({n}){ext}"


def check_dependencies():
    """Verify that the external tools we need are on PATH before starting."""
    missing = [t for t in ("parec", "ffmpeg", "playerctl") if not shutil.which(t)]
    if missing:
        print(f"[!] Missing tools: {', '.join(missing)}", file=sys.stderr)
        print("    Install with your package manager, e.g.:", file=sys.stderr)
        print("    sudo pacman -S libpulse ffmpeg playerctl", file=sys.stderr)
        sys.exit(1)


def run(cmd):
    """Run a command and return its stdout as a string (empty string on failure)."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        return r.stdout.strip()
    except (subprocess.SubprocessError, OSError):
        return ""


def check_volume(sink):
    """Warn if the sink's software volume is not at 100%.

    This matters: the monitor source sits after the mixer, so software
    volume affects the amplitude of the captured signal. Muted means you
    will capture pure silence.
    """
    mute = run(["pactl", "get-sink-mute", sink])
    vol = run(["pactl", "get-sink-volume", sink])

    if "yes" in mute.lower():
        print("[!] WARNING: the sink is MUTED — only silence will be recorded.")
        print("    pactl set-sink-mute @DEFAULT_SINK@ 0")
        return False

    percents = [int(p) for p in re.findall(r"(\d+)%", vol)]
    if percents and max(percents) < 100:
        print(f"[!] WARNING: software volume is at {max(percents)}%.")
        print("    The signal will be captured attenuated (dynamic range lost).")
        print("    Suggested: pactl set-sink-volume @DEFAULT_SINK@ 100%")
        print("    and control loudness from the speakers / amp instead.")
        return False

    return True


def rms_of(raw):
    """Approximate RMS in [0, 1], for a simple level meter.

    We sample every 8th value — accurate enough for a meter, and much
    cheaper than iterating over the entire block.
    """
    a = array.array("h")
    a.frombytes(raw)
    subset = a[::8]
    if not subset:
        return 0.0
    total = sum(v * v for v in subset)
    return (total / len(subset)) ** 0.5 / 32768.0


# ============================================================================
# Silent-capture helpers: null-sink + moving sink-inputs
# ============================================================================

NULL_SINK_NAME = "audiotap_silent"


def load_null_sink():
    """Load module-null-sink and return (module_id, sink_name).

    A null-sink is virtual: it accepts samples normally, exposes them on
    its monitor, but sends them nowhere. Anything routed here is captured
    but NOT heard on the speakers. It also does not suspend on idle like
    a real sink would.
    """
    out = run(["pactl", "load-module", "module-null-sink",
               f"sink_name={NULL_SINK_NAME}",
               "sink_properties=device.description=Audiotap-Silent-Capture"])
    if not out.isdigit():
        print(f"[!] Failed to load null-sink: {out}", file=sys.stderr)
        print("    If a stale one was left behind by a previous crash:",
              file=sys.stderr)
        print(f"    pactl list short modules | grep {NULL_SINK_NAME}",
              file=sys.stderr)
        print("    pactl unload-module <ID>", file=sys.stderr)
        sys.exit(1)
    return int(out), NULL_SINK_NAME


def unload_module(module_id):
    """Unload a module. Any streams routed to it automatically fall back to
    the default sink — no manual restoration needed."""
    if module_id is not None:
        subprocess.run(["pactl", "unload-module", str(module_id)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def list_sink_inputs():
    """Return a list of dicts, one per active sink-input.

    `pactl list sink-inputs` prints blocks that start with 'Sink Input #N'.
    We extract the ID and the application.name / process.binary from each.
    """
    out = run(["pactl", "list", "sink-inputs"])
    results = []
    for block in re.split(r"\n(?=Sink Input #)", out):
        m_id = re.search(r"Sink Input #(\d+)", block)
        m_app = re.search(r'application\.name = "([^"]+)"', block)
        m_bin = re.search(r'application\.process\.binary = "([^"]+)"', block)
        if m_id:
            results.append({
                "id": m_id.group(1),
                "app": (m_app.group(1) if m_app else "").lower(),
                "bin": (m_bin.group(1) if m_bin else "").lower(),
            })
    return results


def move_matching_sink_inputs(pattern, target_sink):
    """Move every sink-input matching `pattern` to `target_sink`.

    Matches case-insensitive substring against application.name OR
    process.binary — so it catches 'Firefox', 'firefox', 'firefox-esr', etc.
    """
    if not pattern:
        return 0
    pattern = pattern.lower()
    count = 0
    for si in list_sink_inputs():
        if pattern in si["app"] or pattern in si["bin"]:
            subprocess.run(["pactl", "move-sink-input", si["id"], target_sink],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            print(f"\r[+] Moved stream #{si['id']} "
                  f"({si['app'] or si['bin']}) -> {target_sink}")
            count += 1
    return count


def start_sink_input_watcher(pattern, target_sink):
    """Watch for new sink-inputs and move the ones that match.

    Useful if the player is opened AFTER the script starts, or if a browser
    tab is reloaded (creating a new stream).
    """
    if not pattern:
        return

    def worker():
        try:
            proc = subprocess.Popen(["pactl", "subscribe"],
                                    stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL, text=True)
        except OSError:
            return
        for line in proc.stdout:
            # Format: "Event 'new' on sink-input #123"
            if "'new'" in line and "sink-input" in line:
                # Give the server ~200ms to populate the stream's properties
                time.sleep(0.2)
                move_matching_sink_inputs(pattern, target_sink)

    threading.Thread(target=worker, daemon=True).start()


def check_disk_space(outdir, hours_planned=3):
    """Warn if the disk does not have enough room for the planned capture.

    Rough figures: FLAC ~5 MB/min, WAV ~11.5 MB/min. We take the worst case.
    """
    free = shutil.disk_usage(outdir).free
    needed = int(hours_planned * 60 * 11.5 * 1024 * 1024)   # WAV upper bound
    if free < needed:
        gb_free = free / (1024**3)
        gb_needed = needed / (1024**3)
        print(f"[!] Low free space: {gb_free:.1f} GB "
              f"(~{gb_needed:.1f} GB needed for {hours_planned}h of WAV).")


# ============================================================================
# Encoder: one ffmpeg process per track
# ============================================================================

class Encoder:
    """Wrap an ffmpeg process that receives raw PCM on stdin.

    Closing stdin acts as EOF: ffmpeg writes the container headers,
    finalises the file and exits. No intermediate .raw file or second pass
    is needed.
    """

    def __init__(self, path, codec_args, tags, min_bytes):
        self.path = path
        self.min_bytes = min_bytes
        self.written = 0

        meta = []
        for key, value in tags.items():
            if value:
                meta += ["-metadata", f"{key}={value}"]

        self.proc = subprocess.Popen(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                # --- INPUT description (goes before -i): raw PCM has no
                #     header, so we must declare the format explicitly ---
                "-f", "s16le", "-ar", str(RATE), "-ac", str(CHANNELS),
                "-i", "pipe:0",
                # --- OUTPUT ---
                *codec_args, *meta, path,
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def write(self, data):
        try:
            self.proc.stdin.write(data)
            self.written += len(data)
        except (BrokenPipeError, ValueError, OSError):
            pass          # ffmpeg died — don't take down the capture loop

    @property
    def seconds(self):
        return self.written / BYTES_SEC

    def close(self):
        """Close stdin and reap the process in a background thread.

        wait() takes tens of milliseconds. If we did it inline in the
        capture loop we would drop samples at the start of the next track.
        """
        try:
            self.proc.stdin.close()
        except (BrokenPipeError, ValueError, OSError):
            pass
        threading.Thread(target=self._reap, daemon=True).start()

    def _reap(self):
        self.proc.wait()
        name = os.path.basename(self.path)
        if self.written < self.min_bytes:
            # Too short: ad, jingle, or a pause/resume pseudo-event
            try:
                os.remove(self.path)
            except OSError:
                pass
            print(f"\r  x  {name}  ({self.seconds:.0f}s -- too short, deleted)")
        else:
            size_mb = (os.path.getsize(self.path) / 1e6
                       if os.path.exists(self.path) else 0)
            print(f"\r  ok {name}  ({self.seconds:.0f}s, {size_mb:.1f} MB)")


# ============================================================================
# MPRIS watcher
# ============================================================================

def start_metadata_watcher(player, out_queue):
    """Run `playerctl --follow` in a thread and push events onto a queue.

    --follow blocks until something changes, so it has to live in its own
    thread — otherwise it would freeze the capture loop.
    """
    cmd = ["playerctl"]
    if player:
        cmd += ["-p", player]
    cmd += [
        "--follow", "metadata",
        "--format",
        "{{artist}}\t{{title}}\t{{album}}\t{{xesam:trackNumber}}",
    ]

    def worker():
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL, text=True)
        except OSError as e:
            out_queue.put(("error", str(e)))
            return
        for line in proc.stdout:
            out_queue.put(("meta", line.rstrip("\n")))
        out_queue.put(("error", "playerctl terminated"))

    threading.Thread(target=worker, daemon=True).start()


def parse_meta(line):
    """Turn a playerctl output line into a dict, or None if it's empty."""
    parts = line.split("\t")
    while len(parts) < 4:
        parts.append("")
    artist, title, album, tracknum = (p.strip() for p in parts[:4])
    if not (artist or title):
        return None
    return {"artist": artist, "title": title,
            "album": album, "track": tracknum}


# ============================================================================
# Main
# ============================================================================

def main():
    ap = argparse.ArgumentParser(
        description="Capture the system audio output and split it into one "
                    "file per track using MPRIS metadata.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    ap.add_argument("-f", "--format", choices=sorted(FORMATS), default="flac",
                    help="output format")
    ap.add_argument("-o", "--outdir", default="captures",
                    help="output directory")
    ap.add_argument("-p", "--player", default=None,
                    help="playerctl player name (e.g. firefox). Without this, "
                         "playerctl picks the first available one.")
    ap.add_argument("-d", "--device", default=None,
                    help="monitor source. Default: the monitor of the default sink.")
    ap.add_argument("--min-seconds", type=float, default=20.0,
                    help="tracks shorter than this are deleted")
    ap.add_argument("--switch-delay", type=float, default=0.4,
                    help="delay the cut by this many seconds, to compensate "
                         "for the MPRIS event arriving before the audio")
    ap.add_argument("--list-players", action="store_true",
                    help="list available MPRIS players and exit")
    ap.add_argument("-s", "--silent", action="store_true",
                    help="Silent mode: create a virtual (null) sink and "
                         "capture from it. Nothing is heard on the speakers, "
                         "but capture is at 100%%. Cleaned up on exit.")
    ap.add_argument("-m", "--move", default=None,
                    help="With --silent: application name (e.g. firefox) "
                         "whose streams will be moved automatically to the "
                         "silent sink — existing ones and any that appear later.")
    args = ap.parse_args()

    check_dependencies()

    if args.list_players:
        players = run(["playerctl", "-l"])
        print(players or "(no active MPRIS players)")
        return

    # --- Pick the monitor source -------------------------------------------
    if args.device:
        device = args.device
        sink = "@DEFAULT_SINK@"
    else:
        sink = run(["pactl", "get-default-sink"])
        if not sink:
            print("[!] No default sink found.", file=sys.stderr)
            sys.exit(1)
        device = sink + ".monitor"

    ext, codec_args = FORMATS[args.format]
    min_bytes = int(args.min_seconds * BYTES_SEC)
    switch_bytes = int(args.switch_delay * BYTES_SEC)

    os.makedirs(args.outdir, exist_ok=True)

    # --- Silent mode: use a null sink instead of the real one --------------
    null_module_id = None
    if args.silent:
        null_module_id, null_sink = load_null_sink()
        device = f"{null_sink}.monitor"
        sink = null_sink
        # Register cleanup as early as possible, before anything else that
        # might throw an exception.
        atexit.register(unload_module, null_module_id)

        # Give the server 100ms to register the new sink before we touch it
        time.sleep(0.1)
        # Force it to 100% and unmuted (usually already so, but be sure)
        subprocess.run(["pactl", "set-sink-volume", null_sink, "100%"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["pactl", "set-sink-mute", null_sink, "0"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        moved = (move_matching_sink_inputs(args.move, null_sink)
                 if args.move else 0)
        if args.move:
            print(f"    Moved {moved} existing stream(s).")
            start_sink_input_watcher(args.move, null_sink)
            print(f"    New streams of '{args.move}' will be moved automatically.")

    print(f"Source : {device}")
    print(f"Format : {args.format}  ->  {os.path.abspath(args.outdir)}/")
    if args.silent:
        print("Mode   : SILENT (nothing plays on the speakers)")
    check_volume(sink)
    check_disk_space(args.outdir)

    players = run(["playerctl", "-l"])
    print(f"Players: {players or '(none yet -- start playback)'}")

    if args.silent and not args.move:
        print()
        print("[?] Silent mode without --move: move the stream manually to")
        print("    'Audiotap-Silent-Capture' from pavucontrol")
        print("    (Playback tab -> dropdown next to each app).")
    print("\nCtrl+C to stop.\n")

    # --- Start capture -----------------------------------------------------
    events = queue.Queue()
    start_metadata_watcher(args.player, events)

    parec = subprocess.Popen(
        ["parec", "-d", device, "--format=s16le",
         f"--rate={RATE}", f"--channels={CHANNELS}", f"--latency={CHUNK}"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

    current_line = None       # last metadata line we saw
    encoder = None            # active encoder
    pending = None            # (meta_dict, bytes_remaining) for delayed switch
    last_paint = 0.0

    def open_encoder(meta):
        label = (f"{meta['artist']} - {meta['title']}"
                 if meta["artist"] else meta["title"])
        path = unique_path(os.path.join(args.outdir, sanitize(label) + ext))
        tags = {"title": meta["title"], "artist": meta["artist"],
                "album": meta["album"], "track": meta["track"]}
        print(f"\r-> {os.path.basename(path)}")
        return Encoder(path, codec_args, tags, min_bytes)

    try:
        while True:
            # --- 1. Check for track change (non-blocking) ------------------
            try:
                kind, payload = events.get_nowait()
                if kind == "error":
                    print(f"\r[!] metadata: {payload}")
                elif payload != current_line:
                    current_line = payload
                    meta = parse_meta(payload)
                    if meta is None:
                        # Player stopped or closed
                        if encoder:
                            encoder.close()
                            encoder = None
                        pending = None
                    else:
                        pending = (meta, switch_bytes)
            except queue.Empty:
                pass

            # --- 2. Read one block of PCM ---------------------------------
            raw = parec.stdout.read(CHUNK)
            if len(raw) < CHUNK:
                print("\r[!] parec ended (did the sink change?)")
                break

            # --- 3. Delayed switch ----------------------------------------
            # The MPRIS event arrives before the audio, because the player
            # updates its metadata as soon as the next track is loaded
            # while the previous track's audio is still in the buffer. We
            # keep the old file open a bit longer.
            if pending is not None:
                meta, remaining = pending
                if remaining <= 0:
                    if encoder:
                        encoder.close()
                    encoder = open_encoder(meta)
                    pending = None
                else:
                    pending = (meta, remaining - CHUNK)

            # --- 4. Write -------------------------------------------------
            if encoder:
                encoder.write(raw)

            # --- 5. Level meter (refresh 4x per second) -------------------
            now = time.monotonic()
            if now - last_paint > 0.25:
                last_paint = now
                level = rms_of(raw)
                bar = "#" * int(min(level * 3, 1.0) * 24)
                elapsed = encoder.seconds if encoder else 0.0
                state = "REC" if encoder else "---"
                sys.stdout.write(f"\r   {state} {elapsed:6.1f}s "
                                 f"[{bar:<24}] {level:.3f}   ")
                sys.stdout.flush()

    except KeyboardInterrupt:
        print("\r\nStopping...")
    finally:
        if encoder:
            encoder.close()
        parec.terminate()
        time.sleep(1.0)       # give the ffmpeg processes time to finish files
        if null_module_id is not None:
            unload_module(null_module_id)
            print("Cleaned up the silent sink.")
        print("Done.")


if __name__ == "__main__":
    main()
