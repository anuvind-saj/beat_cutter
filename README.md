# Beat Cutter

Place a folder of photos on the open DaVinci Resolve timeline, cut to an audio track.

The script reads the track with librosa, adds a marker at every cut, lays the audio on Audio Track 1, and appends the stills to Video Track 1 in one batch so each photo lasts exactly one beat interval. Nothing is rendered and nothing is deleted unless you pass `--overwrite`.

## Enable external scripting

1. Open **DaVinci Resolve Studio**.
2. Open **Preferences > System > General**.
3. Set **External scripting using** to **Local**.
4. Quit and relaunch Resolve, then open the project and the timeline you want to edit.

Leave that timeline open. The script attaches to the running session. It does not create a project.

Run it from Terminal with your own Python, not from Resolve's **Workspace > Scripts** menu. Resolve's embedded interpreter does not have librosa installed.

## Install

```bash
cd beat_cutter
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Use Python 3.10 or newer. `.m4a` files need ffmpeg if librosa cannot decode them (`brew install ffmpeg`).

## Run

Edit the configuration block at the top of `beat_cutter.py`, or pass flags. Flags win over the block.

```bash
python3 beat_cutter.py --audio ~/Music/track.wav --photo-dir ~/Pictures/set
python3 beat_cutter.py ~/Music/track.wav --mode drops --beats-per-cut 4 --zoom
python3 beat_cutter.py --gui
python3 beat_cutter.py --audio ~/Music/track.wav --shuffle --overwrite
python3 beat_cutter.py --audio ~/Music/track.wav --dry-run
python3 beat_cutter.py --self-test
```

Photos are read from `--photo-dir` when you pass one, otherwise from the Media Pool bin `BeatPhotos`. Files are sorted naturally (`IMG_2.jpg` before `IMG_10.jpg`). `--shuffle` keeps that order as a likeness hint and then jumps through it, so burst shots are not placed next to each other. The log prints the seed; pass `--seed` to repeat that order. With `--loop-photos` (the default) the folder repeats until the track ends. `--no-loop-photos` stops when the stills run out and leaves markers on the remaining beats.

`--overwrite` clears Video Track 1 and Audio Track 1 before placing. Without it, the script stops if either track already has clips.

## Detection

| Mode | What it cuts on |
| --- | --- |
| `beats` | Each attack of the pulse. The gap is allowed to speed up or slow down, so a tick track that starts fast and then opens up is cut on the ticks. |
| `drops` | A 4th-order low-pass under 150 Hz, then `librosa.onset.onset_detect(backtrack=True)` so the cut sits on the start of the kick rather than the hi-hats. |

`--beats-per-cut 1` uses every event, `2` every other event, `4` about one cut per bar. `--min-cut-duration 0.04` drops retriggers closer than that. Raise it toward `0.25` when a double-kick should stay one photo.

Blue markers are beat-grid cuts. Red markers are drop cuts. Move a marker, then trim by hand, if a hit should land a frame earlier.

## Frame timing

The timeline frame rate comes from the open timeline, including 23.976 and 29.97. Cut times are converted to integer frames from `timeline.GetStartFrame()`, and each photo's length is the gap to the next cut. Clips are placed back-to-back, so a 12-frame beat is frames `[n, n+12)` with no hole and no overlap.

Resolve keeps a JPEG at one source frame and ignores a still's in and out points, so a direct append becomes the standard still duration (5 seconds) instead of the beat. The script hardlinks each used photo into a short numbered sequence in `.cache/holders` and trims that sequence to the interval. The sequence is imported into the `BeatCutter Holds` bin.

`--zoom` sets `ZoomX` and `ZoomY` a few percent up and down on alternating clips. `--zoom-style dynamic` tries a short push-in across the clip and falls back to the static alternate if this Resolve build rejects zoom keyframes.

## License

MIT. See [LICENSE](LICENSE).
