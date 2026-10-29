"""Normalized SWT, static rest calibration, bounded SURE soft shrinkage."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pywt
from numpy.typing import ArrayLike, NDArray

from ._typing import BoolArray, FloatArray, WaveletOutput
from .filters import BLOCK, FS, WARMUP, CausalFilterBank, sample_matrix

FRAME = 256
LEVELS = 4
WAVELET = "db4"
CENTER = slice(118, 138)  # one-based samples 119..138
RIGHT_CONTEXT = 118
CALIBRATION_SAMPLES = 2 * FS


@dataclass(frozen=True, slots=True)
class WaveletCalibration:
    """sigmas order: D4, D3, D2, D1; units match the input voltage."""

    sigmas: NDArray[np.float64]

    def __post_init__(self) -> None:
        a = np.array(self.sigmas, dtype=np.float64, copy=True)
        if a.ndim != 2 or a.shape[0] != LEVELS or a.shape[1] < 1:
            raise ValueError("sigmas must have shape (4, channels), ordered D4..D1")
        if not np.isfinite(a).all() or (a < 0).any():
            raise ValueError("noise sigmas must be finite and nonnegative")
        a.setflags(write=False)
        object.__setattr__(self, "sigmas", a)

    @property
    def channels(self) -> int:
        return int(self.sigmas.shape[1])


def calibrate_noise(rest_samples: ArrayLike) -> WaveletCalibration:
    """At least 3 seconds of rest: discard 1 s warmup, estimate on the next 2 s."""
    x = sample_matrix(rest_samples)
    if len(x) < WARMUP + CALIBRATION_SAMPLES:
        raise ValueError("rest calibration needs at least 6000 samples (3 seconds)")
    filtered = CausalFilterBank(x.shape[1]).process(x[: WARMUP + CALIBRATION_SAMPLES])
    coeffs = pywt.swt(filtered[WARMUP:], WAVELET, level=LEVELS, axis=0, trim_approx=True, norm=True)
    # db4's level-4 support has 105 samples of accumulated filter extension.
    details = np.stack(coeffs[1:])[:, 105:-105, :]
    median = np.median(details, axis=1, keepdims=True)
    sigmas = np.median(np.abs(details - median), axis=1) / 0.6744897501960817
    return WaveletCalibration(sigmas)


def sure_threshold(detail: NDArray[np.float64], sigma: NDArray[np.float64]) -> NDArray[np.float64]:
    """Minimize SURE over coefficient magnitudes, 0 and the universal bound.

    For standardized d and threshold t: R(t)=m+sum(min(d²,t²))-2*#{|d|<=t}.
    Ties choose the smallest threshold. Zero-noise channels are passed through.
    SWT noise is correlated; empirical qualification remains mandatory.
    """
    m = len(detail)
    threshold = np.zeros(detail.shape[1])
    positive = sigma > 0
    if not positive.any():
        return threshold
    s = sigma[positive]
    # Extremely small calibration scales can underflow when squared. Normalize
    # first and cap magnitudes: candidates outside the universal bound cannot win.
    cap2 = 2 * np.log(m)
    with np.errstate(over="ignore", divide="ignore"):
        z2 = np.sort(np.minimum(np.abs(detail[:, positive] / s), np.sqrt(cap2) + 1) ** 2, axis=0)
    k = np.arange(1, m + 1)[:, None]
    risk = m - 2 * k + np.cumsum(z2, axis=0) + (m - k) * z2
    risk[z2 > cap2] = np.inf
    zero_risk = m - 2 * np.sum(z2 == 0, axis=0)
    cap_risk = m + np.minimum(z2, cap2).sum(axis=0) - 2 * (z2 <= cap2).sum(axis=0)
    risks = np.vstack((zero_risk, risk, cap_risk))
    candidates = np.vstack((np.zeros(len(s)), z2, np.full(len(s), cap2)))
    choice = risks.argmin(axis=0)
    threshold[positive] = s * np.sqrt(candidates[choice, np.arange(len(s))])
    return threshold


def denoise_frame(frame: ArrayLike, calibration: WaveletCalibration) -> NDArray[np.float64]:
    x = sample_matrix(frame, calibration.channels)
    if len(x) != FRAME:
        raise ValueError("SWT frame must contain exactly 256 samples")
    coeffs = pywt.swt(x, WAVELET, level=LEVELS, axis=0, trim_approx=True, norm=True)
    for level in range(LEVELS):
        detail = coeffs[level + 1]
        threshold = sure_threshold(detail, calibration.sigmas[level])
        coeffs[level + 1] = np.sign(detail) * np.maximum(np.abs(detail) - threshold, 0)
    return np.asarray(pywt.iswt(coeffs, WAVELET, axis=0, norm=True), dtype=np.float64)


class CenteredWavelet:
    """Only releases a center block once its real right context exists.

    Initial missing *past* context is reflected and marked invalid by the caller's
    one-second warmup. Future samples are never invented. At 20-sample input hops,
    first output is available at sample 140, with 60..69.5 ms sample ages.
    """

    def __init__(self, calibration: WaveletCalibration) -> None:
        self.calibration = calibration
        self.reset()

    def reset(self) -> None:
        self._samples = np.empty((0, self.calibration.channels))
        self._timestamps = np.empty(0)
        self._valid = np.empty(0, dtype=bool)
        self.received = 0
        self.emitted = 0

    @property
    def pending_samples(self) -> int:
        return self.received - self.emitted

    def push(
        self, samples: FloatArray, timestamps: FloatArray, valid: BoolArray
    ) -> list[WaveletOutput]:
        if not 0 < len(samples) <= BLOCK:
            raise ValueError("push at most 20 samples at a time")
        self._samples = np.concatenate((self._samples, samples))[-(FRAME + BLOCK) :]
        self._timestamps = np.concatenate((self._timestamps, timestamps))[-(FRAME + BLOCK) :]
        self._valid = np.concatenate((self._valid, valid))[-(FRAME + BLOCK) :]
        self.received += len(samples)
        origin = self.received - len(self._samples)
        out = []
        while self.received >= self.emitted + BLOCK + RIGHT_CONTEXT:
            start = self.emitted - CENTER.start
            indices = np.arange(start, start + FRAME)
            indices = np.where(indices < 0, -indices, indices) - origin
            frame = self._samples[indices]
            y = denoise_frame(frame, self.calibration)[CENTER]
            sl = slice(self.emitted - origin, self.emitted - origin + BLOCK)
            out.append((y, self._timestamps[sl].copy(), self._valid[sl].copy()))
            self.emitted += BLOCK
        return out
