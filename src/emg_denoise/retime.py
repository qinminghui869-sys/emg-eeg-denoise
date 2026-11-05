"""Application service for stage one: rebuild a recording's time axis.

Reads an acquisition parquet (or CSV), replaces the tick-quantized timestamps
with a uniform axis, and writes the ``timestamp,channel...`` CSV that stage two
(``emg-denoise process``) consumes.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from ._typing import FloatArray
from .filters import FS, sample_matrix
from .io import atomic_text_writer, dump_json, validate_output_paths
from .timebase import TimebaseReport, rebuild_timestamps

#: Acquisition files name the time column this way; channels are ``dim_0``, ``dim_1``, ...
TIMESTAMP_COLUMN = "timestamp"
CHANNEL_PREFIX = "dim_"

#: Columns carried by acquisition parquet that are not signal channels.
METADATA_COLUMNS = frozenset({TIMESTAMP_COLUMN, "episode_index", "frame_index", "index"})


@dataclass(frozen=True, slots=True)
class RetimeOptions:
    """Paths and parameters for one stage-one run; sample values are never altered."""

    source: Path
    output: Path
    rate_hz: float = float(FS)
    tick_us: float | None = None
    non_negative: bool = True
    #: Segment-local anchoring keeps samples nearer their original stamps but
    #: leaves seams, so the result cannot satisfy stage two. Diagnostics only.
    stitch: bool = True

    @property
    def report_path(self) -> Path:
        return Path(f"{self.output}.timebase.json")

    @property
    def period_s(self) -> float:
        if self.rate_hz <= 0.0:
            raise ValueError("rate must be positive")
        return 1.0 / self.rate_hz

    def validate_paths(self) -> None:
        validate_output_paths([self.source], [self.output, self.report_path])


@dataclass(frozen=True, slots=True)
class Acquisition:
    """Raw recording as stored by the acquisition stack, before retiming."""

    timestamps: FloatArray
    samples: FloatArray
    channel_names: tuple[str, ...]


def _channel_order(names: list[str]) -> list[str]:
    """Signal columns in acquisition order, with ``dim_N`` sorted numerically."""
    channels = [name for name in names if name not in METADATA_COLUMNS]
    if not channels:
        raise ValueError("recording has no channel columns")
    dims = [name for name in channels if name.startswith(CHANNEL_PREFIX)]
    if len(dims) == len(channels):
        # dim_10 must follow dim_9, which lexicographic order would not do.
        return sorted(dims, key=lambda name: int(name.removeprefix(CHANNEL_PREFIX)))
    return channels


def load_acquisition(path: str | Path) -> Acquisition:
    """Load a parquet or CSV recording without validating its sample spacing.

    Unlike :func:`emg_denoise.io.load_recording`, this accepts the non-uniform
    timestamps that stage one exists to repair.
    """
    source = Path(path)
    reader = _read_parquet if source.suffix.lower() in {".parquet", ".pq"} else _read_csv
    stamps, data, channels = reader(source)
    if stamps.size < 2:
        raise ValueError("recording must contain at least two samples")
    return Acquisition(stamps, sample_matrix(data), tuple(channels))


def _read_parquet(source: Path) -> tuple[FloatArray, FloatArray, list[str]]:
    try:
        import pyarrow.parquet as pq
    except ImportError as error:  # pragma: no cover - depends on the environment
        raise ValueError(
            "reading parquet requires pyarrow; install emg-denoise[parquet]"
        ) from error
    # pyarrow ships partial annotations; read_table is untyped upstream.
    table: Any = pq.read_table(source)  # type: ignore[no-untyped-call]
    names: list[str] = list(table.column_names)
    if TIMESTAMP_COLUMN not in names:
        raise ValueError(f"parquet must contain a {TIMESTAMP_COLUMN!r} column")
    channels = _channel_order(names)

    def column(name: str) -> FloatArray:
        values: Any = table[name].to_numpy(zero_copy_only=False)
        return np.asarray(values, dtype=np.float64).reshape(-1)

    return column(TIMESTAMP_COLUMN), np.column_stack([column(c) for c in channels]), channels


def _read_csv(source: Path) -> tuple[FloatArray, FloatArray, list[str]]:
    raw: Any = np.genfromtxt(
        source, delimiter=",", names=True, dtype=np.float64, encoding="utf-8-sig"
    )
    names: list[str] = list(raw.dtype.names or ())
    if TIMESTAMP_COLUMN not in names:
        raise ValueError(f"CSV must contain a {TIMESTAMP_COLUMN!r} column")
    channels = _channel_order(names)

    def column(name: str) -> FloatArray:
        return np.asarray(raw[name], dtype=np.float64).reshape(-1)

    return column(TIMESTAMP_COLUMN), np.column_stack([column(c) for c in channels]), channels


def _verify_uniform(timestamps: FloatArray, period_s: float) -> None:
    """Assert the rebuilt axis satisfies what stage two requires of its input."""
    gaps = np.diff(timestamps)
    worst = float(np.abs(gaps - period_s).max())
    # load_recording and replay both admit 1e-6 s of slack; stay well inside it.
    if worst > 1e-9:
        raise ValueError(f"rebuilt timestamps deviate from the period by {worst * 1e6:.6f} us")
    if np.any(gaps <= 0.0):
        raise ValueError("rebuilt timestamps must be strictly increasing")


def run_retime(options: RetimeOptions) -> dict[str, Any]:
    """Rebuild one recording's time axis; a failed write leaves outputs intact."""
    options.validate_paths()
    acquisition = load_acquisition(options.source)
    timestamps, report = rebuild_timestamps(
        acquisition.timestamps,
        period_s=options.period_s,
        tick_s=None if options.tick_us is None else options.tick_us * 1e-6,
        stitch=options.stitch,
        non_negative=options.non_negative,
    )
    _verify_uniform(timestamps, options.period_s)

    header = [TIMESTAMP_COLUMN, *acquisition.channel_names]
    with atomic_text_writer(options.output) as destination:
        destination.write(",".join(header) + "\n")
        for index in range(timestamps.size):
            values = ",".join(format(value, ".17g") for value in acquisition.samples[index])
            destination.write(f"{format(timestamps[index], '.17g')},{values}\n")

    payload = _payload(options, acquisition, report)
    dump_json(options.report_path, payload)
    return payload


def _payload(
    options: RetimeOptions, acquisition: Acquisition, report: TimebaseReport
) -> dict[str, Any]:
    """Assemble the stage-one report, mirroring the units used in diagnostics."""
    return {
        "output": str(options.output),
        "report": str(options.report_path),
        "samples": report.samples,
        "channels": len(acquisition.channel_names),
        "channel_names": list(acquisition.channel_names),
        "rate_hz": options.rate_hz,
        "measured_rate_hz": report.measured_rate_hz,
        "tick_us": report.tick_s * 1e6,
        "period_us": report.period_s * 1e6,
        "measured_period_us": report.measured_period_s * 1e6,
        "period_mismatch_us": report.period_mismatch_s * 1e6,
        "resync_segments": report.segments,
        "shortest_segment_samples": report.shortest_segment,
        "longest_segment_samples": report.longest_segment,
        "expected_segment_samples": report.expected_segment_samples,
        "max_offset_us": report.max_offset_s * 1e6,
        "first_timestamp_s": report.first_timestamp_s,
        "last_timestamp_s": report.last_timestamp_s,
        "stitched": options.stitch,
        "warnings": list(report.warnings()),
        "note": (
            "Sample values are unchanged; only the time axis is rebuilt. "
            "Stage two (process) requires this uniform axis."
        ),
    }
