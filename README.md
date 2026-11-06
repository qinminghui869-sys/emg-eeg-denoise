# emg-eeg-denoise

EMG / EEG 时基重建与因果去噪工具，以及浏览器对照页。

**四项能力**

1. EMG 时间戳重建（tick 量化 → 均匀时基）
2. EMG 去噪：高通 20 Hz → 低通 450 Hz → 梳状 50 Hz（−3 dB 带宽 2 Hz，Q=25）
3. EEG 去噪：高通 0.5 Hz → 低通 50 Hz → 陷波 50 Hz（Q=25，带宽 2 Hz）
4. 可视化：EMG & EEG Viewer

> 本仓库不含采集驱动。浏览页滤波（镜像 + filtfilt）与 Python 因果主链不同；本机回放不能证明端到端延迟。

[架构说明](docs/architecture.md) · [开发规范](CONTRIBUTING.md) · [变更记录](CHANGELOG.md)

---

## 安装

需要 Python ≥ 3.10。

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[test]'
python -m pytest -q
```

读取 parquet：

```bash
python -m pip install -e '.[parquet]'
```

CLI 入口：`emg-eeg-denoise`（短别名 `eed`）。

---

## 仓库结构

```text
emg-eeg-denoise/
├── src/emg_eeg_denoise/
│   ├── cli.py              # 命令行入口
│   ├── io.py               # CSV/JSON 校验与原子写入
│   ├── dsp/                # 因果滤波与流式去噪
│   │   ├── filters.py      # EMG 梳状 / EEG 陷波
│   │   └── core.py         # RealtimeDenoiser
│   └── pipeline/           # 离线两阶段流水线
│       ├── timebase.py     # tick → 均匀时基算法
│       ├── retime.py       # 阶段一服务
│       ├── replay.py       # 阶段二 CSV 回放
│       └── batch.py        # 批量编排
├── tests/                  # 单元与集成测试
├── viz/                    # EMG & EEG Viewer
│   ├── viewer.html
│   ├── parquet_loader.js
│   ├── export_sample.py
│   └── data/
└── docs/architecture.md
```

---

## 处理链路

| 模态 | 采样率 | 高通 | 低通 | 工频抑制 | 块长 / 预热 |
|---|---:|---|---|---|---|
| EMG | 2000 Hz | Butterworth 二阶 20 Hz | Butterworth 四阶 450 Hz | IIR 梳状 50 Hz，BW=2 Hz，Q=25 | 20 点 / 1 s |
| EEG | 250 Hz | Butterworth 二阶 0.5 Hz | Butterworth 四阶 50 Hz | IIR 陷波 50 Hz，Q=25 | 10 点 / 1 s |

`--modality emg|eeg|auto`（`auto` 时文件名含 `eeg` 则走 EEG）。

---

## 阶段一：时间戳重建

```bash
emg-eeg-denoise retime observation.left_wrist_emg.parquet --output retimed.csv
emg-eeg-denoise retime observation.eeg.parquet --output retimed_eeg.csv
eed retime observation.eeg.parquet --inplace --modality eeg
```

**样本值不做任何修改，只重建时间轴。** 报告写在 `<output>.timebase.json`。

---

## 阶段二：去噪

```python
import numpy as np
from emg_eeg_denoise import DenoiserConfig, RealtimeDenoiser

emg = RealtimeDenoiser(DenoiserConfig(channels=2, modality="emg"))
eeg = RealtimeDenoiser(DenoiserConfig(channels=8, modality="eeg"))

for block in emg.process(np.zeros((20, 2)), np.arange(20) / 2000.0):
    waveform, ready = block.samples, block.valid
```

```bash
emg-eeg-denoise process retimed.csv --output denoised.csv
eed process retimed_eeg.csv --output denoised_eeg.csv --modality eeg
```

输出 CSV 列：`timestamp,<通道>,valid,segment_id`。报告写 `<输出>.report.json`。

---

## 批量处理

```bash
eed batch '数据集/**/observation.*_wrist_emg.parquet' --output-dir out/
eed batch '数据集/**/observation.*.parquet' --output-dir out/ --denoise
eed batch '数据集/**/observation.eeg.parquet' --output-dir out/ --denoise --modality eeg
```

`--inplace` 与 `--denoise` 不能同时使用。输出按输入相对公共父目录镜像。

---

## 实时接口约束

| 规则 | 说明 |
|---|---|
| 采样 | EMG 2000 Hz；EEG 250 Hz；单次调用不可跨缺样区 |
| 分块 | EMG 建议 20 点；EEG 建议 10 点；更长输入内部按块切分 |
| 断流 | 向前时间跳变重置状态并开新分段；倒退/重复拒绝 |
| 预热 | 启动/重置后前 1 s 标记 `valid=False`，滤波仍持续 |
| 无效输入 | 在状态更新前拒绝，避免 NaN 污染 |

详情见 [架构说明](docs/architecture.md)。

---

## 可视化

页面：[`viz/viewer.html`](viz/viewer.html)。

```bash
python3 -m http.server 8765 --directory viz
# http://127.0.0.1:8765/viewer.html
```

| 入口 | 行为 |
|---|---|
| 打开文件夹 | 自动读取左右腕 EMG；同目录有 `observation.eeg.parquet` 一并载入 |
| 打开 Parquet | 单文件或多选；按文件名区分 EMG / EEG |
| JSON/CSV | 示例 JSON 或带 `timestamp` 列的 CSV |

EEG 默认：**高通 0.5 Hz、低通 50 Hz、陷波 50 Hz**。浏览滤波为镜像 padding + filtfilt，与 Python 因果主链不同。

---

## 开发

```bash
python -m pip install -e '.[dev]'
make check
```

| 路径 | 职责 |
|---|---|
| `dsp/` | EMG/EEG 因果带通、梳状/陷波、流式接口 |
| `pipeline/` | 时基重建、回放、批量两阶段 |
| `io` / `cli` | 校验、原子写入、命令行 |
| `viz/` | 离线波形浏览器 |

贡献流程见 [CONTRIBUTING.md](CONTRIBUTING.md)。
