"""Command-line boundary: parse options, invoke services, emit strict JSON."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from .dsp.filters import Modality, infer_modality, modality_preset
from .io import jsonable
from .pipeline.batch import BatchOptions, run_batch
from .pipeline.replay import ReplayOptions, run_replay
from .pipeline.retime import RetimeOptions, run_retime


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI without performing I/O or configuring application state."""
    parser = argparse.ArgumentParser(description="EMG/EEG 时基重建与因果去噪")
    sub = parser.add_subparsers(dest="command", required=True)
    retime = sub.add_parser(
        "retime", help="阶段一：重建采集时间戳为均匀时基（CSV，或 parquet 就地写回）"
    )
    retime.add_argument("input", type=Path, help="采集parquet或CSV；需含timestamp列")
    retime.add_argument("--output", type=Path, help="输出路径；--inplace 时省略")
    retime.add_argument(
        "--inplace",
        action="store_true",
        help="将源parquet改名为*.bak，重建结果写回原文件名",
    )
    retime.add_argument("--force", action="store_true", help="就地重建时允许已有*.bak存在")
    retime.add_argument(
        "--modality",
        choices=("emg", "eeg", "auto"),
        default="auto",
        help="决定默认采样率；auto 从文件名推断（含 eeg → 250 Hz，否则 2000 Hz）",
    )
    retime.add_argument("--rate", type=float, help="真实采样率Hz；覆盖 modality 默认值")
    retime.add_argument("--tick-us", type=float, help="时钟分辨率us；默认由数据估计")
    retime.add_argument("--allow-negative", action="store_true", help="不平移首点至零")
    batch = sub.add_parser("batch", help="批量两阶段处理；输出按输入相对路径镜像，避免同名覆盖")
    batch.add_argument("inputs", nargs="+", help="文件路径或glob模式，如 '数据集/**/*_emg.parquet'")
    batch.add_argument("--output-dir", type=Path, help="镜像输出目录；--inplace 时可省略")
    batch.add_argument(
        "--inplace",
        action="store_true",
        help="就地重建：源parquet→*.bak，重建结果写回原文件名",
    )
    batch.add_argument(
        "--modality",
        choices=("emg", "eeg", "auto"),
        default="auto",
        help="去噪链路与默认采样率；auto 时按每个文件名分别推断",
    )
    batch.add_argument("--rate", type=float, help="真实采样率Hz；覆盖 modality 默认值")
    batch.add_argument("--tick-us", type=float, help="时钟分辨率us；默认逐文件估计")
    batch.add_argument("--allow-negative", action="store_true", help="不平移首点至零")
    batch.add_argument("--denoise", action="store_true", help="重建后继续执行阶段二去噪")
    batch.add_argument("--force", action="store_true", help="覆盖已存在的输出或已有*.bak")
    replay = sub.add_parser("process", help="流式回放CSV；首列timestamp，后续列为电压通道")
    replay.add_argument("input", type=Path)
    replay.add_argument("--output", type=Path, required=True)
    replay.add_argument(
        "--modality",
        choices=("emg", "eeg", "auto"),
        default="auto",
        help="emg=20/450/梳状50；eeg=0.5/50/陷波50；auto 从文件名推断",
    )
    return parser


def _resolve_modality(flag: str, path: Path) -> Modality:
    return infer_modality(path) if flag == "auto" else flag  # type: ignore[return-value]


def _default_rate(modality: Modality) -> float:
    return float(modality_preset(modality).sample_rate)


def _execute(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    if args.command == "retime":
        if not args.inplace and args.output is None:
            raise ValueError("--output is required unless --inplace is set")
        modality = _resolve_modality(args.modality, args.input)
        rate = _default_rate(modality) if args.rate is None else args.rate
        payload = run_retime(
            RetimeOptions(
                source=args.input,
                output=args.output,
                rate_hz=rate,
                tick_us=args.tick_us,
                non_negative=not args.allow_negative,
                inplace=args.inplace,
                force=args.force,
            )
        )
        payload["modality"] = modality
        # Warnings mean the rebuilt axis is suspect; fail software acceptance.
        return payload, 1 if payload["warnings"] else 0
    if args.command == "batch":
        if not args.inplace and args.output_dir is None:
            raise ValueError("--output-dir is required unless --inplace is set")
        batch_modality: Modality | None = None if args.modality == "auto" else args.modality
        payload = run_batch(
            BatchOptions(
                patterns=tuple(args.inputs),
                output_dir=args.output_dir,
                rate_hz=args.rate,
                tick_us=args.tick_us,
                non_negative=not args.allow_negative,
                denoise=args.denoise,
                modality=batch_modality,
                force=args.force,
                inplace=args.inplace,
            )
        )
        # Any failed or flagged recording fails acceptance for the whole batch.
        return payload, 1 if payload["failed"] or payload["flagged"] else 0
    process_modality = _resolve_modality(args.modality, args.input)
    return run_replay(
        ReplayOptions(source=args.input, output=args.output, modality=process_modality)
    ), 0


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
