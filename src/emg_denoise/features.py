"""Signed-spectrum features and trailing windows; no additional lookahead."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from numpy.typing import ArrayLike

from ._typing import FloatArray, SignalBlock
from .filters import FS, sample_matrix, validate_channels


def rms(samples: ArrayLike) -> FloatArray:
    x = sample_matrix(samples)
    if not len(x):
        raise ValueError("RMS requires at least one sample")
    return np.asarray(np.sqrt(np.mean(x * x, axis=0)), dtype=np.float64)


def spectral_features(samples: ArrayLike) -> dict[str, FloatArray]:
    """Hann-windowed signed periodogram; interpolate median frequency."""
    x = sample_matrix(samples)
    if len(x) < 4:
        raise ValueError("at least four samples are needed for a spectrum")
    window = np.hanning(len(x))[:, None]
    power = np.abs(np.fft.rfft((x - x.mean(axis=0)) * window, axis=0)) ** 2
    frequencies = np.fft.rfftfreq(len(x), 1 / FS)
    keep = (frequencies >= 20) & (frequencies <= 450)
    f, p = frequencies[keep], power[keep]
    total = p.sum(axis=0)
    mean = np.full(x.shape[1], np.nan)
    median = np.full(x.shape[1], np.nan)
    for c in range(x.shape[1]):
        if total[c] > np.finfo(float).tiny:
            mean[c] = np.dot(f, p[:, c]) / total[c]
            cumulative = np.cumsum(p[:, c])
            median[c] = np.interp(total[c] / 2, cumulative, f)
    return {"mean_frequency_hz": mean, "median_frequency_hz": median}


@dataclass(frozen=True, slots=True)
class FeatureFrame:
    """Features of trailing windows, timestamped at the final acquisition sample."""

    timestamp: float
    rms_50ms: FloatArray
    envelope_rms_20ms: FloatArray
    mean_frequency_hz: FloatArray | None
    median_frequency_hz: FloatArray | None
    spectral_window_seconds: float


class StreamingFeatures:
    """50 ms RMS and 1 s spectral history, emitted every 10 ms.

    The 1 s spectrum describes that trailing interval, not an instantaneous
    measurement. Only fully valid windows contribute to frequency features.
    """

    def __init__(self, channels: int) -> None:
        self.channels = validate_channels(channels)
        self.reset()

    def reset(self) -> None:
        self._x = np.empty((0, self.channels))
        self._valid = np.empty(0, dtype=bool)
        self._last_timestamp: float | None = None
        self._pending = 0

    def update(self, block: SignalBlock) -> list[FeatureFrame]:
        if block.discontinuity:
            self.reset()
        out = []
        x = sample_matrix(block.samples, self.channels)
        for i, row in enumerate(x):
            stamp = float(block.timestamps[i])
            if (
                self._last_timestamp is not None
                and abs(stamp - self._last_timestamp - 1 / FS) > 1e-6
            ):
                self.reset()
            self._x = np.vstack((self._x, row))[-FS:]
            self._valid = np.append(self._valid, block.valid[i])[-FS:]
            self._last_timestamp = stamp
            self._pending += 1
            if self._pending < 20:
                continue
            self._pending = 0
            if len(self._x) < 100 or not self._valid[-100:].all():
                continue
            spectrum = (
                spectral_features(self._x) if len(self._x) == FS and self._valid.all() else None
            )
            out.append(
                FeatureFrame(
                    timestamp=stamp,
                    rms_50ms=rms(self._x[-100:]),
                    envelope_rms_20ms=rms(self._x[-40:]),
                    mean_frequency_hz=None if spectrum is None else spectrum["mean_frequency_hz"],
                    median_frequency_hz=None
                    if spectrum is None
                    else spectrum["median_frequency_hz"],
                    spectral_window_seconds=1.0,
                )
            )
        return out
