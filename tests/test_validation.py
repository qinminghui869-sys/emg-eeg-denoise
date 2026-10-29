import json
import runpy
import sys

import numpy as np
import pytest

from emg_denoise import DenoiserConfig, RealtimeEMGDenoiser, StreamingFeatures, rms
from emg_denoise.cli import main
from emg_denoise.filters import CausalFilterBank, IIRComb, sample_matrix
from emg_denoise.validation import synthetic_data
from emg_denoise.wavelet import WaveletCalibration, denoise_frame


def _benchmark_observation(channels):
    # CI verifies report assembly against explicit observations. Hardware timing
    # is exercised separately by make validate, not asserted on a shared runner.
    return {
        name: {
            "channels": channels,
            "measured_hops": 380,
            "p99_processing_ms": 1,
            "max_processing_ms": 2,
            "deadline_misses_10ms": 0,
            "max_estimated_digital_latency_ms": delay,
        }
        for name, delay in [("fixed_main_chain", 12), ("swt_candidate", 72)]
    }


def test_validate_command_generates_numerical_report_and_never_approves_synthetic_data(
    tmp_path, monkeypatch
):
    monkeypatch.setattr("emg_denoise.validation.benchmark", _benchmark_observation)
    assert main(["validate", "--output", str(tmp_path), "--channels", "1"]) == 0
    report = json.loads((tmp_path / "validation.json").read_text())
    assert report["frequency"]["minimum_nominal_attenuation_db"] >= 30
    assert report["fixed_chain_software_acceptance_passed"]
    assert not report["wavelet_qualification"]["approved"]
    assert not report["hardware_end_to_end_100ms_verified"]
    assert (tmp_path / "validation.md").exists()
    assert main(["validate", "--channels", "0"]) == 2


def test_qualify_command_checks_alignment_and_writes_strict_json(tmp_path):
    t, clean, noisy, rest, onsets = synthetic_data(seconds=4)
    for name, samples, timestamps in [
        ("clean", clean, t),
        ("noisy", noisy, t),
        ("rest", rest, np.arange(len(rest)) / 2000),
    ]:
        np.savetxt(
            tmp_path / f"{name}.csv",
            np.column_stack((timestamps, samples)),
            delimiter=",",
            header="timestamp,emg1",
            comments="",
            fmt="%.17g",
        )
    calibration, result = tmp_path / "cal.json", tmp_path / "result.json"
    assert main(["calibrate", str(tmp_path / "rest.csv"), "--output", str(calibration)]) == 0
    arguments = [
        "qualify",
        str(tmp_path / "clean.csv"),
        str(tmp_path / "noisy.csv"),
        "--calibration",
        str(calibration),
        "--onsets",
        str(onsets[0]),
        "--output",
        str(result),
    ]
    assert main(arguments) == 0
    report = json.loads(result.read_text())
    assert report["dataset_sha256"] and not report["approved"]
    assert report["additional_onset_shift_ms"] is not None
    noisy_path = tmp_path / "noisy.csv"
    noisy_path.write_text(noisy_path.read_text().replace("emg1", "other_channel", 1))
    before = result.read_bytes()
    assert main(arguments) == 2
    assert result.read_bytes() == before


@pytest.mark.parametrize(
    "constructor", [DenoiserConfig, CausalFilterBank, IIRComb, StreamingFeatures]
)
def test_invalid_channel_counts_are_consistently_rejected(constructor):
    with pytest.raises(ValueError, match="positive integer"):
        constructor(channels=0)


def test_session_mode_is_read_only_and_empty_inputs_have_defined_behavior():
    engine = RealtimeEMGDenoiser()
    with pytest.raises(AttributeError):
        engine.wavelet_active = True
    with pytest.raises(ValueError, match="2000"):
        DenoiserConfig(sample_rate=1000)
    with pytest.raises(ValueError, match="True or False"):
        DenoiserConfig(wavelet="yes")
    assert engine.process(np.empty((0, 1)), []) == []
    with pytest.raises(ValueError, match="one timestamp"):
        engine.process(np.zeros(20), [0])
    with pytest.raises(ValueError, match="at least one"):
        rms(np.empty((0, 1)))
    with pytest.raises(ValueError, match="shape"):
        sample_matrix(np.zeros((2, 2, 2)))
    with pytest.raises(ValueError, match="channels"):
        sample_matrix(np.zeros((20, 2)), channels=1)
    with pytest.raises(ValueError, match="256"):
        denoise_frame(np.zeros(20), WaveletCalibration(np.zeros((4, 1))))


def test_python_module_entrypoint_uses_the_same_cli(tmp_path, monkeypatch):
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "emg-denoise",
            "process",
            str(tmp_path / "missing.csv"),
            "--output",
            str(tmp_path / "out.csv"),
        ],
    )
    with pytest.raises(SystemExit) as result:
        runpy.run_module("emg_denoise", run_name="__main__")
    assert result.value.code == 2
