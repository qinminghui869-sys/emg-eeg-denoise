"""Deterministic engineering validation; no claim of device or clinical validation."""

from __future__ import annotations

import platform
import sys
from collections.abc import Sequence
from pathlib import Path
from time import perf_counter
from types import SimpleNamespace
from typing import Any

import numpy as np
import pywt
import scipy
from scipy import signal

from ._typing import FloatArray
from .core import DenoiserConfig, RealtimeEMGDenoiser
from .features import StreamingFeatures, rms, spectral_features
from .filters import (
    BLOCK,
    COMB_G,
    COMB_R,
    FS,
    CausalFilterBank,
    IIRComb,
    bandpass_sos,
    comb_coefficients,
)
from .io import dump_json, jsonable
from .metrics import onset_crossing
from .qualification import evaluate_wavelet
from .wavelet import CenteredWavelet, calibrate_noise


def synthetic_data(
    seconds: int = 8, channels: int = 1, seed: int = 42
) -> tuple[FloatArray, FloatArray, FloatArray, FloatArray, list[int]]:
    """Synthetic band-limited Gaussian muscle bursts, NOT real participant data."""
    rng = np.random.default_rng(seed)
    n = seconds * FS
    t = np.arange(n) / FS
    sos = signal.butter(3, [35, 300], btype="bandpass", fs=FS, output="sos")
    carrier = signal.sosfilt(sos, rng.standard_normal((n, channels)), axis=0)
    envelope = np.full(n, 0.015)
    onsets = []
    for start in range(2, seconds - 1, 2):
        idx = int(start * FS)
        onsets.append(idx)
        active = (t >= start) & (t < start + 0.8)
        envelope[active] += 0.7 * (1 - np.exp(-(t[active] - start) / 0.015))
    clean = carrier * envelope[:, None] * 100  # illustrative microvolts
    mains = np.zeros(n)
    for k in range(1, 10):
        mains += (18 / k) * np.sin(2 * np.pi * 50 * k * t + k / 5)
    noisy = clean + mains[:, None] + 8 * rng.standard_normal((n, channels))
    rest_t = np.arange(3 * FS) / FS
    rest = (
        8 * rng.standard_normal((3 * FS, channels)) + 18 * np.sin(2 * np.pi * 50 * rest_t)[:, None]
    )
    return t, clean, noisy, rest, onsets


def frequency_validation() -> dict[str, Any]:
    t = np.arange(4 * FS) / FS
    records = []
    for k in range(1, 20):
        for fundamental in (50.0, 49.8, 50.2):
            frequency = fundamental * k
            if frequency >= FS / 2:
                continue
            x = np.sin(2 * np.pi * frequency * t)
            y = IIRComb(1).process(x)[:, 0]
            # Real time-domain RMS, after warmup, includes finite settling residue.
            gain = 20 * np.log10(max(np.linalg.norm(y[FS:]) / np.linalg.norm(x[FS:]), 1e-15))
            records.append(
                {
                    "harmonic": k,
                    "fundamental_hz": fundamental,
                    "frequency_hz": frequency,
                    "gain_db": gain,
                }
            )
    f = np.linspace(20, 450, 17201)
    b, a = comb_coefficients()
    _, h = signal.freqz(b, a, worN=f, fs=FS)
    distance = np.abs((f + 25) % 50 - 25)
    outside = distance >= 5 - 1e-9
    comb_loss = -20 * np.log10(np.maximum(np.abs(h[outside]), 1e-15))
    # Local derivative avoids phase unwrap jumps across exact notch zeros.
    fg = f[outside]
    delta = 0.001
    _, hp = signal.sosfreqz(bandpass_sos(), worN=fg + delta, fs=FS)
    _, hm = signal.sosfreqz(bandpass_sos(), worN=fg - delta, fs=FS)
    _, cp = signal.freqz(b, a, worN=fg + delta, fs=FS)
    _, cm = signal.freqz(b, a, worN=fg - delta, fs=FS)
    group_ms = -np.angle((hp * cp) / (hm * cm)) / (4 * np.pi * delta) * 1000
    nominal = [r["gain_db"] for r in records if r["fundamental_hz"] == 50]
    return {
        "time_domain_tones": records,
        "minimum_nominal_attenuation_db": -max(nominal),
        "nominal_30db_passed": bool(max(nominal) <= -30),
        "max_comb_loss_outside_5hz_db": float(comb_loss.max()),
        "outside_notch_05db_passed": bool(comb_loss.max() <= 0.5),
        "main_chain_group_delay_outside_notches_ms": {
            "min": float(group_ms.min()),
            "max": float(group_ms.max()),
            "exclusion": "Exclude frequencies within 5 Hz of each 50 Hz multiple; 20..450 Hz only.",
        },
    }


def benchmark(channels: int, hops: int = 400) -> dict[str, Any]:
    rng = np.random.default_rng(100 + channels)
    x = rng.normal(size=(hops * BLOCK, channels))
    calibration = calibrate_noise(rng.normal(size=(3 * FS, channels)))
    results = {}
    for mode in ("fixed_main_chain", "swt_candidate"):
        bank = CausalFilterBank(channels)
        wavelet = CenteredWavelet(calibration) if mode == "swt_candidate" else None
        features = StreamingFeatures(channels)
        costs: list[float] = []
        delays: list[float] = []
        for i in range(hops):
            start = i * BLOCK
            stamps = np.arange(start, start + BLOCK) / FS
            began = perf_counter()
            if wavelet is None:
                # Include the public input validation, timestamp and output work.
                if i == 0:
                    live = RealtimeEMGDenoiser(DenoiserConfig(channels=channels))
                ready_blocks = live.process(x[start : start + BLOCK], stamps)
                ages = [(stamps[-1] - r.timestamps[0]) * 1000 for r in ready_blocks]
                for block in ready_blocks:
                    features.update(block)
            else:
                # Intentionally benchmark an UNAPPROVED candidate in isolation;
                # the public live pipeline does not bypass its evidence gate.
                filtered = bank.process(x[start : start + BLOCK])
                ready = wavelet.push(filtered, stamps, stamps >= 1.0)
                ages = [(stamps[-1] - r[1][0]) * 1000 for r in ready]
                for y, ts, valid in ready:
                    features.update(
                        SimpleNamespace(
                            samples=y, timestamps=ts, valid=valid, discontinuity=bool(ts[0] == 0)
                        )
                    )
            cost = (perf_counter() - began) * 1000
            if i >= 20:  # report cold-start separately from the warmed timing sample
                costs.append(cost)
                delays.extend(age + cost for age in ages)
        results[mode] = {
            "channels": channels,
            "measured_hops": len(costs),
            "includes_streaming_features": True,
            "median_processing_ms": float(np.median(costs)),
            "p99_processing_ms": float(np.percentile(costs, 99)),
            "max_processing_ms": float(max(costs)),
            "deadline_misses_10ms": sum(c >= 10 for c in costs),
            "max_estimated_digital_latency_ms": float(max(delays)),
            "under_80ms_digital_budget": bool(max(delays) <= 80),
        }
    return results


def run_validation(
    output_dir: str | Path, channel_counts: Sequence[int] = (1, 8)
) -> dict[str, Any]:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    t, clean, noisy, rest, onsets = synthetic_data()
    filtered = CausalFilterBank(1).process(noisy)
    clean_main = CausalFilterBank(1).process(clean)
    calibration = calibrate_noise(rest)
    qualification = evaluate_wavelet(
        clean,
        noisy,
        calibration,
        onset_indices=onsets,
        representative_data=False,
        note="Deterministic synthetic test only.",
    )
    sig = slice(FS, None)
    clean_rms, main_rms = rms(clean[sig]), rms(clean_main[sig])
    raw_features, main_features = spectral_features(clean[sig]), spectral_features(clean_main[sig])
    raw_mdf = raw_features["median_frequency_hz"]
    main_mdf = main_features["median_frequency_hz"]
    main_onset_shifts = []
    for onset in onsets:
        threshold = 0.2 * rms(clean[onset : onset + 400])
        original = onset_crossing(clean, onset, threshold)
        processed = onset_crossing(clean_main, onset, threshold)
        main_onset_shifts.append((processed - original) * 1000 / FS)
    report: dict[str, Any] = {
        "scope": "Synthetic/reference-software validation only; hardware, transport and representative participant data are unverified.",
        "environment": {
            "platform": platform.platform(),
            "machine": platform.machine(),
            "python": sys.version.split()[0],
            "numpy": np.__version__,
            "scipy": scipy.__version__,
            "pywavelets": pywt.__version__,
        },
        "parameters": {
            "sample_rate_hz": FS,
            "hop_samples": BLOCK,
            "highpass": "Butterworth order 2, 20 Hz",
            "lowpass": "Butterworth order 4, 450 Hz",
            "comb_delay_samples": 40,
            "comb_bandwidth_hz": 2,
            "comb_g": COMB_G,
            "comb_r": COMB_R,
        },
        "frequency": frequency_validation(),
        "synthetic_main_chain_fidelity": {
            "rms_change_percent": 100 * np.abs(main_rms - clean_rms) / clean_rms,
            "raw_features": raw_features,
            "filtered_features": main_features,
            "median_frequency_change_percent": 100 * np.abs(main_mdf - raw_mdf) / raw_mdf,
            "extra_onset_shift_ms_by_burst": main_onset_shifts,
            "note": "Includes unavoidable bandpass and notch removal; NOT the extra SWT acceptance metric.",
        },
        "wavelet_qualification": qualification.to_dict(),
        "benchmarks": {str(c): benchmark(c) for c in channel_counts},
        "hardware_end_to_end_100ms_verified": False,
    }
    report["fixed_chain_software_acceptance_passed"] = bool(
        report["frequency"]["nominal_30db_passed"]
        and report["frequency"]["outside_notch_05db_passed"]
        and all(
            b["fixed_main_chain"]["deadline_misses_10ms"] == 0
            and b["fixed_main_chain"]["max_estimated_digital_latency_ms"] <= 20
            for b in report["benchmarks"].values()
        )
    )
    dump_json(output_dir / "validation.json", report)
    dump_json(output_dir / "synthetic_qualification.json", qualification.to_dict())
    dump_json(
        output_dir / "calibration.json",
        {"sample_rate_hz": FS, "sigmas": calibration.sigmas.tolist()},
    )
    for name, data, time_values in (
        ("clean", clean, t),
        ("noisy", noisy, t),
        ("rest", rest, np.arange(len(rest)) / FS),
    ):
        np.savetxt(
            output_dir / f"{name}.csv",
            np.column_stack((time_values, data)),
            delimiter=",",
            header="timestamp,emg1",
            comments="",
            fmt="%.12g",
        )
    np.savetxt(
        output_dir / "main_chain.csv",
        np.column_stack((t, filtered)),
        delimiter=",",
        header="timestamp,emg1",
        comments="",
        fmt="%.12g",
    )
    freq = report["frequency"]
    qual = report["wavelet_qualification"]
    lines = [
        "# 肌电去噪参考实现验证报告",
        "",
        "本报告使用确定性合成数据；没有真实受试者或采集硬件验收。",
        "",
        f"- 50 Hz及其谐波的最小稳态衰减：{freq['minimum_nominal_attenuation_db']:.2f} dB。",
        f"- 距陷波中心至少5 Hz处的最大额外衰减：{freq['max_comb_loss_outside_5hz_db']:.3f} dB。",
        f"- 小波数值验收通过：{qual['metrics_passed']}；允许真实会话启用：{qual['approved']}。",
        f"- 固定主链软件验收通过：{report['fixed_chain_software_acceptance_passed']}。",
        "- 整机端到端100毫秒：尚未验证。",
        "",
        "| 通道数 | 路径 | P99计算耗时/ms | 最大计算耗时/ms | 10ms超时次数 | 最大估计数字延迟/ms |",
        "|---:|---|---:|---:|---:|---:|",
    ]
    for c, values in report["benchmarks"].items():
        for mode, b in values.items():
            lines.append(
                f"| {c} | {mode} | {b['p99_processing_ms']:.3f} | {b['max_processing_ms']:.3f} | {b['deadline_misses_10ms']} | {b['max_estimated_digital_latency_ms']:.3f} |"
            )
    lines += [
        "",
        "上表包含流式RMS/包络/频谱特征计算，不含设备、传输、CSV和界面。",
        "小波候选计时不绕过正式会话的验收门槛。数据可用延迟、滤波群延迟与启动收敛分开报告。",
        "",
        "## 小波候选验收",
        "",
        "| 指标 | 实测 | 门槛 |",
        "|---|---:|---:|",
        f"| 干净回放RMS变化 | {qual['rms_change_percent']:.3f}% | ≤5% |",
        f"| 干净回放中值频率变化 | {qual['median_frequency_change_percent']:.3f}% | ≤5% |",
        f"| 带噪回放SNR改善 | {qual['snr_improvement_db']:.3f} dB | ≥3 dB |",
        f"| 激活起点额外偏移 | {qual['additional_onset_shift_ms']:.3f} ms | ≤10 ms |",
        "",
        "只有全部指标通过且数据代表目标场景时才批准启用；本报告为合成测试，不批准真实会话。",
        "",
        "完整频漂、幅值、频谱和小波指标见 validation.json。",
    ]
    (output_dir / "validation.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return dict(jsonable(report))
