# emg-eeg-denoise

EMG / EEG 时基重建与因果去噪工具，以及浏览器对照页。

**四项能力**

1. EMG 时间戳重建（tick 量化 → 均匀时基）
2. EMG 去噪：高通 20 Hz → 低通 450 Hz → 梳状 50 Hz（−3 dB 带宽 2 Hz，Q=25）
3. EEG 去噪：高通 0.5 Hz → 低通 50 Hz → 陷波 50 Hz（Q=25，带宽 2 Hz）
4. 可视化：EMG & EEG Viewer

> 本仓库不含采集驱动。文件 `process` 与浏览页同为离线 filtfilt；`RealtimeDenoiser` 为因果流式。本机回放不能证明端到端延迟。

[架构说明](docs/architecture.md) · [开发规范](CONTRIBUTING.md) · [变更记录](CHANGELOG.md)

---

## 安装

需要 Python ≥ 3.10。

```bash
cd emg-eeg-denoise
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'   # 含 parquet / 测试 / lint
```

CLI 入口：`emg-eeg-denoise`（短别名 `eed`）。若提示找不到命令，用：

```bash
.venv/bin/eed --help
```

---

## 仓库结构

```text
emg-eeg-denoise/
├── src/emg_eeg_denoise/
│   ├── cli.py              # 命令行（可读摘要；--json 输出完整 JSON）
│   ├── io.py               # CSV/JSON 校验与原子写入
│   ├── dsp/                # 滤波与去噪
│   │   ├── filters.py      # EMG 梳状 / EEG 陷波（因果积木）
│   │   ├── offline.py      # Viewer 同款离线 filtfilt
│   │   └── core.py         # RealtimeDenoiser（流式因果）
│   └── pipeline/           # 离线两阶段流水线
│       ├── timebase.py     # tick → 均匀时基算法
│       ├── retime.py       # 阶段一
│       ├── replay.py       # 阶段二（默认 offline）
│       ├── batch.py        # 批量编排
│       └── meta.py         # 写入 / 回填 meta/info.json
├── tests/
├── viz/                    # EMG & EEG Viewer
│   ├── viewer.html
│   ├── parquet_loader.js
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

**通用 CLI 选项**

| 选项 | 作用 |
|---|---|
| （默认） | 打印可读摘要 |
| `--json` | 标准输出打印完整 JSON |
| `--sidecar` | 额外写 `.timebase.json` / `.report.json` |
| `--no-update-meta` | 不修改会话 `meta/info.json` |

诊断默认写入 LeRobot 会话的 `meta/info.json`（原 feature 增加 `timebase`，并注册 `<feature>.denoised`）。批量总览写 `batch.json`。

---

## 阶段一：时间戳重建

```bash
eed retime observation.left_wrist_emg.parquet --output retimed.parquet
eed retime observation.eeg.parquet --inplace --modality eeg
```

**只改时间轴，样本值不变。** `--inplace`：源文件改名为 `*.bak`，均匀时基写回原名。

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
# parquet：默认在同目录写出 <stem>.denoised.parquet
# 默认走与 Viewer 相同的离线链路（镜像 padding + filtfilt + 梳状/陷波周期延拓），减轻首尾振铃
eed process observation.left_wrist_emg.parquet
eed process observation.eeg.parquet --modality eeg

# 需要流式因果结果时：
eed process observation.left_wrist_emg.parquet --causal

# CSV；可显式指定输出
eed process retimed.csv --output denoised.csv
```

输入须为均匀时基。输出列：`timestamp,<通道>,valid,segment_id`（parquet 另保留元数据列）。  
文件去噪默认 **离线零相位**；Python `RealtimeDenoiser` API 仍为因果流式。

---

## 批量处理

推荐对采集会话就地重建 + 去噪：

```bash
eed batch \
  '/path/to/bcigo/L3-*/data/chunk-000/episode_000000/observation.*_wrist_emg.parquet' \
  --inplace --denoise --modality emg
```

每个文件产物：

| 文件 | 含义 |
|---|---|
| `observation.*_wrist_emg.parquet` | 均匀时基（已覆盖） |
| `observation.*_wrist_emg.parquet.bak` | 原始备份 |
| `observation.*_wrist_emg.denoised.parquet` | 去噪结果 |
| `meta/info.json` | 写入 `timebase` 与 `*.denoised` feature |

镜像输出（不改原数据）：

```bash
eed batch '数据集/**/observation.*_wrist_emg.parquet' --output-dir out/ --denoise
```

已有 `.bak` 时需加 `--force` 才能再次 `--inplace`。

---

## meta/info.json

就地处理后会更新会话 `meta/info.json`，例如：

- `features.observation.left_wrist_emg.timebase`：重建诊断（测得速率、tick、resync、备份名等）
- `features.observation.left_wrist_emg.denoised`：去噪 feature（`source`、`pipeline`、valid 样本数等）

若目录里还留着旧的 sidecar，可回填并删除：

```bash
eed meta-backfill /path/to/L3-002-2026-10-19-17-49-37
# 保留 sidecar：加 --keep-sidecars
```

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
cd emg-eeg-denoise
python3 -m http.server 8765 --directory viz
# 浏览器打开 http://127.0.0.1:8765/viewer.html
```

在页面中「打开文件夹」，选择 episode 目录，例如：

```text
.../L3-002-2026-10-19-17-49-37/data/chunk-000/episode_000000
```

| 入口 | 行为 |
|---|---|
| 打开文件夹 | 自动读取左右腕 EMG；同目录有 `observation.eeg.parquet` 一并载入；也可选 `*.denoised.parquet` |
| 打开 Parquet | 单文件或多选；按文件名区分 EMG / EEG |
| JSON/CSV | 示例 JSON 或带 `timestamp` 列的 CSV |

EEG 默认：**高通 0.5 Hz、低通 50 Hz、陷波 50 Hz**。浏览滤波与默认 `process` 同为镜像 padding + filtfilt；与 `--causal` / `RealtimeDenoiser` 不同。

---

## 开发

```bash
source .venv/bin/activate
python -m pip install -e '.[dev]'
make check
```

| 路径 | 职责 |
|---|---|
| `dsp/` | 因果积木、离线 filtfilt、流式 `RealtimeDenoiser` |
| `pipeline/` | 时基重建、文件去噪、批量、meta 更新 |
| `io` / `cli` | 校验、原子写入、命令行 |
| `viz/` | 离线波形浏览器 |

贡献流程见 [CONTRIBUTING.md](CONTRIBUTING.md)。
