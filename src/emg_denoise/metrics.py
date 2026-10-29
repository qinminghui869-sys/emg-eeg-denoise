"""Offline signal-quality metrics, shared by qualification and validation reports."""

from __future__ import annotations

import numpy as np

from ._typing import FloatArray


def signal_to_noise_ratio(reference: FloatArray, estimate: FloatArray) -> FloatArray:
    """Return per-channel SNR against an aligned clean reference, in dB."""
    signal_power = (reference**2).sum(axis=0)
    error_power = ((reference - estimate) ** 2).sum(axis=0)
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.asarray(10 * np.log10(signal_power / error_power), dtype=np.float64)


def onset_crossing(samples: FloatArray, onset: int, threshold: FloatArray) -> FloatArray:
    """Detect a sustained crossing in a -50..200 ms annotated onset neighborhood.

    Uses a 20 ms causal RMS envelope and requires 10 ms above the threshold.
    Missing detections are NaN and cannot pass qualification.
    """
    envelope = np.sqrt(
        np.stack(
            [
                np.convolve(samples[:, channel] ** 2, np.ones(40) / 40, mode="full")[: len(samples)]
                for channel in range(samples.shape[1])
            ],
            axis=1,
        )
    )
    result = np.full(samples.shape[1], np.nan)
    begin, end = max(0, onset - 100), min(len(samples) - 19, onset + 400)
    for channel in range(samples.shape[1]):
        above = envelope[begin : end + 19, channel] >= threshold[channel]
        runs = np.convolve(above.astype(int), np.ones(20, dtype=int), mode="valid")
        hits = np.flatnonzero(runs >= 20)
        if len(hits):
            result[channel] = begin + hits[0]
    return result
