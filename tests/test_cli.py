import json
from pathlib import Path

import numpy as np

from emg_denoise.cli import main


def write_csv(path, t, x):
    np.savetxt(
        path,
        np.column_stack((t, x)),
        delimiter=",",
        header="timestamp,emg1",
        comments="",
        fmt="%.17g",
    )


def test_csv_stream_preserves_epoch_time_partial_tail_gap_and_reports(tmp_path):
    t = 1_700_000_000 + np.arange(5003) / 2000
    t[2501:] += 1
    x = np.sin(np.arange(len(t)))
    source, output, features = (
        tmp_path / "input.csv",
        tmp_path / "out.csv",
        tmp_path / "features.json",
    )
    write_csv(source, t, x)
    assert main(["process", str(source), "--output", str(output), "--features", str(features)]) == 0
    data = np.loadtxt(output, delimiter=",", skiprows=1)
    np.testing.assert_array_equal(data[:, 0], t)
    assert data.shape == (5003, 5)
    assert data[2501, 4] == 1
    report = json.loads(Path(str(output) + ".report.json").read_text())
    assert report["input_samples"] == report["output_samples"] == 5003
    assert report["observed_end_to_end_ms"] is None
    assert json.loads(features.read_text())["frames"]


def test_csv_does_not_overwrite_input_and_calibration_roundtrip(tmp_path):
    t = np.arange(6000) / 2000
    source, calibration = tmp_path / "rest.csv", tmp_path / "cal.json"
    write_csv(source, t, np.random.default_rng(20).normal(size=6000))
    original = source.read_bytes()
    assert main(["process", str(source), "--output", str(source)]) == 2
    assert source.read_bytes() == original
    assert main(["calibrate", str(source), "--output", str(calibration)]) == 0
    assert np.asarray(json.loads(calibration.read_text())["sigmas"]).shape == (4, 1)
    out = tmp_path / "out.csv"
    assert (
        main(
            [
                "process",
                str(source),
                "--output",
                str(out),
                "--wavelet",
                "--calibration",
                str(calibration),
            ]
        )
        == 0
    )
    report = json.loads(Path(str(out) + ".report.json").read_text())
    assert not report["wavelet_active"] and report["wavelet_status"] == "missing_qualification"
