import json
import runpy
import sys
from pathlib import Path

import numpy as np
import pytest

from emg_eeg_denoise.cli import main


def write_csv(path, t, x, header="timestamp,emg1"):
    np.savetxt(
        path,
        np.column_stack((t, x)),
        delimiter=",",
        header=header,
        comments="",
        fmt="%.17g",
    )


def test_csv_stream_preserves_epoch_time_partial_tail_gap_and_reports(tmp_path):
    t = 1_700_000_000 + np.arange(5003) / 2000
    t[2501:] += 1
    x = np.sin(np.arange(len(t)))
    source, output = tmp_path / "input.csv", tmp_path / "out.csv"
    write_csv(source, t, x)
    assert main(["process", str(source), "--output", str(output)]) == 0
    data = np.loadtxt(output, delimiter=",", skiprows=1)
    np.testing.assert_array_equal(data[:, 0], t)
    assert data.shape == (5003, 4)
    assert data[2501, 3] == 1
    report = json.loads(Path(str(output) + ".report.json").read_text())
    assert report["input_samples"] == report["output_samples"] == 5003
    assert report["modality"] == "emg"
    assert report["observed_end_to_end_ms"] is None


def test_eeg_process_uses_eeg_chain(tmp_path):
    t = np.arange(800) / 250
    x = np.sin(2 * np.pi * 50 * t)
    source, output = tmp_path / "observation.eeg.csv", tmp_path / "out.csv"
    write_csv(source, t, x, header="timestamp,Fp1")
    assert main(["process", str(source), "--output", str(output), "--modality", "auto"]) == 0
    report = json.loads(Path(str(output) + ".report.json").read_text())
    assert report["modality"] == "eeg"
    assert report["sample_rate_hz"] == 250
    assert report["input_samples"] == report["output_samples"] == 800


def test_csv_does_not_overwrite_input(tmp_path):
    t = np.arange(6000) / 2000
    source = tmp_path / "rest.csv"
    write_csv(source, t, np.random.default_rng(20).normal(size=6000))
    original = source.read_bytes()
    assert main(["process", str(source), "--output", str(source)]) == 2
    assert source.read_bytes() == original


def test_python_module_entrypoint_uses_the_same_cli(tmp_path, monkeypatch):
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "emg-eeg-denoise",
            "process",
            str(tmp_path / "missing.csv"),
            "--output",
            str(tmp_path / "out.csv"),
        ],
    )
    with pytest.raises(SystemExit) as result:
        runpy.run_module("emg_eeg_denoise", run_name="__main__")
    assert result.value.code == 2
