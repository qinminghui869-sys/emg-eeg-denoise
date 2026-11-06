# 架构与接口约束

## 四项能力

1. **EMG 时间戳重建**：采集 tick 量化时间戳 → 均匀时基
2. **EMG 去噪**：20 Hz 高通 → 450 Hz 低通 → 50 Hz 梳状（BW=2 Hz，Q=25），2 kHz 因果
3. **EEG 去噪**：0.5 Hz 高通 → 50 Hz 低通 → 50 Hz 陷波（Q=25），250 Hz 因果
4. **可视化**：`viz/viewer.html` 离线浏览（镜像 + filtfilt，与因果主链不同）

## 包布局

```text
emg_eeg_denoise/
  dsp/         因果滤波与 RealtimeDenoiser
  pipeline/    retime / replay / batch / timebase
  io.py        边界 I/O
  cli.py       命令行
```

## 数据流

```text
采集文件（parquet/CSV：tick量化时间戳 + 通道）
    ↓ pipeline.retime：估计tick、定位2-tick校正点、按样本序号重建时间轴
    → 均匀时基CSV（timestamp,... + .timebase.json报告）
```

```text
均匀时基电压 + 时间戳
    ↓ dsp.RealtimeDenoiser：校验、分块、分段与预热
    ↓ dsp.CausalFilterBank(modality=emg|eeg)
    → DenoisedBlock：有符号波形、原时间戳、有效状态与延迟观测
```

## 模块职责

| 模块 | 职责 | 不承担的职责 |
|---|---|---|
| `dsp.filters` | EMG/EEG 固定 DSP、梳状/陷波、输入矩阵校验 | 数据采集、文件读写 |
| `dsp.core` | 会话配置、因果处理、原时间戳、预热与断流 | 命令行、设备驱动 |
| `pipeline.timebase` | tick估计、校正点定位、均匀时间轴重建 | 读写文件、修改样本值 |
| `pipeline.retime` | 阶段一服务：读采集文件、写阶段二可读CSV | 滤波、重采样、插值 |
| `pipeline.batch` | 展开glob、镜像输出路径、逐文件串联两阶段 | 单文件的算法细节 |
| `io` / `pipeline.replay` | CSV/JSON校验、路径冲突检查、原子替换 | 核心算法参数选择 |
| `cli` | 参数解析、服务调用、严格JSON输出和退出码 | 内嵌滤波逻辑 |
| `viz/` | 离线波形浏览器 | 生产/实时因果主链 |

## 关键约束

- 阶段一只重建时间轴，样本值逐位不变。两个以上tick的跳变是硬件校正点，不等于真实丢样。
- `modality` 在一次会话内只读。`reset()`清空信号/时间状态并重新预热。
- 每次输入先完整校验再改动状态。向前缺样开启新分段；倒退/重复时间被拒绝。
- 输入输出保持同一单位。波形时间戳是采样时刻，不能换成处理完成时间。
- 批量输出按输入相对公共父目录镜像，避免同名文件静默覆盖。
- 输出CSV及单个JSON文件各自原子替换；输入/输出/报告路径不得冲突。

## 时间和预热

- EMG：2 kHz，块长 20 点（10 ms），预热 1 s
- EEG：250 Hz，块长 10 点（40 ms），预热 1 s
- 预热期内滤波持续运行，输出标记 `valid=False`

## 离线浏览

`viz/viewer.html` 只做离线对照，不进入 `RealtimeDenoiser`。高通/低通走镜像延拓 + filtfilt；陷波/梳状用周期延拓。生产路径仍是因果 `sosfilt`。
