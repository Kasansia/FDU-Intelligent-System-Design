# MotionMuse 幻奏 · FDU 智能系统设计

本地视觉乐器：**OpenCV Zoo 双手检测 → 多 ArUco 通用平面定位 → 独立演奏区域 → 左手 C/G/Am/F 和弦 → 右手上下扫事件 → 本地音频**。支持自由贴标、冻结帧标定、部分遮挡追踪、JSONL/CSV 与可选原始图像录制、离线回放。

一个摄像头、一个手部推理后端和一个动作状态机。核心运行无需外网，浏览器不运行 MediaPipe JS。保留 A 的 ONNX、包装类、启动脚本、原回归和 HTTP 路径。两源版本、署名、文件归属见 [BASELINE](docs/integration/BASELINE.md)。

**验证边界：** 新版已验证 Windows Python 3.12 自动化、真实 ONNX 照片回归、合成移动/遮挡/回放、默认四和弦闭环及浏览器标定。真人持板/物理摄像头、人工听感和 Jetson 实机验收尚未完成，见 [新版测试报告](docs/generalized/TEST_REPORT.md)。旧集成的 PortAudio 输出流记录见 [历史报告](docs/integration/TEST_REPORT.md)。合成演示不是硬件性能基准。

## Windows 首次安装

```powershell
git clone https://github.com/Loong-C/FDU-Intelligent-System-Design.git
cd FDU-Intelligent-System-Design
git switch main
powershell -ExecutionPolicy Bypass -File .\setup.ps1
.\start.cmd
```

需要 Python 3.12（PATH 或 py 启动器登记）。setup 仅在项目 `.venv` 安装依赖并验证两份 ONNX SHA-256。PC 固定 OpenCV 4.12.0.88 / NumPy 2.2.6；此 wheel 已有 ArUco，不再安装 contrib。音频新增 sounddevice 0.5.3。

打开 [本地演奏台](http://127.0.0.1:8765/)。默认镜像、640×480，优先 C270，Windows 依次尝试 DSHOW/MSMF。虚拟相机会参与枚举，应选择实际设备。

```powershell
.\.venv\Scripts\python.exe app.py --list-cameras
.\.venv\Scripts\python.exe app.py --camera 0 --backend msmf
.\.venv\Scripts\python.exe app.py --no-mirror
```

`start.cmd` 继续调用 `start.ps1` 在后台启动并打开浏览器。关闭网页不会停服务；点击“停止服务”、Ctrl+C（前台）或执行 `powershell -ExecutionPolicy Bypass -File .\stop.ps1` 释放设备。日志：`outputs/server.stderr.log`。

## 打印和演奏

### 自由贴标与一次性标定（新版）

```powershell
.\.venv\Scripts\python.exe generate_markers.py --ids 4 5 6 7 8 9
.\.venv\Scripts\python.exe app.py
```

1. 打印 `outputs/markers/` 中的标记，保持白边、不镜像。把不同 ID 分散贴在同一刚性平面上；位置、旋转和板子外轮廓可自由选择。默认字典 `DICT_4X4_50` 支持 0–49；配置与测试覆盖 1–12 枚，单枚只能降级显示。建议至少 3 枚、实际布置 4–6 枚以增加遮挡余量。
2. 打开 [本地演奏台](http://127.0.0.1:8765/)，点击 **划定演奏区域 → 捕获**。至少两枚分散、清晰、完整的标记应同时可见。画面冻结，演奏暂停。
3. 在同一刚性板面上分别拖出 **和弦区** 和 **拨弦区**，位置和大小独立选择，无需再选外层板面矩形。也可切换为“依次点选四角”，按画面中的 **左上 → 右上 → 右下 → 左下** 点选，并拖动已有角点贴合倾斜板面。镜像显示时直接按看到的位置操作。两个区域须为不重叠的有效凸四边形，不能随两个物体独立移动。
4. 和弦区沿第一、第二角点方向自动分成 C/G/Am/F 四格；拨弦区生成横向中线，从所选区域上半部跨向下半部为 down。可撤销、重选当前区域或重新捕获。填写英文/数字/下划线/连字符名称，点击 **预览**检查区域，然后 **确认保存并启用**。例如同时保存 `config/boards/my_board.json` 和 `config/presets/my_board.json`，相同名称不会覆盖。取消不改变当前板面和区域。冻结会话 10 分钟过期，过期后自动退出演奏暂停。
5. 重启时只需加载板面配置，会自动加载关联的区域预设；请一起保留 `boards/` 与同级 `presets/` 下的两个文件：

```powershell
.\.venv\Scripts\python.exe app.py --board config/boards/my_board.json
```

显式传入 `--preset` 可覆盖关联预设；没有关联预设的旧板面继续使用默认吉他布局。网页重新划定区域会直接启用所选位置，原来的 `--layout flip-u` 不会再次翻转这次选择。板面坐标是投影 UV，不保证等距或正交测量。

Marker 和交互点允许位于 `[0,1]` 外，不截断。移动、旋转、倾斜板子时，后台使用所有可见已登记标记的编码角点联合 RANSAC；未知 ID 不参与，重复已登记 ID 会拒绝定位。画面和检测来自同一摄像头；标定会话始终使用冻结帧的角点。两个区域通过同一板面定位更新；仅画框而不保留可见 Marker 不能持续追踪。

检查遮挡：先确认 VALID，再遮住一两枚标记。剩余标记足够分散且通过质量检查时保持 VALID；只剩一枚时 DEGRADED，禁止新事件；全部遮挡可短暂 HELD，超过最后可靠姿态的 `.25 s` 后 LOST。恢复后需重新驻留选和弦并观察扫弦一侧。调试面板显示内点、误差、覆盖率及原因。

可用 `--board-hold 0.3` 或 `--tracking-config tracking.json` 调整 [TrackingConfig](aruco_tracking.py)。默认 valid 要求至少 2 枚完整内点标记、至少 8 个内点、内点比例 ≥ .8、重投影 RMSE ≤ 2 px、支持域覆盖 ≥ .10，同时通过角点尺寸、二维分布、矩阵条件、投影极点和时序突跳检查。数量不是单独的有效性判据；这些是启发式门限，并非概率保证。

### 独立演奏预设

`BoardDefinition` 只保存定位对象；`InstrumentPreset` 只保存使用方式。复制 [默认吉他预设](config/presets/guitar.json)，修改 `chords` 的简单多边形、`strum` 多边形、`axis` 两个有向端点，以及可选 `interaction` 阈值，然后用 `--preset` 加载。无需重新标定 Marker。

轴正向法线为 `(-dy, dx)`，沿该法线跨越为 down；反向为 up。状态机使用沿法线的距离与速度，支持斜线、竖线。`above_v`/`below_v` 保留兼容名称，含义为 `.5 + 有符号法向距离` 的滞回阈值。`--layout flip-u` 镜像区域及法线但不交换解剖学左右手。多边形重叠时按 JSON 顺序选择第一个命中区；旧预设的 margin 是到边界的 UV 距离。

网页生成的预设另含 `chord_frame`、`strum_frame` 四角坐标：和弦边界余量以整个和弦区的单位矩形计算，扫弦距离、速度和跳变检查以拨弦区的单位矩形计算。因此区域缩小或放大不需要手动同比缩放触发阈值。未提供这两个字段的旧预设保持原板面 UV 行为。录制与回放保存并恢复这些配置。

当前音频仍只支持 C/G/Am/F；空间接口可复用，不增加其他乐器音色。显示叠加由 Python 对同一份区域做 Homography 逆投影，网页不求解另一套几何。

[六标记合成板](config/boards/synthetic_six.json) 用于工具生成的合成图像；真实自由贴标应通过界面标定。可选 `outline_uv` 仅用于显示和质量支持域，不用于建立对应关系。

### 原四标记免标定演示

```powershell
.\.venv\Scripts\python.exe generate_markers.py
```

打印 `outputs/markers/marker_0.png` 至 `marker_3.png`，保留白边，不镜像打印。四个中心固定在同一刚性平板：

```text
ID 0 ---------------- ID 1
          交互平面
ID 2 ---------------- ID 3
```

省略 `--board` 时，让四个 Marker 首次完整进入画面：以原四中心坐标建立 UV，自动学习全部编码角点，然后可按质量条件部分遮挡追踪。保持旧演示无需重新标定。解剖学左手放在 C/G/Am/F 区域，驻留约 100 ms；右手食指穿过扫弦线发上下扫。四和弦采用区域选择，不是真实吉他指法识别。音色为代码生成的测试音色，可衰减重叠；不是最终吉他采样。

默认和弦区 u=.04–.60，扫弦区 u=.68–.90。朝向不合适时用 `--layout flip-u` 翻转区域，**不交换左右手标签**。短时遮挡最多保持 .25 秒供显示；HELD/LOST 或缺少新鲜左手时均不发新音符。重获后重新驻留、观察扫弦一侧。

UV 是二维成像投影，不表示手指接触板面。原模型 z/world_m 是手部局部估计，不是到板子的厘米距离。

## 音频检查与无摄像头演练

```powershell
.\.venv\Scripts\python.exe audio_engine.py --list-devices
.\.venv\Scripts\python.exe audio_engine.py
# 指定实际输出设备名称或当前枚举索引
.\.venv\Scripts\python.exe app.py --audio-device "G733 Windows WASAPI"

# 明确标记的合成演示，无摄像头，默认 mock 不发声
.\.venv\Scripts\python.exe app.py --demo
# 合成轨迹驱动真实音频输出
.\.venv\Scripts\python.exe app.py --demo --audio sounddevice
# 关闭声音，视觉继续
.\.venv\Scripts\python.exe app.py --audio off
```

初始化失败显示 `audio_status=error` 和原因，视觉继续；mock/disabled 不代表实际发声。事件在本进程消费，浏览器只显示历史。通过 `--interaction-config config.json` 可覆盖 [InteractionConfig](instrument_logic.py)，例如 `{"chord_dwell_s":0.12,"cooldown_s":0.18}`。阈值使用秒和 UV/s，不使用连续帧数。默认保守复位可能漏掉极快/遮挡动作，应实测后调整。

## 接口与录制

`GET /api/landmarks` 与 `/api/state` 继续通过显式适配器返回 schema v2，供旧 `example_client.py` 使用。`/api/v3/landmarks`、`/api/v3/state`、`/api/snapshot` 和新录制使用 schema v3，区分“数学上可投影”与“可靠且可演奏”。见 [新版 API](docs/generalized/API.md)；[旧版契约](docs/integration/API.md) 继续作为兼容说明。

```powershell
.\.venv\Scripts\python.exe example_client.py
```

点击“开始录制/停止录制”，保存到 `outputs/日期_时间_微秒/`：

- `landmarks.jsonl`：所有处理帧，含无手帧及完整 instrument/interaction。
- `landmarks.csv`：原列保留，追加 instrument_u/v；无投影留空。
- `events.jsonl`：每个新 event_id 一条。

独立写线程和有界队列，磁盘失败/积压显式报告。`outputs/` 全部忽略，不提交照片、视频或日志。调试面板保留节点表及 XY/XZ 局部投影。网页使用 `/api/snapshot` 获取同一帧图像与状态，原 `/frame.jpg` 保留。

### 实验采集和无摄像头回放

勾选网页 **实验采集：同时保存原始图像** 后开始录制，或以 `--record-video` 设置默认勾选。默认不保存图像。保存的是每个已处理帧的原始未镜像、无叠加无损 PNG；不是摄像头内部帧率的完整保证。编码和磁盘写入在独立线程，原始帧队列最多 16 项。数据包含：

- `frames/<frame_id>.png`、`frames.jsonl`：frame_id、两种时间戳、路径、SHA-256 与保存状态。
- `configurations.jsonl`：每帧引用的板面、预设、追踪与动作参数；支持录制期间切换标定配置。
- `manifest.json`：完整性、接收/写入数量、溢出帧 ID、错误。失败或队列溢出停止接收并明确标记不完整；中断进程的清单仍为 recording，不会冒充完整。

```powershell
# 使用新的输出文件名，不覆盖源数据或已有回放
.\.venv\Scripts\python.exe tools/replay_experiment.py outputs/<录制目录> --output outputs/replay.jsonl
.\.venv\Scripts\python.exe tools/analyze_experiment.py outputs/replay.jsonl --output outputs/analysis.json
# 重跑同一套 OpenCV Zoo；默认复用记录的手节点、重新检测 ArUco 并运行几何/动作逻辑
.\.venv\Scripts\python.exe tools/replay_experiment.py outputs/<录制目录> --rerun-hands --output outputs/replay_onnx.jsonl
```

回放默认 mock、不真实发声；显式 `--audio sounddevice` 才按原始相对间隔播放。记录的绝对单调时间映射到可控回放时钟，不会被实时过期检查拒绝。回放初始化时恢复记录前的板面姿态，因此从 held 中途开始录制也可重现显示状态；动作重新驻留，首段事件可能因缺少录制前手部历史而不同。`--board`、`--preset` 可比较同段图像的不同配置；`--allow-incomplete` 明确允许跳过录制时已标记失败的帧，仍报告缺帧。文件损坏、哈希/时间戳/帧序列不一致会报错。

无硬件完整生成与复现：

```powershell
.\.venv\Scripts\python.exe tools/generate_synthetic_experiment.py
# 按上条输出目录执行 replay_experiment.py
.\.venv\Scripts\python.exe tools/benchmark_tracking.py --iterations 300
node --check web/app.js
node --check web/calibration.js
node tests/frontend_coordinates.cjs
```

分析报告包括定位状态占比、可见数量分布、重投影误差、UV 帧间位移、恢复时长及视觉耗时。只有手与板固定相对关系时，UV 帧间位移才可解释为抖动。合成实验和 PC 耗时不代表 Jetson 实机演奏性能。

## 可重复验证

先下载原回归必需的 Google 官方双手照片（不随仓库分发）：

```powershell
New-Item -ItemType Directory -Force outputs | Out-Null
Invoke-WebRequest https://storage.googleapis.com/mediapipe-tasks/hand_landmarker/woman_hands.jpg -OutFile outputs/test_hands.jpg
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe tools/verify_runtime.py --seconds 32 --mode synthetic --audio mock
.\.venv\Scripts\python.exe tools/verify_runtime.py --seconds 32 --mode synthetic --audio sounddevice
.\.venv\Scripts\python.exe tools/verify_runtime.py --seconds 62 --mode onnx --audio mock
```

前两种运行使用脚本手部 + 真 ArUco；第三种用真实 ONNX 识别静态照片及生成的 Marker，检查一分钟静止误触。报告在各录制目录的 `runtime_report.json`。原 `tests/test_pipeline.py` 三项测试未删除或跳过。照片来源：[Google 官方手部示例](https://github.com/google-ai-edge/mediapipe-samples/blob/main/examples/hand_landmarker/python/hand_landmarker.ipynb)。

## Jetson Xavier NX

参阅 [JETSON](docs/integration/JETSON.md)。Linux 摄像头隔离到 V4L2/GStreamer，不强制导入 Windows 枚举；提供候选 `requirements-jetson.txt` 和只读 `tools/jetson_preflight.py`。不假设 PC wheel/Python 3.12 可直接移植。≥20 FPS 与 <200 ms 是尚未实测的目标。

默认 loopback；远程观看优先 SSH 转发。显式 `--host` 开启局域网查看，远端控制还需 `--allow-remote-controls`。服务无认证，不在公网暴露。

## 来源和限制

Zoo 固定 `47534e27c9851bb1128ccc0102f1145e27f23f98`，Apache-2.0 文本和原署名在 `vendor/opencv_zoo_runtime/`。模型校验：

```text
palm_detection_mediapipe_2023feb.onnx
78ff51c38496b7fc8b8ebdb6cc8c1abb02fa6c38427c6848254cdaba57fcce7c
handpose_estimation_mediapipe_2023feb.onnx
db0898ae717b76b075d9bf563af315b29562e11f8df5027a1ef07b02bef6d81c
```

MotionMuse 的 ArUco、布局、交互设计与视觉语言来源于 SMF-an 及原贡献者；B 未声明公开许可证，本次按组内任务整合，**对外重新发布前由成员确认授权**。未替他人代码声明新许可证。音源见 [assets/audio](assets/audio/README.md)。

真人遮挡、左右手交叉、倾斜、反光、低光及高速运动仍需实测；track_id 不是永久身份。原 README 的 C270/FPS 成绩属于原作者环境，本次不沿用为集成验收结果。
