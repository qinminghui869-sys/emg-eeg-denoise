#!/usr/bin/env python3
"""Export L3 wrist EMG parquet into compact JSON for emg_viewer.html."""

from __future__ import annotations

import argparse
import base64
import json
from pathlib import Path

import numpy as np
import pandas as pd

DEFAULT_SRC = Path(
    "/home/baai/qmh/dataset/L3/L3_128_data/L3-001-2026-10-09-15-45-54"
)
DEFAULT_OUT = Path(__file__).resolve().parent / "data" / "sample_emg.json"


def _enc_f32(arr: np.ndarray) -> str:
    return base64.b64encode(np.ascontiguousarray(arr, dtype=np.float32).tobytes()).decode(
        "ascii"
    )


def _load_side(episode_dir: Path, name: str, decim: int) -> tuple[np.ndarray, np.ndarray]:
    df = pd.read_parquet(episode_dir / name)
    cols = [f"dim_{i}" for i in range(8)]
    t = df["timestamp"].to_numpy(dtype=np.float64)[::decim]
    x = df[cols].to_numpy(dtype=np.float32)[::decim]
    return t, x


def export(src: Path, out: Path, *, original_fs: float, target_fs: float) -> Path:
    episode_dir = src / "data" / "chunk-000" / "episode_000000"
    if not episode_dir.is_dir():
        raise FileNotFoundError(f"episode dir not found: {episode_dir}")

    decim = max(1, int(round(original_fs / target_fs)))
    effective_fs = original_fs / decim

    lt, lx = _load_side(episode_dir, "observation.left_wrist_emg.parquet", decim)
    rt, rx = _load_side(episode_dir, "observation.right_wrist_emg.parquet", decim)
    n = min(len(lt), len(rt))
    lt, lx, rt, rx = lt[:n], lx[:n], rt[:n], rx[:n]
    t0 = float(min(lt[0], rt[0]))
    t_rel = (lt - t0).astype(np.float32)

    payload = {
        "source": f"{src.name} / episode_000000",
        "source_path": str(src.resolve()),
        "original_fs_hz": float(original_fs),
        "fs_hz": float(effective_fs),
        "n_samples": int(n),
        "duration_s": float(t_rel[-1] - t_rel[0]),
        "unit": "ADC counts",
        "dtype": "float32",
        "encoding": "base64",
        "t0_s": t0,
        "time": _enc_f32(t_rel),
        "left": {
            "names": [f"L{i}" for i in range(8)],
            "channels": [_enc_f32(lx[:, i]) for i in range(8)],
            "mean": lx.mean(axis=0).astype(float).tolist(),
            "std": lx.std(axis=0).astype(float).tolist(),
        },
        "right": {
            "names": [f"R{i}" for i in range(8)],
            "channels": [_enc_f32(rx[:, i]) for i in range(8)],
            "mean": rx.mean(axis=0).astype(float).tolist(),
            "std": rx.std(axis=0).astype(float).tolist(),
        },
    }

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--src", type=Path, default=DEFAULT_SRC)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--original-fs", type=float, default=2000.0)
    parser.add_argument(
        "--target-fs",
        type=float,
        default=1000.0,
        help="Decimated rate for the viewer (1000 Hz keeps 450 Hz lowpass usable)",
    )
    args = parser.parse_args()
    path = export(
        args.src,
        args.out,
        original_fs=args.original_fs,
        target_fs=args.target_fs,
    )
    size_kb = path.stat().st_size / 1024
    print(f"wrote {path} ({size_kb:.1f} KiB)")


if __name__ == "__main__":
    main()
