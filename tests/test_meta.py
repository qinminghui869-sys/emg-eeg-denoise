import json
from pathlib import Path

from emg_eeg_denoise.cli import main
from emg_eeg_denoise.pipeline.meta import (
    backfill_session_meta,
    feature_key_from_path,
    find_session_root,
    register_recording_meta,
)
from emg_eeg_denoise.pipeline.retime import RetimeOptions, run_retime
from tests.test_timebase import write_acquisition_parquet


def _session_tree(root: Path, *, samples: int = 4000) -> Path:
    session = root / "L3-demo"
    episode = session / "data" / "chunk-000" / "episode_000000"
    episode.mkdir(parents=True)
    (session / "meta").mkdir()
    info = {
        "codebase_version": "v3.1",
        "features": {
            "observation.left_wrist_emg": {
                "dtype": "float32",
                "shape": [8],
                "names": None,
                "fps": 2000.0,
                "tolerance_s": 0.005,
                "window": [-0.033, 0.0],
            }
        },
    }
    (session / "meta" / "info.json").write_text(json.dumps(info), encoding="utf-8")
    source = episode / "observation.left_wrist_emg.parquet"
    write_acquisition_parquet(source, samples=samples, channels=8)
    return session


def test_feature_key_and_session_root(tmp_path):
    session = _session_tree(tmp_path)
    source = next(session.rglob("observation.left_wrist_emg.parquet"))
    assert feature_key_from_path(source) == "observation.left_wrist_emg"
    assert find_session_root(source) == session.resolve()


def test_inplace_retime_updates_info_without_sidecar(tmp_path):
    session = _session_tree(tmp_path)
    source = next(session.rglob("observation.left_wrist_emg.parquet"))
    payload = run_retime(RetimeOptions(source=source, inplace=True, sidecar=False))
    assert payload["report"] is None
    assert not Path(f"{source}.timebase.json").exists()
    info = json.loads((session / "meta" / "info.json").read_text())
    timebase = info["features"]["observation.left_wrist_emg"]["timebase"]
    assert timebase["rebuilt"] is True
    assert timebase["samples"] == 4000
    assert payload["info"] == str(session / "meta" / "info.json")


def test_meta_backfill_merges_sidecars_and_removes_them(tmp_path):
    session = _session_tree(tmp_path, samples=2000)
    source = next(session.rglob("observation.left_wrist_emg.parquet"))
    run_retime(RetimeOptions(source=source, inplace=True, sidecar=True, update_meta=False))
    assert Path(f"{source}.timebase.json").is_file()
    result = backfill_session_meta(session, remove_sidecars=True)
    assert not Path(f"{source}.timebase.json").exists()
    info = json.loads(Path(result["info"]).read_text())
    assert info["features"]["observation.left_wrist_emg"]["timebase"]["rebuilt"] is True


def test_cli_meta_backfill(tmp_path, capsys):
    session = _session_tree(tmp_path, samples=2000)
    source = next(session.rglob("observation.left_wrist_emg.parquet"))
    run_retime(RetimeOptions(source=source, inplace=True, sidecar=True, update_meta=False))
    assert main(["meta-backfill", str(session)]) == 0
    text = capsys.readouterr().out
    assert "meta-backfill summary" in text
    assert "removed_sidecars=1" in text


def test_register_denoised_feature(tmp_path):
    session = _session_tree(tmp_path, samples=800)
    source = next(session.rglob("observation.left_wrist_emg.parquet"))
    register_recording_meta(
        source,
        denoise_payload={
            "modality": "emg",
            "sample_rate_hz": 2000,
            "channels": 8,
            "input_samples": 800,
            "valid_samples": 0,
            "segments": 1,
            "output": str(source.with_name("observation.left_wrist_emg.denoised.parquet")),
        },
    )
    info = json.loads((session / "meta" / "info.json").read_text())
    denoised = info["features"]["observation.left_wrist_emg.denoised"]
    assert denoised["source"] == "observation.left_wrist_emg"
    assert denoised["pipeline"]["modality"] == "emg"
    assert denoised["shape"] == [8]
