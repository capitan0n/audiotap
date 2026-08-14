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
import base64
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request

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
    """Warn if the sink is muted.

    Software volume does NOT need to be at 100%: on PipeWire (and modern
    PulseAudio) the monitor source is tapped before the sink's volume stage,
    so the captured signal is unaffected by the slider position. Muting is
    a separate matter — some setups still route silence to the monitor when
    the sink is muted, so we keep that check.
    """
    mute = run(["pactl", "get-sink-mute", sink])

    if "yes" in mute.lower():
        print("[!] WARNING: the sink is MUTED — the capture may be silent.")
        print("    pactl set-sink-mute @DEFAULT_SINK@ 0")
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


def check_disk_space(outdir, fmt, hours_planned=3):
    """Warn if the disk does not have enough room for the planned capture.

    Rough per-format footprint in MB per minute. Used for a sanity check
    only — actual size varies with silence, compression, and content.
    """
    mb_per_min = {
        "flac": 5.0,
        "mp3":  1.5,
        "opus": 1.0,
        "wav":  11.5,
    }.get(fmt, 11.5)
    free = shutil.disk_usage(outdir).free
    needed = int(hours_planned * 60 * mb_per_min * 1024 * 1024)
    if free < needed:
        gb_free = free / (1024**3)
        gb_needed = needed / (1024**3)
        print(f"[!] Low free space: {gb_free:.1f} GB "
              f"(~{gb_needed:.1f} GB needed for {hours_planned}h of {fmt}).")


# ============================================================================
# Encoder: one ffmpeg process per track
# ============================================================================

class Encoder:
    """Wrap an ffmpeg process that receives raw PCM on stdin.

    Closing stdin acts as EOF: ffmpeg writes the container headers,
    finalises the file and exits. No intermediate .raw file or second pass
    is needed.
    """

    def __init__(self, path, codec_args, tags, min_bytes, cover_path=None):
        self.path = path
        self.min_bytes = min_bytes
        self.written = 0

        meta = []
        for key, value in tags.items():
            if value:
                meta += ["-metadata", f"{key}={value}"]

        # --- Attached picture ------------------------------------------------
        # ffmpeg embeds a cover if we add it as a second input and map it as
        # an attached picture. Only FLAC and MP3 accept this directly.
        #
        # WAV has no cover support in any player.
        # Opus/Ogg requires the picture to be base64-encoded into the
        # METADATA_BLOCK_PICTURE Vorbis comment, which ffmpeg does not do on
        # its own — implementing it here would need a dependency or a lot of
        # hand-rolled byte packing, so we skip it and keep the audio only.
        pic_in = []
        pic_map = []
        ext = os.path.splitext(path)[1].lower()
        if cover_path and ext in (".flac", ".mp3"):
            pic_in = ["-i", cover_path]
            pic_map = [
                "-map", "0:a", "-map", "1:v",
                "-c:v", "copy",
                "-disposition:v:0", "attached_pic",
                "-metadata:s:v", "title=Cover",
                "-metadata:s:v", "comment=Cover (front)",
            ]

        self.proc = subprocess.Popen(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                # --- INPUT 0: raw PCM (no header, declare format inline) ---
                "-f", "s16le", "-ar", str(RATE), "-ac", str(CHANNELS),
                "-i", "pipe:0",
                # --- INPUT 1 (optional): cover image ---
                *pic_in,
                # --- OUTPUT ---
                *codec_args, *pic_map, *meta, path,
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
            # Too short: ad, jingle, or a pause/resume pseudo-event.
            # Remove the audio file AND any sidecar we may have written
            # (e.g. the .lrc from a --lyrics fetch), so nothing is left
            # orphan.
            for stray in (self.path, os.path.splitext(self.path)[0] + ".lrc"):
                try:
                    os.remove(stray)
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
        "{{artist}}\t{{title}}\t{{album}}\t{{xesam:trackNumber}}\t{{mpris:artUrl}}",
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
    while len(parts) < 5:
        parts.append("")
    artist, title, album, tracknum, art_url = (p.strip() for p in parts[:5])
    if not (artist or title):
        return None
    return {"artist": artist, "title": title,
            "album": album, "track": tracknum,
            "art_url": art_url}


# ============================================================================
# Cover art
# ============================================================================
#
# MPRIS exposes the current track's cover through the mpris:artUrl field.
# The value can be one of three schemes:
#   file:///...    — a local file, usually the player's on-disk cache
#   https://...    — a remote URL served by the streaming provider's CDN
#   data:image/... — base64-embedded image (rare, but valid)
#
# We resolve each of them into a temporary file on disk and hand its path
# to ffmpeg as a second input, so the picture is embedded natively in a
# single encoding pass. Downloaded / decoded files are tracked in a set and
# removed on exit; cached results are keyed by URL to avoid re-downloading
# the same album cover for every track.
# ----------------------------------------------------------------------------

_COVER_CACHE = {}          # url -> local path (or None on failure)
_COVER_TEMPFILES = set()   # paths we created and must clean up on exit
_COVER_TIMEOUT = 3.0       # seconds — must stay small: fetch is synchronous

# Known CDN patterns where a size hint is baked into the URL. Players usually
# hand us a small thumbnail (64–300 px) that looks terrible when embedded and
# viewed in a music app. Rewriting the URL to the same CDN's larger variant
# gives us a hi-res cover for free — same host, same auth, same request
# count, no extra API and no scraping. The rewrite is purely mechanical: if
# the pattern doesn't match, we leave the URL alone.
#
# Spotify (i.scdn.co):
#   Album covers use the namespace `ab67616d0000` followed by a 4-char size
#   code and the image hash. Size codes: 4851 = 64px, 1e02 = 300px,
#   b273 = 640px. We upgrade to b273 whenever we see an album URL.
#   Artist images (ab67616100000...) and playlist images (ab67706f00000...)
#   use different size codes we don't try to guess — left as-is.
_SPOTIFY_ALBUM_RE = re.compile(
    r"(https?://i\.scdn\.co/image/ab67616d0000)"
    r"[0-9a-f]{4}"
    r"([0-9a-f]+)"
)


def upgrade_cover_url(url):
    """Return a higher-resolution variant of `url` when we recognise the CDN.

    Only known-safe rewrites are applied. Anything unrecognised is returned
    unchanged, so this is always a safe transformation to attempt.
    """
    if not url:
        return url
    # Spotify album covers -> 640x640
    m = _SPOTIFY_ALBUM_RE.match(url)
    if m:
        return f"{m.group(1)}b273{m.group(2)}"
    return url


def _cleanup_covers():
    for path in _COVER_TEMPFILES:
        try:
            os.remove(path)
        except OSError:
            pass


atexit.register(_cleanup_covers)


def _guess_ext(content_type, url):
    """Pick a sensible extension from a Content-Type header or a URL path."""
    ct = (content_type or "").lower()
    if "jpeg" in ct or "jpg" in ct:
        return ".jpg"
    if "png" in ct:
        return ".png"
    if "webp" in ct:
        return ".webp"
    # Fallback: sniff the URL path
    lower = url.lower()
    for ext in (".jpg", ".jpeg", ".png", ".webp"):
        if ext in lower:
            return ".jpg" if ext == ".jpeg" else ext
    return ".jpg"           # most CDNs serve JPEG anyway


def fetch_cover(art_url):
    """Resolve mpris:artUrl to a local file path, or return None on failure.

    Results are cached per URL — the same album cover is downloaded once
    even across many tracks. Never raises: any failure just returns None,
    so recording is never blocked by a network hiccup.
    """
    if not art_url:
        return None
    if art_url in _COVER_CACHE:
        return _COVER_CACHE[art_url]

    path = None
    try:
        parsed = urllib.parse.urlparse(art_url)

        if parsed.scheme == "file":
            # Player already has it on disk — no download, no temp file.
            local = urllib.parse.unquote(parsed.path)
            if os.path.isfile(local):
                path = local

        elif parsed.scheme in ("http", "https"):
            fetch_url = upgrade_cover_url(art_url)
            req = urllib.request.Request(
                fetch_url,
                headers={"User-Agent": "audiotap/0.1"},
            )
            with urllib.request.urlopen(req, timeout=_COVER_TIMEOUT) as resp:
                data = resp.read(8 * 1024 * 1024)   # cap at 8 MB
                ext = _guess_ext(resp.headers.get("Content-Type"), fetch_url)
            fd, path = tempfile.mkstemp(prefix="audiotap_cover_", suffix=ext)
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            _COVER_TEMPFILES.add(path)

        elif parsed.scheme == "data":
            # data:[<mediatype>][;base64],<data>
            header, _, payload = art_url[5:].partition(",")
            if "base64" in header and payload:
                ext = _guess_ext(header, "")
                raw = base64.b64decode(payload, validate=False)
                fd, path = tempfile.mkstemp(prefix="audiotap_cover_",
                                            suffix=ext)
                with os.fdopen(fd, "wb") as f:
                    f.write(raw)
                _COVER_TEMPFILES.add(path)

    except Exception:
        # Network error, DNS failure, malformed URL, decode error — all fine.
        path = None

    _COVER_CACHE[art_url] = path
    return path


# ============================================================================
# Lyrics (LRCLIB)
# ============================================================================
#
# LRCLIB (https://lrclib.net) is a FOSS, community-maintained lyrics database
# with an open REST API — no auth, no key, generous rate limits. We hit
# /api/get with artist_name + track_name (plus album_name when we have it)
# and get back JSON with `plainLyrics` and/or `syncedLyrics` (LRC format).
#
# Storage:
#   - synced lyrics -> sidecar `<track>.lrc` next to the audio file
#   - plain lyrics  -> embedded as a `lyrics` tag inside the audio file
# The sidecar is the reliable path: universal player support, no encoder
# re-run, and it survives any tag stripping the user might do later.
#
# We cache per (artist, title, album) tuple so repeated tracks in a session
# don't hit the API twice. All failures are silent: if LRCLIB doesn't have
# the song, or the network hiccups, we simply skip lyrics for that track.
# ----------------------------------------------------------------------------

_LYRICS_CACHE = {}         # (artist, title, album) -> dict|None
_LYRICS_TIMEOUT = 3.0
_LRCLIB_GET = "https://lrclib.net/api/get"
_LRCLIB_SEARCH = "https://lrclib.net/api/search"


def _lrclib_request(url):
    """Perform one HTTPS GET against LRCLIB and return decoded JSON or None."""
    req = urllib.request.Request(
        url,
        headers={
            # LRCLIB asks clients to identify themselves — see
            # https://lrclib.net/docs.
            "User-Agent": "audiotap/0.1 "
                          "(https://github.com/capitan0n/audiotap)",
        },
    )
    with urllib.request.urlopen(req, timeout=_LYRICS_TIMEOUT) as resp:
        return json.loads(resp.read(2 * 1024 * 1024).decode("utf-8"))


def _extract_lyrics(data):
    """Turn a LRCLIB record into {'plain', 'synced'} or None if both empty."""
    plain = (data.get("plainLyrics") or "").strip()
    synced = (data.get("syncedLyrics") or "").strip()
    if plain or synced:
        return {"plain": plain, "synced": synced}
    return None


def fetch_lyrics(artist, title, album=""):
    """Query LRCLIB and return {'plain': str, 'synced': str} or None.

    Two-step lookup:
      1. /api/get — exact match on artist/title/album. Fast, but strict:
         casing and album spelling must be right, so many real-world MPRIS
         payloads (e.g. all-caps artist names) miss.
      2. /api/search — fallback fuzzy search on the same keys. Returns an
         array; we take the first hit.

    Positive results are cached forever within the session; negative results
    are cached too, but only for the current run. Never raises — network
    errors, 404s, and malformed responses all just return None.
    """
    if not (artist and title):
        return None
    key = (artist, title, album)
    if key in _LYRICS_CACHE:
        return _LYRICS_CACHE[key]

    result = None

    # --- 1. Exact match ------------------------------------------------------
    try:
        params = {"artist_name": artist, "track_name": title}
        if album:
            params["album_name"] = album
        result = _extract_lyrics(
            _lrclib_request(f"{_LRCLIB_GET}?{urllib.parse.urlencode(params)}")
        )
    except Exception:
        result = None

    # --- 2. Fuzzy search fallback -------------------------------------------
    if result is None:
        try:
            params = {"artist_name": artist, "track_name": title}
            data = _lrclib_request(
                f"{_LRCLIB_SEARCH}?{urllib.parse.urlencode(params)}"
            )
            if isinstance(data, list) and data:
                result = _extract_lyrics(data[0])
        except Exception:
            result = None

    _LYRICS_CACHE[key] = result
    return result


def write_lrc_sidecar(audio_path, synced_text):
    """Write synced lyrics as `<audio>.lrc` next to the audio file."""
    lrc_path = os.path.splitext(audio_path)[0] + ".lrc"
    try:
        with open(lrc_path, "w", encoding="utf-8") as f:
            f.write(synced_text)
            if not synced_text.endswith("\n"):
                f.write("\n")
        return lrc_path
    except OSError:
        return None


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
    ap.add_argument("--no-cover", action="store_true",
                    help="Do not embed the album cover. By default audiotap "
                         "reads mpris:artUrl and embeds the picture. Local "
                         "file:// URLs stay offline; http(s):// URLs cause a "
                         "small fetch (3s timeout).")
    ap.add_argument("-l", "--lyrics", action="store_true",
                    help="Fetch lyrics from LRCLIB (lrclib.net) for each "
                         "track. Synced lyrics are written as a .lrc file "
                         "next to the audio; plain lyrics are embedded as a "
                         "'lyrics' tag. Off by default — enables one HTTPS "
                         "request per unique track.")
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
    check_disk_space(args.outdir, args.format)

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

    cover_supported = args.format in ("flac", "mp3")

    def open_encoder(meta):
        label = (f"{meta['artist']} - {meta['title']}"
                 if meta["artist"] else meta["title"])
        path = unique_path(os.path.join(args.outdir, sanitize(label) + ext))
        tags = {"title": meta["title"], "artist": meta["artist"],
                "album": meta["album"], "track": meta["track"]}
        cover_path = (fetch_cover(meta.get("art_url"))
                      if not args.no_cover and cover_supported else None)

        # --- Lyrics (opt-in via -l) --------------------------------------
        lyr = (fetch_lyrics(meta["artist"], meta["title"], meta["album"])
               if args.lyrics else None)
        markers = []
        if cover_path:
            markers.append("+cover")
        if lyr:
            # Plain text goes into the audio file's tags; synced text is
            # written as a sidecar after the encoder starts.
            if lyr["plain"]:
                tags["lyrics"] = lyr["plain"]
                markers.append("+lyrics")
            if lyr["synced"]:
                sidecar = write_lrc_sidecar(path, lyr["synced"])
                if sidecar:
                    markers.append("+lrc")

        marker_str = f"  [{' '.join(markers)}]" if markers else ""
        print(f"\r-> {os.path.basename(path)}{marker_str}")
        return Encoder(path, codec_args, tags, min_bytes,
                       cover_path=cover_path)

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
        parec.wait()          # reap the process, don't leave a zombie
        time.sleep(1.0)       # give the ffmpeg processes time to finish files
        # Silent-sink cleanup happens via atexit — no need to duplicate here.
        print("Done.")


if __name__ == "__main__":
    main()
