from __future__ import annotations

from pathlib import Path

import numpy as np

from helper.read_suite2p_bin import read_suite2p_bin


def _write_plane(plane_dir: Path, frames: np.ndarray, frame_times: np.ndarray) -> None:
    plane_dir.mkdir(parents=True, exist_ok=True)
    np.save(plane_dir / "ops.npy", {"Ly": int(frames.shape[1]), "Lx": int(frames.shape[2])}, allow_pickle=True)
    np.save(plane_dir / "timeline_frame_times.npy", frame_times)
    frames.astype(np.int16).tofile(plane_dir / "data.bin")


def test_read_suite2p_bin_reads_standard_plane_by_timeline_time(tmp_path: Path) -> None:
    exp_root = tmp_path / "2025-01-01_01_ESPM001"
    frames = np.arange(5 * 2 * 3, dtype=np.int16).reshape(5, 2, 3)
    frame_times = np.array([10.0, 11.0, 12.0, 13.0, 14.0], dtype=float)
    _write_plane(exp_root / "suite2p" / "plane0", frames, frame_times)

    chunks = read_suite2p_bin(
        "user",
        "2025-01-01_01_ESPM001",
        11.5,
        13.0,
        experiment_root=exp_root,
    )

    assert len(chunks) == 1
    chunk = chunks[0]
    assert chunk.height == 2
    assert chunk.width == 3
    assert chunk.frame_indices.tolist() == [2, 3]
    np.testing.assert_array_equal(chunk.frame_times, np.array([12.0, 13.0]))
    np.testing.assert_array_equal(chunk.frames, frames[[2, 3]])


def test_read_suite2p_bin_filters_meso_path_roi_plane(tmp_path: Path) -> None:
    exp_root = tmp_path / "2025-01-01_01_ESPM001"
    frame_times = np.array([20.0, 21.0, 22.0], dtype=float)
    frames_a = np.arange(3 * 2 * 2, dtype=np.int16).reshape(3, 2, 2)
    frames_b = (np.arange(3 * 2 * 2, dtype=np.int16) + 100).reshape(3, 2, 2)

    _write_plane(exp_root / "P1" / "roiA" / "suite2p" / "plane0", frames_a, frame_times)
    _write_plane(exp_root / "P1" / "roiB" / "suite2p" / "plane1", frames_b, frame_times)

    chunks = read_suite2p_bin(
        "user",
        "2025-01-01_01_ESPM001",
        20.0,
        21.0,
        experiment_root=exp_root,
        paths=["P1"],
        rois=["roiB"],
        planes=[1],
    )

    assert len(chunks) == 1
    chunk = chunks[0]
    assert chunk.path_name == "P1"
    assert chunk.roi_name == "roiB"
    assert chunk.plane == 1
    np.testing.assert_array_equal(chunk.frame_indices, np.array([0, 1]))
    np.testing.assert_array_equal(chunk.frames, frames_b[[0, 1]])
