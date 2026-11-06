"""File denoise service: viewer-style offline filtfilt by default (CSV/parquet)."""

from __future__ import annotations

import csv
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from time import perf_counter
from typing import Any

import numpy as np
from numpy.typing import NDArray

from .._typing import FloatArray, IntArray
from ..dsp.core import DenoiserConfig, RealtimeDenoiser
from ..dsp.filters import Modality, infer_modality, modality_preset
from ..dsp.offline import offline_denoise
from ..io import atomic_text_writer, dump_json, parse_row, read_header, validate_output_paths
from .retime import TIMESTAMP_COLUMN, Acquisition, load_acquisition


@dataclass(frozen=True, slots=True)
class ReplayOptions:
    """Paths and session options for offline file denoise; input units are unchanged."""

    source: Path
    output: Path | None = None
    modality: Modality | None = None
    #: Write ``<output>.report.json`` beside the recording (off by default).
    sidecar: bool = False
    #: Merge diagnostics into the enclosing LeRobot ``meta/info.json`` when present.
    update_meta: bool = True
    #: Use streaming causal filters instead of viewer-style filtfilt (debug / realtime parity).
    causal: bool = False

    @property
    def destination(self) -> Path:
        return Path(self.output) if self.output is not None else default_process_output(self.source)

    @property
    def report_path(self) -> Path:
        return Path(f"{self.destination}.report.json")

    @property
    def resolved_modality(self) -> Modality:
        return self.modality if self.modality is not None else infer_modality(self.source)

    def validate_paths(self) -> None:
        outputs = [self.destination]
        if self.sidecar:
            outputs.append(self.report_path)
        validate_output_paths([self.source], outputs)


@dataclass(slots=True)
class ReplayReport:
    modality: str = "emg"
    sample_rate_hz: int = 2000
    channels: int = 0
    input_samples: int = 0
    output_samples: int = 0
    valid_samples: int = 0
    segments: int = 0
    max_hop_processing_ms: float = 0.0
    max_estimated_digital_latency_ms: float = 0.0
    observed_end_to_end_ms: float | None = None
    unemitted_samples: int = 0
    method: str = "offline_filtfilt"
    note: str = (
        "Offline file denoise matches viz/viewer.html "
        "(mirror pad + filtfilt + period-extended comb/notch)."
    )
    stages: list[str] = field(default_factory=list)


def default_process_output(source: str | Path) -> Path:
    """Sibling ``<stem>.denoised.<ext>`` next to the input recording."""
    path = Path(source)
    suffix = path.suffix if path.suffix else ".parquet"
    return path.with_name(f"{path.stem}.denoised{suffix}")


def _is_parquet_path(path: Path) -> bool:
    name = path.name.lower()
    if name.endswith(".bak"):
        name = name[: -len(".bak")]
    return name.endswith(".parquet") or name.endswith(".pq")


def _segment_bounds(timestamps: FloatArray, period: float) -> list[tuple[int, int]]:
    """Half-open contiguous spans where consecutive gaps equal ``period``."""
    if timestamps.size == 0:
        return []
    bounds: list[tuple[int, int]] = []
    start = 0
    for index in range(1, timestamps.size):
        if abs(timestamps[index] - timestamps[index - 1] - period) > 1e-6:
            bounds.append((start, index))
            start = index
    bounds.append((start, timestamps.size))
    return bounds


def run_replay(options: ReplayOptions) -> dict[str, Any]:
    """Denoise a recording; a failed write leaves any existing output intact."""
    options.validate_paths()
    if _is_parquet_path(Path(options.source)):
        return _run_replay_parquet(options)
    return _run_replay_csv(options)


def _denoise_arrays(
    timestamps: FloatArray,
    samples: FloatArray,
    *,
    modality: Modality,
    causal: bool,
) -> tuple[FloatArray, FloatArray, NDArray[np.bool_], IntArray, ReplayReport]:
    preset = modality_preset(modality)
    period = 1 / preset.sample_rate
    report = ReplayReport(
        modality=modality,
        sample_rate_hz=preset.sample_rate,
        channels=int(samples.shape[1]),
        method="causal_stream" if causal else "offline_filtfilt",
    )
    if causal:
        report.note = (
            "Causal streaming denoise (RealtimeDenoiser); edges may ring until settled."
        )
        engine = RealtimeDenoiser(
            DenoiserConfig(channels=samples.shape[1], modality=modality)
        )
        out_stamps: list[FloatArray] = []
        out_samples: list[FloatArray] = []
        out_valid: list[NDArray[np.bool_]] = []
        out_segments: list[IntArray] = []
        for start, stop in _segment_bounds(timestamps, period):
            chunk_t = timestamps[start:stop]
            chunk_x = samples[start:stop]
            report.input_samples += stop - start
            for offset in range(0, stop - start, preset.block_size):
                hop_t = chunk_t[offset : offset + preset.block_size]
                hop_x = chunk_x[offset : offset + preset.block_size]
                for block in engine.process(hop_x, hop_t):
                    out_stamps.append(block.timestamps)
                    out_samples.append(block.samples)
                    out_valid.append(block.valid)
                    out_segments.append(
                        np.full(len(block.samples), block.segment_id, dtype=np.int64)
                    )
                    _accumulate_block(report, block)
        if not out_stamps:
            raise ValueError("recording contains no samples")
        return (
            np.concatenate(out_stamps),
            np.vstack(out_samples),
            np.concatenate(out_valid),
            np.concatenate(out_segments),
            report,
        )

    out_stamps = []
    out_samples = []
    out_valid = []
    out_segments = []
    bounds = _segment_bounds(timestamps, period)
    for segment_id, (start, stop) in enumerate(bounds):
        chunk_t = timestamps[start:stop]
        chunk_x = samples[start:stop]
        report.input_samples += stop - start
        started = perf_counter()
        filtered, diagnostics = offline_denoise(
            chunk_x, modality=modality, sample_rate=float(preset.sample_rate)
        )
        cost_ms = (perf_counter() - started) * 1000.0
        report.max_hop_processing_ms = max(report.max_hop_processing_ms, cost_ms)
        report.stages = list(diagnostics.get("stages") or [])
        report.note = str(diagnostics.get("note") or report.note)
        count = stop - start
        out_stamps.append(chunk_t)
        out_samples.append(filtered)
        out_valid.append(np.ones(count, dtype=np.bool_))
        out_segments.append(np.full(count, segment_id, dtype=np.int64))
        report.output_samples += count
        report.valid_samples += count
        report.segments = segment_id + 1
    if not out_stamps:
        raise ValueError("recording contains no samples")
    return (
        np.concatenate(out_stamps),
        np.vstack(out_samples),
        np.concatenate(out_valid),
        np.concatenate(out_segments),
        report,
    )


def _run_replay_csv(options: ReplayOptions) -> dict[str, Any]:
    modality = options.resolved_modality
    with Path(options.source).open(newline="", encoding="utf-8-sig") as source:
        reader = csv.reader(source)
        header = read_header(reader)
        rows = [parse_row(row, len(header), reader.line_num) for row in reader if row]
    if not rows:
        raise ValueError("CSV contains no samples")
    data = np.asarray(rows, dtype=np.float64)
    stamps, samples, valid, segments, report = _denoise_arrays(
        data[:, 0],
        data[:, 1:],
        modality=modality,
        causal=options.causal,
    )
    with atomic_text_writer(options.destination) as handle:
        writer = csv.writer(handle)
        writer.writerow(header + ["valid", "segment_id"])
        for index in range(stamps.size):
            writer.writerow(
                [
                    format(stamps[index], ".17g"),
                    *samples[index],
                    int(valid[index]),
                    int(segments[index]),
                ]
            )
    return _finish_report(options, report)


def _run_replay_parquet(options: ReplayOptions) -> dict[str, Any]:
    modality = options.resolved_modality
    acquisition = load_acquisition(options.source)
    timestamps = np.asarray(acquisition.timestamps, dtype=np.float64)
    samples = np.asarray(acquisition.samples, dtype=np.float64)
    if timestamps.size == 0:
        raise ValueError("parquet contains no samples")
    stamps, filtered, valid, segments, report = _denoise_arrays(
        timestamps,
        samples,
        modality=modality,
        causal=options.causal,
    )
    keep_meta = stamps.size == timestamps.size
    extras = (
        {name: np.asarray(values) for name, values in acquisition.extras.items()}
        if keep_meta
        else {}
    )
    columns = (
        acquisition.column_names
        if keep_meta
        else (TIMESTAMP_COLUMN, *acquisition.channel_names)
    )
    rebuilt = Acquisition(
        timestamps=stamps,
        samples=filtered,
        channel_names=acquisition.channel_names,
        column_names=columns,
        extras=extras,
        channel_dtypes=acquisition.channel_dtypes,
    )
    _write_denoised_parquet(
        options.destination,
        rebuilt,
        valid=valid,
        segment_id=segments,
    )
    return _finish_report(options, report)


def _accumulate_block(report: ReplayReport, block: Any) -> None:
    report.output_samples += len(block.samples)
    report.valid_samples += int(block.valid.sum())
    report.segments = block.segment_id + 1
    report.max_hop_processing_ms = max(report.max_hop_processing_ms, block.processing_ms)
    report.max_estimated_digital_latency_ms = max(
        report.max_estimated_digital_latency_ms, block.estimated_digital_latency_ms
    )


def _finish_report(options: ReplayOptions, report: ReplayReport) -> dict[str, Any]:
    report.unemitted_samples = report.input_samples - report.output_samples
    payload = asdict(report)
    payload["output"] = str(options.destination)
    payload["report"] = str(options.report_path) if options.sidecar else None
    payload["causal"] = options.causal
    if options.sidecar:
        dump_json(options.report_path, payload)
    if options.update_meta:
        from .meta import register_recording_meta

        info = register_recording_meta(options.source, denoise_payload=payload)
        if info is not None:
            payload["info"] = str(info)
    return payload


def _write_denoised_parquet(
    path: Path,
    acquisition: Acquisition,
    *,
    valid: NDArray[np.bool_],
    segment_id: IntArray,
) -> None:
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError as error:  # pragma: no cover - depends on the environment
        raise ValueError(
            "writing parquet requires pyarrow; install emg-eeg-denoise[parquet]"
        ) from error

    arrays: dict[str, Any] = {}
    for name in acquisition.column_names:
        if name == TIMESTAMP_COLUMN:
            arrays[name] = pa.array(
                np.asarray(acquisition.timestamps, dtype=np.float64), type=pa.float64()
            )
            continue
        if name in acquisition.channel_names:
            index = acquisition.channel_names.index(name)
            values = np.asarray(acquisition.samples[:, index])
            dtype = acquisition.channel_dtypes.get(name)
            arrays[name] = pa.array(values, type=dtype) if dtype is not None else pa.array(values)
            continue
        arrays[name] = pa.array(acquisition.extras[name])
    arrays["valid"] = pa.array(np.asarray(valid, dtype=np.bool_))
    arrays["segment_id"] = pa.array(np.asarray(segment_id, dtype=np.int64))

    table = pa.table(arrays)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
        write_table: Any = pq.write_table
        write_table(table, temporary)
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
