"""Evidence-based SWT enablement. Synthetic data never approve a real session."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any

import numpy as np
from numpy.typing import ArrayLike

from ._typing import FloatArray, IntArray
from .features import rms, spectral_features
from .filters import BLOCK, FS, WARMUP, CausalFilterBank, sample_matrix
from .metrics import onset_crossing, signal_to_noise_ratio
from .wavelet import CenteredWavelet, WaveletCalibration


def calibration_digest(calibration: WaveletCalibration) -> str:
    return hashlib.sha256(calibration.sigmas.astype("<f8").tobytes()).hexdigest()


@dataclass(frozen=True, slots=True)
class WaveletQualification:
    """Per-channel worst-case acceptance results bound to one rest calibration."""

    channels: int
    calibration_sha256: str
    rms_change_percent: float
    median_frequency_change_percent: float
    snr_improvement_db: float
    additional_onset_shift_ms: float | None
    representative_data: bool = False
    dataset_sha256: str = ""
    note: str = ""

    def __post_init__(self) -> None:
        if (
            isinstance(self.channels, bool)
            or not isinstance(self.channels, int)
            or self.channels < 1
        ):
            raise ValueError("qualification channels must be a positive integer")
        if not isinstance(self.representative_data, bool):
            raise ValueError("representative_data must be a boolean")
        for name in (
            "rms_change_percent",
            "median_frequency_change_percent",
            "snr_improvement_db",
            "additional_onset_shift_ms",
        ):
            value = getattr(self, name)
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, (int, float, np.number))
            ):
                raise ValueError(f"{name} must be a number or null")
        if not isinstance(self.calibration_sha256, str) or not isinstance(self.dataset_sha256, str):
            raise ValueError("qualification digests must be strings")

    @property
    def metrics_passed(self) -> bool:
        values = [
            self.rms_change_percent,
            self.median_frequency_change_percent,
            self.snr_improvement_db,
            self.additional_onset_shift_ms,
        ]
        if any(v is None or not np.isfinite(v) for v in values):
            return False
        shift = self.additional_onset_shift_ms
        if shift is None:
            return False
        return (
            0 <= self.rms_change_percent <= 5
            and 0 <= self.median_frequency_change_percent <= 5
            and self.snr_improvement_db >= 3
            and 0 <= shift <= 10
        )

    @property
    def approved(self) -> bool:
        return bool(self.metrics_passed and self.representative_data and self.dataset_sha256)

    def matches(self, calibration: WaveletCalibration) -> bool:
        return (
            self.channels == calibration.channels
            and self.calibration_sha256 == calibration_digest(calibration)
        )

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result.update(metrics_passed=self.metrics_passed, approved=self.approved)
        # JSON null instead of nonstandard Infinity/NaN for unavailable metrics.
        return {
            k: (float(v) if np.isfinite(v) else None) if isinstance(v, (float, np.floating)) else v
            for k, v in result.items()
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> WaveletQualification:
        fields = cls.__dataclass_fields__
        values = {k: v for k, v in data.items() if k in fields}
        for k in ("rms_change_percent", "median_frequency_change_percent", "snr_improvement_db"):
            if values.get(k) is None:
                values[k] = float("nan")
        return cls(**values)


def enhance_replay(
    filtered: FloatArray, calibration: WaveletCalibration
) -> tuple[FloatArray, IntArray]:
    """Internal ungated candidate used only for qualification, not live enablement."""
    stream = CenteredWavelet(calibration)
    chunks, indices = [], []
    for start in range(0, len(filtered), BLOCK):
        stop = min(start + BLOCK, len(filtered))
        stamps = np.arange(start, stop) / FS
        ready = stream.push(filtered[start:stop], stamps, np.arange(start, stop) >= WARMUP)
        for y, t, _ in ready:
            chunks.append(y)
            indices.append(np.rint(t * FS).astype(int))
    if not chunks:
        return np.empty((0, filtered.shape[1])), np.empty(0, dtype=int)
    return np.concatenate(chunks), np.concatenate(indices)


def evaluate_wavelet(
    clean_samples: ArrayLike,
    noisy_samples: ArrayLike,
    calibration: WaveletCalibration,
    *,
    onset_indices: list[int] | None = None,
    representative_data: bool = False,
    note: str = "",
) -> WaveletQualification:
    """Assess candidate against the *same causal main-chain clean reference*.

    clean/noisy must be paired and aligned voltage data. onsets are annotated
    sample indices. With no valid annotation, onset acceptance remains unknown
    and approval is impossible. This routine does not claim pure clean in-vivo
    reference data exist; semi-synthetic contamination is a valid test design.
    """
    clean = sample_matrix(clean_samples, calibration.channels)
    noisy = sample_matrix(noisy_samples, calibration.channels)
    if clean.shape != noisy.shape or len(clean) < 3 * FS:
        raise ValueError("clean/noisy must be aligned, same shape, and at least 3 seconds")
    clean_main = CausalFilterBank(calibration.channels).process(clean)
    noisy_main = CausalFilterBank(calibration.channels).process(noisy)
    clean_swt, indices = enhance_replay(clean_main, calibration)
    noisy_swt, other_indices = enhance_replay(noisy_main, calibration)
    if not np.array_equal(indices, other_indices):
        raise RuntimeError("candidate/reference alignment failed")
    valid = indices >= WARMUP
    reference, before = clean_main[indices][valid], noisy_main[indices][valid]
    clean_candidate, after = clean_swt[valid], noisy_swt[valid]
    ref_rms = rms(reference)
    with np.errstate(divide="ignore", invalid="ignore"):
        rms_change = float(np.max(100 * np.abs(rms(clean_candidate) - ref_rms) / ref_rms))
        ref_mdf = spectral_features(reference)["median_frequency_hz"]
        new_mdf = spectral_features(clean_candidate)["median_frequency_hz"]
        mdf_change = float(np.max(100 * np.abs(new_mdf - ref_mdf) / ref_mdf))
        snr_gain = float(
            np.min(
                signal_to_noise_ratio(reference, after) - signal_to_noise_ratio(reference, before)
            )
        )
    shifts = []
    for onset in onset_indices or []:
        if (
            not isinstance(onset, (int, np.integer))
            or not WARMUP + 100 <= onset < indices[-1] - 400
        ):
            raise ValueError("onsets need 50 ms valid prehistory and 200 ms candidate posthistory")
        # Use one threshold from the clean reference for both processing paths.
        threshold = 0.2 * rms(clean_main[onset : onset + 400])
        a = onset_crossing(noisy_main[: len(noisy_swt)], onset, threshold)
        b = onset_crossing(noisy_swt, onset, threshold)
        if (threshold <= 0).any() or not np.isfinite(a).all() or not np.isfinite(b).all():
            shifts.append(float("nan"))
        else:
            shifts.append(float(np.max(np.abs(a - b)) * 1000 / FS))
    onset_shift = None if not shifts else float(np.max(shifts))
    digest = hashlib.sha256()
    for a in (clean, noisy):
        digest.update(a.astype("<f8").tobytes())
    return WaveletQualification(
        channels=calibration.channels,
        calibration_sha256=calibration_digest(calibration),
        rms_change_percent=rms_change,
        median_frequency_change_percent=mdf_change,
        snr_improvement_db=snr_gain,
        additional_onset_shift_ms=onset_shift,
        representative_data=representative_data,
        dataset_sha256=digest.hexdigest(),
        note=note,
    )
