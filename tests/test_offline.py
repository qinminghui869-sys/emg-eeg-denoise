import numpy as np

from emg_eeg_denoise.dsp.filters import CausalFilterBank
from emg_eeg_denoise.dsp.offline import mirror_pad, mirror_unpad, offline_denoise


def test_mirror_pad_roundtrip():
    x = np.arange(10, dtype=np.float64).reshape(-1, 1)
    padded, used = mirror_pad(x)
    assert used
    assert padded.shape[0] == 3 * 10 - 2
    np.testing.assert_array_equal(mirror_unpad(padded, 10), x)


def test_offline_emg_reduces_edge_energy_vs_causal():
    rng = np.random.default_rng(0)
    n = 8000
    t = np.arange(n) / 2000.0
    x = (rng.normal(scale=0.3, size=n) + 0.8 * np.sin(2 * np.pi * 50 * t)).reshape(-1, 1)
    offline, info = offline_denoise(x, modality="emg")
    causal = CausalFilterBank(1, modality="emg").process(x)
    edge = 200  # 100 ms
    off_e = float(np.mean(offline[:edge] ** 2) + np.mean(offline[-edge:] ** 2))
    caus_e = float(np.mean(causal[:edge] ** 2) + np.mean(causal[-edge:] ** 2))
    assert info["method"] == "offline_filtfilt"
    assert "梳状" in "".join(info["stages"])
    assert off_e < 0.6 * caus_e


def test_offline_eeg_runs_and_keeps_shape():
    x = np.random.default_rng(1).normal(size=(2000, 2))
    y, info = offline_denoise(x, modality="eeg", sample_rate=250)
    assert y.shape == x.shape
    assert info["method"] == "offline_filtfilt"
    assert any("陷波" in stage for stage in info["stages"])
