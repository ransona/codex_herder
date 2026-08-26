"""Read registered Suite2p binary frames over a Timeline-time window.

How this works
--------------
This helper reads registered imaging frames directly from Suite2p `data.bin`
files using the matching per-plane Timeline timing arrays that live beside
those binaries.

The intended workflow is:

1. Discover the relevant Suite2p plane folders for a standard experiment
   (`suite2p/planeN`) or a mesoscope experiment (`P{path}/{roi}/suite2p/planeN`).
2. For each selected plane folder, load:
   - `data.bin` for the registered frames
   - `ops.npy` for the true frame size (`Ly`, `Lx`) or `meanImg.shape`
   - `timeline_frame_times.npy` for the Timeline time of each frame
3. Convert the requested Timeline time window into frame indices by selecting
   all frame times within `[start_time, end_time]`.
4. Memory-map the binary file and read only the selected frames.

This helper does not guess frame size from the binary length. It always checks
Suite2p metadata in `ops.npy` first so the binary can be reshaped correctly.

This helper also does not recompute frame timing from the raw Timeline file.
If `timeline_frame_times.npy` is missing, backfill it first using the
preprocessing helper scripts that generate the saved timing arrays.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

import numpy as np


@dataclass(frozen=True)
class Suite2PBinChunk:
    """Frames read from one Suite2p plane folder."""

    plane_dir: Path
    bin_path: Path
    ops_path: Path
    path_name: str | None
    roi_name: str | None
    channel: int
    plane: int
    height: int
    width: int
    frame_indices: np.ndarray
    frame_times: np.ndarray
    frames: np.ndarray


@dataclass(frozen=True)
class _PlaneTarget:
    plane_dir: Path
    path_name: str | None
    roi_name: str | None
    channel: int
    plane: int


def _resolve_bin_path(plane_dir: Path) -> Path:
    candidates = [
        plane_dir / "data.bin",
        plane_dir / "data_chan2.bin",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    raise FileNotFoundError(
        f"Missing Suite2p binary file in {plane_dir}; expected one of: "
        + ", ".join(str(path.name) for path in candidates)
    )


def _derive_animal_id(exp_id: str) -> str:
    parts = exp_id.split("_")
    if len(parts) < 3:
        raise ValueError(f"Cannot derive animal ID from expID: {exp_id}")
    return parts[2]


def _default_experiment_root(user_id: str, exp_id: str) -> Path:
    return Path("/home") / user_id / "data" / "Repository" / _derive_animal_id(exp_id) / exp_id


def _normalize_path_selectors(paths: Sequence[str | int] | None) -> set[str] | None:
    if paths is None:
        return None
    normalized: set[str] = set()
    for item in paths:
        if isinstance(item, int):
            normalized.add(f"P{item}")
        else:
            text = str(item).strip()
            normalized.add(text if text.startswith("P") else f"P{text}")
    return normalized


def _normalize_name_selectors(values: Sequence[str] | None) -> set[str] | None:
    if values is None:
        return None
    return {str(value).strip() for value in values}


def _normalize_int_selectors(values: Sequence[int] | None) -> set[int] | None:
    if values is None:
        return None
    return {int(value) for value in values}


def _available_channel_roots(base_dir: Path) -> list[tuple[int, Path]]:
    roots: list[tuple[int, Path]] = []
    primary = base_dir / "suite2p"
    if primary.exists():
        roots.append((1, primary))
    secondary = base_dir / "ch2" / "suite2p"
    if secondary.exists():
        roots.append((2, secondary))
    return roots


def _sorted_plane_numbers(suite2p_root: Path) -> list[int]:
    plane_numbers: list[int] = []
    for item in suite2p_root.iterdir():
        if item.is_dir() and item.name.startswith("plane"):
            suffix = item.name.replace("plane", "", 1)
            if suffix.isdigit():
                plane_numbers.append(int(suffix))
    return sorted(plane_numbers)


def _discover_plane_targets(
    experiment_root: Path,
    paths: Sequence[str | int] | None,
    rois: Sequence[str] | None,
    planes: Sequence[int] | None,
    channels: Sequence[int] | None,
) -> list[_PlaneTarget]:
    selected_paths = _normalize_path_selectors(paths)
    selected_rois = _normalize_name_selectors(rois)
    selected_planes = _normalize_int_selectors(planes)
    selected_channels = _normalize_int_selectors(channels)

    meso_paths = sorted(
        item for item in experiment_root.iterdir() if item.is_dir() and item.name.startswith("P")
    )
    targets: list[_PlaneTarget] = []

    if meso_paths:
        for path_dir in meso_paths:
            if selected_paths is not None and path_dir.name not in selected_paths:
                continue
            roi_dirs = sorted(item for item in path_dir.iterdir() if item.is_dir())
            for roi_dir in roi_dirs:
                if selected_rois is not None and roi_dir.name not in selected_rois:
                    continue
                for channel, suite2p_root in _available_channel_roots(roi_dir):
                    if selected_channels is not None and channel not in selected_channels:
                        continue
                    for plane in _sorted_plane_numbers(suite2p_root):
                        if selected_planes is not None and plane not in selected_planes:
                            continue
                        targets.append(
                            _PlaneTarget(
                                plane_dir=suite2p_root / f"plane{plane}",
                                path_name=path_dir.name,
                                roi_name=roi_dir.name,
                                channel=channel,
                                plane=plane,
                            )
                        )
        return targets

    if paths is not None or rois is not None:
        raise ValueError("This experiment does not use mesoscope path/ROI folders, so paths/rois cannot be selected.")

    for channel, suite2p_root in _available_channel_roots(experiment_root):
        if selected_channels is not None and channel not in selected_channels:
            continue
        for plane in _sorted_plane_numbers(suite2p_root):
            if selected_planes is not None and plane not in selected_planes:
                continue
            targets.append(
                _PlaneTarget(
                    plane_dir=suite2p_root / f"plane{plane}",
                    path_name=None,
                    roi_name=None,
                    channel=channel,
                    plane=plane,
                )
            )
    return targets


def _read_frame_size(ops_path: Path) -> tuple[int, int]:
    ops = np.load(ops_path, allow_pickle=True).item()
    if "Ly" in ops and "Lx" in ops:
        return int(ops["Ly"]), int(ops["Lx"])
    if "meanImg" in ops and getattr(ops["meanImg"], "shape", None) is not None:
        shape = tuple(int(value) for value in ops["meanImg"].shape[:2])
        if len(shape) == 2:
            return shape
    raise KeyError(f"Could not determine frame size from {ops_path}; expected Ly/Lx or meanImg.shape.")


def _read_selected_frames(
    bin_path: Path,
    height: int,
    width: int,
    frame_indices: np.ndarray,
    frame_count: int,
) -> np.ndarray:
    mm = np.memmap(bin_path, dtype=np.int16, mode="r")
    pixels_per_frame = height * width
    usable_pixels = frame_count * pixels_per_frame
    if usable_pixels > mm.size:
        raise ValueError(f"Requested frame count exceeds binary length for {bin_path}")
    frame_view = mm[:usable_pixels].reshape(frame_count, height, width)
    if frame_indices.size == 0:
        return np.empty((0, height, width), dtype=np.int16)
    return np.asarray(frame_view[frame_indices])


def _frame_count_from_bin(bin_path: Path, height: int, width: int) -> int:
    mm = np.memmap(bin_path, dtype=np.int16, mode="r")
    pixels_per_frame = height * width
    if mm.size % pixels_per_frame != 0:
        raise ValueError(
            f"Binary size is not divisible by frame size for {bin_path}: "
            f"{mm.size} pixels vs {height}x{width}"
        )
    return int(mm.size // pixels_per_frame)


def _select_frame_indices(frame_times: np.ndarray, start_time: float, end_time: float) -> np.ndarray:
    mask = (frame_times >= start_time) & (frame_times <= end_time)
    return np.flatnonzero(mask)


def _require_timing_file(plane_dir: Path) -> np.ndarray:
    timing_path = plane_dir / "timeline_frame_times.npy"
    if not timing_path.exists():
        raise FileNotFoundError(
            f"Missing {timing_path}. Backfill the timing files before reading by Timeline time."
        )
    return np.load(timing_path)


def read_suite2p_bin(
    user_id: str,
    exp_id: str,
    start_time: float,
    end_time: float,
    *,
    experiment_root: str | Path | None = None,
    paths: Sequence[str | int] | None = None,
    rois: Sequence[str] | None = None,
    planes: Sequence[int] | None = None,
    channels: Sequence[int] | None = None,
    max_frame_mismatch: int = 4,
) -> list[Suite2PBinChunk]:
    """Read registered Suite2p frames for a Timeline-time interval.

    Parameters
    ----------
    user_id, exp_id
        Experiment identifiers used to resolve the experiment root when
        `experiment_root` is not supplied.
    start_time, end_time
        Inclusive Timeline-time window in seconds.
    experiment_root
        Optional explicit experiment directory override.
    paths, rois, planes, channels
        Optional selectors. Each accepts multiple values. If omitted, all
        available entries are read.
    max_frame_mismatch
        Allowed difference between frame count in `data.bin` and in
        `timeline_frame_times.npy` before an error is raised.

    Returns
    -------
    list[Suite2PBinChunk]
        One entry per selected plane folder. Each entry contains the selected
        frame indices, their Timeline times, and the frame stack itself with
        shape `(n_frames, Ly, Lx)`.
    """

    if end_time < start_time:
        raise ValueError("end_time must be greater than or equal to start_time")

    root = Path(experiment_root) if experiment_root is not None else _default_experiment_root(user_id, exp_id)
    if not root.exists():
        raise FileNotFoundError(f"Experiment root does not exist: {root}")

    targets = _discover_plane_targets(root, paths=paths, rois=rois, planes=planes, channels=channels)
    if not targets:
        raise FileNotFoundError(f"No Suite2p plane folders matched the requested selectors in {root}")

    chunks: list[Suite2PBinChunk] = []
    for target in targets:
        bin_path = _resolve_bin_path(target.plane_dir)
        ops_path = target.plane_dir / "ops.npy"
        if not ops_path.exists():
            raise FileNotFoundError(f"Missing Suite2p ops file: {ops_path}")

        frame_times = _require_timing_file(target.plane_dir)
        height, width = _read_frame_size(ops_path)
        bin_frame_count = _frame_count_from_bin(bin_path, height, width)
        timing_frame_count = int(frame_times.shape[0])

        if abs(bin_frame_count - timing_frame_count) > max_frame_mismatch:
            raise ValueError(
                f"Frame count mismatch too large for {target.plane_dir}: "
                f"data.bin={bin_frame_count}, timeline_frame_times.npy={timing_frame_count}, "
                f"max allowed={max_frame_mismatch}"
            )

        frame_count = min(bin_frame_count, timing_frame_count)
        frame_times = np.asarray(frame_times[:frame_count], dtype=float)
        frame_indices = _select_frame_indices(frame_times, start_time, end_time)
        frames = _read_selected_frames(bin_path, height, width, frame_indices, frame_count)

        chunks.append(
            Suite2PBinChunk(
                plane_dir=target.plane_dir,
                bin_path=bin_path,
                ops_path=ops_path,
                path_name=target.path_name,
                roi_name=target.roi_name,
                channel=target.channel,
                plane=target.plane,
                height=height,
                width=width,
                frame_indices=frame_indices,
                frame_times=frame_times[frame_indices],
                frames=frames,
            )
        )

    return chunks
