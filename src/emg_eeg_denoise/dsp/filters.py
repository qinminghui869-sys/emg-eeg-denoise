"""Causal EMG/EEG filter banks. Sample matrices have shape (samples, channels)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import numpy as np
from numpy.typing import ArrayLike, NDArray
from scipy import signal

Modality = Literal["emg", "eeg"]


def validate_channels(channels: int) -> int:
    """Normalize supported integral channel counts, excluding booleans."""
    if isinstance(channels, bool) or not isinstance(channels, (int, np.integer)) or channels < 1:
        raise ValueError("channels must be a positive integer")
    return int(channels)


#: Back-compat aliases for the EMG preset.
FS = 2000
BLOCK = 20
WARMUP = FS
COMB_DELAY = 40
COMB_BANDWIDTH = 2.0
_beta = np.tan(np.pi * COMB_BANDWIDTH / (2 * 50.0))
COMB_R = float((1 - _beta) / (1 + _beta))
COMB_G = float((1 + COMB_R) / 2)

EEG_FS = 250
EEG_BLOCK = 10
EEG_WARMUP = EEG_FS
EEG_NOTCH_HZ = 50.0
EEG_NOTCH_Q = 25.0  # Q = f0 / bandwidth → 2 Hz at 50 Hz


@dataclass(frozen=True, slots=True)
class ModalityPreset:
    """Fixed DSP parameters for one signal modality."""

    modality: Modality
    sample_rate: int
    block_size: int
    warmup_samples: int


PRESETS: dict[Modality, ModalityPreset] = {
    "emg": ModalityPreset("emg", FS, BLOCK, WARMUP),
    "eeg": ModalityPreset("eeg", EEG_FS, EEG_BLOCK, EEG_WARMUP),
}


def modality_preset(modality: Modality | str) -> ModalityPreset:
    key = str(modality).lower()
    if key == "emg":
        return PRESETS["emg"]
    if key == "eeg":
        return PRESETS["eeg"]
    raise ValueError("modality must be 'emg' or 'eeg'")


def estimate_rate_hz(timestamps: ArrayLike) -> float | None:
    """Median reciprocal gap from a timestamp vector; ``None`` if unusable."""
    stamps = np.asarray(timestamps, dtype=np.float64).reshape(-1)
    if stamps.size < 2 or not np.isfinite(stamps).all():
        return None
    gaps = np.diff(stamps)
    positive = gaps[gaps > 0.0]
    if positive.size == 0:
        return None
    median_dt = float(np.median(positive))
    if median_dt <= 0.0:
        return None
    return 1.0 / median_dt


def _assert_filter_rate(modality: Modality, sample_rate: int) -> None:
    """Reject rates that cannot host the fixed cutoff frequencies."""
    if sample_rate < 1:
        raise ValueError("sample rate must be a positive integer")
    if modality == "emg":
        if sample_rate <= 2 * 450:
            raise ValueError(f"EMG sample rate {sample_rate} Hz is too low for a 450 Hz lowpass")
        # Comb delay is 40 samples ≈ 20 ms only at 2 kHz.
        if abs(sample_rate - FS) / FS > 0.05:
            raise ValueError(
                f"EMG denoise expects ~{FS} Hz (comb delay is fixed); received {sample_rate} Hz"
            )
        return
    if sample_rate <= 2 * 50:
        raise ValueError(f"EEG sample rate {sample_rate} Hz is too low for a 50 Hz lowpass/notch")


def resolved_preset(
    modality: Modality | str, sample_rate: float | int | None = None
) -> ModalityPreset:
    """Preset with optional sample-rate override (scales block size and 1 s warmup)."""
    base = modality_preset(modality)
    if sample_rate is None:
        return base
    fs = int(round(float(sample_rate)))
    _assert_filter_rate(base.modality, fs)
    hop_s = base.block_size / base.sample_rate
    block = max(1, int(round(hop_s * fs)))
    return ModalityPreset(base.modality, fs, block, fs)


def infer_modality(path: str | object) -> Modality:
    """Guess modality from a file name; anything without ``eeg`` defaults to EMG."""
    name = str(getattr(path, "name", path)).lower()
    return "eeg" if "eeg" in name else "emg"


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


def emg_bandpass_sos(fs: int = FS) -> NDArray[np.float64]:
    """EMG: 2nd-order 20 Hz highpass + 4th-order 450 Hz lowpass."""
    return np.vstack(
        (
            signal.butter(2, 20, btype="highpass", fs=fs, output="sos"),
            signal.butter(4, 450, btype="lowpass", fs=fs, output="sos"),
        )
    )


def eeg_bandpass_sos(fs: int = EEG_FS) -> NDArray[np.float64]:
    """EEG: 2nd-order 0.5 Hz highpass + 4th-order 50 Hz lowpass."""
    return np.vstack(
        (
            signal.butter(2, 0.5, btype="highpass", fs=fs, output="sos"),
            signal.butter(4, 50, btype="lowpass", fs=fs, output="sos"),
        )
    )


def bandpass_sos() -> NDArray[np.float64]:
    """Back-compat: EMG bandpass at 2 kHz."""
    return emg_bandpass_sos(FS)


def comb_coefficients() -> tuple[NDArray[np.float64], NDArray[np.float64]]:
    """Sparse coefficients for reference analysis/export; runtime uses a ring buffer."""
    b, a = np.zeros(COMB_DELAY + 1), np.zeros(COMB_DELAY + 1)
    b[0], b[-1], a[0], a[-1] = COMB_G, -COMB_G, 1.0, -COMB_R
    return b, a


def eeg_notch_ba(fs: int = EEG_FS) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
    """Single IIR notch at 50 Hz with Q=25 (−3 dB bandwidth 2 Hz)."""
    b, a = signal.iirnotch(EEG_NOTCH_HZ, EEG_NOTCH_Q, fs=fs)
    return np.asarray(b, dtype=np.float64), np.asarray(a, dtype=np.float64)


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


class IIRNotch:
    """Stateful direct-form II transposed biquad notch (one section per channel)."""

    def __init__(self, channels: int, fs: int = EEG_FS) -> None:
        self.channels = validate_channels(channels)
        self.fs = int(fs)
        self.b, self.a = eeg_notch_ba(self.fs)
        self.reset()

    def reset(self) -> None:
        self._zi = np.zeros((max(len(self.a), len(self.b)) - 1, self.channels))

    def process(self, samples: ArrayLike) -> NDArray[np.float64]:
        x = sample_matrix(samples, self.channels)
        if not len(x):
            return x.copy()
        y, self._zi = signal.lfilter(self.b, self.a, x, axis=0, zi=self._zi)
        return np.asarray(y, dtype=np.float64)


class CausalFilterBank:
    """Modality-specific causal bandpass followed by comb (EMG) or notch (EEG)."""

    def __init__(
        self,
        channels: int,
        modality: Modality | str = "emg",
        *,
        sample_rate: float | int | None = None,
    ) -> None:
        self.channels = validate_channels(channels)
        self.preset = resolved_preset(modality, sample_rate)
        self.modality = self.preset.modality
        self.sample_rate = self.preset.sample_rate
        if self.modality == "emg":
            self.sos = emg_bandpass_sos(self.sample_rate)
            self._post: IIRComb | IIRNotch = IIRComb(self.channels)
        else:
            self.sos = eeg_bandpass_sos(self.sample_rate)
            self._post = IIRNotch(self.channels, fs=self.sample_rate)
        self.reset()

    def reset(self) -> None:
        self._state = np.zeros((len(self.sos), 2, self.channels))
        self._post.reset()

    def process(self, samples: ArrayLike) -> NDArray[np.float64]:
        x = sample_matrix(samples, self.channels)
        if not len(x):
            return x.copy()
        y, self._state = signal.sosfilt(self.sos, x, axis=0, zi=self._state)
        return self._post.process(y)
