import numpy as np
import pytest
import pywt
from test_streaming import collect

from emg_denoise import (
    DenoiserConfig,
    RealtimeEMGDenoiser,
    WaveletCalibration,
    WaveletQualification,
    calibrate_noise,
    evaluate_wavelet,
)
from emg_denoise.filters import CausalFilterBank
from emg_denoise.qualification import calibration_digest
from emg_denoise.wavelet import denoise_frame, sure_threshold


def approved_test_report(calibration):
    # Isolated gate fixture only; not saved as real-world evidence or a report.
    return WaveletQualification(
        calibration.channels,
        calibration_digest(calibration),
        0,
        0,
        4,
        2,
        representative_data=True,
        dataset_sha256="unit-test-fixture-only",
    )


def test_calibration_requires_warmup_and_rest_and_is_per_channel():
    with pytest.raises(ValueError, match="6000"):
        calibrate_noise(np.zeros(5999))
    x = np.random.default_rng(10).normal(size=(6000, 1))
    c = calibrate_noise(np.column_stack((x, x * 3)))
    assert c.sigmas.shape == (4, 2)
    np.testing.assert_allclose(c.sigmas[:, 1], c.sigmas[:, 0] * 3, atol=1e-13)
    assert not c.sigmas.flags.writeable


def test_sure_matches_brute_force_risk_and_never_amplifies_coefficients():
    x = np.random.default_rng(11).normal(size=(256, 3))
    sigma = np.array([0.1, 1.0, 0.0])
    thresholds = sure_threshold(x, sigma)
    for c in range(2):
        d = x[:, c] / sigma[c]
        bound = np.sqrt(2 * np.log(len(d)))
        candidates = sorted(set([0, bound] + [float(v) for v in abs(d) if v <= bound]))
        risk = [len(d) + np.minimum(d**2, t**2).sum() - 2 * (abs(d) <= t).sum() for t in candidates]
        expected = candidates[int(np.argmin(risk))] * sigma[c]
        assert thresholds[c] == pytest.approx(expected, abs=1e-12)
    shrunk = np.sign(x) * np.maximum(abs(x) - thresholds, 0)
    assert (abs(shrunk) <= abs(x)).all()
    assert not np.any(shrunk * x < 0)
    assert thresholds[2] == 0


def test_frame_roundtrip_zero_and_tiny_calibration_scales():
    x = np.random.default_rng(12).normal(size=(256, 2))
    np.testing.assert_allclose(
        denoise_frame(x, WaveletCalibration(np.zeros((4, 2)))), x, atol=1e-12
    )
    np.testing.assert_array_equal(
        denoise_frame(np.zeros((256, 2)), WaveletCalibration(np.ones((4, 2)))), 0
    )
    assert np.isfinite(denoise_frame(x, WaveletCalibration(np.full((4, 2), 1e-300)))).all()


def test_centered_stream_alignment_no_future_fabrication_and_chunk_invariance():
    rng = np.random.default_rng(13)
    x = rng.normal(size=(6003, 2))
    t = np.arange(len(x)) / 2000
    calibration = calibrate_noise(rng.normal(size=(6000, 2)))
    q = approved_test_report(calibration)
    a = RealtimeEMGDenoiser(
        DenoiserConfig(channels=2, wavelet=True), calibration=calibration, qualification=q
    )
    b = RealtimeEMGDenoiser(
        DenoiserConfig(channels=2, wavelet=True), calibration=calibration, qualification=q
    )
    aa = collect(a, x, t)
    bb = collect(b, x, t, (7, 39, 2, 20))
    ys = np.concatenate([r.samples for r in aa])
    np.testing.assert_allclose(ys, np.concatenate([r.samples for r in bb]), atol=1e-12)
    stamps = np.concatenate([r.timestamps for r in aa])
    np.testing.assert_array_equal(stamps, t[: len(stamps)])
    assert a.pending_samples == len(x) - len(stamps)
    assert 118 <= a.pending_samples < 138
    assert max(r.buffering_delay_ms for r in aa) <= 69.500001
    np.testing.assert_array_equal(
        np.concatenate([r.valid for r in aa]), np.arange(len(stamps)) >= 2000
    )
    # One central frame against a global SWT, with fixed threshold: no padding influence
    # should reach the center if the 118-sample margins are respected.
    f = CausalFilterBank(2).process(x[:6000])
    local = f[1000:1256]
    gl = pywt.swt(f, "db4", level=4, axis=0, trim_approx=True, norm=True)
    lc = pywt.swt(local, "db4", level=4, axis=0, trim_approx=True, norm=True)
    for coeff in gl[1:]:
        coeff[:] = np.sign(coeff) * np.maximum(abs(coeff) - 0.1, 0)
    for coeff in lc[1:]:
        coeff[:] = np.sign(coeff) * np.maximum(abs(coeff) - 0.1, 0)
    whole = pywt.iswt(gl, "db4", axis=0, norm=True)
    segment = pywt.iswt(lc, "db4", axis=0, norm=True)
    np.testing.assert_allclose(segment[118:138], whole[1118:1138], atol=1e-12)


def test_wavelet_waits_for_real_context_and_reset_clears_old_samples():
    calibration = WaveletCalibration(np.zeros((4, 1)))
    engine = RealtimeEMGDenoiser(
        DenoiserConfig(wavelet=True),
        calibration=calibration,
        qualification=approved_test_report(calibration),
    )
    for i in range(6):
        assert engine.process(np.ones(20), np.arange(i * 20, i * 20 + 20) / 2000) == []
    first = engine.process(np.ones(20), np.arange(120, 140) / 2000)[0]
    assert first.timestamps[0] == 0 and first.discontinuity
    assert engine.process(np.zeros(20), 5 + np.arange(20) / 2000) == []
    for i in range(1, 6):
        assert engine.process(np.zeros(20), 5 + np.arange(i * 20, i * 20 + 20) / 2000) == []
    following = engine.process(np.zeros(20), 5 + np.arange(120, 140) / 2000)[0]
    np.testing.assert_array_equal(following.samples, 0)
    assert following.timestamps[0] == 5 and not following.valid.any()


def test_gate_rejects_synthetic_missing_onsets_failed_metrics_and_wrong_calibration():
    c = WaveletCalibration(np.ones((4, 1)))
    values = approved_test_report(c).to_dict()
    for changes in (
        {"representative_data": False},
        {"additional_onset_shift_ms": None},
        {"rms_change_percent": 6},
        {"snr_improvement_db": 2.9},
    ):
        report = WaveletQualification.from_dict(dict(values, **changes))
        engine = RealtimeEMGDenoiser(
            DenoiserConfig(wavelet=True), calibration=c, qualification=report
        )
        assert not engine.wavelet_active
    changed = WaveletCalibration(np.ones((4, 1)) * 2)
    engine = RealtimeEMGDenoiser(
        DenoiserConfig(wavelet=True), calibration=changed, qualification=approved_test_report(c)
    )
    assert engine.wavelet_status == "qualification_calibration_mismatch"


def test_qualification_reports_unavailable_onset_and_zero_signal_without_approval():
    c = WaveletCalibration(np.zeros((4, 1)))
    report = evaluate_wavelet(np.zeros(6000), np.zeros(6000), c, representative_data=True)
    assert not report.approved and not report.metrics_passed
    assert report.to_dict()["rms_change_percent"] is None
    assert report.to_dict()["additional_onset_shift_ms"] is None
