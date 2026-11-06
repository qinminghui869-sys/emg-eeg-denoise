"""Merge retime/denoise diagnostics into a LeRobot-style ``meta/info.json``."""

from __future__ import annotations

import json
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

from ..io import dump_json

TOOL_NAME = "emg-eeg-denoise"


def tool_version() -> str:
    try:
        return version("emg-eeg-denoise")
    except PackageNotFoundError:  # pragma: no cover - editable / source tree
        return "0.2.0"


def find_session_root(path: str | Path) -> Path | None:
    """Walk parents for ``meta/info.json`` (LeRobot session root)."""
    current = Path(path).resolve()
    if current.is_file():
        current = current.parent
    for candidate in [current, *current.parents]:
        info = candidate / "meta" / "info.json"
        if info.is_file():
            return candidate
    return None


def feature_key_from_path(path: str | Path) -> str:
    """Map ``observation.left_wrist_emg.parquet`` → feature key."""
    name = Path(path).name
    lowered = name.lower()
    for suffix in (".parquet", ".pq", ".csv"):
        if lowered.endswith(suffix):
            return name[: -len(suffix)]
    return Path(path).stem


def timebase_patch(payload: dict[str, Any]) -> dict[str, Any]:
    backup = payload.get("backup")
    return {
        "timebase": {
            "rebuilt": True,
            "tool": TOOL_NAME,
            "tool_version": tool_version(),
            "backup": Path(backup).name if backup else None,
            "measured_rate_hz": payload.get("measured_rate_hz"),
            "tick_us": payload.get("tick_us"),
            "resync_segments": payload.get("resync_segments"),
            "max_offset_us": payload.get("max_offset_us"),
            "samples": payload.get("samples"),
            "warnings": list(payload.get("warnings") or []),
        }
    }


def denoised_feature(
    base_key: str,
    payload: dict[str, Any],
    *,
    template: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a new feature entry for ``<base>.denoised``."""
    source = dict(template or {})
    source.pop("timebase", None)
    source.pop("pipeline", None)
    channels = source.get("shape")
    if not isinstance(channels, list) or not channels:
        channels = [int(payload.get("channels") or 1)]
    entry = {
        "dtype": source.get("dtype", "float32"),
        "shape": list(channels),
        "names": source.get("names"),
        "fps": float(payload.get("sample_rate_hz") or source.get("fps") or 0.0),
        "tolerance_s": source.get("tolerance_s", 0.005),
        "window": source.get("window", [-0.033, 0.0]),
        "source": base_key,
        "pipeline": {
            "tool": TOOL_NAME,
            "tool_version": tool_version(),
            "modality": payload.get("modality"),
            "method": payload.get("method", "offline_filtfilt"),
            "stages": list(payload.get("stages") or ["denoise"]),
            "samples": payload.get("input_samples"),
            "valid_samples": payload.get("valid_samples"),
            "segments": payload.get("segments"),
            "warmup_s": 1.0 if payload.get("method") == "causal_stream" else 0.0,
            "output": Path(payload["output"]).name if payload.get("output") else None,
        },
    }
    return entry


def update_info_features(info_path: Path, patches: dict[str, dict[str, Any]]) -> Path:
    """Merge feature patches into ``info.json`` and rewrite atomically."""
    info = json.loads(info_path.read_text(encoding="utf-8"))
    if not isinstance(info, dict):
        raise ValueError(f"info.json must contain an object: {info_path}")
    features = info.setdefault("features", {})
    if not isinstance(features, dict):
        raise ValueError(f"info.json features must be an object: {info_path}")

    for key, patch in patches.items():
        existing = features.get(key)
        if existing is None:
            features[key] = dict(patch)
            continue
        if not isinstance(existing, dict):
            raise ValueError(f"feature {key!r} must be an object in {info_path}")
        merged = dict(existing)
        for field, value in patch.items():
            if field in {"timebase", "pipeline"} and isinstance(value, dict):
                prior = merged.get(field)
                merged[field] = {**prior, **value} if isinstance(prior, dict) else dict(value)
            else:
                merged[field] = value
        features[key] = merged

    dump_json(info_path, info)
    return info_path


def register_recording_meta(
    source: str | Path,
    *,
    retime_payload: dict[str, Any] | None = None,
    denoise_payload: dict[str, Any] | None = None,
) -> Path | None:
    """Update the enclosing session ``meta/info.json`` when one exists."""
    source_path = Path(source)
    root = find_session_root(source_path)
    if root is None:
        return None
    info_path = root / "meta" / "info.json"
    base_key = feature_key_from_path(source_path)
    # Retimed in place keeps the same feature key; denoised adds ``.denoised``.
    if base_key.endswith(".denoised"):
        base_key = base_key[: -len(".denoised")]
    patches: dict[str, dict[str, Any]] = {}
    if retime_payload is not None:
        patches[base_key] = timebase_patch(retime_payload)
    if denoise_payload is not None:
        info = json.loads(info_path.read_text(encoding="utf-8"))
        template = (info.get("features") or {}).get(base_key)
        if not isinstance(template, dict):
            template = None
        patches[f"{base_key}.denoised"] = denoised_feature(
            base_key, denoise_payload, template=template
        )
    if not patches:
        return None
    return update_info_features(info_path, patches)


def backfill_session_meta(session_root: str | Path, *, remove_sidecars: bool = True) -> dict[str, Any]:
    """Rebuild ``info.json`` patches from existing sidecar reports, then delete them."""
    root = Path(session_root)
    info_path = root / "meta" / "info.json"
    if not info_path.is_file():
        raise ValueError(f"missing meta/info.json under {root}")
    sidecars = sorted(root.rglob("*.timebase.json")) + sorted(root.rglob("*.report.json"))
    # Exclude batch.json-style names mistakenly matched — only feature sidecars.
    sidecars = [path for path in sidecars if path.name != "batch.json"]
    updated: list[str] = []
    removed: list[str] = []
    for path in sidecars:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if path.name.endswith(".timebase.json"):
            source = Path(payload.get("output") or str(path).removesuffix(".timebase.json"))
            register_recording_meta(source, retime_payload=payload)
            updated.append(str(source))
        elif path.name.endswith(".report.json"):
            source = Path(payload.get("output") or str(path).removesuffix(".report.json"))
            # Denoised report: register against the pre-denoise feature.
            base = feature_key_from_path(source)
            if base.endswith(".denoised"):
                raw = source.with_name(base[: -len(".denoised")] + source.suffix)
            else:
                raw = source
            register_recording_meta(raw, denoise_payload=payload)
            updated.append(str(raw))
        if remove_sidecars:
            path.unlink(missing_ok=True)
            removed.append(str(path))
    return {
        "session": str(root),
        "info": str(info_path),
        "updated": sorted(set(updated)),
        "removed_sidecars": removed,
    }
