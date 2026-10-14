# TapeRoll Rehearsal Recorder

Terminal UI for rehearsal recording from any audio interface. Live stereo level meters with peak-hold, named and rated takes, optional multitrack stems, a sample-accurate metronome, setlist navigation, automatic normalisation, background encoding, and a one-command cloud upload for when you're back online.

## Setup

Everything (Python, numpy, sounddevice, ffmpeg, rclone) is managed by [pixi](https://pixi.sh):

```bash
brew install pixi
pixi install

# Find your audio device index or name
pixi run devices
```

Copy `config.example.toml` to `config.toml` and set at least `audio.device` (see [Configuration](#configuration)). To upload recordings later, also set `upload.destination`.

For cloud upload, configure an rclone remote once:
```bash
rclone config   # follow prompts to set up Google Drive (or Dropbox, S3, etc.)
```

## Usage

```bash
pixi run start
```

### Install globally (optional)

To run `taperoll` from any directory, install it as a global tool:

```bash
pixi global install --path .
```

The install is editable, so code changes take effect immediately, and `config.toml`, `setlist.txt`, `songs.json` and `recordings/` stay in the project folder. If you change the dependencies or the entry point, re-run the command with `--force-reinstall`.

Why pixi rather than `uv`? On macOS, uv's and Homebrew's Python link against Apple's ancient system ncurses, which has no UTF-8 support and garbles the Unicode level meters as soon as colours are enabled. pixi installs Python from conda-forge, which ships a modern `libncursesw`.

### Keys

| Key | Action |
|-----|--------|
| `r` | Start / stop recording |
| `n` | Set / edit song name (fuzzy-matches saved songs; arrows to select suggestion) |
| `m` | Toggle metronome on / off |
| `b` | Enter BPM value |
| `t` | Cycle time signature (1/4 → 3/4 → 4/4 → 5/4 → 6/4) |
| `s` | Toggle eighth-note subdivision clicks |
| `x` | Toggle stems recording (saved per song; needs a channel map in `config.toml`) |
| `a` | Add the current song to `setlist.txt` |
| `d` | Remove the current / selected song from `setlist.txt` |
| `↑` / `↓` | Navigate setlist (when `setlist.txt` is present) |
| `1`–`5` | Rate the last take (`0` / `Esc` skips) |
| `q` | Quit (when not recording; waits for pending encodes to finish) |

Stopping a recording immediately saves and encodes it in the background. You can start the next take straight away.

## Rating takes

After a take stops, a prompt asks you to rate it from 1 to 5. Saving the final files waits for the rating, and the rating is appended to the filename, e.g. `circles_2026-10-08_20-14-03_rating=4.mp3`, so good takes are easy to find later by searching for `rating=4` or `rating=5`.

Skipping (`0` / `Esc`), starting the next take with `r`, or quitting leaves the take unrated, under its plain name.

## Stems

Besides the stereo mixdown, the recorder can capture every individual mixer channel as a separate 24-bit FLAC file (Ableton Live, Logic, Reaper etc. import FLAC directly; set `stems.format = "mp3"` for much smaller lossy files while experimenting), sample-aligned with the mixdown, as a starting point for producing a song from a live take.

1. In `config.toml`, list which device inputs hold the mixdown and which hold which instrument (`[channels]`, see `config.example.toml`):

   ```toml
   [channels]
   mixdown = [1, 2]

   [[channels.stem]]
   channel = 3
   name = "kick"
   ```

2. Press `x` to turn stems on. The setting is saved per song in `songs.json`, so a song you're working on records stems every time it comes up in the setlist.

Stems are written straight to disk while recording (`recordings/<take>_stems.partial/`), and channels that stayed silent for the whole take are dropped. Each file carries the take's song, date and time plus the channel, e.g. `circles_2026-10-08_20-14-03_05_bass.flac`, so you can still tell where it came from once it's in a DAW project. They are saved next to the mixdown as `<take>_rating=N_stems/`; when you [upload](#uploading-recordings), they go into the same dated folder as the mixdown, **only if the take is rated at least `stems.upload_min_rating`** (if that setting is absent, all stems are uploaded). With it set, unrated and lower-rated takes keep their stems in `recordings/`, so delete those by hand once you no longer need them.

Stems are large (roughly 3–5 MB per channel per minute), so uploading them can take a while.

If the configured device isn't connected (e.g. the recorder fell back to the laptop mic), stems are unavailable and only the mixdown is recorded.

## Metronome

The built-in sample-accurate metronome plays a click track through the output channels of the selected device. Press `m` to toggle it, `b` to type a new BPM, and `t` to cycle the time signature. BPM and time signature are saved per song name and restored automatically when you select the same song again.

The 1/4 time signature plays every beat at the same unaccented click — useful for free-tempo or rubato sections.

## Setlist

Create a `setlist.txt` file in the project folder (or point `[setlist] file` in `config.toml` at another one, e.g. `setlists/friday.txt`, to keep several and pick one) (one song name per line, `#` for comments; `setlist.example.txt` is a starting point):

```
# Set 1
Highway to Hell
Paranoid
Smells Like Teen Spirit
```

Song names are matched case-insensitively against `songs.json` (see `songs.example.json`; it's created and updated by the app as you set a song's BPM, time signature and stems) using fuzzy prefix matching, so you can be slightly sloppy with spelling. Any unresolvable lines (no match or ambiguous) are flagged as warnings in the UI at startup.

Use `↓` / `↑` to move through the setlist. Advancing to a song immediately loads its saved BPM and time signature and restarts the metronome (if running). The song name is pre-filled in the recording, so the workflow is just:

1. `↓` — advance to next song
2. `r` — start recording
3. `r` — stop recording

`setlist.txt` is gitignored — it's a per-session file, not part of the repository.

## Uploading recordings

Recordings stay in the recordings folder until you upload them. Once you're online, upload everything that's still local:

```bash
pixi run upload          # or: taperoll --upload
```

This scans the recordings folder for any `.mp3` (or `.wav` if MP3 encoding is disabled) files and stems folders of takes rated at least `stems.upload_min_rating` (all of them if unset), and uploads each one. Because rclone's `copy` is idempotent, files that were already uploaded successfully are silently skipped — it's safe to run at any time.

## Level meters

The TUI shows a colour-coded bar for each channel with a dB readout:

| Colour | Range |
|--------|-------|
| 🟢 Green | ≤ −18 dB |
| 🟡 Yellow | −18 dB … −6 dB |
| 🔴 Red | > −6 dB |

A peak-hold indicator (`▒`) marks the highest level reached and holds for 10 seconds before resetting, making it easy to catch transient peaks at a glance.

## Configuration

Settings live in `config.toml` in the project folder (gitignored). Every setting has a default, so the file only needs what you change; `config.example.toml` lists them all with their defaults and explanations. The sections are:

| Section | What it controls |
|---------|------------------|
| `[audio]` | Input device, sample rate, sample format |
| `[recording]` | Recordings folder, minimum take length, normalisation, MP3 encoding |
| `[upload]` | rclone destination for `--upload`, deleting after upload, a command to run after each upload |
| `[setlist]` | Which setlist file to use (default `setlist.txt`) |
| `[metronome]` | Default and allowed BPM, click sounds, time signatures to cycle through |
| `[stems]` | Minimum rating for uploading stems, silence threshold |
| `[channels]` | Which inputs hold the mixdown and which hold each stem |

Invalid or unknown settings are shown as warnings when the recorder starts and replaced by their defaults, so a typo never keeps it from starting.

The project folder can be moved elsewhere with the `TAPEROLL_HOME` environment variable.

## Development

The dev environment adds pytest, coverage, ruff, ty and pre-commit:

```bash
pixi run -e dev test        # all tests (unit + end-to-end, ~1 min)
pixi run -e dev test-unit   # unit tests only (~2 s)
pixi run -e dev cov         # tests with a coverage report
pixi run -e dev lint        # ruff check
pixi run -e dev fmt         # ruff format
pixi run -e dev typecheck   # ty check
pixi run -e dev check       # everything above, as CI would
pixi run -e dev install-hooks   # pre-commit: lint, format, types and unit tests on commit, all tests on push
```

All tests run without audio hardware: `tests/fake_sounddevice.py` stands in for an 8-input interface.

The **end-to-end tests** (`tests/e2e/`) run the real app in a pseudo-terminal (via [pexpect](https://pexpect.readthedocs.io)) and read the screen through a terminal emulator ([pyte](https://pyte.readthedocs.io)), the terminal equivalent of Playwright. They press keys, wait for text to appear, and check the files that end up in a local folder standing in for the rclone remote. Real ffmpeg and rclone run in these tests. Coverage includes the app process they launch.

### Code layout

| Module | Responsibility |
|--------|----------------|
| `cli` | Entry point: argument parsing, wiring, finishing pending takes at quit |
| `tui` | Curses front end: renders a `Session` as rows of styled text, maps keys to actions |
| `session` | The recorder's state and actions (record, rate, songs, setlist, metronome, stems), UI-independent |
| `takes` | What happens after a take stops: save, normalise, encode, finish stems, wait for rating |
| `upload` | rclone uploads for `--upload` |
| `audio` | Input stream, level meters, in-memory mixdown, streamed 24-bit stem files |
| `metronome` | Sample-accurate click track |
| `songs` / `setlist` | `songs.json` and `setlist.txt`, fuzzy song-name matching |
| `config` | `config.toml` loading and validation |
| `naming` | File and folder naming rules |
| `activity` | The thread-safe activity log shown in the TUI |

## License

[MIT](LICENSE)
