# Changelog

## Unreleased

- EMG `retime` 默认在相邻校正点之间固定两端并按实际样本数均分（间距可为 9、18 或其他数量）。未被双端点覆盖的首尾保持原样；不足两个校正点时整个文件不变。局部周期偏离标称超过 20%，或区间内超过三个标称周期的间隔，保持原样并告警。信号值不变。`--method uniform` 仍为显式旧模式；EEG 命令行与批处理默认仍用该模式。
- 离线 EMG 去噪适配上述时基：大间隔处分段，段内不均匀时间戳经内部均匀网格滤波后映回，保存的时间戳和样本数不变。`--causal` 仍拒绝非均匀时基。
- 去噪信号文件不再写入 `valid`/`segment_id`（避免上游按列数把 8 通道读成 10）；可选 `--sidecar` 旁路 `*.mask.parquet`。读取时也将这两列视为元数据。
- `process`/`batch` 支持 `--rate` 贯穿 EEG/EMG 滤波与分段；无显式速率时优先 `meta/info.json` 的 feature.fps，再测时间戳（修复 Neuroscan 1000 Hz vs 预设 250 Hz）。
- 去噪后向 `episodes_stats.jsonl` 写入派生特征的 min/max/mean/std/count。
- 文件级 `process` / `batch --denoise` 默认改用与 Viewer 相同的离线链路（镜像 padding + filtfilt + 梳状/陷波周期延拓 + 端点消振），减轻首尾振铃；`--causal` 可切回原流式因果路径。`RealtimeDenoiser` API 不变。
- 诊断默认写入会话 `meta/info.json`（时基字段 + `<feature>.denoised`）；`.timebase.json` / `.report.json` 改为可选 `--sidecar`。新增 `meta-backfill` 从 sidecar 回填并删除。
- CLI 默认打印可读摘要（`batch` / `retime` / `process`）；需要机器解析时加 `--json`。批量总览仍写 `batch.json`。
- 阶段二 `process` 支持 parquet 输入；默认在同目录写出 `<stem>.denoised.parquet`（CSV 同理为 `.denoised.csv`）；保留元数据列。
- `batch --inplace --denoise` 可用：就地重建后在旁写出去噪 parquet。
- 项目更名为 `emg-eeg-denoise`（包 `emg_eeg_denoise`，CLI `emg-eeg-denoise` / `eed`）；源码按 `dsp/` 与 `pipeline/` 分组；浏览页改为 `viz/viewer.html`。
- 重构为四项能力：EMG 时基重建、EMG 去噪（20/450/梳状 50 Hz）、EEG 去噪（0.5/50/陷波 50 Hz）、以及 EMG & EEG Viewer。
- 新增因果 EEG 链路与 `--modality emg|eeg|auto`；`process` / `batch` 同时支持流式与离线批处理。
- 删除流式特征、`validate` 合成验收与 `metrics` 辅助模块。
- Viewer 的 EEG 默认改为高通 0.5 Hz、低通 50 Hz、陷波 50 Hz。
- 移除可选小波增强（SWT）：删除 `wavelet` / `qualification` 模块、`calibrate` / `qualify` 子命令及 PyWavelets 依赖；主链仅保留因果带通 + 50 Hz 梳状陷波。
- 新增阶段一 `retime` 子命令：重建采集时间戳且不改信号值；诊断可写入 `meta/info.json`，`--sidecar` 时另写 `.timebase.json`。
- 新增 `timebase` 模块：估计时钟分辨率、定位约两个 tick 的候选校正点，并在通过检查的相邻校正点之间均分时间。
- `retime` 支持 parquet（可选依赖 `[parquet]`）与 CSV，自动按 `dim_N` 数字顺序识别通道、跳过 `episode_index` 等元数据列。
- 新增 `batch` 子命令：按 glob 批量串联两阶段（`--denoise` 可选），输出按输入相对公共父目录的路径镜像，避免采集布局中同名文件互相覆盖。单文件失败不中断批次，已存在输出默认拒绝覆盖（`--force` 可覆盖），派生文件不会被当作输入。
- 浏览页改为 EMG & EEG Viewer：可导入 episode 文件夹中的左右腕 EMG parquet，以及 EEG parquet。
- 浏览页可同时串联多个陷波，并显示当前窗口的频谱。
- 浏览页的陷波和梳状改为周期延拓。低截止高通留下的工频在端点被镜像折返时，不再振铃约 1 秒。
- `retime` / `batch` 支持 `--inplace`：源 parquet 改名为 `*.bak`，重建后的时间轴写回原文件名；也可 `--output *.parquet` 写出新的 parquet。

## 0.1.0

- 实现2 kHz多通道因果肌电去噪与50 Hz IIR梳状陷波。
- 提供原时间戳、预热/断流状态、流式特征和CSV回放工具。
- 将命令行、数据I/O、回放服务、质量指标与DSP核心分层。
- 增加严格类型检查、统一格式、分支覆盖率门槛、跨Python版本CI及打包检查。
- 防止文件路径别名覆盖输入，解析失败时保留已有输出。

### EMG QC

- 新增只读 `eed qc`：批量 Parquet/CSV 通道质量、时间轴、备份一致性与去噪对照。
- 支持 JSON/CSV 报告、可配置工程门限、严格退出码和未验证状态；不修改源数据或 meta。
