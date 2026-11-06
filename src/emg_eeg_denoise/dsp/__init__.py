"""Causal DSP: EMG/EEG filter banks and the streaming denoiser."""

from .core import DenoisedBlock, DenoiserConfig, RealtimeDenoiser
from .filters import (
    CausalFilterBank,
    IIRComb,
    IIRNotch,
    Modality,
    ModalityPreset,
    infer_modality,
    modality_preset,
)

__all__ = [
    "DenoisedBlock",
    "DenoiserConfig",
    "RealtimeDenoiser",
    "CausalFilterBank",
    "IIRComb",
    "IIRNotch",
    "Modality",
    "ModalityPreset",
    "infer_modality",
    "modality_preset",
]
