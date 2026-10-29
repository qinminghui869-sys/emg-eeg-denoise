"""2 kHz real-time surface EMG denoising reference implementation."""

from .core import DenoisedBlock, DenoiserConfig, RealtimeEMGDenoiser
from .features import FeatureFrame, StreamingFeatures, rms, spectral_features
from .qualification import WaveletQualification, evaluate_wavelet
from .wavelet import WaveletCalibration, calibrate_noise

__all__ = [
    "DenoisedBlock",
    "DenoiserConfig",
    "RealtimeEMGDenoiser",
    "FeatureFrame",
    "StreamingFeatures",
    "rms",
    "spectral_features",
    "WaveletQualification",
    "evaluate_wavelet",
    "WaveletCalibration",
    "calibrate_noise",
]
