import numpy as np
import pytest

from emg_denoise import DenoiserConfig, RealtimeEMGDenoiser, StreamingFeatures
from emg_denoise.filters import CausalFilterBank


def collect(engine, x, t, sizes=(20,)):
    result = []
    start = i = 0
    while start < len(x):
        count = sizes[i % len(sizes)]
        result.extend(engine.process(x[start : start + count], t[start : start + count]))
        start += count
        i += 1
    return result


def test_timestamps_units_validity_and_partial_tail():
    x = np.random.default_rng(7).normal(size=(5003, 2)) * 0.0002
    t = 1_700_000_000.0 + np.arange(len(x)) / 2000
    engine = RealtimeEMGDenoiser(DenoiserConfig(channels=2))
    out = collect(engine, x, t, (17, 41, 3, 20))
    np.testing.assert_array_equal(np.concatenate([b.timestamps for b in out]), t)
    np.testing.assert_allclose(
        np.concatenate([b.samples for b in out]), CausalFilterBank(2).process(x), atol=1e-17
    )
    np.testing.assert_array_equal(np.concatenate([b.valid for b in out]), np.arange(len(x)) >= 2000)
    assert all(b.observed_end_to_end_ms is None for b in out)
    assert out[0].discontinuity and sum(b.discontinuity for b in out) == 1


def test_gap_resets_all_filter_state_and_warmup():
    rng = np.random.default_rng(8)
    x = rng.normal(size=(3000, 1))
    engine = RealtimeEMGDenoiser()
    collect(engine, x, np.arange(len(x)) / 2000)
    following = engine.process(x[:20], 5 + np.arange(20) / 2000)[0]
    expected = RealtimeEMGDenoiser().process(x[:20], 5 + np.arange(20) / 2000)[0]
    np.testing.assert_array_equal(following.samples, expected.samples)
    assert following.discontinuity and following.segment_id == 1
    assert not following.valid.any()


def test_bad_input_does_not_poison_state_and_backward_time_requires_reset():
    engine = RealtimeEMGDenoiser()
    t = np.arange(20) / 2000
    with pytest.raises(ValueError, match="finite"):
        engine.process(np.full(20, np.nan), t)
    with pytest.raises(ValueError, match="contiguous"):
        engine.process(np.zeros(20), np.arange(20) / 1000)
    out = engine.process(np.ones(20), t)[0]
    expected = RealtimeEMGDenoiser().process(np.ones(20), t)[0]
    np.testing.assert_array_equal(out.samples, expected.samples)
    with pytest.raises(ValueError, match="backward"):
        engine.process(np.zeros(20), t)
    engine.reset()
    assert engine.process(np.zeros(20), t)[0].discontinuity


def test_actual_receipt_latency_is_separate_from_digital_estimate():
    t = np.arange(20) / 2000
    out = RealtimeEMGDenoiser().process(np.zeros(20), t, received_at=0.05)[0]
    assert out.observed_end_to_end_ms >= 50
    assert out.buffering_delay_ms == pytest.approx(9.5)
    with pytest.raises(ValueError, match="received_at"):
        RealtimeEMGDenoiser().process(np.zeros(20), t, received_at=0)


def test_requesting_wavelet_without_evidence_keeps_main_chain():
    engine = RealtimeEMGDenoiser(DenoiserConfig(wavelet=True))
    assert engine.wavelet_status == "missing_rest_calibration"
    out = engine.process(np.zeros(20), np.arange(20) / 2000)[0]
    assert not out.wavelet_active


def test_feature_windows_preserve_signed_spectrum_and_clear_at_gap():
    t = np.arange(6200) / 2000
    # 123 Hz is not a mains harmonic; mean frequency of rectified data would differ.
    x = 4 * np.sin(2 * np.pi * 123 * t)
    engine = RealtimeEMGDenoiser()
    extractor = StreamingFeatures(1)
    frames = []
    for block in collect(engine, x, t):
        frames.extend(extractor.update(block))
    spectral = [f for f in frames if f.mean_frequency_hz is not None]
    assert spectral and spectral[-1].mean_frequency_hz[0] == pytest.approx(123, abs=0.1)
    assert spectral[-1].rms_50ms[0] > 1
    gap = engine.process(np.zeros(20), 10 + np.arange(20) / 2000)[0]
    assert extractor.update(gap) == []
