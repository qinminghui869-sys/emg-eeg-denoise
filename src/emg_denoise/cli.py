"""Command-line boundary: parse options, invoke services, emit strict JSON."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import numpy as np

from .filters import FS
from .io import dump_json, jsonable, load_calibration, load_recording, validate_output_paths
from .qualification import evaluate_wavelet
from .replay import ReplayOptions, run_replay
from .validation import run_validation
from .wavelet import calibrate_noise


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI without performing I/O or configuring application state."""
    parser = argparse.ArgumentParser(description="2 kHz流式肌电去噪、校准与验证")
    sub = parser.add_subparsers(dest="command", required=True)
    validate = sub.add_parser("validate", help="生成合成回放、工频/频谱验证及本机基准报告")
    validate.add_argument("--output", type=Path, default=Path("artifacts"))
    validate.add_argument("--channels", type=int, nargs="+", default=[1, 8])
    replay = sub.add_parser("process", help="流式回放CSV；首列timestamp，后续列为肌电电压")
    replay.add_argument("input", type=Path)
    replay.add_argument("--output", type=Path, required=True)
    replay.add_argument("--wavelet", action="store_true")
    replay.add_argument("--calibration", type=Path)
    replay.add_argument("--qualification", type=Path)
    replay.add_argument("--features", type=Path, help="可选特征JSON输出路径")
    calibrate = sub.add_parser("calibrate", help="以至少3秒静息CSV估计小波噪声")
    calibrate.add_argument("input", type=Path)
    calibrate.add_argument("--output", type=Path, required=True)
    qualify = sub.add_parser("qualify", help="对齐的干净/带噪回放评估小波启用门槛")
    qualify.add_argument("clean", type=Path)
    qualify.add_argument("noisy", type=Path)
    qualify.add_argument("--calibration", type=Path, required=True)
    qualify.add_argument(
        "--onsets", type=int, nargs="+", required=True, help="原始样本索引，零起始"
    )
    qualify.add_argument(
        "--representative-data",
        action="store_true",
        help="确认数据代表目标设备/受试者/任务；合成演示不能使用此标记",
    )
    qualify.add_argument("--note", default="")
    qualify.add_argument("--output", type=Path, required=True)
    return parser


def _execute(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    if args.command == "validate":
        if any(channel < 1 for channel in args.channels):
            raise ValueError("benchmark channel counts must be positive")
        report = run_validation(args.output, args.channels)
        payload = {
            "report": str(args.output / "validation.md"),
            "minimum_nominal_attenuation_db": report["frequency"]["minimum_nominal_attenuation_db"],
            "wavelet_approved": report["wavelet_qualification"]["approved"],
        }
        return payload, 0 if report["fixed_chain_software_acceptance_passed"] else 1
    if args.command == "process":
        return run_replay(
            ReplayOptions(
                source=args.input,
                output=args.output,
                wavelet=args.wavelet,
                calibration=args.calibration,
                qualification=args.qualification,
                features=args.features,
            )
        ), 0
    if args.command == "calibrate":
        validate_output_paths([args.input], [args.output])
        recording = load_recording(args.input)
        calibration = calibrate_noise(recording.samples)
        dump_json(args.output, {"sample_rate_hz": FS, "sigmas": calibration.sigmas.tolist()})
        return {"calibration": str(args.output), "channels": calibration.channels}, 0
    validate_output_paths([args.clean, args.noisy, args.calibration], [args.output])
    clean = load_recording(args.clean)
    noisy = load_recording(args.noisy)
    if clean.channel_names != noisy.channel_names or not np.array_equal(
        clean.timestamps, noisy.timestamps
    ):
        raise ValueError("clean/noisy timestamps and channel names must match exactly")
    qualification = evaluate_wavelet(
        clean.samples,
        noisy.samples,
        load_calibration(args.calibration),
        onset_indices=args.onsets,
        representative_data=args.representative_data,
        note=args.note,
    )
    payload = qualification.to_dict()
    dump_json(args.output, payload)
    return payload, 0


def main(argv: Sequence[str] | None = None) -> int:
    """Exit 0 on success, 1 on failed software acceptance, 2 on invalid input/I/O."""
    args = build_parser().parse_args(argv)
    try:
        payload, exit_code = _execute(args)
        print(json.dumps(jsonable(payload), ensure_ascii=False, allow_nan=False))
        return exit_code
    except (ValueError, OSError, KeyError, TypeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
