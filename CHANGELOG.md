# Changelog

## Unreleased

- 文件级 `process` / `batch --denoise` 默认改用与 Viewer 相同的离线链路（镜像 padding + filtfilt + 梳状/陷波周期延拓 + 端点消振），减轻首尾振铃；`--causal` 可切回原流式因果路径。`RealtimeDenoiser` API 不变。
- 诊断默认写入会话 `meta/info.json`（时基字段 + `<feature>.denoised`）；`.timebase.json` / `.report.json` 改为可选 `--sidecar`。新增 `meta-backfill` 从 sidecar 回填并删除。
- CLI 默认打印可读摘要（`batch` / `retime` / `process`）；需要机器解析时加 `--json`。批量总览仍写 `batch.json`。
- 阶段二 `process` 支持 parquet 输入；默认在同目录写出 `<stem>.denoised.parquet`（CSV 同理为 `.denoised.csv`）；输出含 `valid` / `segment_id`，并保留元数据列。
- `batch --inplace --denoise` 可用：就地重建后在旁写出去噪 parquet。
- 项目更名为 `emg-eeg-denoise`（包 `emg_eeg_denoise`，CLI `emg-eeg-denoise` / `eed`）；源码按 `dsp/` 与 `pipeline/` 分组；浏览页改为 `viz/viewer.html`。
- 重构为四项能力：EMG 时基重建、EMG 去噪（20/450/梳状 50 Hz）、EEG 去噪（0.5/50/陷波 50 Hz）、以及 EMG & EEG Viewer。
- 新增因果 EEG 链路与 `--modality emg|eeg|auto`；`process` / `batch` 同时支持流式与离线批处理。
- 删除流式特征、`validate` 合成验收与 `metrics` 辅助模块。
- Viewer 的 EEG 默认改为高通 0.5 Hz、低通 50 Hz、陷波 50 Hz。
- 移除可选小波增强（SWT）：删除 `wavelet` / `qualification` 模块、`calibrate` / `qualify` 子命令及 PyWavelets 依赖；主链仅保留因果带通 + 50 Hz 梳状陷波。
- 新增阶段一 `retime` 子命令：重建采集数据的 tick 量化时间戳为均匀时基，输出 `process` 可直接读取的 CSV 与 `.timebase.json` 报告。样本值逐位不变。
- 新增 `timebase` 模块：估计时钟分辨率、定位 2-tick 硬件校正点、按样本序号重建时间轴，并给出偏移诊断与不可信时的告警。
- `retime` 支持 parquet（可选依赖 `[parquet]`）与 CSV，自动按 `dim_N` 数字顺序识别通道、跳过 `episode_index` 等元数据列。
- 新增 `batch` 子命令：按 glob 批量串联两阶段（`--denoise` 可选），输出按输入相对公共父目录的路径镜像，避免采集布局中同名文件互相覆盖。单文件失败不中断批次，已存在输出默认拒绝覆盖（`--force` 可覆盖），派生文件不会被当作输入。
- 浏览页改为 EMG & EEG Viewer：可导入 episode 文件夹中的左右腕 EMG parquet，以及 EEG parquet。
- 浏览页可同时串联多个陷波，并显示当前窗口的频谱。
- 浏览页的陷波和梳状改为周期延拓。低截止高通留下的工频在端点被镜像折返时，不再振铃约 1 秒。
- `retime` / `batch` 支持 `--inplace`：源 parquet 改名为 `*.bak`，均匀时基结果写回原文件名；也可 `--output *.parquet` 写出新的 parquet。

## 0.1.0

- 实现2 kHz多通道因果肌电去噪与50 Hz IIR梳状陷波。
- 提供原时间戳、预热/断流状态、流式特征和CSV回放工具。
- 将命令行、数据I/O、回放服务、质量指标与DSP核心分层。
- 增加严格类型检查、统一格式、分支覆盖率门槛、跨Python版本CI及打包检查。
- 防止文件路径别名覆盖输入，解析失败时保留已有输出。
