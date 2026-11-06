# 架构与接口约束

## 四项能力

1. **EMG 时间戳重建**：采集 tick 量化时间戳 → 均匀时基
2. **EMG 去噪**：20 Hz 高通 → 450 Hz 低通 → 50 Hz 梳状（BW=2 Hz，Q=25），2 kHz
3. **EEG 去噪**：0.5 Hz 高通 → 50 Hz 低通 → 50 Hz 陷波（Q=25），250 Hz
4. **可视化**：`viz/viewer.html` 离线浏览（与文件 `process` 同款 filtfilt）

## 包布局

```text
emg_eeg_denoise/
  dsp/         filters / offline / RealtimeDenoiser
  pipeline/    retime / replay / batch / timebase / meta
  io.py        边界 I/O
  cli.py       命令行
```

## 数据流

```text
采集文件（parquet/CSV：tick量化时间戳 + 通道）
    ↓ pipeline.retime：估计tick、定位2-tick校正点、按样本序号重建时间轴
    → 均匀时基文件；诊断合并进 meta/info.json（可选 .timebase.json）
```

```text
均匀时基 CSV/parquet（timestamp + 通道）
    ↓ pipeline.replay（默认）：dsp.offline（镜像+filtfilt+周期延拓梳状/陷波）
    ↓ 可选 --causal：RealtimeDenoiser 流式因果
    → 同目录 <stem>.denoised.*（+ valid/segment_id）；注册 features.<key>.denoised
```

- **文件去噪**（`process` / `batch --denoise`）与 **Viewer** 对齐：离线零相位，减轻首尾振铃。
- **实时 API** `RealtimeDenoiser` 仍为因果 `sosfilt`（可加 `--causal` 让文件路径与之对齐）。

## 模块职责

| 模块 | 职责 | 不承担的职责 |
|---|---|---|
| `dsp.filters` | EMG/EEG 固定 DSP、梳状/陷波、输入矩阵校验 | 数据采集、文件读写 |
| `dsp.offline` | Viewer 同款离线 filtfilt（镜像/周期延拓、端点消振） | 流式状态机 |
| `dsp.core` | 会话配置、因果处理、原时间戳、预热与断流 | 命令行、设备驱动 |
| `pipeline.timebase` | tick估计、校正点定位、均匀时间轴重建 | 读写文件、修改样本值 |
| `pipeline.retime` | 阶段一：读采集文件、写均匀时基 | 滤波、重采样、插值 |
| `pipeline.replay` | 阶段二文件去噪（默认 offline，可选 causal） | 参数调优 UI |
| `pipeline.batch` | 展开glob、镜像/就地编排两阶段 | 单文件算法细节 |
| `pipeline.meta` | 合并诊断进 `meta/info.json`、sidecar 回填 | DSP 计算 |
| `io` / `cli` | 校验、原子写入、可读摘要 / `--json` | 内嵌滤波系数选择 |
| `viz/` | 离线波形浏览器 | 实时设备驱动 |

## 关键约束

- 阶段一只重建时间轴，样本值逐位不变。两个以上tick的跳变是硬件校正点，不等于真实丢样。
- `modality` 在一次会话内只读。`reset()`清空信号/时间状态并重新预热（因果 API）。
- 因果路径：每次输入先完整校验再改动状态；向前缺样开启新分段；倒退/重复时间被拒绝。
- 输入输出保持同一单位。波形时间戳是采样时刻，不能换成处理完成时间。
- 批量输出按输入相对公共父目录镜像，避免同名文件静默覆盖。
- 诊断默认写入 `meta/info.json`；`.timebase.json` / `.report.json` 仅在 `--sidecar` 时写出。
- 输出CSV及单个JSON文件各自原子替换；输入/输出/报告路径不得冲突。

## 时间和预热

- EMG：2 kHz，因果块长 20 点（10 ms），预热 1 s（仅 `--causal` / `RealtimeDenoiser`）
- EEG：250 Hz，因果块长 10 点（40 ms），预热 1 s
- 离线 filtfilt 路径无预热窗口，输出默认 `valid=True`

## 离线浏览与文件去噪

`viz/viewer.html` 与默认 `process` 同属离线对照链路：高通/低通镜像延拓 + filtfilt；陷波/梳状周期延拓 + 端点消振。实时因果主链仅用于 `RealtimeDenoiser` 与 `--causal`。
