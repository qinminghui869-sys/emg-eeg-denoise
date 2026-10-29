"""Fixed causal filter chain. All sample matrices have shape (samples, channels)."""

from __future__ import annotations

import numpy as np
from numpy.typing import ArrayLike, NDArray
from scipy import signal


def validate_channels(channels: int) -> int:
    """Normalize supported integral channel counts, excluding booleans."""
    if isinstance(channels, bool) or not isinstance(channels, (int, np.integer)) or channels < 1:
        raise ValueError("channels must be a positive integer")
    return int(channels)


FS = 2000
BLOCK = 20
WARMUP = FS
COMB_DELAY = 40
COMB_BANDWIDTH = 2.0
_beta = np.tan(np.pi * COMB_BANDWIDTH / (2 * 50.0))
COMB_R = float((1 - _beta) / (1 + _beta))
COMB_G = float((1 + COMB_R) / 2)


def sample_matrix(samples: ArrayLike, channels: int | None = None) -> NDArray[np.float64]:
    x = np.asarray(samples, dtype=np.float64)
    if x.ndim == 1:
        x = x[:, None]
    if x.ndim != 2 or x.shape[1] < 1:
        raise ValueError("samples must have shape (samples, channels)")
    if channels is not None and x.shape[1] != channels:
        raise ValueError(f"expected {channels} channels, received {x.shape[1]}")
    if not np.isfinite(x).all():
        raise ValueError("samples must be finite; split the stream around missing data")
    return x


def bandpass_sos() -> NDArray[np.float64]:
    return np.vstack(
        (
            signal.butter(2, 20, btype="highpass", fs=FS, output="sos"),
            signal.butter(4, 450, btype="lowpass", fs=FS, output="sos"),
        )
    )


def comb_coefficients() -> tuple[NDArray[np.float64], NDArray[np.float64]]:
    """Sparse coefficients for reference analysis/export; runtime uses a ring buffer."""
    b, a = np.zeros(COMB_DELAY + 1), np.zeros(COMB_DELAY + 1)
    b[0], b[-1], a[0], a[-1] = COMB_G, -COMB_G, 1.0, -COMB_R
    return b, a


class IIRComb:
    """40-sample transposed delay line: g(x[n]-x[n-40])+r*y[n-40]."""

    def __init__(self, channels: int) -> None:
        self.channels = validate_channels(channels)
        self.reset()

    def reset(self) -> None:
        self._delay = np.zeros((COMB_DELAY, self.channels))
        self._position = 0

    def process(self, samples: ArrayLike) -> NDArray[np.float64]:
        x = sample_matrix(samples, self.channels)
        y = np.empty_like(x)
        start = 0
        while start < len(x):
            count = min(COMB_DELAY - self._position, len(x) - start)
            section = slice(self._position, self._position + count)
            gx = COMB_G * x[start : start + count]
            out = gx + self._delay[section]
            y[start : start + count] = out
            self._delay[section] = COMB_R * out - gx
            self._position = (self._position + count) % COMB_DELAY
            start += count
        return y


class CausalFilterBank:
    """Stateful 20–450 Hz Butterworth chain followed by a 50 Hz IIR comb."""

    def __init__(self, channels: int) -> None:
        self.channels = validate_channels(channels)
        self.sos = bandpass_sos()
        self.comb = IIRComb(self.channels)
        self.reset()

    def reset(self) -> None:
        self._state = np.zeros((len(self.sos), 2, self.channels))
        self.comb.reset()

    def process(self, samples: ArrayLike) -> NDArray[np.float64]:
        x = sample_matrix(samples, self.channels)
        if not len(x):
            return x.copy()
        y, self._state = signal.sosfilt(self.sos, x, axis=0, zi=self._state)
        return self.comb.process(y)
