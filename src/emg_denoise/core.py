"""Timestamp-preserving streaming interface with explicit quality and latency state."""

from __future__ import annotations

from dataclasses import dataclass
from time import perf_counter
from typing import TYPE_CHECKING

import numpy as np
from numpy.typing import ArrayLike, NDArray

from .filters import BLOCK, FS, WARMUP, CausalFilterBank, sample_matrix, validate_channels
from .wavelet import CenteredWavelet, WaveletCalibration

if TYPE_CHECKING:
    from .qualification import WaveletQualification


@dataclass(frozen=True, slots=True)
class DenoiserConfig:
    """Immutable session configuration; v1 supports the fixed 2 kHz signal chain."""

    channels: int = 1
    wavelet: bool = False
    sample_rate: int = FS
    block_size: int = BLOCK

    def __post_init__(self) -> None:
        validate_channels(self.channels)
        if self.sample_rate != FS or self.block_size != BLOCK:
            raise ValueError("v1 is specified for 2000 Hz and 20-sample processing hops")
        if not isinstance(self.wavelet, bool):
            raise ValueError("wavelet must be True or False")


@dataclass(frozen=True, slots=True)
class DenoisedBlock:
    """Aligned voltage samples, acquisition timestamps and software latency observations."""

    samples: NDArray[np.float64]
    timestamps: NDArray[np.float64]
    valid: NDArray[np.bool_]
    wavelet_active: bool
    discontinuity: bool
    segment_id: int
    processing_ms: float
    buffering_delay_ms: float
    estimated_digital_latency_ms: float
    observed_end_to_end_ms: float | None


class RealtimeEMGDenoiser:
    """Input acquisition timestamps are seconds, ordered and sampled at 2 kHz.

    process() accepts any positive sample count and internally consumes <=20
    samples per hop. A normal live caller should submit exactly 20 samples.
    A forward timestamp gap resets state and warmup. Backward/repeated time is
    rejected; reset() explicitly before restarting a device's timestamp clock.
    """

    def __init__(
        self,
        config: DenoiserConfig | None = None,
        *,
        calibration: WaveletCalibration | None = None,
        qualification: WaveletQualification | None = None,
    ) -> None:
        self._config = config or DenoiserConfig()
        if calibration is not None and calibration.channels != self.config.channels:
            raise ValueError("calibration channel count differs from the stream")
        self.calibration = calibration
        self.qualification = qualification
        self._wavelet_active = False
        self._wavelet_status = "disabled_by_config"
        if self.config.wavelet:
            if calibration is None:
                self._wavelet_status = "missing_rest_calibration"
            elif qualification is None:
                self._wavelet_status = "missing_qualification"
            elif not qualification.approved:
                self._wavelet_status = "qualification_not_approved"
            elif qualification.channels != self.config.channels or not qualification.matches(
                calibration
            ):
                self._wavelet_status = "qualification_calibration_mismatch"
            else:
                self._wavelet_active = True
                self._wavelet_status = "active"
        self._bank = CausalFilterBank(self.config.channels)
        self._wavelet = (
            CenteredWavelet(calibration)
            if self.wavelet_active and calibration is not None
            else None
        )
        self.segment_id = -1
        self.reset()

    @property
    def config(self) -> DenoiserConfig:
        return self._config

    @property
    def wavelet_active(self) -> bool:
        return self._wavelet_active

    @property
    def wavelet_status(self) -> str:
        return self._wavelet_status

    @property
    def pending_samples(self) -> int:
        """Tail awaiting real future data; deliberately not padded on finite replay."""
        return 0 if self._wavelet is None else self._wavelet.pending_samples

    def reset(self) -> None:
        """Preserve session config/calibration; clear all signal and time state.

        Recalibrate and construct a new instance if electrodes, voltage scaling,
        or recording conditions changed. Mode changes also require a new instance.
        """
        self._bank.reset()
        if self._wavelet is not None:
            self._wavelet.reset()
        self._received = 0
        self._last_timestamp: float | None = None
        self._pending_discontinuity = True
        self.segment_id += 1

    def process(
        self, samples: ArrayLike, timestamps: ArrayLike, *, received_at: float | None = None
    ) -> list[DenoisedBlock]:
        """Return all ready output blocks, or [] while collecting right context.

        received_at is optional host receipt time in the SAME clock domain as
        acquisition timestamps. Without it, observed end-to-end latency is None;
        the reported digital estimate excludes transport and scheduling delays.
        """
        call_started = perf_counter()
        x = sample_matrix(samples, self.config.channels)
        t = np.asarray(timestamps, dtype=np.float64)
        if t.ndim != 1 or len(t) != len(x) or not np.isfinite(t).all():
            raise ValueError("timestamps must be a finite vector, one timestamp per sample")
        if not len(x):
            return []
        if len(t) > 1 and not np.all(np.abs(np.diff(t) - 1 / FS) <= 1e-6):
            raise ValueError(
                "timestamps inside a block must be contiguous at 2000 Hz; split at gaps"
            )
        if received_at is not None and (not np.isfinite(received_at) or received_at < t[-1]):
            raise ValueError(
                "received_at must be finite, in the acquisition clock, and >= the last sample time"
            )
        # Validate the whole input BEFORE modifying filter state.
        if self._last_timestamp is not None:
            delta = t[0] - self._last_timestamp
            if delta <= 0:
                raise ValueError(
                    "backward/duplicate timestamps; call reset() for a restarted device clock"
                )
            if abs(delta - 1 / FS) > 1e-6:
                self.reset()
        result = []
        for offset in range(0, len(x), BLOCK):
            hop_started = call_started if offset == 0 else perf_counter()
            xx, tt = x[offset : offset + BLOCK], t[offset : offset + BLOCK]
            filtered = self._bank.process(xx)
            valid = np.arange(self._received, self._received + len(xx)) >= WARMUP
            self._received += len(xx)
            self._last_timestamp = float(tt[-1])
            ready = (
                [(filtered, tt.copy(), valid)]
                if self._wavelet is None
                else self._wavelet.push(filtered, tt, valid)
            )
            for yy, stamps, mask in ready:
                now = perf_counter()
                cost_ms = (now - hop_started) * 1000
                wait_ms = max(0.0, float(tt[-1] - stamps[0]) * 1000)
                observed = (
                    None
                    if received_at is None
                    else float((received_at - stamps[0]) * 1000 + (now - call_started) * 1000)
                )
                result.append(
                    DenoisedBlock(
                        samples=yy,
                        timestamps=stamps,
                        valid=mask,
                        wavelet_active=self.wavelet_active,
                        discontinuity=self._pending_discontinuity,
                        segment_id=self.segment_id,
                        processing_ms=cost_ms,
                        buffering_delay_ms=wait_ms,
                        estimated_digital_latency_ms=wait_ms + cost_ms,
                        observed_end_to_end_ms=observed,
                    )
                )
                self._pending_discontinuity = False
        return result
