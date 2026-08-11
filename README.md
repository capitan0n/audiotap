# audiotap

Capture your Linux system's audio output and automatically split the
recording into one file per track using MPRIS metadata from the player.

Works with any media player that exposes MPRIS over D-Bus — mpv, VLC,
Rhythmbox, Clementine, most web browsers, and many others. Uses
PipeWire's PulseAudio compatibility layer, so it runs on modern PipeWire
systems as well as classic PulseAudio.

## What it does

- Reads raw PCM directly from a sink's monitor source — no re-recording
  through the analog path, no quality loss from mic pickup
- Watches `playerctl --follow metadata` in the background and starts a
  new file every time the track changes
- Encodes each track live through `ffmpeg`, with the artist / title /
  album / track number embedded as tags
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

    git clone https://github.com/YOUR_USER/audiotap.git
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

### All options

    -f, --format          flac | mp3 | opus | wav       (default: flac)
    -o, --outdir DIR      output directory              (default: captures)
    -p, --player NAME     playerctl target
    -d, --device NAME     monitor source
    -s, --silent          route capture through a null sink
    -m, --move NAME       auto-move a given application's streams
    --min-seconds N       drop files shorter than N seconds  (default: 20)
    --switch-delay N      delay cuts by N seconds             (default: 0.4)
    --list-players        list MPRIS players and exit

## How it works

1. `parec` opens the sink's monitor source and streams raw signed 16-bit
   little-endian PCM on stdout. The sound server does any resampling
   needed to get the requested format.
2. `playerctl --follow metadata` runs in a thread and writes a line
   every time the currently-playing track changes.
3. The main loop reads audio in ~21 ms blocks. Whenever a new metadata
   line arrives, the current `ffmpeg` process is closed (via EOF on its
   stdin) and a new one is started for the next track.
4. Because the MPRIS event arrives slightly before the audio (the player
   updates its metadata as soon as it loads the next track, while the
   old track's audio is still buffered), cuts are delayed by
   `--switch-delay` seconds.

## Recording quality

The monitor source sits after the sound server's mixer, which means:

- **Software volume affects amplitude.** Set the sink volume to 100 %
  and control loudness from your speakers or amp. Otherwise you're
  capturing an attenuated signal at reduced dynamic range. In silent
  mode this is handled automatically.
- **All streams routed to the same sink get mixed in.** Notifications,
  other apps, everything. Use silent mode with `-m` to route only one
  application, so nothing else ends up in the recording.
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

MIT. See [LICENSE](LICENSE).

## Contributing

Bug reports and small patches welcome. Try to keep the tool small — the
whole point is that it's a single file you can read in one sitting.
