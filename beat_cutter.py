#!/usr/bin/env python3
"""Cut a bin of photos to an audio track on the open DaVinci Resolve timeline.

The script talks to a running DaVinci Resolve Studio session on macOS, detects
beats or low-frequency drops with librosa, and appends stills to Video Track 1
so each photo occupies one interval with no one-frame gap or overlap.

Edit the configuration block below, or override it from the command line.
Run ``python3 beat_cutter.py --help`` for the flags.
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import math
import os
import random
import re
import shutil
import subprocess
import sys
import unicodedata
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Iterable, Sequence


# =============================================================================
# Configuration
# Edit these, then run:  python3 beat_cutter.py
# A flag on the command line overrides the matching value.
# =============================================================================

AUDIO_PATH = ""  # .wav, .mp3, .m4a, or .flac. Blank: pass --audio or use --gui.
PHOTO_DIR = ""  # Folder of stills. Blank: use the Media Pool bin named below.
MEDIA_POOL_BIN = "BeatPhotos"
MODE = "beats"  # "beats" follows the pulse, even if it speeds up or slows down. "drops" is kicks.
BEATS_PER_CUT = 1  # 1 = every beat, 2 = every other beat, 4 = about one cut per bar
MIN_CUT_DURATION_SEC = 0.04  # Drop retriggers closer than this. Keeps a fast opening tick.
LOOP_PHOTOS = True  # Repeat the stills when there are more intervals than photos.
SHUFFLE_PHOTOS = False  # Spread the filename order so burst shots are not adjacent.
SHUFFLE_SEED = None  # Blank picks a seed and prints it. Set an int to repeat a shuffle.
APPLY_SUBTLE_ZOOM = False
ZOOM_STYLE = "static"  # "static" alternates scale. "dynamic" eases across the clip.
ZOOM_RATIO = 1.045  # 1.0 is 100%. 1.045 is a 4.5% punch-in.
LOWPASS_HZ = 150.0  # Drop mode keeps energy under this frequency.
OVERWRITE_TRACKS = False  # True deletes existing clips on V1 and A1 first.
COVER_FROM_START = True  # Hold a photo from the timeline start until the first beat.

# =============================================================================

RESOLVE_MODULE_DIR = (
    "/Library/Application Support/Blackmagic Design/DaVinci Resolve/"
    "Developer/Scripting/Modules/"
)
RESOLVE_SCRIPT_API = (
    "/Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Scripting"
)
RESOLVE_SCRIPT_LIB = (
    "/Applications/DaVinci Resolve/DaVinci Resolve.app/Contents/Libraries/Fusion/fusionscript.so"
)

AUDIO_EXTENSIONS = {".wav", ".mp3", ".m4a", ".flac"}
IMAGE_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".jpe",
    ".png",
    ".tif",
    ".tiff",
    ".bmp",
    ".gif",
    ".webp",
    ".heic",
    ".heif",
    ".exr",
    ".dpx",
    ".tga",
}
MARKER_CUSTOM_DATA = "beat_cutter"
HOLDER_BIN = "BeatCutter Holds"
HOLDER_CACHE = Path(__file__).resolve().parent / ".cache" / "holders"
HOP_LENGTH = 512
# Resolve reports these rates to 3 decimals. The edit uses the exact NTSC ratio
# so a long track does not drift by a frame.
_EXACT_FRAME_RATES = {
    23.976: 24000 / 1001,
    23.98: 24000 / 1001,
    29.97: 30000 / 1001,
    47.952: 48000 / 1001,
    59.94: 60000 / 1001,
    119.88: 120000 / 1001,
}
_NATURAL_SPLIT = re.compile(r"(\d+)")

log = logging.getLogger("beat_cutter")


class BeatCutterError(Exception):
    """A problem the user can fix, with the reason in the message."""


class Cancelled(BeatCutterError):
    """The file picker was closed without a selection."""


@dataclass
class JobConfig:
    """Everything required to analyze a track and lay out one timeline."""

    audio_path: str = AUDIO_PATH
    photo_dir: str = PHOTO_DIR
    media_pool_bin: str = MEDIA_POOL_BIN
    mode: str = MODE
    beats_per_cut: int = BEATS_PER_CUT
    min_cut_duration_sec: float = MIN_CUT_DURATION_SEC
    loop_photos: bool = LOOP_PHOTOS
    shuffle_photos: bool = SHUFFLE_PHOTOS
    shuffle_seed: int | None = SHUFFLE_SEED
    apply_subtle_zoom: bool = APPLY_SUBTLE_ZOOM
    zoom_style: str = ZOOM_STYLE
    zoom_ratio: float = ZOOM_RATIO
    lowpass_hz: float = LOWPASS_HZ
    overwrite: bool = OVERWRITE_TRACKS
    cover_from_start: bool = COVER_FROM_START
    dry_run: bool = False
    verbose: bool = False

    def prepare(self) -> "JobConfig":
        """Return a copy with cleaned paths and validated knobs."""

        mode = self.mode.strip().lower()
        zoom_style = self.zoom_style.strip().lower()
        audio = self.audio_path.strip()
        photo_dir = self.photo_dir.strip()
        bin_name = self.media_pool_bin.strip()

        if mode not in {"beats", "drops"}:
            raise BeatCutterError("Mode must be 'beats' or 'drops'.")
        if zoom_style not in {"static", "dynamic"}:
            raise BeatCutterError("Zoom style must be 'static' or 'dynamic'.")
        if self.beats_per_cut < 1:
            raise BeatCutterError("--beats-per-cut must be 1 or greater.")
        if self.min_cut_duration_sec < 0:
            raise BeatCutterError("--min-cut-duration cannot be negative.")
        if not 1.0 <= self.zoom_ratio <= 4.0:
            raise BeatCutterError("--zoom-ratio must be between 1.0 and 4.0.")
        if not 20.0 <= self.lowpass_hz <= 500.0:
            raise BeatCutterError("--lowpass-hz must be between 20 and 500.")
        if not bin_name:
            raise BeatCutterError("The Media Pool bin name is empty.")
        if not audio:
            raise BeatCutterError(
                "No audio file. Set AUDIO_PATH at the top of beat_cutter.py, "
                "pass the file as an argument, or run with --gui."
            )

        audio_path = Path(audio).expanduser()
        if not audio_path.is_file():
            raise BeatCutterError(f"Audio file not found: {audio_path}")
        if audio_path.suffix.lower() not in AUDIO_EXTENSIONS:
            allowed = ", ".join(sorted(AUDIO_EXTENSIONS))
            raise BeatCutterError(
                f"Unsupported audio type '{audio_path.suffix}'. Use one of: {allowed}."
            )

        resolved_photo = ""
        if photo_dir:
            folder = Path(photo_dir).expanduser()
            if not folder.is_dir():
                raise BeatCutterError(f"Photo folder not found: {folder}")
            resolved_photo = str(folder.resolve())

        return replace(
            self,
            audio_path=str(audio_path.resolve()),
            photo_dir=resolved_photo,
            media_pool_bin=bin_name,
            mode=mode,
            zoom_style=zoom_style,
        )


@dataclass(frozen=True)
class Analysis:
    """Beat or onset times for one audio file, in seconds from the start."""

    duration_sec: float
    sample_rate: int
    event_times: list[float]
    tempo_bpm: float | None
    mode: str


@dataclass(frozen=True)
class PoolClip:
    """A media pool item plus the fields the edit needs to sort and place it."""

    item: Any
    name: str
    path: str
    frames: int | None


@dataclass
class ResolveSession:
    """The open Resolve objects this edit writes into."""

    resolve: Any
    project: Any
    timeline: Any
    media_pool: Any
    fps: float
    timeline_start: int


def configure_logging(verbose: bool) -> None:
    """Send progress to stdout and failures to stderr."""

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    formatter = logging.Formatter("%(asctime)s  %(levelname)s  %(message)s", "%H:%M:%S")

    info = logging.StreamHandler(sys.stdout)
    info.setLevel(logging.DEBUG)
    info.addFilter(lambda record: record.levelno < logging.ERROR)
    info.setFormatter(formatter)

    errors = logging.StreamHandler(sys.stderr)
    errors.setLevel(logging.ERROR)
    errors.setFormatter(formatter)

    root.addHandler(info)
    root.addHandler(errors)


def natural_sort_key(name: str) -> list[tuple[int, Any]]:
    """Sort ``IMG_2`` before ``IMG_10`` without ignoring the rest of the name."""

    key: list[tuple[int, Any]] = []
    for part in _NATURAL_SPLIT.split(name):
        if not part:
            continue
        if part.isdigit():
            key.append((0, int(part)))
        else:
            key.append((1, part.lower()))
    return key


def parse_frame_rate(raw: object) -> float:
    """Convert a Resolve frame-rate setting to an exact frames-per-second value.

    ``23.976`` and ``29.97`` (including a trailing ``DF``) map to the NTSC
    ratios ``24000/1001`` and ``30000/1001``. Integer rates stay integers.
    """

    if isinstance(raw, bool) or raw is None:
        raise ValueError(f"invalid frame rate: {raw!r}")
    if isinstance(raw, (int, float)):
        value = float(raw)
    else:
        text = str(raw).strip().upper().replace("NDF", "").replace("DF", "").strip()
        value = float(text)
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"invalid frame rate: {raw!r}")
    exact = _EXACT_FRAME_RATES.get(round(value, 3))
    return exact if exact is not None else value


def seconds_to_frame_count(seconds: float, fps: float) -> int:
    """Nearest whole frame for a non-negative timestamp or duration."""

    if seconds <= 0:
        return 0
    return int(math.floor(seconds * fps + 0.5))


def frames_to_timecode(frames: int, fps: float) -> str:
    """Format an absolute frame count as ``HH:MM:SS:FF`` using rounded fps.

    Drop-frame timelines display a different counting pattern in the UI.
    This timecode is the frame address the script actually uses.
    """

    nominal = max(1, int(round(fps)))
    frames = max(0, int(frames))
    ff = frames % nominal
    whole_seconds = frames // nominal
    ss = whole_seconds % 60
    mm = (whole_seconds // 60) % 60
    hh = whole_seconds // 3600
    return f"{hh:02d}:{mm:02d}:{ss:02d}:{ff:02d}"


_FPS_NAMES = {
    23.976: "23.976 (24000/1001)",
    23.98: "23.976 (24000/1001)",
    29.97: "29.97 (30000/1001)",
    47.952: "47.952 (48000/1001)",
    59.94: "59.94 (60000/1001)",
    119.88: "119.88 (120000/1001)",
}


def format_fps(fps: float) -> str:
    """Short frame-rate label, with the NTSC ratio when the rate is fractional."""

    for shown, exact in _EXACT_FRAME_RATES.items():
        if abs(fps - exact) < 1e-9:
            return _FPS_NAMES.get(shown, f"{shown:.3f}")
    nearest = round(fps)
    if abs(fps - nearest) < 1e-6:
        return str(int(nearest))
    return f"{fps:.3f}"


def build_cut_seconds(
    event_times: Sequence[float],
    audio_duration: float,
    *,
    beats_per_cut: int,
    min_cut_duration_sec: float,
    cover_from_start: bool,
) -> list[float]:
    """Return cut times in seconds, not including the end of the audio.

    ``beats_per_cut`` keeps every Nth detected event. Events closer than
    ``min_cut_duration_sec`` to the previous kept cut are discarded so a
    double kick cannot produce a micro-cut. The caller adds the audio end
    as the exclusive end of the final photo.
    """

    if beats_per_cut < 1:
        raise ValueError("beats_per_cut must be at least 1")
    if audio_duration <= 0:
        raise ValueError("audio duration must be positive")
    if min_cut_duration_sec < 0:
        raise ValueError("min_cut_duration_sec cannot be negative")

    events = sorted(float(t) for t in event_times if 0.0 <= float(t) < audio_duration)
    sampled = events[::beats_per_cut]

    cuts: list[float] = []
    if cover_from_start:
        cuts.append(0.0)
    for timestamp in sampled:
        if not cuts:
            cuts.append(timestamp)
            continue
        gap = timestamp - cuts[-1]
        if gap > 1e-6 and gap + 1e-6 >= min_cut_duration_sec:
            cuts.append(timestamp)
    if not cuts:
        cuts.append(0.0)
    return cuts


def build_boundaries(
    cut_seconds: Sequence[float],
    audio_duration: float,
    fps: float,
    timeline_start: int,
    min_cut_duration_sec: float,
) -> list[int]:
    """Absolute timeline frames for each photo edge, plus the exclusive end.

    Photo ``i`` occupies ``[boundaries[i], boundaries[i + 1])``. Its frame
    count is ``boundaries[i + 1] - boundaries[i]``, which is the half-open
    duration used to abut clips. A tail shorter than the minimum is absorbed
    into the previous photo instead of becoming its own sliver.
    """

    if fps <= 0:
        raise ValueError("fps must be positive")
    audio_frames = max(1, seconds_to_frame_count(audio_duration, fps))
    min_frames = (
        max(1, seconds_to_frame_count(min_cut_duration_sec, fps))
        if min_cut_duration_sec > 0
        else 1
    )

    offsets: list[int] = []
    for timestamp in sorted(float(t) for t in cut_seconds):
        offset = seconds_to_frame_count(max(0.0, timestamp), fps)
        if offset >= audio_frames:
            continue
        if not offsets or offset >= offsets[-1] + min_frames:
            offsets.append(offset)
    if not offsets:
        offsets = [0]

    while len(offsets) > 1 and audio_frames - offsets[-1] < min_frames:
        offsets.pop()

    end = audio_frames
    if end <= offsets[-1]:
        end = offsets[-1] + min_frames
    return [int(timeline_start) + offset for offset in offsets] + [int(timeline_start) + end]


def spread_photo_order(count: int, rng: random.Random) -> list[int]:
    """Permutation of the sorted photos where successive picks stay far apart.

    Sorted order is the stand-in for likeness: ``IMG_100`` sits next to
    ``IMG_101``. A stride coprime to the count visits every photo while
    jumping about half the roll, so a burst is not placed as a run. When no
    stride can jump by more than one (a few small counts), the two halves
    are interleaved instead.
    """

    if count <= 1:
        return list(range(count))
    if count == 2:
        order = [0, 1]
        rng.shuffle(order)
        return order

    coprime = [stride for stride in range(1, count) if math.gcd(stride, count) == 1]
    best_gap = max(min(stride, count - stride) for stride in coprime)
    if best_gap >= 2:
        wide = [stride for stride in coprime if min(stride, count - stride) == best_gap]
        stride = rng.choice(wide)
        start = rng.randrange(count)
        return [(start + index * stride) % count for index in range(count)]

    half = (count + 1) // 2
    order: list[int] = []
    for index in range(half):
        order.append(index)
        other = index + half
        if other < count:
            order.append(other)
    shift = rng.randrange(count)
    order = order[shift:] + order[:shift]
    if rng.randrange(2):
        order.reverse()
    return order


def shuffle_stills(photos: list[PoolClip], seed: int | None) -> tuple[list[PoolClip], int]:
    """Reorder stills so neighbors in filename order are not neighbors on the timeline."""

    if len(photos) < 3:
        return photos, 0
    chosen = seed if seed is not None else random.SystemRandom().randrange(1, 2**31 - 1)
    order = spread_photo_order(len(photos), random.Random(chosen))
    return [photos[index] for index in order], chosen


def assign_photo_indexes(interval_count: int, photo_count: int, loop: bool) -> list[int]:
    """Choose which still fills each interval.

    With ``loop``, indexes wrap. Without it, placement stops when the stills
    run out and the extra intervals stay empty for the markers.
    """

    if interval_count <= 0 or photo_count <= 0:
        return []
    count = interval_count if loop else min(interval_count, photo_count)
    return [index % photo_count for index in range(count)]


def source_end_frame(duration_frames: int, *, inclusive: bool) -> int:
    """Source out-point for a still that starts at frame 0.

    The installed Resolve example treats ``endFrame`` as inclusive:
    ``startFrame=0`` and ``endFrame=23`` is the first 24 frames. Pass
    ``inclusive=False`` only when a build measures duration as
    ``endFrame - startFrame`` with an exclusive out-point.
    """

    if duration_frames < 1:
        raise ValueError("duration_frames must be at least 1")
    if inclusive:
        return duration_frames - 1
    return duration_frames


def clip_infos_for_boundaries(
    items: Sequence[Any],
    boundaries: Sequence[int],
    *,
    inclusive_end: bool,
    media_type: int,
    track_index: int = 1,
) -> list[dict[str, Any]]:
    """Build one ``AppendToTimeline`` dict per item.

    ``len(boundaries)`` must be ``len(items) + 1``. Record frames are absolute
    timeline frames. Consecutive record frames differ by ``end - start``, so
    the batch has no gap and no overlap when Resolve honors ``recordFrame``.
    """

    if len(boundaries) != len(items) + 1:
        raise ValueError("boundaries must contain one more entry than items")
    infos: list[dict[str, Any]] = []
    for index, item in enumerate(items):
        start_frame = int(boundaries[index])
        end_frame = int(boundaries[index + 1])
        duration = end_frame - start_frame
        if duration < 1:
            raise ValueError(f"interval {index} has no frames")
        infos.append(
            {
                "mediaPoolItem": item,
                "startFrame": 0,
                "endFrame": source_end_frame(duration, inclusive=inclusive_end),
                "trackIndex": track_index,
                "recordFrame": start_frame,
                "mediaType": media_type,
            }
        )
    return infos


def load_resolve_module() -> Any:
    """Import ``DaVinciResolveScript`` after setting the macOS module paths.

    The environment variables are set before the import because
    ``DaVinciResolveScript.py`` reads ``RESOLVE_SCRIPT_LIB`` while it loads
    ``fusionscript.so``.
    """

    os.environ["RESOLVE_SCRIPT_API"] = RESOLVE_SCRIPT_API
    os.environ["RESOLVE_SCRIPT_LIB"] = RESOLVE_SCRIPT_LIB

    module_dir = os.path.normpath(RESOLVE_MODULE_DIR)
    already_present = any(
        os.path.normpath(entry) == module_dir for entry in sys.path if entry
    )
    if not already_present:
        sys.path.append(RESOLVE_MODULE_DIR)

    import DaVinciResolveScript as dvr_script

    return dvr_script


def resolve_process_running() -> bool | None:
    """True when the Resolve app process is up. None if the check cannot run.

    Match the process name exactly. A ``pgrep -f`` pattern would also match
    the pgrep command itself and report Resolve as running when it is not.
    """

    try:
        exact = subprocess.run(
            ["pgrep", "-x", "Resolve"],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return None
    return exact.returncode == 0


def connect_resolve() -> ResolveSession:
    """Attach to the running session, project, and active timeline.

    Raises ``BeatCutterError`` with a specific cause when Resolve is closed,
    external scripting is not set to Local, or no timeline is open.
    """

    if sys.platform != "darwin":
        raise BeatCutterError("This script supports macOS and the Resolve paths used there.")
    if not Path(RESOLVE_SCRIPT_LIB).is_file():
        raise BeatCutterError(
            "DaVinci Resolve does not appear to be installed. Expected the scripting "
            f"library at {RESOLVE_SCRIPT_LIB}."
        )
    if not Path(RESOLVE_MODULE_DIR, "DaVinciResolveScript.py").is_file():
        raise BeatCutterError(
            "The Resolve scripting module was not found at "
            f"{RESOLVE_MODULE_DIR}. Reinstall DaVinci Resolve Studio."
        )

    try:
        dvr_script = load_resolve_module()
    except ImportError as exc:
        raise BeatCutterError(
            "Could not load DaVinciResolveScript. Install DaVinci Resolve Studio and "
            f"confirm {RESOLVE_SCRIPT_LIB} is present. ({exc})"
        ) from exc

    resolve = dvr_script.scriptapp("Resolve")
    if not resolve:
        running = resolve_process_running()
        if running is False:
            raise BeatCutterError(
                "DaVinci Resolve isn't open. Launch DaVinci Resolve Studio, open a project, "
                "and open the timeline you want to edit."
            )
        raise BeatCutterError(
            "Resolve did not return a scripting instance. Open Preferences > System > "
            "General and set External scripting using to Local, then quit and relaunch Resolve."
        )

    project_manager = resolve.GetProjectManager()
    project = project_manager.GetCurrentProject() if project_manager else None
    if not project:
        raise BeatCutterError(
            "No project is open in DaVinci Resolve. Open a project and try again."
        )

    timeline = project.GetCurrentTimeline()
    if not timeline:
        raise BeatCutterError(
            "No timeline is active. Open a timeline in the current project and try again."
        )

    media_pool = project.GetMediaPool()
    if not media_pool:
        raise BeatCutterError("The current project has no Media Pool.")

    fps = read_timeline_frame_rate(timeline)
    start = timeline.GetStartFrame()
    if start is None:
        raise BeatCutterError("Could not read the timeline start frame.")

    log.info(
        "Connected to '%s' / '%s' at %s fps, start frame %s.",
        project.GetName(),
        timeline.GetName(),
        format_fps(fps),
        int(start),
    )
    return ResolveSession(
        resolve=resolve,
        project=project,
        timeline=timeline,
        media_pool=media_pool,
        fps=fps,
        timeline_start=int(start),
    )


def read_timeline_frame_rate(timeline: Any) -> float:
    """Read ``timelineFrameRate`` and convert fractional rates exactly.

    Current Studio builds expose ``GetSettings()``. Older builds expose
    ``GetSetting("timelineFrameRate")``, which is what this calls first when
    the method is still present.
    """

    raw: object = None
    get_setting = getattr(timeline, "GetSetting", None)
    if callable(get_setting):
        try:
            raw = get_setting("timelineFrameRate")
        except Exception:
            raw = None
        if raw not in (None, "", False):
            # Honor the documented accessor, including values that are already floats.
            if isinstance(raw, (int, float)) and not isinstance(raw, bool):
                raw = float(raw)
    if raw in (None, "", False):
        get_settings = getattr(timeline, "GetSettings", None)
        settings = get_settings() if callable(get_settings) else None
        if not isinstance(settings, dict):
            raise BeatCutterError("Could not read timeline settings from Resolve.")
        raw = settings.get("timelineFrameRate")
    if raw in (None, "", False):
        raise BeatCutterError(
            "The active timeline has no timelineFrameRate. Set it in Project Settings."
        )
    try:
        return parse_frame_rate(raw)
    except (TypeError, ValueError) as exc:
        raise BeatCutterError(f"Unrecognized timeline frame rate: {raw!r}.") from exc


def analyze_audio(config: JobConfig) -> Analysis:
    """Load the track and detect either a beat grid or low-frequency onsets."""

    log.info("Loading librosa. The first import can take a few seconds.")
    try:
        import librosa
        import numpy as np
    except ImportError as exc:
        raise BeatCutterError(
            "Audio analysis libraries are missing. From the beat_cutter folder run: "
            "python3 -m pip install -r requirements.txt"
        ) from exc

    path = config.audio_path
    log.info("Reading %s", path)
    try:
        samples, sample_rate = librosa.load(path, sr=None, mono=True)
    except Exception as exc:
        raise BeatCutterError(
            f"Could not decode '{Path(path).name}': {exc}. "
            "For .m4a, install ffmpeg (brew install ffmpeg) and try again."
        ) from exc

    samples = np.asarray(samples, dtype=np.float32)
    if samples.size == 0 or int(sample_rate) <= 0:
        raise BeatCutterError(f"The audio file is empty: {path}")

    duration = float(librosa.get_duration(y=samples, sr=sample_rate))
    if duration <= 0:
        raise BeatCutterError(f"The audio file has no duration: {path}")
    log.info("Audio is %.2fs at %d Hz.", duration, int(sample_rate))

    if config.mode == "beats":
        event_times, tempo = detect_beats(librosa, np, samples, int(sample_rate), duration)
    else:
        event_times = detect_drops(
            librosa,
            np,
            samples,
            int(sample_rate),
            duration,
            lowpass_hz=config.lowpass_hz,
        )
        tempo = None

    if not event_times:
        hint = (
            "No beats were detected. Try --mode drops, or a track with a clearer pulse."
            if config.mode == "beats"
            else "No low-frequency onsets were detected. Try --mode beats, or lower --lowpass-hz."
        )
        raise BeatCutterError(hint)

    if tempo is not None and len(event_times) > 1:
        gaps = [event_times[index + 1] - event_times[index] for index in range(len(event_times) - 1)]
        log.info(
            "Detected %d pulses, %.3fs to %.3fs apart.",
            len(event_times),
            min(gaps),
            max(gaps),
        )
    elif tempo is not None:
        log.info("Detected %d pulse(s).", len(event_times))
    else:
        log.info("Detected %d low-frequency onsets under %.0f Hz.", len(event_times), config.lowpass_hz)
    return Analysis(
        duration_sec=duration,
        sample_rate=int(sample_rate),
        event_times=event_times,
        tempo_bpm=tempo,
        mode=config.mode,
    )


def pulse_peak_frames(
    onset: Any,
    sample_rate: int,
    hop_length: int,
    min_interval_sec: float = 0.05,
    relative_height: float = 0.05,
) -> list[int]:
    """Frame indexes of attacks, without locking them to one tempo.

    ``onset`` is a median-aggregated onset envelope: quiet between hits and
    spiked on each attack. Peaks weaker than ``relative_height`` of the
    loudest attack are ignored, and peaks closer than ``min_interval_sec``
    collapse to one hit.
    """

    import numpy as np
    from scipy.signal import find_peaks

    envelope = np.asarray(onset, dtype=float).reshape(-1)
    if envelope.size < 3 or sample_rate <= 0 or hop_length <= 0:
        return []
    strongest = float(np.max(envelope))
    if not math.isfinite(strongest) or strongest <= 1e-8:
        return []
    distance = max(1, int(round(min_interval_sec * sample_rate / hop_length)))
    height = strongest * relative_height
    peaks, _ = find_peaks(envelope, height=height, distance=distance, prominence=height)
    return [int(index) for index in peaks]


def detect_beats(librosa: Any, np: Any, samples: Any, sample_rate: int, duration: float) -> tuple[list[float], float]:
    """Attack times of the pulse, including one that speeds up or slows down.

    ``beat_track`` fits a single tempo. A tick that starts fast and then
    opens up is not on that grid, so the cuts miss the ticks. Median
    aggregation leaves one spike per attack, and those spikes are used as
    the cut times.
    """

    hop = 256
    onset = librosa.onset.onset_strength(
        y=samples,
        sr=sample_rate,
        hop_length=hop,
        aggregate=np.median,
    )
    frames = pulse_peak_frames(onset, sample_rate, hop)
    times = librosa.frames_to_time(np.asarray(frames, dtype=float), sr=sample_rate, hop_length=hop)
    events = coerce_event_times(librosa, times, sample_rate, hop, duration)
    tempo = 0.0
    if len(events) > 1:
        gaps = [events[index + 1] - events[index] for index in range(len(events) - 1)]
        median_gap = float(np.median(gaps))
        if median_gap > 0:
            tempo = 60.0 / median_gap
    return events, tempo


def detect_drops(
    librosa: Any,
    np: Any,
    samples: Any,
    sample_rate: int,
    duration: float,
    *,
    lowpass_hz: float,
) -> list[float]:
    """Onsets of kick and bass energy, backtracked to the start of each hit.

    A zero-phase Butterworth low-pass removes hats and cymbals before onset
    detection, and ``backtrack=True`` pulls each hit to the local energy
    minimum so the cut lands on the attack. Double-kicks are removed later
    by ``min_cut_duration_sec`` rather than inside the detector, so the first
    hit keeps the backtracked attack instead of being merged away.
    """

    if sample_rate <= lowpass_hz * 2:
        raise BeatCutterError(
            f"Sample rate {sample_rate} Hz is too low for a {lowpass_hz:.0f} Hz low-pass."
        )
    try:
        from scipy.signal import butter, sosfiltfilt
    except ImportError as exc:
        raise BeatCutterError(
            "scipy is required for drop detection. Run: python3 -m pip install -r requirements.txt"
        ) from exc

    sos = butter(4, lowpass_hz, btype="lowpass", fs=sample_rate, output="sos")
    try:
        filtered = sosfiltfilt(sos, np.asarray(samples, dtype=np.float64))
    except ValueError as exc:
        raise BeatCutterError(
            f"The low-pass filter could not run on this file ({exc}). The track may be too short."
        ) from exc

    peak = float(np.max(np.abs(filtered))) if filtered.size else 0.0
    if peak > 0:
        filtered = filtered / peak

    hop = HOP_LENGTH
    frames = librosa.onset.onset_detect(
        y=filtered,
        sr=sample_rate,
        hop_length=hop,
        backtrack=True,
        units="frames",
    )
    times = librosa.frames_to_time(frames, sr=sample_rate, hop_length=hop)
    return coerce_event_times(librosa, times, sample_rate, hop, duration)


def coerce_event_times(
    librosa: Any,
    values: Any,
    sample_rate: int,
    hop_length: int,
    duration: float,
) -> list[float]:
    """Normalize librosa output to seconds and drop events outside the file."""

    import numpy as np

    array = np.atleast_1d(np.asarray(values, dtype=float)).reshape(-1)
    times = [float(value) for value in array if math.isfinite(float(value))]
    if times and max(times) > duration + 1.0:
        times = [
            float(value)
            for value in librosa.frames_to_time(times, sr=sample_rate, hop_length=hop_length)
        ]
    return sorted(value for value in times if 0.0 <= value < duration)


def plan_edit(config: JobConfig, analysis: Analysis, fps: float, timeline_start: int) -> list[int]:
    """Turn detected events into absolute, abutting timeline frame boundaries."""

    cuts = build_cut_seconds(
        analysis.event_times,
        analysis.duration_sec,
        beats_per_cut=config.beats_per_cut,
        min_cut_duration_sec=config.min_cut_duration_sec,
        cover_from_start=config.cover_from_start,
    )
    boundaries = build_boundaries(
        cuts,
        analysis.duration_sec,
        fps,
        timeline_start,
        config.min_cut_duration_sec,
    )
    intervals = len(boundaries) - 1
    if intervals < 1:
        raise BeatCutterError("Beat detection produced no intervals to cut.")
    widths = [boundaries[index + 1] - boundaries[index] for index in range(intervals)]
    log.info(
        "Kept %d cuts from %d events (%d photos, %.2fs minimum). "
        "Shortest hold %d frames, longest %d frames.",
        intervals,
        len(analysis.event_times),
        intervals,
        config.min_cut_duration_sec,
        min(widths),
        max(widths),
    )
    if max(widths) > int(round(fps * 8)):
        log.warning(
            "The longest hold is %s. Detection may have missed the end of the track.",
            frames_to_timecode(max(widths), fps),
        )
    return boundaries


def log_dry_run(config: JobConfig, analysis: Analysis) -> None:
    """Print the second-accurate cut list without opening Resolve."""

    cuts = build_cut_seconds(
        analysis.event_times,
        analysis.duration_sec,
        beats_per_cut=config.beats_per_cut,
        min_cut_duration_sec=config.min_cut_duration_sec,
        cover_from_start=config.cover_from_start,
    )
    points = list(cuts) + [analysis.duration_sec]
    log.info(
        "Dry run: %d cut points over %.2fs. Frame snapping happens once a timeline is open.",
        len(cuts),
        analysis.duration_sec,
    )
    preview = len(points) if config.verbose else min(len(points), 13)
    for index in range(preview - 1):
        gap = points[index + 1] - points[index]
        log.info("  %3d  %8.3fs   hold %6.3fs", index + 1, points[index], gap)
    if preview < len(points):
        log.info("  … %d more cuts. Pass --verbose to print them all.", len(points) - preview)


def as_list(value: Any) -> list[Any]:
    """Normalize Resolve values that are either a list or an older dict."""

    if not value:
        return []
    if isinstance(value, dict):
        return list(value.values())
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def clip_properties(item: Any) -> dict[str, Any]:
    getter = getattr(item, "GetClipProperty", None)
    if not callable(getter):
        return {}
    try:
        props = getter()
    except TypeError:
        props = getter(None)
    return props if isinstance(props, dict) else {}


def normalize_media_path(path: str) -> str:
    """Comparison key for a media path.

    macOS stores many photo names in decomposed Unicode, and the default
    volume is case-insensitive. Resolve may return either form.
    """

    text = (path or "").strip()
    if text.startswith("file://"):
        text = text[7:]
    if not text:
        return ""
    try:
        text = os.path.realpath(text)
    except OSError:
        text = os.path.abspath(text)
    text = unicodedata.normalize("NFC", os.path.normpath(text))
    if sys.platform == "darwin":
        text = text.casefold()
    return text


def frame_count_of(props: dict[str, Any]) -> int | None:
    raw = props.get("Frames")
    try:
        value = int(float(str(raw)))
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def pool_clip_from_item(item: Any) -> PoolClip:
    props = clip_properties(item)
    name = str(props.get("File Name") or props.get("Clip Name") or "").strip()
    if not name:
        get_name = getattr(item, "GetName", None)
        name = str(get_name() if callable(get_name) else "") or "clip"
    path = normalize_media_path(str(props.get("File Path") or ""))
    return PoolClip(item=item, name=name, path=path, frames=frame_count_of(props))


def walk_folders(folder: Any) -> Iterable[Any]:
    yield folder
    for subfolder in as_list(folder.GetSubFolderList()):
        yield from walk_folders(subfolder)


def walk_clips(folder: Any) -> Iterable[PoolClip]:
    for current in walk_folders(folder):
        for item in as_list(current.GetClipList()):
            yield pool_clip_from_item(item)


def find_bin(root: Any, name: str) -> Any | None:
    for folder in walk_folders(root):
        get_name = getattr(folder, "GetName", None)
        if callable(get_name) and get_name() == name and folder is not root:
            return folder
    return None


def ensure_bin(media_pool: Any, name: str) -> Any:
    root = media_pool.GetRootFolder()
    if not root:
        raise BeatCutterError("The Media Pool has no root folder.")
    existing = find_bin(root, name)
    if existing is not None:
        return existing
    created = media_pool.AddSubFolder(root, name)
    if not created:
        raise BeatCutterError(f"Could not create the Media Pool bin '{name}'.")
    log.info("Created Media Pool bin '%s'.", name)
    return created


def is_still(clip: PoolClip) -> bool:
    suffix = Path(clip.path).suffix.lower() if clip.path else Path(clip.name).suffix.lower()
    if suffix in IMAGE_EXTENSIONS:
        return True
    props = clip_properties(clip.item)
    kind = str(props.get("Type") or "").lower()
    image_format = str(props.get("Format") or "").lower()
    if "still" in kind:
        return True
    return any(token in image_format for token in ("jpeg", "png", "tiff", "bmp", "heif", "webp", "dpx", "exr"))


def list_image_files(folder: Path) -> list[Path]:
    files = [
        path
        for path in folder.iterdir()
        if path.is_file() and not path.name.startswith(".") and path.suffix.lower() in IMAGE_EXTENSIONS
    ]
    return sorted(files, key=lambda path: natural_sort_key(path.name))


def find_clip_by_path(root: Any, path: Path) -> PoolClip | None:
    target = normalize_media_path(str(path))
    for clip in walk_clips(root):
        if clip.path and clip.path == target:
            return clip
    return None


def import_with_fallback(session: ResolveSession, folder: Any, paths: Sequence[Path]) -> None:
    """Import paths, then retry through Media Storage if the pool import is empty."""

    if not paths:
        return
    root = session.media_pool.GetRootFolder()
    known = {clip.path for clip in walk_clips(root) if clip.path}
    missing = [path for path in paths if normalize_media_path(str(path)) not in known]
    if not missing:
        log.info("All %d file(s) are already in the Media Pool.", len(paths))
        return
    if not session.media_pool.SetCurrentFolder(folder):
        raise BeatCutterError(f"Could not select the '{folder.GetName()}' bin.")
    log.info("Importing %d file(s) into '%s'.", len(missing), folder.GetName())
    imported = as_list(session.media_pool.ImportMedia([{"FilePath": str(path)} for path in missing]))
    if imported:
        return

    storage = session.resolve.GetMediaStorage() if hasattr(session.resolve, "GetMediaStorage") else None
    if storage is None:
        raise BeatCutterError(
            "Resolve did not import the files. Check that the paths are readable and that "
            "the Media Pool bin is not locked by a collaboration session."
        )
    log.warning("ImportMedia returned no clips. Trying Media Storage.")
    storage.AddItemListToMediaPool([{"media": str(path)} for path in missing])


def collect_stills(session: ResolveSession, config: JobConfig, bin_folder: Any) -> list[PoolClip]:
    """Stills from the photo folder, or every still already in the bin."""

    root = session.media_pool.GetRootFolder()
    if config.photo_dir:
        files = list_image_files(Path(config.photo_dir))
        if not files:
            raise BeatCutterError(
                f"No still images in {config.photo_dir}. "
                "The scan is not recursive. Move the photos into this folder, "
                f"or leave it blank to use the '{config.media_pool_bin}' bin."
            )
        import_with_fallback(session, bin_folder, files)
        wanted = {normalize_media_path(str(path)) for path in files}
        found: dict[str, PoolClip] = {}
        for clip in walk_clips(root):
            if clip.path in wanted and is_still(clip):
                found.setdefault(clip.path, clip)
        missing_names = [
            path.name for path in files if normalize_media_path(str(path)) not in found
        ]
        if missing_names:
            preview = ", ".join(missing_names[:8])
            extra = "" if len(missing_names) <= 8 else f" (+{len(missing_names) - 8} more)"
            raise BeatCutterError(
                f"Resolve did not import {len(missing_names)} photo(s): {preview}{extra}."
            )
        ordered = [found[normalize_media_path(str(path))] for path in files]
    else:
        ordered = [clip for clip in walk_clips(bin_folder) if is_still(clip)]
        ordered.sort(key=lambda clip: natural_sort_key(clip.name))
        if not ordered:
            raise BeatCutterError(
                f"The Media Pool bin '{config.media_pool_bin}' has no stills. "
                "Add photos to that bin, or pass --photo-dir."
            )
    log.info("Using %d still(s), sorted by filename.", len(ordered))
    return ordered


def ensure_still_duration(clip: PoolClip, needed_frames: int, fps: float) -> None:
    """Ask Resolve to extend a still that is shorter than the longest hold.

    Many builds keep the preference **Standard still duration** as a hard cap.
    When the property write does not stick, placement verification reports it.
    """

    if clip.frames is not None and clip.frames >= needed_frames:
        return
    target = needed_frames + max(1, int(round(fps)))
    timecode = frames_to_timecode(target, fps)
    setter = getattr(clip.item, "SetClipProperty", None)
    if not callable(setter):
        return
    try:
        setter("Duration", timecode)
    except Exception as exc:
        log.debug("Could not set still duration on %s: %s", clip.name, exc)
    refreshed = frame_count_of(clip_properties(clip.item))
    if refreshed is not None:
        # PoolClip is frozen so later holds of the same still can skip this write.
        object.__setattr__(clip, "frames", refreshed)


def still_source_path(clip: PoolClip) -> Path:
    """Filesystem path for a media-pool still, before macOS case-folding."""

    raw = str(clip_properties(clip.item).get("File Path") or "").strip()
    path = Path(raw) if raw else Path(clip.path)
    if not path.is_file():
        raise BeatCutterError(f"Cannot read '{clip.name}' at {path}.")
    return path


def holder_stem(source: Path) -> str:
    cleaned = re.sub(r"[^\w.\-]+", "_", source.stem).strip()
    return cleaned or "still"


def holder_directory(identity: str, frames: int, cache: Path | None = None) -> Path:
    digest = hashlib.sha1(identity.encode("utf-8")).hexdigest()[:12]
    return (cache or HOLDER_CACHE) / f"photo_{digest}_{frames}"


def write_holder_sequence(
    source: Path,
    identity: str,
    frames: int,
    cache: Path | None = None,
) -> Path:
    """Hardlink ``frames`` copies of a still into a numbered sequence.

    Resolve stores a JPEG as one source frame and ignores ``endFrame`` when
    that still is appended, so the timeline clip becomes the standard still
    duration (5 seconds here) instead of the beat. A numbered sequence is a
    real clip: the out-point trims it. Hardlinks share the original bytes.
    """

    if frames < 2:
        raise BeatCutterError("A hold sequence needs at least 2 frames.")
    directory = holder_directory(identity, frames, cache)
    directory.mkdir(parents=True, exist_ok=True)
    width = max(4, len(str(frames - 1)))
    suffix = source.suffix.lower() or ".jpg"
    stem = holder_stem(source)
    expected = [directory / f"{stem}_{index:0{width}d}{suffix}" for index in range(frames)]
    present = {path.name for path in directory.iterdir()}
    if present != {path.name for path in expected}:
        for path in directory.iterdir():
            path.unlink()
        present = set()
    for dest in expected:
        if dest.name in present:
            continue
        try:
            os.link(source, dest)
        except OSError:
            shutil.copy2(source, dest)
    return directory


def sequence_pattern(directory: Path, frames: int, source: Path) -> Path:
    width = max(4, len(str(frames - 1)))
    suffix = source.suffix.lower() or ".jpg"
    return directory / f"{holder_stem(source)}_%0{width}d{suffix}"


def find_holder_clip(root: Any, directory_name: str, frames: int) -> PoolClip | None:
    token = directory_name.casefold()
    for clip in walk_clips(root):
        if token not in clip.path.casefold() and token not in clip.name.casefold():
            continue
        if clip.frames is not None and clip.frames < frames:
            continue
        return clip
    return None


def import_still_holder(
    session: ResolveSession,
    folder: Any,
    clip: PoolClip,
    frames: int,
) -> PoolClip:
    """Import one still as a sequence long enough for its longest hold."""

    source = still_source_path(clip)
    directory = write_holder_sequence(source, clip.path or str(source), frames)
    existing = find_holder_clip(session.media_pool.GetRootFolder(), directory.name, frames)
    if existing is not None:
        return existing
    if not session.media_pool.SetCurrentFolder(folder):
        raise BeatCutterError(f"Could not select the '{folder.GetName()}' bin.")
    pattern = sequence_pattern(directory, frames, source)
    log.info("Importing a %d-frame hold of %s.", frames, clip.name)
    imported = as_list(
        session.media_pool.ImportMedia(
            [{"FilePath": str(pattern), "StartIndex": 0, "EndIndex": frames - 1}]
        )
    )
    imported = [item for item in imported if item]
    if not imported:
        storage = session.resolve.GetMediaStorage() if hasattr(session.resolve, "GetMediaStorage") else None
        if storage is not None:
            log.warning("ImportMedia returned no hold sequence. Trying Media Storage.")
            storage.AddItemListToMediaPool(
                [{"media": str(pattern), "startFrame": 0, "endFrame": frames - 1}]
            )
    found = find_holder_clip(session.media_pool.GetRootFolder(), directory.name, frames)
    if found is None and imported:
        found = pool_clip_from_item(imported[0])
    if found is None:
        raise BeatCutterError(f"Resolve did not import the hold sequence for {clip.name}.")
    if found.frames is not None and found.frames < frames:
        raise BeatCutterError(
            f"The hold sequence for {clip.name} has {found.frames} frames; {frames} are required."
        )
    return found


def prepare_still_holders(
    session: ResolveSession,
    photos: Sequence[PoolClip],
    durations: Sequence[int],
) -> list[PoolClip]:
    """One trimmable sequence per still, reused when that still is looped."""

    if len(photos) != len(durations):
        raise BeatCutterError("Photo and duration counts do not match.")
    required: dict[str, tuple[PoolClip, int]] = {}
    for clip, duration in zip(photos, durations):
        # One spare frame so an exclusive out-point (endFrame == duration) is in range.
        frames = int(duration) + 1
        current = required.get(clip.path)
        if current is None or frames > current[1]:
            required[clip.path] = (clip, frames)
    log.info(
        "Preparing %d hold sequence(s). A still's timeline length is fixed by "
        "Resolve, so each photo is linked into a short frame sequence that can be trimmed.",
        len(required),
    )
    folder = ensure_bin(session.media_pool, HOLDER_BIN)
    holders = {
        path: import_still_holder(session, folder, clip, frames)
        for path, (clip, frames) in required.items()
    }
    return [
        PoolClip(
            item=holders[clip.path].item,
            name=clip.name,
            path=clip.path,
            frames=holders[clip.path].frames,
        )
        for clip in photos
    ]


def ensure_track(timeline: Any, track_type: str) -> None:
    count = int(timeline.GetTrackCount(track_type) or 0)
    if count < 1:
        if not timeline.AddTrack(track_type):
            raise BeatCutterError(f"The timeline has no {track_type} track, and adding one failed.")
    locked = False
    try:
        locked = bool(timeline.GetIsTrackLocked(track_type, 1))
    except Exception:
        locked = False
    if locked:
        raise BeatCutterError(
            f"Timeline {track_type} track 1 is locked. Unlock it in Resolve and run the script again."
        )


def track_items(timeline: Any, track_type: str, index: int = 1) -> list[Any]:
    return as_list(timeline.GetItemListInTrack(track_type, index))


def guard_existing_clips(session: ResolveSession, config: JobConfig) -> None:
    """Refuse to stack a new edit on top of clips already on V1 or A1."""

    ensure_track(session.timeline, "video")
    ensure_track(session.timeline, "audio")
    video = track_items(session.timeline, "video")
    audio = track_items(session.timeline, "audio")
    if not video and not audio:
        return
    if not config.overwrite:
        raise BeatCutterError(
            f"Video Track 1 has {len(video)} clip(s) and Audio Track 1 has {len(audio)} clip(s). "
            "Open an empty timeline, or run again with --overwrite to replace those two tracks."
        )
    log.info("Removing %d clip(s) from V1 and %d from A1.", len(video), len(audio))
    if video and not session.timeline.DeleteClips(video, False):
        raise BeatCutterError("Could not clear Video Track 1. Unlock the track and try again.")
    if audio and not session.timeline.DeleteClips(audio, False):
        raise BeatCutterError("Could not clear Audio Track 1. Unlock the track and try again.")


def place_audio(session: ResolveSession, audio_clip: PoolClip) -> None:
    info = {
        "mediaPoolItem": audio_clip.item,
        "trackIndex": 1,
        "recordFrame": session.timeline_start,
        "mediaType": 2,
    }
    placed = as_list(session.media_pool.AppendToTimeline([info]))
    if not placed:
        raise BeatCutterError(
            f"Could not place '{audio_clip.name}' on Audio Track 1. "
            "Confirm the timeline has an unlocked audio track."
        )
    log.info("Placed audio on A1 at frame %d (%s).", session.timeline_start, audio_clip.name)


def item_duration(item: Any) -> int | None:
    try:
        return int(round(float(item.GetDuration())))
    except (TypeError, ValueError, AttributeError):
        return None


def item_start(item: Any) -> int | None:
    try:
        return int(round(float(item.GetStart())))
    except (TypeError, ValueError, AttributeError):
        return None


def place_photos(
    session: ResolveSession,
    photos: Sequence[PoolClip],
    boundaries: Sequence[int],
) -> list[Any]:
    """Append every photo in one batch, repairing an exclusive out-point if needed.

    The timeline interval is ``end_frame - start_frame``. Resolve's own
    subclip example treats the source ``endFrame`` as inclusive, so the value
    written into the batch is ``duration - 1``. If every placed clip then
    comes back one frame short, the batch is rebuilt with an exclusive out-point.
    """

    count = len(photos)
    if count < 1:
        raise BeatCutterError("There are no photos to place.")
    if len(boundaries) < count + 1:
        raise BeatCutterError("Not enough beat boundaries for the photos.")

    used = list(boundaries[: count + 1])
    log.info("Appending %d photo(s) to Video Track 1 in one batch.", count)
    placed, convention = append_photos(session, photos, used, inclusive_end=True)
    if duration_mismatch(placed, used) != "ok":
        inclusive_score = duration_matches(placed, used)
        log.warning(
            "Inclusive out-points did not match the beat grid. Re-placing photos."
        )
        if placed and not session.timeline.DeleteClips(placed, False):
            raise BeatCutterError("Could not remove the first photo pass from Video Track 1.")
        exclusive, exclusive_name = append_photos(session, photos, used, inclusive_end=False)
        if duration_matches(exclusive, used) >= inclusive_score:
            placed, convention = exclusive, exclusive_name
        else:
            log.warning("Exclusive out-points matched fewer clips. Restoring the inclusive batch.")
            if exclusive and not session.timeline.DeleteClips(exclusive, False):
                raise BeatCutterError("Could not remove the exclusive photo pass from Video Track 1.")
            placed, convention = append_photos(session, photos, used, inclusive_end=True)
    log.info("Photo batch used %s source end frames.", convention)
    for item, clip in zip(placed, photos):
        renamer = getattr(item, "SetName", None)
        if not callable(renamer):
            continue
        try:
            renamer(clip.name)
        except Exception as exc:
            log.debug("Could not rename %s: %s", clip.name, exc)
    return placed


def append_photos(
    session: ResolveSession,
    photos: Sequence[PoolClip],
    boundaries: Sequence[int],
    *,
    inclusive_end: bool,
) -> tuple[list[Any], str]:
    infos = clip_infos_for_boundaries(
        [clip.item for clip in photos],
        boundaries,
        inclusive_end=inclusive_end,
        media_type=1,
        track_index=1,
    )
    placed = as_list(session.media_pool.AppendToTimeline(infos))
    placed = [item for item in placed if item]
    placed.sort(key=lambda item: item_start(item) if item_start(item) is not None else 0)
    convention = "inclusive" if inclusive_end else "exclusive"
    return placed, convention


def expected_durations(boundaries: Sequence[int], count: int) -> list[int]:
    return [int(boundaries[index + 1]) - int(boundaries[index]) for index in range(count)]


def duration_matches(placed: Sequence[Any], boundaries: Sequence[int]) -> int:
    """How many placed clips have the planned duration. ``-1`` if the count differs."""

    expected = expected_durations(boundaries, len(boundaries) - 1)
    actual = [item_duration(item) for item in placed]
    if len(actual) != len(expected):
        return -1
    return sum(1 for got, want in zip(actual, expected) if got == want)


def duration_mismatch(placed: Sequence[Any], boundaries: Sequence[int]) -> str:
    """Classify the batch as ``ok``, ``short-one``, ``count``, or ``other``."""

    expected = expected_durations(boundaries, len(boundaries) - 1)
    actual = [item_duration(item) for item in placed]
    if len(actual) != len(expected) or any(value is None for value in actual):
        return "count"
    pairs = list(zip(actual, expected))
    if all(got == want for got, want in pairs):
        return "ok"
    if all(got == want - 1 and want > 1 for got, want in pairs):
        return "short-one"
    return "other"


def verify_placement(
    placed: Sequence[Any],
    boundaries: Sequence[int],
    fps: float,
) -> list[str]:
    """Check that placed clips start on the planned frames and abut each other."""

    expected_starts = [int(boundaries[index]) for index in range(len(boundaries) - 1)]
    expected = expected_durations(boundaries, len(expected_starts))
    problems: list[str] = []
    if len(placed) != len(expected_starts):
        problems.append(
            f"Resolve placed {len(placed)} of {len(expected_starts)} photos."
        )
        return problems

    starts: list[int] = []
    durations: list[int] = []
    for index, item in enumerate(placed):
        start = item_start(item)
        duration = item_duration(item)
        if start is None or duration is None:
            problems.append(f"Photo {index + 1} did not report a start or duration.")
            continue
        starts.append(start)
        durations.append(duration)
        if start != expected_starts[index]:
            problems.append(
                f"Photo {index + 1} starts at frame {start}, expected {expected_starts[index]}."
            )
        if duration != expected[index]:
            problems.append(
                f"Photo {index + 1} is {duration} frames, expected {expected[index]} "
                f"({frames_to_timecode(expected[index], fps)})."
            )

    if len(starts) == len(placed) and len(durations) == len(placed):
        for index in range(len(placed) - 1):
            gap = starts[index + 1] - (starts[index] + durations[index])
            if gap != 0:
                problems.append(
                    f"Gap of {gap} frame(s) between photo {index + 1} and photo {index + 2}."
                )
    return problems


def marker_frame_ids(markers: Any) -> set[int]:
    keys: set[int] = set()
    if not isinstance(markers, dict):
        return keys
    for key in markers:
        try:
            keys.add(int(key))
        except (TypeError, ValueError):
            continue
    return keys


def drain_script_markers(timeline: Any) -> int:
    """Remove markers this script created on an earlier run."""

    removed = 0
    for _ in range(10000):
        deleter = getattr(timeline, "DeleteMarkerByCustomData", None)
        if not callable(deleter) or not deleter(MARKER_CUSTOM_DATA):
            break
        removed += 1
    return removed


def marker_uses_absolute_frames(timeline: Any, absolute: int, timeline_start: int) -> bool:
    """Detect whether ``AddMarker`` wants absolute frames or a start-relative offset.

    Timelines that start at frame 0 use the same number either way, so the
    probe is skipped. The probe marker is deleted before real markers are added.
    """

    if timeline_start == 0:
        return True
    relative = absolute - timeline_start
    for frame_id, uses_absolute in ((absolute, True), (relative, False)):
        if frame_id < 0:
            continue
        added = bool(
            timeline.AddMarker(
                int(frame_id),
                "Blue",
                "Beat Cutter",
                "probe",
                1,
                MARKER_CUSTOM_DATA,
            )
        )
        if not added:
            continue
        keys = marker_frame_ids(timeline.GetMarkers())
        timeline.DeleteMarkerAtFrame(int(frame_id))
        drain_script_markers(timeline)
        if int(frame_id) in keys:
            return uses_absolute
    return True


def add_markers(
    session: ResolveSession,
    config: JobConfig,
    boundaries: Sequence[int],
    photo_names: Sequence[str],
) -> int:
    """Add a marker at every cut. Blue for the beat grid, red for drops."""

    color = "Blue" if config.mode == "beats" else "Red"
    label = "Beat" if config.mode == "beats" else "Drop"
    removed = drain_script_markers(session.timeline)
    if removed:
        log.info("Cleared %d previous Beat Cutter marker(s).", removed)

    cut_frames = [int(boundaries[index]) for index in range(len(boundaries) - 1)]
    if not cut_frames:
        return 0
    absolute = marker_uses_absolute_frames(
        session.timeline,
        cut_frames[0],
        session.timeline_start,
    )
    added = 0
    for index, frame in enumerate(cut_frames):
        frame_id = frame if absolute else frame - session.timeline_start
        offset = frame - session.timeline_start
        seconds = offset / session.fps
        duration = int(boundaries[index + 1]) - frame
        photo = photo_names[index] if index < len(photo_names) else "no photo"
        note = f"{seconds:.3f}s | {duration}f | {photo}"
        ok = bool(
            session.timeline.AddMarker(
                int(frame_id),
                color,
                f"{label} {index + 1:03d}",
                note,
                1,
                MARKER_CUSTOM_DATA,
            )
        )
        if ok:
            added += 1
        elif index < 5 or config.verbose:
            log.warning("Could not add a marker at frame %d. A marker may already be there.", frame)
    log.info("Added %d %s marker(s).", added, color)
    return added


def read_zoom_identity(item: Any) -> float:
    """Return the clip's current ZoomX, defaulting to the 1.0 = 100% API scale."""

    reader = getattr(item, "GetProperties", None)
    props = reader() if callable(reader) else None
    if isinstance(props, dict) and props.get("ZoomX") is not None:
        try:
            value = float(props["ZoomX"])
        except (TypeError, ValueError):
            value = 1.0
        if value > 10.0:
            return value
    return 1.0


def set_item_property(item: Any, key: str, value: Any) -> bool:
    """Set one timeline-item property, preferring ``SetProperty``."""

    setter = getattr(item, "SetProperty", None)
    if callable(setter):
        try:
            if setter(key, value):
                return True
        except Exception as exc:
            log.debug("SetProperty %s failed: %s", key, exc)
    bulk = getattr(item, "SetProperties", None)
    if callable(bulk):
        try:
            return bool(bulk({key: value}))
        except Exception as exc:
            log.debug("SetProperties %s failed: %s", key, exc)
    return False


def apply_zoom(item: Any, start_zoom: float, end_zoom: float, duration: int, dynamic: bool) -> str:
    """Apply ZoomX and ZoomY. Returns ``dynamic``, ``static``, or ``failed``."""

    hold = end_zoom
    set_item_property(item, "TransformEnabled", True)
    set_item_property(item, "ZoomGang", True)
    ok_x = set_item_property(item, "ZoomX", hold if not dynamic else start_zoom)
    ok_y = set_item_property(item, "ZoomY", hold if not dynamic else start_zoom)
    if not dynamic:
        return "static" if ok_x and ok_y else "failed"

    keyed = {0: float(start_zoom), max(int(duration) - 1, 0): float(end_zoom)}
    moved = False
    setter = getattr(item, "SetProperty", None)
    if callable(setter):
        try:
            moved = bool(setter("ZoomX", keyed)) and bool(setter("ZoomY", keyed))
        except Exception:
            moved = False
    if moved:
        return "dynamic"
    # This build rejected keyframed zoom. Leave the alternating static scale.
    settled_x = set_item_property(item, "ZoomX", end_zoom)
    settled_y = set_item_property(item, "ZoomY", end_zoom)
    return "static" if settled_x and settled_y else "failed"


def apply_zoom_pass(
    items: Sequence[Any],
    boundaries: Sequence[int],
    config: JobConfig,
) -> None:
    if not items:
        return
    identity = read_zoom_identity(items[0])
    near = identity
    far = identity * config.zoom_ratio
    dynamic = config.zoom_style == "dynamic"
    results: list[str] = []
    for index, item in enumerate(items):
        duration = int(boundaries[index + 1]) - int(boundaries[index])
        if index % 2 == 0:
            start_zoom, end_zoom = near, far
        else:
            start_zoom, end_zoom = far, near
        if not dynamic:
            start_zoom = end_zoom
        results.append(apply_zoom(item, start_zoom, end_zoom, duration, dynamic))
    failed = results.count("failed")
    if failed:
        log.warning("Zoom did not apply to %d clip(s).", failed)
    elif dynamic and "dynamic" not in results:
        log.info(
            "This Resolve build did not accept zoom keyframes. Applied an alternating %.1f%% scale instead.",
            (config.zoom_ratio - 1.0) * 100.0,
        )
    else:
        kind = "dynamic" if dynamic and "dynamic" in results else "static"
        log.info(
            "Applied %s zoom, alternating between %.3f and %.3f.",
            kind,
            near,
            far,
        )


def reveal_timeline(session: ResolveSession) -> None:
    opener = getattr(session.resolve, "OpenPage", None)
    if callable(opener):
        opener("edit")
    start_tc = session.timeline.GetStartTimecode() if hasattr(session.timeline, "GetStartTimecode") else None
    if start_tc:
        session.timeline.SetCurrentTimecode(start_tc)


def run_edit(config: JobConfig) -> None:
    """Analyze, import, and lay the open timeline."""

    config = config.prepare()
    if config.dry_run:
        log.info("Analyzing '%s' in %s mode. Resolve is not contacted.", Path(config.audio_path).name, config.mode)
        log_dry_run(config, analyze_audio(config))
        return

    log.info("[1/5] Connecting to DaVinci Resolve.")
    session = connect_resolve()
    log.info("[2/5] Analyzing '%s' in %s mode.", Path(config.audio_path).name, config.mode)
    analysis = analyze_audio(config)
    boundaries = plan_edit(config, analysis, session.fps, session.timeline_start)
    interval_count = len(boundaries) - 1

    log.info("[3/5] Collecting media in bin '%s'.", config.media_pool_bin)
    bin_folder = ensure_bin(session.media_pool, config.media_pool_bin)
    import_with_fallback(session, bin_folder, [Path(config.audio_path)])
    audio_clip = find_clip_by_path(session.media_pool.GetRootFolder(), Path(config.audio_path))
    if audio_clip is None:
        raise BeatCutterError(
            f"The audio file was not found in the Media Pool after import: {config.audio_path}"
        )
    photos = collect_stills(session, config, bin_folder)
    if config.shuffle_photos:
        photos, seed = shuffle_stills(photos, config.shuffle_seed)
        log.info(
            "Shuffled %d stills so nearby filenames are not adjacent (seed %d).",
            len(photos),
            seed,
        )
    indexes = assign_photo_indexes(interval_count, len(photos), config.loop_photos)
    if not indexes:
        raise BeatCutterError("There are no stills to place.")
    if not config.loop_photos and len(indexes) < interval_count:
        log.info(
            "loop_photos is off. Placing %d of %d intervals and leaving the rest marked only.",
            len(indexes),
            interval_count,
        )
    elif config.loop_photos and interval_count > len(photos):
        log.info(
            "Looping %d still(s) across %d intervals.",
            len(photos),
            interval_count,
        )

    chosen = [photos[index] for index in indexes]
    placement_boundaries = list(boundaries[: len(chosen) + 1])
    durations = expected_durations(placement_boundaries, len(chosen))
    longest = max(durations)
    holders = prepare_still_holders(session, chosen, durations)

    log.info("[4/5] Editing timeline '%s'.", session.timeline.GetName())
    guard_existing_clips(session, config)
    place_audio(session, audio_clip)
    placed = place_photos(session, holders, placement_boundaries)
    problems = verify_placement(placed, placement_boundaries, session.fps)

    log.info("[5/5] Markers and zoom.")
    names = [clip.name for clip in chosen]
    # Markers cover every detected cut, including intervals left empty when
    # the stills run out and looping is off.
    marker_names = list(names) + ["no photo"] * max(0, interval_count - len(names))
    add_markers(session, config, boundaries, marker_names)
    if config.apply_subtle_zoom and not problems:
        apply_zoom_pass(placed, placement_boundaries, config)
    reveal_timeline(session)

    if problems:
        preview = " ".join(problems[:6])
        extra = "" if len(problems) <= 6 else f" (+{len(problems) - 6} more; re-run with --verbose)"
        if config.verbose:
            for line in problems:
                log.error("%s", line)
            extra = ""
            preview = problems[0]
        raise BeatCutterError(
            "The timeline was written, but the clip lengths do not match the beat grid. "
            f"{preview}{extra} Clear Video Track 1 and run again with --overwrite."
        )

    log.info(
        "Done. %d photo(s) on V1, audio on A1, %d cut marker(s). Play from the start of '%s'.",
        len(placed),
        interval_count,
        session.timeline.GetName(),
    )


def prompt_with_gui(config: JobConfig) -> JobConfig:
    """Small file-picker form. Returns the config from the dialog."""

    try:
        import tkinter as tk
        from tkinter import filedialog, messagebox, ttk
    except Exception as exc:
        raise BeatCutterError(
            f"The file picker is unavailable ({exc}). Pass --audio and --photo-dir instead."
        ) from exc

    root = tk.Tk()
    root.title("Beat Cutter")
    root.resizable(False, False)
    root.lift()
    try:
        root.attributes("-topmost", True)
        root.after(300, lambda: root.attributes("-topmost", False))
    except tk.TclError:
        pass

    audio_var = tk.StringVar(value=config.audio_path)
    photo_var = tk.StringVar(value=config.photo_dir)
    mode_var = tk.StringVar(value=config.mode if config.mode in {"beats", "drops"} else "beats")
    every_var = tk.StringVar(value=str(config.beats_per_cut))
    min_var = tk.StringVar(value=str(config.min_cut_duration_sec))
    loop_var = tk.BooleanVar(value=config.loop_photos)
    shuffle_var = tk.BooleanVar(value=config.shuffle_photos)
    zoom_var = tk.BooleanVar(value=config.apply_subtle_zoom)
    style_var = tk.StringVar(value=config.zoom_style if config.zoom_style in {"static", "dynamic"} else "static")
    overwrite_var = tk.BooleanVar(value=config.overwrite)
    result: dict[str, JobConfig | None] = {"config": None}

    def browse_audio() -> None:
        path = filedialog.askopenfilename(
            title="Choose the audio track",
            filetypes=[("Audio", "*.wav *.mp3 *.m4a *.flac"), ("All files", "*.*")],
        )
        if path:
            audio_var.set(path)

    def browse_photos() -> None:
        path = filedialog.askdirectory(title="Choose a folder of photos")
        if path:
            photo_var.set(path)

    def submit() -> None:
        try:
            every = int(every_var.get().strip())
            minimum = float(min_var.get().strip())
        except ValueError:
            messagebox.showerror(
                "Beat Cutter",
                "Beats per cut must be a whole number, and the minimum duration must be a number of seconds.",
            )
            return
        if not audio_var.get().strip():
            messagebox.showerror("Beat Cutter", "Choose an audio file.")
            return
        result["config"] = replace(
            config,
            audio_path=audio_var.get().strip(),
            photo_dir=photo_var.get().strip(),
            mode=mode_var.get(),
            beats_per_cut=every,
            min_cut_duration_sec=minimum,
            loop_photos=bool(loop_var.get()),
            shuffle_photos=bool(shuffle_var.get()),
            apply_subtle_zoom=bool(zoom_var.get()),
            zoom_style=style_var.get(),
            overwrite=bool(overwrite_var.get()),
        )
        root.destroy()

    pad = {"padx": 10, "pady": 4}
    frame = ttk.Frame(root, padding=8)
    frame.grid(row=0, column=0, sticky="nsew")
    ttk.Label(
        frame,
        text="Cut the open Resolve timeline's photos to this track.",
    ).grid(row=0, column=0, columnspan=3, sticky="w", **pad)

    ttk.Label(frame, text="Audio").grid(row=1, column=0, sticky="w", **pad)
    ttk.Entry(frame, textvariable=audio_var, width=52).grid(row=1, column=1, **pad)
    ttk.Button(frame, text="Browse", command=browse_audio).grid(row=1, column=2, **pad)

    ttk.Label(frame, text="Photos").grid(row=2, column=0, sticky="w", **pad)
    ttk.Entry(frame, textvariable=photo_var, width=52).grid(row=2, column=1, **pad)
    ttk.Button(frame, text="Browse", command=browse_photos).grid(row=2, column=2, **pad)

    ttk.Label(frame, text="Mode").grid(row=3, column=0, sticky="w", **pad)
    mode_box = ttk.Frame(frame)
    mode_box.grid(row=3, column=1, sticky="w", **pad)
    ttk.Radiobutton(mode_box, text="Beats", value="beats", variable=mode_var).pack(side="left")
    ttk.Radiobutton(mode_box, text="Drops", value="drops", variable=mode_var).pack(side="left", padx=12)

    ttk.Label(frame, text="Beats per cut").grid(row=4, column=0, sticky="w", **pad)
    ttk.Entry(frame, textvariable=every_var, width=8).grid(row=4, column=1, sticky="w", **pad)
    ttk.Label(frame, text="Minimum seconds").grid(row=5, column=0, sticky="w", **pad)
    ttk.Entry(frame, textvariable=min_var, width=8).grid(row=5, column=1, sticky="w", **pad)

    flags = ttk.Frame(frame)
    flags.grid(row=6, column=0, columnspan=3, sticky="w", **pad)
    ttk.Checkbutton(flags, text="Loop photos", variable=loop_var).pack(side="left", padx=(0, 12))
    ttk.Checkbutton(flags, text="Shuffle photos", variable=shuffle_var).pack(side="left", padx=(0, 12))
    ttk.Checkbutton(flags, text="Subtle zoom", variable=zoom_var).pack(side="left", padx=(0, 12))
    ttk.Radiobutton(flags, text="Static", value="static", variable=style_var).pack(side="left")
    ttk.Radiobutton(flags, text="Dynamic", value="dynamic", variable=style_var).pack(side="left", padx=8)
    ttk.Checkbutton(flags, text="Overwrite V1 and A1", variable=overwrite_var).pack(side="left", padx=(12, 0))

    buttons = ttk.Frame(frame)
    buttons.grid(row=7, column=0, columnspan=3, sticky="e", **pad)
    ttk.Button(buttons, text="Cancel", command=root.destroy).pack(side="right")
    ttk.Button(buttons, text="Cut", command=submit).pack(side="right", padx=(0, 8))

    root.mainloop()
    chosen = result["config"]
    if chosen is None:
        raise Cancelled("Cancelled.")
    return chosen


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Cut a Media Pool bin of photos to an audio track on the open DaVinci Resolve timeline.",
    )
    parser.add_argument(
        "audio",
        nargs="?",
        default="",
        help="Audio file (.wav, .mp3, .m4a, .flac). Same as --audio.",
    )
    parser.add_argument("--audio", dest="audio_flag", default="", help="Audio file path.")
    parser.add_argument("--photo-dir", default=PHOTO_DIR, help="Folder of stills to import into the bin.")
    parser.add_argument("--bin", default=MEDIA_POOL_BIN, help="Media Pool bin that holds the stills.")
    parser.add_argument("--mode", choices=("beats", "drops"), default=MODE, help="Detection mode.")
    parser.add_argument("--beats-per-cut", type=int, default=BEATS_PER_CUT, help="Keep every Nth detected event.")
    parser.add_argument(
        "--min-cut-duration",
        type=float,
        default=MIN_CUT_DURATION_SEC,
        help="Minimum seconds between cuts.",
    )
    parser.add_argument("--loop-photos", action=argparse.BooleanOptionalAction, default=LOOP_PHOTOS)
    parser.add_argument(
        "--shuffle",
        action=argparse.BooleanOptionalAction,
        default=SHUFFLE_PHOTOS,
        help="Spread filename order so similar burst shots are not adjacent.",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=SHUFFLE_SEED,
        help="Repeat a previous --shuffle. Printed in the log when omitted.",
    )
    parser.add_argument("--zoom", action=argparse.BooleanOptionalAction, default=APPLY_SUBTLE_ZOOM)
    parser.add_argument("--zoom-style", choices=("static", "dynamic"), default=ZOOM_STYLE)
    parser.add_argument(
        "--zoom-ratio",
        type=float,
        default=ZOOM_RATIO,
        help="Punch-in scale. 1.045 is 4.5%% larger than the normal size.",
    )
    parser.add_argument("--lowpass-hz", type=float, default=LOWPASS_HZ, help="Drop-mode low-pass cutoff.")
    parser.add_argument("--overwrite", action=argparse.BooleanOptionalAction, default=OVERWRITE_TRACKS)
    parser.add_argument(
        "--cover-from-start",
        action=argparse.BooleanOptionalAction,
        default=COVER_FROM_START,
        help="Hold a photo from the timeline start until the first detected hit.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print cuts and do not touch Resolve.")
    parser.add_argument("--gui", action="store_true", help="Open a file picker before cutting.")
    parser.add_argument("--self-test", action="store_true", help="Check frame math and sorting, then exit.")
    parser.add_argument("--verbose", action="store_true", help="Print every cut.")
    return parser.parse_args(list(argv))


def config_from_args(args: argparse.Namespace) -> JobConfig:
    audio = args.audio_flag or args.audio or AUDIO_PATH
    return JobConfig(
        audio_path=audio,
        photo_dir=args.photo_dir,
        media_pool_bin=args.bin,
        mode=args.mode,
        beats_per_cut=args.beats_per_cut,
        min_cut_duration_sec=args.min_cut_duration,
        loop_photos=args.loop_photos,
        shuffle_photos=args.shuffle,
        shuffle_seed=args.seed,
        apply_subtle_zoom=args.zoom,
        zoom_style=args.zoom_style,
        zoom_ratio=args.zoom_ratio,
        lowpass_hz=args.lowpass_hz,
        overwrite=args.overwrite,
        cover_from_start=args.cover_from_start,
        dry_run=args.dry_run,
        verbose=args.verbose,
    )


def self_test() -> None:
    """Check sorting, fractional frame rates, and gap-free intervals."""

    def check_sort() -> None:
        names = ["IMG_10.jpg", "IMG_2.jpg", "IMG_001.jpg", "photo.jpg", "IMG_2.JPG"]
        ordered = sorted(names, key=natural_sort_key)
        assert ordered == ["IMG_001.jpg", "IMG_2.jpg", "IMG_2.JPG", "IMG_10.jpg", "photo.jpg"], ordered

    def check_rates() -> None:
        assert parse_frame_rate("23.976") == 24000 / 1001
        assert parse_frame_rate("29.97 DF") == 30000 / 1001
        assert parse_frame_rate(24) == 24.0
        assert parse_frame_rate("25") == 25.0
        assert parse_frame_rate(59.94) == 60000 / 1001
        assert seconds_to_frame_count(1.0, 24) == 24
        assert seconds_to_frame_count(1.0, 24000 / 1001) == 24
        assert seconds_to_frame_count(0.5, 24) == 12
        assert seconds_to_frame_count(0, 24) == 0

    def check_thinning() -> None:
        cuts = build_cut_seconds(
            [1.0, 1.10, 2.0],
            3.0,
            beats_per_cut=1,
            min_cut_duration_sec=0.25,
            cover_from_start=True,
        )
        assert cuts == [0.0, 1.0, 2.0], cuts
        every_fourth = build_cut_seconds(
            [1, 2, 3, 4, 5, 6, 7, 8],
            10,
            beats_per_cut=4,
            min_cut_duration_sec=0.25,
            cover_from_start=False,
        )
        assert every_fourth == [1.0, 5.0], every_fourth

    def check_no_gaps() -> None:
        fps = 24000 / 1001
        duration = 180.0
        times = [index * (60 / 128) for index in range(1, 420)]
        cuts = build_cut_seconds(
            times,
            duration,
            beats_per_cut=1,
            min_cut_duration_sec=0.25,
            cover_from_start=True,
        )
        start = 86383
        boundaries = build_boundaries(cuts, duration, fps, start, 0.25)
        widths = [boundaries[i + 1] - boundaries[i] for i in range(len(boundaries) - 1)]
        assert boundaries[0] == start
        assert all(width >= 1 for width in widths)
        assert sum(widths) == boundaries[-1] - boundaries[0]
        audio_frames = seconds_to_frame_count(duration, fps)
        assert boundaries[-1] == start + audio_frames
        infos = clip_infos_for_boundaries(
            list(range(len(widths))),
            boundaries,
            inclusive_end=True,
            media_type=1,
        )
        for index, info in enumerate(infos):
            duration_frames = widths[index]
            assert info["recordFrame"] == boundaries[index]
            assert info["endFrame"] - info["startFrame"] + 1 == duration_frames
            if index:
                previous = infos[index - 1]
                assert info["recordFrame"] == previous["recordFrame"] + (
                    previous["endFrame"] - previous["startFrame"] + 1
                )
        assert source_end_frame(24, inclusive=True) == 23
        assert source_end_frame(24, inclusive=False) == 24

    def check_still_duration() -> None:
        class Item:
            def __init__(self) -> None:
                self.calls = 0

            def SetClipProperty(self, key: str, value: str) -> bool:
                assert key == "Duration"
                assert value
                self.calls += 1
                return True

            def GetClipProperty(self) -> dict[str, str]:
                return {"Frames": "120", "File Name": "a.jpg", "File Path": "/tmp/a.jpg"}

        item = Item()
        clip = PoolClip(item=item, name="a.jpg", path="/tmp/a.jpg", frames=5)
        ensure_still_duration(clip, 24, 24.0)
        assert clip.frames == 120, clip.frames
        ensure_still_duration(clip, 24, 24.0)
        assert item.calls == 1, item.calls
        long_enough = PoolClip(item=item, name="b.jpg", path="/tmp/b.jpg", frames=48)
        ensure_still_duration(long_enough, 24, 24.0)
        assert item.calls == 1

    def check_holder_sequence() -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "DSC_1476.jpg"
            source.write_bytes(b"jpeg-bytes")
            cache = Path(temp) / "holders"
            directory = write_holder_sequence(source, "/photos/DSC_1476.jpg", 4, cache)
            links = sorted(directory.iterdir())
            assert len(links) == 4, links
            assert links[0].stat().st_ino == source.stat().st_ino
            pattern = sequence_pattern(directory, 4, source)
            assert pattern.name == "DSC_1476_%04d.jpg"
            assert holder_stem(Path("_DSF8691.JPG")) == "_DSF8691"
            again = write_holder_sequence(source, "/photos/DSC_1476.jpg", 4, cache)
            assert again == directory
            assert len(list(directory.iterdir())) == 4

    def check_variable_pulse() -> None:
        envelope = [0.0] * 80
        spikes = [5, 15, 27, 41, 58]
        for frame in spikes:
            envelope[frame] = 4.0
        found = pulse_peak_frames(envelope, 44100, 256, min_interval_sec=0.05, relative_height=0.05)
        assert found == spikes, found
        events = [0.0, 0.06, 0.12, 0.30]
        kept = build_cut_seconds(
            events,
            1.0,
            beats_per_cut=1,
            min_cut_duration_sec=0.04,
            cover_from_start=False,
        )
        assert kept == [0.0, 0.06, 0.12, 0.30], kept
        coarse = build_cut_seconds(
            events,
            1.0,
            beats_per_cut=1,
            min_cut_duration_sec=0.25,
            cover_from_start=False,
        )
        assert coarse == [0.0, 0.30], coarse

    def check_shuffle() -> None:
        for count in (5, 6, 8, 12, 47, 112):
            for seed in range(8):
                order = spread_photo_order(count, random.Random(seed))
                assert sorted(order) == list(range(count)), order
                gaps = [abs(order[index + 1] - order[index]) for index in range(count - 1)]
                assert min(gaps) >= 2, (count, seed, order)
        again = spread_photo_order(112, random.Random(7))
        assert again == spread_photo_order(112, random.Random(7))
        assert again != list(range(112))

    def check_tail_and_photos() -> None:
        boundaries = build_boundaries([0.0, 0.5, 2.9], 3.0, 24.0, 100, 0.25)
        # 2.9s is 70 frames, 2 frames from the end, under the 6-frame minimum.
        assert boundaries == [100, 112, 172], boundaries
        assert assign_photo_indexes(5, 2, True) == [0, 1, 0, 1, 0]
        assert assign_photo_indexes(5, 2, False) == [0, 1]
        assert assign_photo_indexes(4, 0, True) == []

    checks = [
        check_sort,
        check_rates,
        check_thinning,
        check_no_gaps,
        check_still_duration,
        check_holder_sequence,
        check_variable_pulse,
        check_shuffle,
        check_tail_and_photos,
    ]
    for check in checks:
        check()
        print(f"  ok  {check.__name__}")
    print(f"self-test passed ({len(checks)} checks)")


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    if args.self_test:
        self_test()
        return 0
    configure_logging(args.verbose)
    try:
        config = config_from_args(args)
        if args.gui:
            config = prompt_with_gui(config)
            configure_logging(config.verbose)
        run_edit(config)
    except Cancelled as exc:
        log.info("%s", exc)
        return 0
    except BeatCutterError as exc:
        log.error("%s", exc)
        return 1
    except KeyboardInterrupt:
        log.error("Stopped.")
        return 1
    except Exception as exc:
        log.exception("Unexpected failure: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
