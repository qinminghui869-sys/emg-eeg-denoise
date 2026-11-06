"""Reconstruct a uniform sample clock from tick-quantized acquisition timestamps.

Acquisition hardware stamps samples with a clock whose resolution (one tick) is
finer than, but not a divisor of, the true sampling period. Each sample accrues
``period - tick`` of error; once that reaches a whole tick the reported stamp
jumps two ticks to resynchronize. A 2 kHz sEMG stream on a 451.088 us tick
therefore reports only four distinct gaps -- 451.088, 450.850 (one tick, two
float32 neighbours) and 902.176, 901.937 (two ticks) -- while averaging exactly
500 us.

The raw stamps are consequently unusable downstream: :func:`emg_eeg_denoise.io.load_recording`
requires contiguous uniform stamps, and replay treats any off-period gap as a
segment break, which would shatter one recording into thousands of fragments and
reset the streaming filter state at each one.

This module discards the per-sample stamp values, keeps them only to fix a time
origin, and rebuilds the axis from the sample ordinal::

    t[i] = offset + i * period

Resync points (gaps of two or more ticks) bound the segments used to anchor
``offset``, so a quantization artefact is never mistaken for a real dropout.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

import numpy as np

from .._typing import FloatArray, IntArray

#: Clock resolution observed on the BCIGO wrist-EMG recorders, in seconds.
OBSERVED_TICK_S: Final = 451.08795166015625e-6

#: Gaps at or above this many ticks mark a hardware resync, not a dropped sample.
RESYNC_TICKS: Final = 2


@dataclass(frozen=True, slots=True)
class TimebaseReport:
    """Diagnostics describing how far the rebuilt axis moved the original stamps."""

    samples: int
    tick_s: float
    period_s: float
    measured_period_s: float
    segments: int
    shortest_segment: int
    longest_segment: int
    expected_segment_samples: float
    max_offset_s: float
    first_timestamp_s: float
    last_timestamp_s: float

    @property
    def measured_rate_hz(self) -> float:
        """Sampling rate implied by the original stamps, before rebuilding."""
        return 1.0 / self.measured_period_s

    @property
    def period_mismatch_s(self) -> float:
        """Gap between the requested period and the one the stamps actually imply."""
        return abs(self.measured_period_s - self.period_s)

    def warnings(self) -> tuple[str, ...]:
        """Conditions under which the rebuilt axis should not be trusted."""
        issues: list[str] = []
        if self.period_mismatch_s > self.tick_s / 2:
            issues.append(
                f"measured period {self.measured_period_s * 1e6:.3f} us differs from the "
                f"requested {self.period_s * 1e6:.3f} us by more than half a tick"
            )
        if self.first_timestamp_s < 0.0:
            issues.append(f"first timestamp is negative ({self.first_timestamp_s * 1e6:.3f} us)")
        return tuple(issues)


def estimate_tick(timestamps: FloatArray) -> float:
    """Estimate the clock resolution from the smallest reported gap.

    One-tick gaps bound the difference set from below, so the smallest cluster of
    positive gaps is averaged; clustering absorbs the sub-microsecond spread that
    float32 storage introduces into otherwise identical intervals.
    """
    gaps = np.diff(timestamps)
    positive = gaps[gaps > 0.0]
    if positive.size == 0:
        raise ValueError("timestamps must be strictly increasing to estimate the clock tick")
    cluster = positive[positive < 1.5 * positive.min()]
    # The median picks the dominant float32 representation rather than averaging
    # it with its neighbour, which would bias the tick by a fraction of itself.
    return float(np.median(cluster))


def find_resync_points(timestamps: FloatArray, tick_s: float) -> IntArray:
    """Locate samples whose reported gap spans two or more ticks.

    These are the clock's own correction points. They are the only places where
    the quantization error resets, so they bound the anchoring segments.
    """
    if tick_s <= 0.0:
        raise ValueError("tick must be positive")
    jumps = np.rint(np.diff(timestamps) / tick_s) >= RESYNC_TICKS
    return (np.flatnonzero(jumps) + 1).astype(np.int64)


def _segment_bounds(samples: int, resync: IntArray) -> list[tuple[int, int]]:
    """Half-open [start, stop) spans delimited by resync points."""
    edges = [0, *resync.tolist(), samples]
    return [(edges[i], edges[i + 1]) for i in range(len(edges) - 1)]


def rebuild_timestamps(
    timestamps: FloatArray,
    *,
    period_s: float = 1.0 / 2000,
    tick_s: float | None = None,
    stitch: bool = True,
    non_negative: bool = True,
) -> tuple[FloatArray, TimebaseReport]:
    """Rebuild a uniform time axis from tick-quantized stamps.

    Args:
        timestamps: Original stamps in seconds, strictly increasing.
        period_s: True sampling period. The default matches the 2 kHz chain.
        tick_s: Clock resolution; estimated from the data when omitted.
        stitch: Continue each segment from the previous one so every gap equals
            ``period_s`` exactly. Offsets from the original stamps then accumulate
            across segments. When false, each segment is anchored on its own
            centroid, which keeps every sample within half a tick of its original
            stamp but leaves a seam at each segment boundary.
        non_negative: Shift the axis so it starts at or after zero. Centroid
            anchoring can otherwise extrapolate the first segment below zero.

    Returns:
        The rebuilt axis and a :class:`TimebaseReport`. Inspect
        :meth:`TimebaseReport.warnings` before trusting the result.
    """
    stamps = np.asarray(timestamps, dtype=np.float64)
    if stamps.ndim != 1:
        raise ValueError("timestamps must be one-dimensional")
    if stamps.size < 2:
        raise ValueError("at least two timestamps are required")
    if not np.isfinite(stamps).all():
        raise ValueError("timestamps must be finite")
    if np.any(np.diff(stamps) <= 0.0):
        raise ValueError("timestamps must be strictly increasing")
    if period_s <= 0.0:
        raise ValueError("period must be positive")

    tick = estimate_tick(stamps) if tick_s is None else float(tick_s)
    if tick <= 0.0:
        raise ValueError("tick must be positive")

    ordinal = np.arange(stamps.size, dtype=np.float64)
    resync = find_resync_points(stamps, tick)
    bounds = _segment_bounds(stamps.size, resync)

    rebuilt = np.empty_like(stamps)
    for index, (start, stop) in enumerate(bounds):
        if stitch and index > 0:
            # Continue from the previous sample, keeping every gap at one period.
            offset = rebuilt[start - 1] + period_s - ordinal[start] * period_s
        else:
            # Least-squares offset for a line of fixed slope: the mean residual.
            offset = float(np.mean(stamps[start:stop] - period_s * ordinal[start:stop]))
        rebuilt[start:stop] = offset + period_s * ordinal[start:stop]

    if non_negative and rebuilt[0] < 0.0:
        rebuilt -= rebuilt[0]

    lengths = [stop - start for start, stop in bounds]
    drift = period_s - tick
    report = TimebaseReport(
        samples=int(stamps.size),
        tick_s=tick,
        period_s=period_s,
        measured_period_s=float((stamps[-1] - stamps[0]) / (stamps.size - 1)),
        segments=len(bounds),
        shortest_segment=min(lengths),
        longest_segment=max(lengths),
        # Error accrues at `drift` per sample and resyncs after a whole tick.
        expected_segment_samples=tick / drift if drift > 0.0 else float("inf"),
        max_offset_s=float(np.abs(rebuilt - stamps).max()),
        first_timestamp_s=float(rebuilt[0]),
        last_timestamp_s=float(rebuilt[-1]),
    )
    return rebuilt, report
