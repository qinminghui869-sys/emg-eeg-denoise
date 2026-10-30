# emg-denoise

2 kHz 多通道表面肌电（sEMG）实时去噪参考实现。

**处理链路：** 二阶 20 Hz 高通 → 四阶 450 Hz 低通 → 50 Hz IIR 梳状陷波 → 可选 db4/SWT 增强。滤波因果运行，状态跨块保留，输出保留原始电压单位与采样时间戳。

> 本仓库不含采集驱动。本机回放/基准不能证明端到端延迟低于 100 ms。小波默认关闭；校准或验收未通过时自动回退主链。

[架构说明](docs/architecture.md) · [验证报告](docs/validation.md) · [开发规范](CONTRIBUTING.md) · [变更记录](CHANGELOG.md)

---

## 特性

- 固定因果主链：20–450 Hz 带通 + 50 Hz 梳状陷波（SciPy `iircomb` 等价）
- 流式接口：建议每块 20 点（10 ms），状态可跨调用复用
- 可选小波增强：需静息校准 + 配对回放验收后启用
- CLI：`validate` / `calibrate` / `qualify` / `process`
- 尾随特征：50 ms RMS、20 ms 包络、1 s 频谱均值/中值频率

---

## 安装

需要 Python ≥ 3.10。

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[test]'
python -m pytest -q
```

不做可编辑安装时：

```bash
PYTHONPATH=src python -m emg_denoise ...
```

---

## 快速验证

```bash
emg-denoise validate --output artifacts --channels 1 8
emg-denoise process artifacts/noisy.csv --output artifacts/denoised.csv \
  --features artifacts/features.json
```

`validate` 生成合成肌电、工频/频漂测试与运行时间，并刷新 [验证报告](docs/validation.md)。合成数据仅用于工程验证。

---

## 实时接口

```python
import numpy as np
from emg_denoise import DenoiserConfig, RealtimeEMGDenoiser, StreamingFeatures

engine = RealtimeEMGDenoiser(DenoiserConfig(channels=2))
features = StreamingFeatures(channels=2)

samples = np.zeros((20, 2))          # 样本 × 通道；一维视为单通道
timestamps = np.arange(20) / 2000.0  # 必须来自原始采样时钟

for block in engine.process(samples, timestamps):
    waveform = block.samples          # 有符号电压
    ts = block.timestamps             # 原采样时间戳
    ready = block.valid               # 预热期内为 False
    feature_frames = features.update(block)
```

**约束摘要**

| 规则 | 说明 |
|---|---|
| 采样 | 固定 2000 Hz，间隔 0.5 ms；单次调用不可跨缺样区 |
| 分块 | 实时建议 20 点；更长输入内部按 ≤20 点切分 |
| 断流 | 向前时间跳变会重置状态并开启新分段；倒退/重复拒绝 |
| 预热 | 启动/重置后前 1 s 标记 `valid=False`，滤波仍持续 |
| 无效输入 | 在状态更新前拒绝，避免 NaN 污染 |
| 小波 | 等待真实后文时 `process()` 可能返回空列表 |

详情见 [架构说明](docs/architecture.md)。

---

## 滤波参数

| 项目 | 值 |
|---|---|
| 采样率 / 推荐块长 | 2000 Hz / 20 点（10 ms） |
| 高通 | Butterworth 二阶，20 Hz，SOS |
| 低通 | Butterworth 四阶，450 Hz，SOS |
| 梳状器 | 基频 50 Hz，−3 dB 带宽 2 Hz，Q=25 |
| 预热 | 前 1 s 样本标记无效 |

梳状器为 40 点转置延时线，等价于 `scipy.signal.iircomb(50, 25, fs=2000, ftype='notch', pass_zero=False)`。中心频率固定为 50 Hz，不跟踪电网频漂；陷波也会削弱附近有效肌电，分析 RMS/疲劳时请查看主链保真报告。完整数值见 [验证报告](docs/validation.md)。

---

## 小波增强（可选）

默认关闭。启用需三步：校准 → 验收 → 处理。

### 1. 静息校准

静息 CSV ≥ 3 s（前 1 s 收敛，后 2 s 估噪），通道与电压单位须与会话一致。

```bash
emg-denoise calibrate rest.csv --output calibration.json
```

### 2. 配对回放验收

准备时间戳/通道对齐的 `clean.csv`、`noisy.csv`，以及肌肉激活起点索引。`--representative-data` 表示调用者声明数据具代表性。

```bash
emg-denoise qualify clean.csv noisy.csv \
  --calibration calibration.json \
  --onsets 4000 8000 12000 \
  --representative-data \
  --note '目标设备、佩戴位置、任务及受试者范围' \
  --output qualification.json
```

**门槛（取最差通道）：** RMS 变化 ≤ 5%；中值频率变化 ≤ 5%；SNR 改善 ≥ 3 dB；激活起点额外偏移 ≤ 10 ms。不达标时固定主链仍可用；合成验收文件不会批准实际会话。

### 3. 启用处理

```bash
emg-denoise process input.csv --output denoised.csv --wavelet \
  --calibration calibration.json --qualification qualification.json
```

```python
import json
from emg_denoise import (
    DenoiserConfig,
    RealtimeEMGDenoiser,
    WaveletCalibration,
    WaveletQualification,
)

calibration = WaveletCalibration(json.load(open("calibration.json"))["sigmas"])
qualification = WaveletQualification.from_dict(json.load(open("qualification.json")))
engine = RealtimeEMGDenoiser(
    DenoiserConfig(channels=calibration.channels, wavelet=True),
    calibration=calibration,
    qualification=qualification,
)
print(engine.wavelet_status)
```

小波在 256 点帧上跑四层 db4 SWT，逐层 SURE 软阈值。输出样本年龄约 60–69.5 ms；数字延迟目标 ≤ 80 ms。切换模式请新建实例。算法细节见 [架构说明](docs/architecture.md)。

---

## 延迟与输出

`DenoisedBlock` 字段含义：

| 字段 | 含义 |
|---|---|
| `processing_ms` | 本次滤波/小波计算耗时 |
| `buffering_delay_ms` | 等待同块或小波后文的缓冲时间 |
| `estimated_digital_latency_ms` | 上两者之和（不含采集/传输） |
| `observed_end_to_end_ms` | 仅在传入同时钟域的 `received_at` 时计算 |

目标：主链数字延迟 ≤ 20 ms；小波 ≤ 80 ms（为采集传输留 20 ms）。端到端 100 ms 须在真实硬件链路上验收。

输出 CSV 列：`timestamp,<原通道名>,valid,wavelet_active,segment_id`。报告默认写 `<输出>.report.json`。退出码：`0` 成功，`1` 验收未通过，`2` 输入/I/O 错误。

---

## 可视化

多通道 EMG 波形浏览器见 [`viz/emg_viewer.html`](viz/emg_viewer.html)。示例数据来自 L3 左右腕 8 通道原始 ADC（默认降采样至 1000 Hz）。

页面内可切换滤波：原始 / 高通 20 Hz / 带通 20–450 Hz / **主链（带通 + 50 Hz 梳状）**，参数与 `emg_denoise.filters` 对齐，在浏览器中因果运行。

```bash
# 如需从原始 parquet 重新导出
python3 viz/export_sample.py \
  --src /home/baai/qmh/dataset/L3/L3_128_data/L3-001-2026-10-09-15-45-54

# 本地打开（避免 file:// 无法 fetch JSON）
python3 -m http.server 8765 --directory viz
# 浏览器访问 http://127.0.0.1:8765/emg_viewer.html
```

也支持打开导出的 JSON，或 `timestamp,dim_0,…` 格式 CSV。

---

## 开发

```bash
python -m pip install -e '.[dev]'
make check
```

`make check` 依次跑 Ruff、mypy（strict）、pytest（分支覆盖率 ≥ 85%）与打包元数据检查。CI 覆盖 Python 3.10–3.13（Linux / macOS）。

| 模块 | 职责 |
|---|---|
| `filters` | 因果 SOS 带通、稀疏 IIR 梳状器 |
| `core` | 流式接口、时间戳、有效标记、断流与延迟 |
| `wavelet` / `qualification` | 校准、SURE、验收门槛 |
| `features` | 尾随 RMS / 包络 / 频谱 |
| `io` / `replay` | 校验、原子写入、CSV 回放 |
| `cli` / `validation` | 命令行、合成验证与本机基准 |

贡献流程见 [CONTRIBUTING.md](CONTRIBUTING.md)。`artifacts/` 与虚拟环境为本地生成内容，请勿提交。
