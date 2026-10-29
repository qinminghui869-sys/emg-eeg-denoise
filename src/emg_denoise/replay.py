"""Application service for bounded-block CSV replay, independent of argparse."""

from __future__ import annotations

import csv
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np

from .core import DenoiserConfig, RealtimeEMGDenoiser
from .features import StreamingFeatures
from .filters import BLOCK, FS
from .io import (
    atomic_text_writer,
    dump_json,
    load_calibration,
    load_json_object,
    parse_row,
    read_header,
    validate_output_paths,
)
from .qualification import WaveletQualification


@dataclass(frozen=True, slots=True)
class ReplayOptions:
    """Paths and session options for offline replay; input units are unchanged."""

    source: Path
    output: Path
    wavelet: bool = False
    calibration: Path | None = None
    qualification: Path | None = None
    features: Path | None = None

    @property
    def report_path(self) -> Path:
        return Path(f"{self.output}.report.json")

    def validate_paths(self) -> None:
        inputs = [
            self.source,
            *[p for p in (self.calibration, self.qualification) if p is not None],
        ]
        outputs = [self.output, self.report_path]
        if self.features is not None:
            outputs.append(self.features)
        validate_output_paths(inputs, outputs)


@dataclass(slots=True)
class ReplayReport:
    input_samples: int = 0
    output_samples: int = 0
    valid_samples: int = 0
    segments: int = 0
    max_hop_processing_ms: float = 0.0
    max_estimated_digital_latency_ms: float = 0.0
    observed_end_to_end_ms: float | None = None
    wavelet_status: str = "disabled_by_config"
    wavelet_active: bool = False
    pending_future_context_samples: int = 0
    unemitted_samples: int = 0
    note: str = "File replay cannot measure hardware/transport end-to-end latency."


def run_replay(options: ReplayOptions) -> dict[str, Any]:
    """Replay a recording; on malformed CSV, an existing output is left intact."""
    options.validate_paths()
    calibration = None if options.calibration is None else load_calibration(options.calibration)
    qualification = (
        None
        if options.qualification is None
        else WaveletQualification.from_dict(load_json_object(options.qualification))
    )
    report = ReplayReport()
    feature_records: list[dict[str, Any]] = []
    with options.source.open(newline="", encoding="utf-8-sig") as source:
        reader = csv.reader(source)
        header = read_header(reader)
        engine = RealtimeEMGDenoiser(
            DenoiserConfig(channels=len(header) - 1, wavelet=options.wavelet),
            calibration=calibration,
            qualification=qualification,
        )
        features = StreamingFeatures(len(header) - 1)
        with atomic_text_writer(options.output) as destination:
            writer = csv.writer(destination)
            writer.writerow(header + ["valid", "wavelet_active", "segment_id"])

            def submit(rows: list[list[float]]) -> None:
                data = np.asarray(rows, dtype=np.float64)
                report.input_samples += len(data)
                for block in engine.process(data[:, 1:], data[:, 0]):
                    for index, row in enumerate(block.samples):
                        writer.writerow(
                            [
                                format(block.timestamps[index], ".17g"),
                                *row,
                                int(block.valid[index]),
                                int(block.wavelet_active),
                                block.segment_id,
                            ]
                        )
                    report.output_samples += len(block.samples)
                    report.valid_samples += int(block.valid.sum())
                    report.segments = block.segment_id + 1
                    report.max_hop_processing_ms = max(
                        report.max_hop_processing_ms, block.processing_ms
                    )
                    report.max_estimated_digital_latency_ms = max(
                        report.max_estimated_digital_latency_ms, block.estimated_digital_latency_ms
                    )
                    if options.features is not None:
                        feature_records.extend(asdict(frame) for frame in features.update(block))

            pending: list[list[float]] = []
            previous_stamp: float | None = None
            for row in reader:
                if not row:
                    continue
                values = parse_row(row, len(header), reader.line_num)
                stamp = values[0]
                if (
                    previous_stamp is not None
                    and abs(stamp - previous_stamp - 1 / FS) > 1e-6
                    and pending
                ):
                    submit(pending)
                    pending = []
                pending.append(values)
                previous_stamp = stamp
                if len(pending) == BLOCK:
                    submit(pending)
                    pending = []
            if pending:
                submit(pending)
            if not report.input_samples:
                raise ValueError("CSV contains no samples")
    report.wavelet_status = engine.wavelet_status
    report.wavelet_active = engine.wavelet_active
    report.pending_future_context_samples = engine.pending_samples
    report.unemitted_samples = report.input_samples - report.output_samples
    payload = asdict(report)
    dump_json(options.report_path, payload)
    if options.features is not None:
        dump_json(
            options.features,
            {
                "frames": feature_records,
                "note": "RMS uses 50 ms trailing history; spectrum uses 1 s. No new lookahead.",
            },
        )
    return payload
