# audiotap

Capture your Linux system's audio output and automatically split the
recording into one file per track using MPRIS metadata from the player.

Works with any media player that exposes MPRIS over D-Bus — mpv, VLC,
Rhythmbox, Clementine, Spotify, most web browsers, and many others.
Uses PipeWire's PulseAudio compatibility layer, so it runs on modern
PipeWire systems as well as classic PulseAudio.

## What it does

- Reads raw PCM directly from a sink's monitor source — no re-recording
  through the analog path, no quality loss from mic pickup
- Watches `playerctl --follow metadata` in the background and starts a
  new file every time the track changes
- Encodes each track live through `ffmpeg`, with the artist / title /
  album / track number embedded as tags
- Embeds the album cover from the player's MPRIS metadata (FLAC and MP3)
- Optional lyrics fetch from [LRCLIB](https://lrclib.net): plain lyrics
  go into the file's tags, synced lyrics become a `.lrc` sidecar
- Sanitises track titles into safe filenames (Unicode-aware — Greek,
  Japanese, emoji, all fine)
- Deletes files shorter than a configurable threshold (default 20 s), to
  drop ads, jingles and pause/resume glitches
- Optional silent mode: creates a virtual sink so nothing plays through
  the speakers while capture is running

## Requirements

- Linux with PipeWire (via `pipewire-pulse`) or PulseAudio
- Python 3.8 or newer (only the standard library — no `pip install`)
- `parec` (from `libpulse` / `pulseaudio-utils`)
- `ffmpeg`
- `playerctl`

Arch / Manjaro:

    sudo pacman -S libpulse ffmpeg playerctl

Debian / Ubuntu:

    sudo apt install pulseaudio-utils ffmpeg playerctl

Fedora:

    sudo dnf install pulseaudio-utils ffmpeg playerctl

## Install

    git clone https://github.com/capitan0n/audiotap.git
    cd audiotap
    chmod +x audiotap.py

That's it. No packaging, no virtualenv, no daemon.

## Usage

Check that your player is visible over MPRIS:

    ./audiotap.py --list-players

Then start capture:

    ./audiotap.py                            # FLAC into ./captures/
    ./audiotap.py -f opus -o ~/Recordings    # Opus into a different folder
    ./audiotap.py -p mpv                     # only follow mpv
    ./audiotap.py -l                         # also fetch lyrics from LRCLIB

Start playback in your player. A new file appears the moment the first
track's metadata is announced, and each subsequent track change closes
the current file and opens the next one.

Press Ctrl+C to stop.

### Silent mode

If you don't want to hear the audio through your speakers during a long
capture, use `-s`. This creates a temporary null sink that receives the
audio (so the capture still works at full amplitude) but doesn't send it
to hardware.

    ./audiotap.py -s                         # create silent sink; move manually
    ./audiotap.py -s -m firefox              # auto-move Firefox's stream

With `-m`, streams matching the given application name are moved to the
silent sink at startup, and any new streams that appear later are moved
too. On exit, the sink is unloaded and streams fall back to your normal
output automatically.

Without `-m`, you can move streams by hand from `pavucontrol` (Playback
tab → dropdown → *Audiotap-Silent-Capture*).

### Album covers (on by default)

The cover is read straight from the current track's `mpris:artUrl` and
embedded during encoding, so no separate step is needed.

- Most native players (Spotify client, mpv, Rhythmbox, ...) expose the
  cover as a `file://` URL pointing at their own on-disk cache. In that
  case the fetch is a local file read — no network, no temp files.
- Web players (music sites in Firefox / Chromium) usually expose the
  cover as `https://` or as a `data:` URL. Those are downloaded once
  per unique URL into `/tmp/audiotap_cover_*`, cached for the rest of
  the session, and removed on exit.
- Embedding is supported in **FLAC** and **MP3**. Opus is skipped
  because embedding a picture into an Ogg Opus stream requires a
  base64-encoded `METADATA_BLOCK_PICTURE` Vorbis comment that ffmpeg
  does not write on its own; WAV has no cover format at all.
- Disable with `--no-cover`.

Cover resolution is whatever the player hands us — usually 300×300 or
smaller. audiotap does not do external lookups against streaming
provider APIs to fetch a higher-resolution version.

### Lyrics (opt-in with `-l`)

`-l` enables per-track lookups against [LRCLIB](https://lrclib.net) —
an open, community-driven lyrics database with a no-key REST API. For
each track:

- Plain lyrics are embedded as a `lyrics` tag inside the audio file
  (readable by most desktop players).
- Synced lyrics are written as a sidecar `<track>.lrc` next to the
  audio, in the standard LRC format:

        Rosalia - Motomami.flac
        Rosalia - Motomami.lrc

  LRC sidecars are read by mpv, Strawberry, foobar2000, Poweramp, and
  many other players — often with scrolling highlight during playback.

The lookup is two-step: an exact match against `/api/get` first, then a
fuzzy fallback against `/api/search` when casing or album spelling
doesn't align (real-world case: MPRIS ships `TOQUEL` but LRCLIB has
`Toquel`). Results are cached per track for the session. Any failure —
no match, network error, malformed response — is silent, and capture
continues.

Coverage is best for popular international tracks; niche and
underground artists may not be in the database. When they aren't, no
sidecar is written and no tag is added.

### All options

    -f, --format          flac | mp3 | opus | wav       (default: flac)
    -o, --outdir DIR      output directory              (default: captures)
    -p, --player NAME     playerctl target
    -d, --device NAME     monitor source
    -s, --silent          route capture through a null sink
    -m, --move NAME       auto-move a given application's streams
    -l, --lyrics          fetch lyrics from LRCLIB      (default: off)
    --no-cover            do not embed album cover      (default: embed)
    --min-seconds N       drop files shorter than N seconds  (default: 20)
    --switch-delay N      delay cuts by N seconds             (default: 0.4)
    --list-players        list MPRIS players and exit

## Network behaviour

By default, audiotap only touches the network when the player itself
gives us a remote URL for the cover — i.e. when using a web player.
Native players with a local cover cache trigger zero outbound requests.

Opting in to lyrics (`-l`) adds one HTTPS request per unique track to
`lrclib.net`, and covers from web players hit whichever CDN the player
uses (typically the streaming service's own image host). If you want
a fully offline run:

    ./audiotap.py --no-cover                 # covers off, lyrics off

## How it works

1. `parec` opens the sink's monitor source and streams raw signed 16-bit
   little-endian PCM on stdout. The sound server does any resampling
   needed to get the requested format.
2. `playerctl --follow metadata` runs in a thread and writes a line
   every time the currently-playing track changes.
3. The main loop reads audio in ~21 ms blocks. Whenever a new metadata
   line arrives, the current `ffmpeg` process is closed (via EOF on its
   stdin) and a new one is started for the next track — with the cover
   attached as a second input and lyrics wired in as tags / sidecar
   when enabled.
4. Because the MPRIS event arrives slightly before the audio (the player
   updates its metadata as soon as it loads the next track, while the
   old track's audio is still buffered), cuts are delayed by
   `--switch-delay` seconds.

## Player compatibility

audiotap does not integrate with any specific service — it captures
whatever is being sent to your speakers, and it reads track metadata
from whichever player exposes it over MPRIS. If `playerctl -l` sees
your player, audiotap will work with it.

Confirmed working:

- **Local players:** mpv, VLC, Rhythmbox, Clementine, Strawberry, Audacious
- **Native Linux clients:** Spotify (official `.deb` / AUR / Flatpak build)
- **Anything in a browser:** Firefox and Chromium expose MPRIS for
  HTML5 audio and video, which covers most web-based playback

For browser-based players, use `-p firefox` (or `-p chromium`) as the
target and `-m firefox` in silent mode to route only that browser's
audio to the null sink.

Some players emit sparse or partial metadata over MPRIS — track title
may be present but artist or album empty. `parse_meta()` treats a line
with at least one non-empty field as a valid track; empty fields are
simply not written into the file tags.

## Recording quality

The monitor source is captured before the sink's software volume stage
on PipeWire and modern PulseAudio, so the slider position doesn't
attenuate the recording. A few things still matter:

- **All streams routed to the same sink get mixed in.** Notifications,
  other apps, everything. Use silent mode with `-m` to route only one
  application, so nothing else ends up in the recording.
- **Muted sinks may still produce silence** on some setups. If the sink
  is muted, audiotap warns at startup.
- **Format is negotiated with the server**, not the hardware. Any
  standard rate/depth combination works.

Bitrate figures for planning disk space:

| Format          | ~MB / minute | ~MB / hour |
|-----------------|--------------|------------|
| FLAC (default)  |   5          |  300       |
| Opus 128 kbps   |   1          |   60       |
| MP3 192 kbps    |   1.4        |   85       |
| WAV             |  11.5        |  690       |

FLAC is a lossless container over the same PCM you'd get from WAV, so
if you have the disk space it's the default for a reason. Opus is a
good pick when size matters — it's substantially more efficient than
MP3 at similar perceived quality.

## Legitimate uses

- Recording live radio streams for later listening
- Archiving your own live sets, DJ mixes, and streams
- Capturing lectures, talks, podcasts and conference audio you're
  allowed to record
- Preserving audio from Bandcamp / SoundCloud pages that you own
- Testing audio pipelines, plugins, or streaming setups

Please respect the terms of service of any streaming platform you use
and applicable copyright law in your jurisdiction. The tool captures
audio that is already being played through your speakers — how you use
it is your responsibility.

## License

GPLv3. See [LICENSE](LICENSE).

## Stability

Tested running continuously for 48 hours with various MPRIS-compatible players
(browsers, native Linux clients) on Manjaro KDE (PipeWire) without crashes,
memory leaks, or missed track splits.

## Contributing

Bug reports and small patches welcome. Try to keep the tool small — the
whole point is that it's a single file you can read in one sitting.
