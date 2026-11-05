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
- 浏览器可视化：EMG & EEG Viewer，可导入腕部 EMG 文件夹与 EEG/EMG parquet

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

## 处理采集数据：两阶段

采集数据必须先重建时间戳，再去噪。两步不能跳过第一步，原因见下。

```bash
# 阶段一：重建时间戳（parquet 或 CSV -> 均匀时基 CSV）
emg-denoise retime observation.left_wrist_emg.parquet --output retimed.csv

# 阶段二：去噪
emg-denoise process retimed.csv --output denoised.csv
```

### 为什么必须先重建

采集时钟的分辨率（1 tick ≈ 451.088 µs）比真实采样周期（500 µs）粗，且不是其约数。
每个样本累积 `500 − 451.088 = 48.912 µs` 误差，攒满一个 tick 后时间戳跳 2 tick 校正。
于是 2 kHz 数据只报出四种间隔 —— 451.088、450.850（1 tick 的两个 float32 相邻值）与
902.176、901.937（2 tick）—— 平均值恰好 500 µs。

这样的时间戳下游无法使用：

- `load_recording` 要求 2 kHz 连续时间戳，四种间隔全部被拒；
- `process` 把任何偏离周期的间隔视为段断裂，一段 42 秒录制会被切成约 9300 段，
  流式滤波器状态在每段重置，去噪质量无从保证。

`retime` 丢弃逐点时间戳数值，仅用其确定时间原点，再按样本序号重建：
`t[i] = offset + i × period`。两个以上 tick 的跳变标记硬件校正点，用于划分锚定区间，
因此量化伪影不会被误判为真实丢样。**样本值不做任何修改，只重建时间轴。**

### 阶段一报告

`retime` 在输出旁写 `<output>.timebase.json`，并把同样内容打到 stdout：

| 字段 | 含义 |
| --- | --- |
| `measured_rate_hz` | 原始时间戳隐含的采样率，用于核对 `--rate` |
| `tick_us` | 估计的时钟分辨率 |
| `resync_segments` | 硬件校正点划分出的区间数 |
| `expected_segment_samples` | 理论区间长度 `tick / (period − tick)`，与实测对照 |
| `max_offset_us` | 重建后相对原时间戳的最大偏移 |
| `warnings` | 非空则时基不可信，退出码为 1 |

若 `--rate` 与数据实际不符，`warnings` 会给出提示且退出码为 1。其他采样率同样适用，
例如 250 Hz 的 EEG 用 `--rate 250`。时钟分辨率默认由数据估计，可用 `--tick-us` 指定。

读取 parquet 需要额外依赖：

```bash
python -m pip install -e '.[parquet]'
```

### 批量处理

`batch` 对匹配到的每个文件依次执行两阶段，一个文件失败不影响其余：

```bash
# 仅重建时间戳
emg-denoise batch '数据集/L3-*/data/chunk-000/episode_*/observation.*_wrist_emg.parquet' \
  --output-dir out/

# 重建并去噪
emg-denoise batch '数据集/**/observation.*_wrist_emg.parquet' --output-dir out/ --denoise
```

输出按输入相对于公共父目录的路径镜像，每个录制一个子目录：

```text
out/
├── batch.json                                   汇总报告
└── L3-002-.../data/chunk-000/episode_000000/
    ├── observation.left_wrist_emg/
    │   ├── retimed.csv            阶段一输出
    │   ├── retimed.csv.timebase.json
    │   ├── denoised.csv           阶段二输出（--denoise）
    │   └── denoised.csv.report.json
    └── observation.right_wrist_emg/ ...
```

**镜像目录结构不是为了好看。** 采集布局在不同 session 下重复同一文件名
（20 个文件只有 2 个不同名字），平铺到一个目录会让它们互相静默覆盖，只剩最后一个。

其他行为：

- 已存在的输出默认拒绝覆盖，需要重跑时加 `--force`；
- `.timebase.json`、`.report.json` 等派生文件不会被当作输入，重跑不会吃掉自己的产物；
- `batch.json` 汇总 `matched` / `succeeded` / `failed` / `flagged` 与逐文件诊断；
- 任一文件失败或被标记（时基告警，或阶段二把录制切成多段）时退出码为 1。

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

EMG-EEG-DENOISE 的多通道浏览器：**EMG & EEG Viewer**，页面为 [`viz/emg_viewer.html`](viz/emg_viewer.html)。实时去噪库仍是 2 kHz sEMG 主链；这个页面用来对照浏览腕部 EMG 和 EEG。

### 需要打开 / 依赖的文件

| 文件 | 作用 |
|---|---|
| [`viz/emg_viewer.html`](viz/emg_viewer.html) | 页面本身（浏览器打开它） |
| [`viz/data/sample_emg.json`](viz/data/sample_emg.json) | 「加载示例」自动 fetch 的默认数据 |
| [`viz/export_sample.py`](viz/export_sample.py) | 从 L3 会话 parquet 生成上述 JSON |
| [`viz/parquet_loader.js`](viz/parquet_loader.js) | 浏览器内解析 parquet |
| [`viz/vendor/hyparquet.js`](viz/vendor/hyparquet.js) | 离线 parquet 解码（hyparquet，MIT） |

不要用 `file://` 直接双击 HTML 再指望自动加载 JSON（浏览器会拦截本地 fetch）。请用本地 HTTP 服务打开。

### 打开方式

```bash
# 1) 从 L3 会话导出示例（默认降采样至 1000 Hz）
python3 viz/export_sample.py \
  --src /path/to/L3-xxx-session \
  --out viz/data/sample_emg.json

# 2) 在 viz 目录起本地服务
python3 -m http.server 8765 --directory viz

# 3) 浏览器打开页面
# http://127.0.0.1:8765/emg_viewer.html
```

页面内可以直接导入，不必先导出 JSON：

| 入口 | 行为 |
|---|---|
| 打开文件夹 | 在所选目录（含子目录）中自动读取 `observation.left_wrist_emg.parquet` 与 `observation.right_wrist_emg.parquet`。同一目录若有 `observation.eeg.parquet` 会一并载入 |
| 打开 Parquet | 单个或一次多选。`observation.eeg.parquet` 按 EEG 打开；腕部文件按左右 EMG 打开 |
| JSON/CSV | `export_sample.py` 的 base64 JSON，或带 `timestamp` 列的 CSV |

有多个目录时，优先加载同时含左右腕 EMG 的那个，并在状态栏说明。EMG 与 EEG 采样率不同，视图分开；切到 EEG 时滤波预设为高通 1 Hz、低通 40 Hz、50 Hz 陷波。

### 界面能力

| 能力 | 说明 |
|---|---|
| 视图 | 左/右腕/双侧 EMG，以及 EEG（导入后出现） |
| 通道 | 单通道勾选；EEG 使用 parquet 里的电极名 |
| 浏览 | 窗长/起点/增益；滚轮缩放、拖拽平移、双击复位 |
| 频谱 | 当前窗口的 Welch 谱（约 1 s 分段）；可通道平均、限制频率上限；陷波/梳状中心画成虚线 |
| 去直流 | 可开关 |
| 数据 | 「加载示例」、文件夹、Parquet，或 JSON/CSV |

### 滤波（可独立开关）

左侧方式均可单独开启或关闭：

| 开关 | 作用 |
|---|---|
| 原始 | 旁路，显示未滤波信号（与其它滤波互斥） |
| 高通 | Butterworth 高通（截止、阶数可调） |
| 低通 | Butterworth 低通（截止、阶数可调） |
| 带通 | 再串联一段高通+低通带通 |
| 陷波 | 可同时开多路 IIR 陷波（各自中心、带宽；按频率从低到高串联，最多 8 个） |
| 梳状 | IIR 梳状（基频及谐波，带宽可调） |

启用项按固定顺序串联：**高通 → 低通 → 带通 → 陷波 → 梳状**。

**浏览策略（与实时主链不同）：**

1. **镜像 padding（默认开）**：整段扩展为 `CBABCBA`（`3N-2`），处理后裁回 `N`。高通/低通/带通走这条路径。
2. **filtfilt 零相位**：每个启用的 IIR 级做正向+反向滤波，首尾暂态对称压低。
3. **边缘**：
   - 未开陷波、也未开梳状时，filtfilt 端点尖峰用内侧约 50 ms 偶镜像回填。
   - 开陷波或梳状时，低截止高通（如 0.5 Hz）叠加低通 40 Hz 后，50 Hz 工频仍在。偶镜像会把正弦在端点折返，窄带陷波和梳状都会在这个接缝上振铃约 1 s，首尾可比中间大几十到几百倍。二者都改为在裁剪后的信号上做：两端不满足该频率周期的若干周波换成内侧干净周期，再向前向后接一段同样的周期（陷波按带宽留出衰减时间，约 1 s 起；梳状约 2 s），正反滤波，让滤波器进入数据前已处于稳态。若端点仍有正在衰减的振铃（约 0.8 s 内），再用余弦交叠消掉。记录后段通带里的真实动作不会被这段处理去掉。原理与数据流见 [架构说明](docs/architecture.md) 的「离线浏览」。

关闭镜像 padding 时，仍会标出约 1 s 的因果启动参考灰区（便于对比）。参数可用滑条或数字框修改；「默认」为高通+低通（20 / 450 Hz）+ 镜像 padding。低通截止最高可调到 1000 Hz；若超过当前采样率的 Nyquist，实际设计会钳到略低于 Nyquist 并在提示中标明。

> 可视化仅供离线对照。生产/实时请用 `emg_denoise` Python 主链（固定 2 kHz、因果 `sosfilt`、无整段镜像/filtfilt）。

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
| `viz/` | 离线波形浏览器、parquet 加载与 L3 导出 |

贡献流程见 [CONTRIBUTING.md](CONTRIBUTING.md)。`artifacts/` 与虚拟环境为本地生成内容，请勿提交。
