# 2 kHz 实时表面肌电去噪

[架构说明](docs/architecture.md) · [开发规范](CONTRIBUTING.md) · [验证报告](docs/validation.md) · [变更记录](CHANGELOG.md)

可运行的多通道 Python 参考实现：**二阶20 Hz高通 → 四阶450 Hz低通 → 50 Hz IIR梳状陷波 → 可选db4/SWT增强**。滤波因果运行，状态跨块保留，输出保留原始电压单位和采样时间戳。

小波默认关闭；缺少静息校准或代表性数据验收不通过时，自动回到固定主链，并明确给出原因。本项目不含采集设备驱动；本机回放/基准测试不能证明采集、传输及目标设备合计延迟低于100毫秒。

## 安装和快速验证

Python 3.10及以上：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[test]'
python -m pytest -q
emg-denoise validate --output artifacts --channels 1 8
emg-denoise process artifacts/noisy.csv --output artifacts/denoised.csv \
  --features artifacts/features.json
```

不做可编辑安装时，可在项目目录使用 `PYTHONPATH=src .venv/bin/python -m emg_denoise ...`。

`validate`生成确定性合成肌电、静息噪声、工频与频漂测试、1/8通道运行时间，以及 [验证报告快照](docs/validation.md)。合成数据仅为工程验证，不代表真实受试者表现。

## 实时接口

```python
import numpy as np
from emg_denoise import DenoiserConfig, RealtimeEMGDenoiser, StreamingFeatures

engine = RealtimeEMGDenoiser(DenoiserConfig(channels=2))
features = StreamingFeatures(channels=2)

# 每次收到20×2个电压样本；timestamps必须来自原始采样时钟。
samples = np.zeros((20, 2))
timestamps = np.arange(20) / 2000.0
for block in engine.process(samples, timestamps):
    signed_voltage = block.samples
    original_timestamps = block.timestamps
    valid_for_analysis = block.valid
    feature_frames = features.update(block)
```

- 一维输入视为单通道，二维布局为`样本×通道`。建议实时调用每次输入20点；回放允许任意长度，内部每次最多处理20点，末尾不足20点也会处理。
- 输入必须有限、连续、采样间隔0.5毫秒；单个调用中不允许跨越缺样区。通道数固定，通道之间不共享滤波状态。
- 跨调用遇到向前时间跳变会清空滤波、小波与时间状态，开启新分段；前2000点重新标记`valid=False`。CSV工具会自动在时间间断处分块。
- 时间倒退或重复会拒绝输入。设备时间计数器重启时，调用`reset()`后重新提交。
- 无效输入会在状态更新前拒绝，不让NaN污染后续数据。重新连接、更换电极或电压标定改变时，应重新评估静息校准和小波验收。
- `process()`返回已就绪的块列表；小波模式等待真实后文时可能返回空列表。
- `StreamingFeatures`给出50毫秒尾随RMS、20毫秒尾随RMS包络和1秒尾随频谱的平均/中值频率。只有整个特征窗口均有效时才输出对应特征。频谱使用带正负值的信号；低能量/全零频谱的频率为NaN。1秒频谱描述一段历史，不是瞬时的疲劳估计。

## 固定滤波参数和状态

| 项目 | 参数 |
|---|---|
| 采样率 / 常规输入块 | 2000 Hz / 20点（10 ms） |
| 高通 | Butterworth二阶，20 Hz，SOS |
| 低通 | Butterworth四阶，450 Hz，SOS |
| 梳状器 | 基频50 Hz，−3 dB带宽2 Hz，Q=25 |
| 梳状递推 | `y[n] = g*(x[n]-x[n-40]) + r*y[n-40]` |
| 常数 | `g=0.9408092961815946`，`r=0.8816185923631891` |
| 预热 | 启动/重置后的前1秒样本标记无效，仍持续计算 |

梳状器使用40点转置延时线，等价于SciPy `iircomb(50,25,fs=2000,ftype='notch',pass_zero=False)`，并经过独立设计对照测试。它在0、50、100 Hz等位置具有陷波，不是简单地每40点做一次差分。带通使用3个二阶节，每通道主链只需6个SOS状态和40个梳状状态。

固定50 Hz中心不会跟踪电网频漂。49.8/50.2 Hz处梳状器约衰减14.16 dB；相同基频漂移在高次谐波上会放大。完整频漂结果见验证JSON。陷波也会移除附近的有效肌电，分析RMS或疲劳时须查看主链保真报告。

## 小波校准、验收和启用

### 先校准噪声

静息CSV至少3秒：前1秒用于主链收敛，随后2秒用于估计噪声。所有通道和电压单位必须与实际会话一致。

```bash
emg-denoise calibrate rest.csv --output calibration.json
```

校准数据先经过固定主链，再做四层归一化SWT。每层用去中位数的MAD/0.67448975估计噪声标准差；排除变换两端的105点。校准矩阵顺序是`D4,D3,D2,D1`。

### 对代表性配对回放验收

准备时间戳和通道完全一致的`clean.csv`、`noisy.csv`，以及零起始的肌肉激活起点样本索引。可使用有可靠低污染参考的半合成加噪实验；不要将未经验证的真实带噪数据当成干净参考。

```bash
emg-denoise qualify clean.csv noisy.csv --calibration calibration.json \
  --onsets 4000 8000 12000 --representative-data \
  --note '目标设备、佩戴位置、任务及受试者范围说明' --output qualification.json
```

这里的`--representative-data`是调用者对数据代表性的明确声明，不是程序能自动判断的事实。程序生成数据摘要，绑定校准参数，并重新计算批准状态，不信任JSON中的现成`approved`字段。

必须同时满足：

- 干净回放相对仅主链的RMS变化≤5%，中值频率变化≤5%；取最差通道。
- 带噪回放相对同一主链干净参考，SNR改善≥3 dB；取最差通道。
- 相同阈值、20毫秒因果RMS包络与10毫秒持续跨越规则下，激活起点额外偏移≤10毫秒。起点由配对干净参考设定阈值，需人工标注；无标注或检测不到时不批准。
- 数据被声明为代表目标场景，且校准摘要匹配。

静息全零、无信号或没有可靠起点时，指标标记为不可用，不会误批准。自动生成的`artifacts/synthetic_qualification.json`始终不批准实际会话。验收不达标本身是有效结果，固定主链继续可用。

### 以固定模式开始新会话

```bash
emg-denoise process input.csv --output denoised.csv --wavelet \
  --calibration calibration.json --qualification qualification.json
```

Python：

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

小波在256点帧上运行db4、四层SWT，逐层选择SURE最小风险软阈值，上限为通用阈值，保留`A4`。归一化正反变换一致。SWT噪声系数存在相关性，因此SURE只作为候选策略，必须由配对回放保真验收决定是否启用。

每20点释放一次帧的第119–138点。启动时仅镜像缺失的历史上下文，预热标记不变；不补造未来数据。20点输入分块造成最多1毫秒的帧对齐额外等待，因此输出样本年龄为60–69.5毫秒，而非理想帧就绪时的59–68.5毫秒。处理低于10毫秒时，数字延迟仍低于80毫秒。

小波模式首次输出需等到140点（70毫秒）上下文，之后每10毫秒输出；这是有界等待，不是1秒预热数据的额外扣留。有限回放末尾约118–137点因缺乏真实后文不会输出，`pending_samples`和CSV报告会明确列出；不以零填充伪装为有效输出。切换模式请新建实例，重新开始分段和预热。

## 时间、性能与验收边界

`DenoisedBlock`区分：

- `processing_ms`：该次滤波/小波与输出对象准备的计算耗时。
- `buffering_delay_ms`：原始样本等待同块或小波后文的时间。
- `estimated_digital_latency_ms`：前两者之和，不含采集设备、传输及调度等待。
- `observed_end_to_end_ms`：只有调用`process(...,received_at=...)`并提供与采样时间戳**同一时钟域**的实际收包时间时才计算；文件回放保持为空。

100毫秒要求的验收需在实际通道数、目标硬件、采集及传输链路上测量。主链目标数字延迟≤20毫秒；小波数字延迟≤80毫秒，为采集传输保留20毫秒。通用操作系统的短时基准不能证明硬实时保证；10毫秒期限只由每次最大耗时/超时次数判断，不能只看平均或P99。

2 Hz陷波器的极点包络衰减至1%约需0.73秒，所以标记前1秒预热。滤波相位/群延迟和数据可用延迟分开报告；零相位`filtfilt`没有用于实现或流式对照。

输出CSV列为`timestamp,<原通道名>,valid,wavelet_active,segment_id`，原时间戳以17位有效数字保存。报告默认写入`<输出文件>.report.json`。CSV回放可选特征另写JSON；特征文件适合回放分析，长期在线系统应将`StreamingFeatures`结果逐帧发送到下游。

## 代码与测试

- `src/emg_denoise/filters.py`：因果SOS带通、稀疏IIR梳状器。
- `core.py`：流式接口、原时间戳、有效标记、断流和延迟统计。
- `wavelet.py` / `qualification.py`：静息校准、中心帧、SURE、验收门槛。
- `features.py`：后置RMS/包络/有符号频谱特征。
- `io.py` / `replay.py`：数据校验、文件保护和CSV回放服务。
- `cli.py` / `validation.py`：命令行边界、合成验证与本机基准。

测试覆盖独立SciPy对照、不同分块一致性、通道隔离、50 Hz谐波衰减、频漂限制、原单位/时间戳、预热和断流、SURE与暴力风险搜索对照、小波中心边界、零信号、弱信号、异常输入和验收拒绝路径。

设计参考：[SciPy IIR梳状器](https://docs.scipy.org/doc/scipy/reference/generated/scipy.signal.iircomb.html)、[因果SOS状态](https://docs.scipy.org/doc/scipy/reference/generated/scipy.signal.sosfilt.html)、[SWT](https://pywavelets.readthedocs.io/en/latest/ref/swt-stationary-wavelet-transform.html)。



## 工程质量与自动检查

```bash
python -m pip install -e '.[dev]'
make check
```

`make check`依次检查Ruff格式/导入/静态规则、mypy严格类型、pytest分支覆盖率，以及wheel/sdist元数据。开发环境使用Python 3.12；运行时由CI覆盖Python 3.10–3.13，并包含Linux和macOS。覆盖率最低85%。详细流程见[贡献说明](CONTRIBUTING.md)。

核心模块与数据I/O、命令行分离。会话配置只读，报告为严格JSON，输出采用单文件原子替换，并拒绝输入/输出/报告/特征路径冲突及已有文件别名。退出码：0成功、1软件验收未通过、2输入或I/O错误。CSV校准和配对验收命令的成功输出也采用JSON。

`artifacts/`和虚拟环境属于本地生成内容，不提交；公开报告只保留快照，数据可用上述命令重新生成。GitHub Actions运行结果以目标仓库Actions页为准。
