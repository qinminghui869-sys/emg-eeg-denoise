"""Application service for running the two stages over many recordings.

Acquisition layouts repeat the same file name across sessions, for example
``L3-002-.../episode_000000/observation.left_wrist_emg.parquet``. Flattening
those into one output directory would silently overwrite all but the last file,
so every output keeps its path relative to the inputs' common parent.

``--inplace`` instead renames each matched parquet to ``*.bak`` and writes the
rebuilt parquet under the original name.

Each recording is processed independently: one failure is reported and the batch
continues, so a single malformed file cannot discard the rest of the run.
"""

from __future__ import annotations

import glob
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..dsp.filters import Modality, infer_modality, modality_preset
from ..io import dump_json
from .replay import ReplayOptions, run_replay
from .retime import RetimeOptions, run_retime

#: Suffixes excluded from inputs so reruns never consume their own outputs.
DERIVED_SUFFIXES = (".timebase.json", ".report.json", ".bak")


@dataclass(frozen=True, slots=True)
class BatchOptions:
    """Patterns and parameters for one batch run."""

    patterns: tuple[str, ...]
    output_dir: Path | None = None
    rate_hz: float | None = None
    tick_us: float | None = None
    non_negative: bool = True
    #: Also run stage two on each retimed recording.
    denoise: bool = False
    #: Fixed modality for every file; None infers per path (``eeg`` in name → EEG).
    modality: Modality | None = None
    #: Overwrite outputs that already exist instead of refusing.
    force: bool = False
    #: Rebuild matched parquet files in place (``source`` → ``source.bak``).
    inplace: bool = False

    @property
    def report_path(self) -> Path:
        if self.output_dir is not None:
            return self.output_dir / "batch.json"
        return Path("batch.json")


def _is_derived(path: str) -> bool:
    return any(path.endswith(suffix) for suffix in DERIVED_SUFFIXES)


def resolve_inputs(patterns: tuple[str, ...]) -> list[Path]:
    """Expand glob patterns into a deduplicated, sorted input list.

    Derived outputs are excluded so a rerun never consumes its own results.
    """
    found: list[str] = []
    for pattern in patterns:
        # glob returns a plain path unchanged, so a literal file needs no special case.
        found.extend(sorted(glob.glob(pattern, recursive=True)))
    unique = sorted(dict.fromkeys(found))
    inputs = [Path(path) for path in unique if not _is_derived(path) and Path(path).is_file()]
    if not inputs:
        raise ValueError("no input files matched")
    return inputs


def mirror_root(inputs: list[Path]) -> Path:
    """Common parent whose relative paths keep same-named inputs distinct."""
    directories = [path.resolve().parent for path in inputs]
    first = directories[0]
    if all(directory == first for directory in directories):
        return first
    common = Path(*first.parts)
    for directory in directories[1:]:
        parts: list[str] = []
        # Paths differ in depth; truncating to the shorter one is intended.
        for left, right in zip(common.parts, directory.parts, strict=False):
            if left != right:
                break
            parts.append(left)
        common = Path(*parts) if parts else Path(directory.anchor)
    return common


def destination(source: Path, root: Path, output_dir: Path) -> Path:
    """Output directory for one recording, mirroring its path under ``root``."""
    try:
        relative = source.resolve().parent.relative_to(root)
    except ValueError:  # pragma: no cover - root is derived from the inputs
        relative = Path()
    return output_dir / relative / source.stem


def _modality_for(source: Path, options: BatchOptions) -> Modality:
    return options.modality if options.modality is not None else infer_modality(source)


def _rate_for(source: Path, options: BatchOptions) -> float:
    if options.rate_hz is not None:
        return float(options.rate_hz)
    return float(modality_preset(_modality_for(source, options)).sample_rate)


def _process_one_mirrored(source: Path, target: Path, options: BatchOptions) -> dict[str, Any]:
    """Run stage one, and stage two when requested, into a mirrored output directory."""
    assert options.output_dir is not None
    modality = _modality_for(source, options)
    retimed = target / "retimed.csv"
    if retimed.exists() and not options.force:
        raise ValueError(f"output exists; pass --force to overwrite: {retimed}")
    target.mkdir(parents=True, exist_ok=True)

    stage_one = run_retime(
        RetimeOptions(
            source=source,
            output=retimed,
            rate_hz=_rate_for(source, options),
            tick_us=options.tick_us,
            non_negative=options.non_negative,
            force=options.force,
        )
    )
    entry: dict[str, Any] = {
        "input": str(source),
        "modality": modality,
        "retimed": str(retimed),
        "samples": stage_one["samples"],
        "channels": stage_one["channels"],
        "measured_rate_hz": stage_one["measured_rate_hz"],
        "resync_segments": stage_one["resync_segments"],
        "max_offset_us": stage_one["max_offset_us"],
        "warnings": stage_one["warnings"],
    }
    if not options.denoise:
        return entry

    denoised = target / "denoised.csv"
    if denoised.exists() and not options.force:
        raise ValueError(f"output exists; pass --force to overwrite: {denoised}")
    stage_two = run_replay(
        ReplayOptions(source=retimed, output=denoised, modality=modality)
    )
    entry["denoised"] = str(denoised)
    entry["segments"] = stage_two["segments"]
    entry["valid_samples"] = stage_two["valid_samples"]
    entry["unemitted_samples"] = stage_two["unemitted_samples"]
    # A rebuilt axis must look continuous to the streaming engine; more than one
    # segment means the filter state was reset mid-recording.
    if stage_two["segments"] != 1:
        entry["warnings"] = [
            *entry["warnings"],
            f"stage two split the recording into {stage_two['segments']} segments",
        ]
    return entry


def _process_one_inplace(source: Path, options: BatchOptions) -> dict[str, Any]:
    """Rebuild one parquet recording under its own path, keeping a ``.bak`` copy."""
    if options.denoise:
        raise ValueError("--inplace cannot be combined with --denoise; stage two still needs CSV")
    modality = _modality_for(source, options)
    stage_one = run_retime(
        RetimeOptions(
            source=source,
            inplace=True,
            rate_hz=_rate_for(source, options),
            tick_us=options.tick_us,
            non_negative=options.non_negative,
            force=options.force,
        )
    )
    return {
        "input": str(source),
        "modality": modality,
        "retimed": str(source),
        "backup": stage_one["backup"],
        "samples": stage_one["samples"],
        "channels": stage_one["channels"],
        "measured_rate_hz": stage_one["measured_rate_hz"],
        "resync_segments": stage_one["resync_segments"],
        "max_offset_us": stage_one["max_offset_us"],
        "warnings": stage_one["warnings"],
    }


def run_batch(options: BatchOptions) -> dict[str, Any]:
    """Process every matched recording, continuing past individual failures."""
    if options.inplace and options.denoise:
        raise ValueError("--inplace cannot be combined with --denoise; stage two still needs CSV")
    if not options.inplace and options.output_dir is None:
        raise ValueError("--output-dir is required unless --inplace is set")

    inputs = resolve_inputs(options.patterns)
    root = mirror_root(inputs)
    processed: list[dict[str, Any]] = []
    failed: list[dict[str, str]] = []

    for source in inputs:
        try:
            if options.inplace:
                processed.append(_process_one_inplace(source, options))
            else:
                assert options.output_dir is not None
                target = destination(source, root, options.output_dir)
                processed.append(_process_one_mirrored(source, target, options))
        except (ValueError, OSError, KeyError, TypeError) as error:
            failed.append({"input": str(source), "error": str(error)})

    flagged = [entry["input"] for entry in processed if entry["warnings"]]
    report_path = options.report_path
    if options.inplace and options.output_dir is None:
        report_path = root / "batch.json"
    payload: dict[str, Any] = {
        "report": str(report_path),
        "root": str(root),
        "matched": len(inputs),
        "succeeded": len(processed),
        "failed": len(failed),
        "flagged": len(flagged),
        "total_samples": sum(int(entry["samples"]) for entry in processed),
        "denoised": options.denoise,
        "modality": options.modality or "auto",
        "inplace": options.inplace,
        "recordings": processed,
        "failures": failed,
        "note": (
            "In-place mode renames each parquet to *.bak and writes the rebuild as "
            "the original name."
            if options.inplace
            else (
                "Outputs mirror each input's path relative to root, because acquisition "
                "layouts reuse file names across sessions."
            )
        ),
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    dump_json(report_path, payload)
    return payload
