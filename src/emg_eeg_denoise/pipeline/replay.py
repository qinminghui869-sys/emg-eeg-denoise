"""Application service for bounded-block CSV replay, independent of argparse."""

from __future__ import annotations

import csv
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np

from ..dsp.core import DenoiserConfig, RealtimeDenoiser
from ..dsp.filters import Modality, infer_modality, modality_preset
from ..io import atomic_text_writer, dump_json, parse_row, read_header, validate_output_paths


@dataclass(frozen=True, slots=True)
class ReplayOptions:
    """Paths and session options for offline replay; input units are unchanged."""

    source: Path
    output: Path
    modality: Modality | None = None

    @property
    def report_path(self) -> Path:
        return Path(f"{self.output}.report.json")

    @property
    def resolved_modality(self) -> Modality:
        return self.modality if self.modality is not None else infer_modality(self.source)

    def validate_paths(self) -> None:
        validate_output_paths([self.source], [self.output, self.report_path])


@dataclass(slots=True)
class ReplayReport:
    modality: str = "emg"
    sample_rate_hz: int = 2000
    input_samples: int = 0
    output_samples: int = 0
    valid_samples: int = 0
    segments: int = 0
    max_hop_processing_ms: float = 0.0
    max_estimated_digital_latency_ms: float = 0.0
    observed_end_to_end_ms: float | None = None
    unemitted_samples: int = 0
    note: str = "File replay cannot measure hardware/transport end-to-end latency."


def run_replay(options: ReplayOptions) -> dict[str, Any]:
    """Replay a recording; on malformed CSV, an existing output is left intact."""
    options.validate_paths()
    modality = options.resolved_modality
    preset = modality_preset(modality)
    report = ReplayReport(modality=modality, sample_rate_hz=preset.sample_rate)
    with options.source.open(newline="", encoding="utf-8-sig") as source:
        reader = csv.reader(source)
        header = read_header(reader)
        engine = RealtimeDenoiser(
            DenoiserConfig(channels=len(header) - 1, modality=modality)
        )
        with atomic_text_writer(options.output) as destination:
            writer = csv.writer(destination)
            writer.writerow(header + ["valid", "segment_id"])

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

            pending: list[list[float]] = []
            previous_stamp: float | None = None
            period = 1 / preset.sample_rate
            for row in reader:
                if not row:
                    continue
                values = parse_row(row, len(header), reader.line_num)
                stamp = values[0]
                if (
                    previous_stamp is not None
                    and abs(stamp - previous_stamp - period) > 1e-6
                    and pending
                ):
                    submit(pending)
                    pending = []
                pending.append(values)
                previous_stamp = stamp
                if len(pending) == preset.block_size:
                    submit(pending)
                    pending = []
            if pending:
                submit(pending)
            if not report.input_samples:
                raise ValueError("CSV contains no samples")
    report.unemitted_samples = report.input_samples - report.output_samples
    payload = asdict(report)
    dump_json(options.report_path, payload)
    return payload
