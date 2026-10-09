# OpenCV Zoo 手部节点实验台

本项目基于 OpenCV Zoo 的手掌检测与手部关键点模型，首次验证使用 Logitech C270。原始本地部署位置为 `D:\智能系统`，也支持克隆到其他目录。当前已完成摄像头采集、双手关键点提取、四个 ArUco Marker 的乐器平面定位、手部相对乐器 UV 坐标、骨架可视化和节点导出。音乐交互层（音高、和弦、拨弦、音频）尚未实现。

## 首次安装

```powershell
git clone https://github.com/Kasansia/FDU-Intelligent-System-Design.git
cd FDU-Intelligent-System-Design
powershell -ExecutionPolicy Bypass -File .\setup.ps1
.\start.cmd
```

需要 Windows 和 Python 3.12。仓库包含完整 ONNX 模型、所需官方包装类及许可证，无需 Git LFS 或子模块。`setup.ps1` 建立虚拟环境、安装依赖并校验模型。已安装的本机可以直接启动。

## 启动和停止

双击 **start.cmd**，打开 <http://127.0.0.1:8765/>。默认优先选择 **Logi C270 HD WebCam**；服务仅监听本机，不上传画面或节点。

本机 C270 的 DirectShow 后端打不开，程序会自动改用 MSMF（Media Foundation）。首次启动约需 30 秒；等待界面显示“本地实时运行”。C270 已完成 640×480 实时双手识别验证。

- 绿色骨架是左手，橙色骨架是右手；各 21 个点，编号 0–20。
- 下方表格可选择手部，查看逐节点数值。
- “开始录制节点 / 停止录制”输出 `outputs/日期_时间/landmarks.jsonl` 和 `landmarks.csv`，不录制视频。
- “停止摄像头”会结束本地服务、释放设备。**关闭网页不会停止后台服务**。
- 也可执行 `powershell -ExecutionPolicy Bypass -File .\stop.ps1` 停止。
- 重新双击 start.cmd 恢复。默认不录制，不会开机自启。

命令行启动（前台日志，Ctrl+C 停止）：

```powershell
cd D:\智能系统
.\.venv\Scripts\python.exe app.py --camera 1 --backend msmf
```

其他命令：

```powershell
# 查看 DirectShow 设备索引（重新插拔后索引可能变化）
.\.venv\Scripts\python.exe app.py --list-cameras
# USB Camera，本机当前索引为 0；请先停止正在运行的服务
.\.venv\Scripts\python.exe app.py --camera 0 --backend dshow
# 关闭镜像显示与推理；左右手标签会相应修正
.\.venv\Scripts\python.exe app.py --no-mirror
```

## 后续乐器项目的数据接口

`GET http://127.0.0.1:8765/api/landmarks` 获取最新一帧的 JSON。摄像头只由后台采集一次；后续程序使用此接口，或直接复用 `hand_tracking.HandTracker.process(bgr_frame)`。

运行 `.\.venv\Scripts\python.exe example_client.py` 可以持续读取食指尖图像坐标，并在定位可用时输出 UV、valid/held 状态及 H 年龄；不可用时打印 `instrument unavailable`，Ctrl+C 结束客户端。单独调用 HandTracker 仍返回原有 v1 手部包；app.py 通过融合函数生成 v2 HTTP/录制包。

| 字段 | 含义 |
| --- | --- |
| `schema_version` | HTTP/录制当前为 2；保留所有 v1 字段语义 |
| `status` | starting / running / error |
| `frame_id` | 本次运行的递增处理帧号；重启归零 |
| `timestamp_unix_s` | 摄像头帧读取完成时的 Unix 秒时间戳 |
| `frame_width`, `frame_height` | 实际图像尺寸 |
| `mirrored` | 默认 true；推理、骨架和导出的二维坐标都基于镜像帧 |
| `hands` | 本帧检测结果；无手时为空数组，最多两只手 |
| `hands[].track_id` | 基于手腕距离的短时空间关联 ID |
| `hands[].handedness` | Left / Right，模型估计的解剖学左右手 |
| `hands[].handedness_score` | 左右手分类置信度 |
| `hands[].confidence` | 整只手的存在置信度，不是逐节点置信度 |
| `hands[].landmarks_px` | 21×3，x/y 为像素，z 为相对手腕的模型深度值，按图像尺度缩放 |
| `hands[].landmarks_normalized` | 21×3，x/宽、y/高、z/宽 |
| `hands[].landmarks_world_m` | 21×3，模型估计的手部局部三维坐标，单位米，原点约在手部几何中心 |
| `hands[].landmarks_instrument_uv` | 有可用定位时为 21×2 的乐器平面坐标，否则为 null；不截断 |
| `instrument` | Marker 检测、平面定位状态、正逆 Homography 及其年龄，详见下文 |
| `aruco_ms` | ArUco 检测及平面计算耗时；instrument 内同名字段相同 |
| `vision_total_ms` | 手部、ArUco 和坐标融合的总耗时，不含采集、绘制、JPEG、录制 |
| `landmark_names`, `connections` | 节点名和骨架连接关系 |

二维坐标原点在左上，x 向右、y 向下；边缘外预测可能小于 0 或大于 1，不进行截断。z 越小通常表示越靠近摄像头，**不是手到摄像头的距离**。世界坐标也不是经相机/板子标定的场景绝对坐标；不同手的局部原点不能直接用于测量双手间距离。

后续动作判断应只消费 `status == running`、时间戳足够新、`frame_id` 未重复的包，使用时间戳计算速度。示例客户端采用 0.5 秒过期阈值。HTTP 返回最新帧，低频轮询可能跳帧；JSONL 录制包含每个处理帧（包括空手帧），CSV 每帧每只手每节点一行，无手帧没有 CSV 节点行。

节点编号：0 手腕；1–4 拇指；5–8 食指；9–12 中指；13–16 无名指；17–20 小指。指尖为 4 / 8 / 12 / 16 / 20。

目前保留原始关键点，不做平滑、不推测遮挡后运动。track_id 使用 0.5 秒失踪保留和 0.2 归一化距离门限；交叉、遮挡、快速移动时可能换 ID，不应将它视为永久身份。握板时遮挡关键点仍会降低可靠性；本阶段以四个 Marker 定义二维平面坐标；滤波和动作判定留待下一阶段。

## ArUco 乐器平面定位

### Marker 生成与物理安装

使用 OpenCV `DICT_4X4_50`，只用 ID 0、1、2、3 建立乐器平面。四个 Marker 的**中心**按乐器自身方向排列：

```text
ID 0  (0,0) ---------------- ID 1  (1,0)
     |                              |
     |       标准演奏区域           |     u 向乐器右侧增加
     |                              |     v 向乐器下方增加
ID 2  (0,1) ---------------- ID 3  (1,1)
```

四个 Marker 必须固定在同一块不会弯曲的刚性平面上，固定后不要单独移动。保留 Marker 周围白边，不折叠、不起皱，避免胶带产生强反光。不要把相同必需 ID 在画面里重复放置。Marker 本身可旋转 90°、180°，坐标轴仅由 ID 和中心位置决定，不按 Marker 角点朝向或当前屏幕上下左右重排。

实体板的外轮廓可以不规则；四个参考中心应按 0→1→3→2 形成面积足够大、不自交的凸四边形。`0≤u≤1, 0≤v≤1` 是这个参考区域，不表示整块实体板的精确边缘。四角之外的 UV 可以小于 0 或大于 1。

生成带白边的四张 PNG（不需要新模型或额外依赖）：

```powershell
.\.venv\Scripts\python.exe generate_markers.py
```

输出到 `outputs/markers/DICT_4X4_50_ID_0.png` 至 `ID_3.png`。默认黑色 Marker 正方形边长 600 像素、四周白边各 100 像素。打印时建议**黑色正方形**约 50 mm，保留白边；程序不会自动校准打印机缩放比例。50 mm 不是当前二维 Homography 的计算参数，只是有利于稳定识别；未来 solvePnP 才需要精确物理尺寸。正常运行无需执行生成脚本。

### 数据流与镜像

1. app.py 读取一份原始 BGR 摄像头帧，送入 `InstrumentTracker.process(raw_bgr)`。检测使用 `ArucoDetector`、`DetectorParameters` 和 `CORNER_REFINE_SUBPIX`。
2. 同一份原始帧送入原有 HandTracker，它按设置在内部翻转自己的副本。
3. 检测到 ID 0、1、2、3 后，以四角平均值作为各 Marker 中心，严格按 ID 对应 `(0,0)、(1,0)、(0,1)、(1,1)` 求原始像素→UV 的 H 和逆矩阵。
4. 融合时若 `mirrored=true`，先将每个手节点恢复为 `x_raw=frame_width-1-x_display, y_raw=y_display`，再做齐次投影；非镜像时直接使用原坐标。
5. 绘制 Marker 与参考四边形时，将原始像素同样按 `width-1-x` 变换到显示画面。**不在镜像帧重新检测 Marker**。

`landmarks_normalized` 是相对于摄像头图像的坐标；`landmarks_instrument_uv` 是相对于乐器平面的坐标。板子和手一起向右移动时，前者会变化，后者应基本不变。UV 只描述二维位置，不输出虚构的 instrument z；`landmarks_world_m` 仍是手部模型的局部三维估计，与 ArUco 无关。手指离开板面有高度时，单目平面投影存在视差，不能用手部模型的 z 推断手到板子的真实距离。

### 定位状态与失效处理

| 状态 | 含义 | Homography / UV |
| --- | --- | --- |
| `valid` | 当前帧四个 ID 各检测到一次，几何校验通过，重新计算 H | 当前 H，年龄 0 |
| `held` | 当前帧缺少 Marker，距最近有效帧不超过 0.25 秒 | 暂用上次 H，显式输出年龄 |
| `lost` | 未成功定位过、保持超时、退化布局、重复 ID 或相机异常 | 两个 H 均为 null，每只手 UV 为 null |

四个中心间距必须至少 8 像素，参考四边形面积默认至少 400 平方像素；检查凸性、自交、近共线、有限值、可逆性和投影残差。检测到四个 ID 但几何无效或必需 ID 重复时立即丢弃旧 H，不使用 held 掩盖错误。帧尺寸变化、时钟倒退或相机读帧失败时清空缓存。使用单调时钟控制保持时间；丢失后露出四个 Marker 会自动恢复，无需重启。

held 期间板子移动会造成短暂误差，后续动作逻辑可选择仅使用 valid。单个手节点落在投影无穷远附近或含非有限值时，整只手的 UV 为 null，保证 `allow_nan=False`；不会将无效结果填为零。正常区域外坐标不 clamp。

`instrument` 字段说明：

| 字段 | 含义 |
| --- | --- |
| `dictionary`, `required_ids` | 固定 `DICT_4X4_50` 和 `[0,1,2,3]` |
| `visible_ids` | 本帧检测到的全部不同 ID，可含额外 ID；界面 4/4 只统计必需 ID |
| `markers` | 每个检测的 id、`corners_px_raw`（4×2）、`center_px_raw`（2）；均为未镜像原始像素 |
| `homography_camera_to_instrument` | 原始像素→UV 的 3×3 H，不可用时 null |
| `homography_instrument_to_camera` | UV→原始像素的逆 H，不可用时 null |
| `homography_age_s` | 最近有效 H 的年龄；从未有效或主动清空时 null，超时 lost 时可保留年龄供诊断 |
| `status`, `reason` | 定位状态及诊断原因；valid 的 reason 为 null |
| `aruco_ms` | 检测、几何计算和状态处理耗时 |

额外 ID 会记录到 markers，但不参与 H。手部 `inference_ms` 保持原有含义，只计算手部推理链路。未修改 ONNX 模型或模型包装层。本机已实际确认固定的 `opencv-python==4.12.0.88` 自带 `cv.aruco.ArucoDetector`，requirements 不变，也不安装会覆盖 cv2 命名空间的 opencv-contrib-python。

### JSON 与录制示例

[完整结构示例](docs/schema_v2.example.json) 使用合成 Marker 和示意手部节点，展示每只手的 21×2 数组，不是摄像头实测。以下是尚未定位时的字段节选（旧字段仍然保留）：

```json
{
  "schema_version": 2,
  "mirrored": true,
  "instrument": {
    "status": "lost",
    "dictionary": "DICT_4X4_50",
    "required_ids": [0, 1, 2, 3],
    "visible_ids": [],
    "markers": [],
    "homography_camera_to_instrument": null,
    "homography_instrument_to_camera": null,
    "homography_age_s": null,
    "reason": "missing_markers",
    "aruco_ms": 2.0
  },
  "hands": [{"landmarks_instrument_uv": null}]
}
```

有定位时 `landmarks_instrument_uv` 是按照原有 0–20 编号排列的 21×2 数组，例如食指尖读取 `hands[0]["landmarks_instrument_uv"][8]`。JSONL 记录完整 v2 包；CSV 在原有列末尾追加 `instrument_u`、`instrument_v`，valid/held 时填写，无坐标时为空，不覆盖原有 x/y/z 列。每次录制创建独立目录，避免不同会话表头混合。

### 人工验收

1. 打印并按上述布局把四个 Marker 平铺固定在板子上。启动系统，确认网页显示 `4 / 4`、IDs `0,1,2,3` 和 `VALID`，图像参考四边形连接正确。
2. 把食指尖放在参考区域中心附近，确认 U、V 约为 `(0.5,0.5)`；在边缘外确认可出现小于 0 或大于 1 的值。
3. 保持手和板子的相对位置，将二者一起左右、上下移动。图像归一化坐标应变化，UV 应基本稳定。
4. 整体旋转和适度透视倾斜，确认 ID 轴没有翻转，UV 仍基本稳定。Marker 中心按四角平均计算，较大透视和有限分辨率会带来小误差，不承诺绝对不变。
5. 短暂遮住任一 Marker：应 `VALID → HELD`；0.25 秒内恢复时重新 valid。持续遮挡应变成 `LOST`，UV 显示“—”、API 为 null、CSV 为空。
6. 再露出四个 Marker：自动恢复 valid，无需重启；在默认镜像和 `--no-mirror` 模式中分别确认同一物理位置的 UV 一致。

2026-10-09：19 项自动化测试通过（保留原 3 项手部测试）；pip check 通过。新增测试全部使用动态生成的 Marker，不依赖摄像头或外部图片，覆盖四角和内部点、平移、90°/180°旋转、透视、Marker 自转、额外/重复 ID、退化布局、保持与超时、恢复、镜像一致性、越界 UV、CSV 和实际 HTTP/录制循环。

本次硬件运行检查只枚举到 **USB Camera**，未检测到 C270。在 640×480 下采样 70 个不同帧包，平均处理速度约 **25.17 FPS**，ArUco 平均 **2.475 ms**；期间最多识别到一只手。无实体 Marker 入镜，仪器状态正确为 lost。这不是四 Marker 实板精度验证；真实贴板后的上述人工步骤仍待完成。四 Marker 合成图像预热后 100 帧的 ArUco 平均耗时约 2.08 ms。详细本机记录为 `outputs/aruco_verification_20261009.json`，不提交摄像头数据。随后在实时画面检测到实体板，11 个四 Marker 齐全的抽样帧均因 ID 连线自交而被拒绝（`non_convex_or_crossed_layout`）；界面已显示具体排布提示。尚未完成正确贴标后的有效 UV 与运动稳定性人工验收。

### 2026-10-09 追加实板复测

再次运行全部 19 项测试和 pip check，均通过。USB Camera 实板测试已获得 valid Homography 和食指 UV；对所有采样帧验证了 21×2 有限值、镜像还原后的投影一致性、held 不超过 0.25 秒以及 lost 时矩阵/UV 置空，无错误。

两段采样分别包含 342 帧和 227 帧，其中 valid 为 24 帧和 19 帧，平均约 23.30 / 23.66 FPS；第一段 held 74 帧、lost 244 帧。检测丢失集中在 ID 0 和 ID 3，当前安装条件下尚不能视为稳定定位。原始画面可见白边较窄和手部遮挡；同一批 70 张原始帧的阈值/窗口对比未显示改善，因此未改变检测参数，也未放宽超时或几何校验。请使用脚本生成的完整白边图案，避免遮挡后再验收。现有实测不能量化证明手板一起移动时 UV 的稳定性，仍需固定相对位置重新测试。

原始帧与节点采样仅保留在本机被忽略的 `outputs/retest_20261009/`，不纳入仓库。

## 文件与环境

- `app.py`：摄像头线程、本地 HTTP 服务、录制。
- `hand_tracking.py`：官方模型调用、原有节点结构、短时 ID 关联、绘制，保持不变。
- `aruco_tracking.py`：Marker 检测、几何校验、定位状态、Homography 和显示叠加。
- `instrument_coordinates.py`：原始/镜像像素转换、二维投影、schema v2 融合。
- `generate_markers.py`：可选离线 Marker PNG 生成，不是正常运行的依赖。
- `tests/test_aruco.py`、`tests/test_service.py`：合成几何测试及服务/录制集成测试。
- `web/index.html`：实时可视化、二维节点表、XY/XZ 三维投影。
- `models/`：两份完整的浮点 ONNX 模型。
- `vendor/opencv_zoo_runtime/`：项目所需官方源代码、来源说明及 Apache-2.0 许可证。
- `requirements.txt`：固定 OpenCV 4.12.0.88 / NumPy 2.2.6 / cv2-enumerate-cameras 1.3.3。
- `.venv/`：Python 3.12 独立环境。
- `outputs/server.stderr.log`：运行日志；`outputs/server.pid`：启动时的进程号。

如需重建环境：安装 Python 3.12 后，执行 `powershell -ExecutionPolicy Bypass -File .\setup.ps1`，再双击 start.cmd。已下载模型会通过 SHA-256 校验后复用，部署完成后运行无需网络。

官方源码固定版本：`47534e27c9851bb1128ccc0102f1145e27f23f98`。

- [手掌检测模型](https://github.com/opencv/opencv_zoo/tree/47534e27c9851bb1128ccc0102f1145e27f23f98/models/palm_detection_mediapipe)
- [手部关键点模型](https://github.com/opencv/opencv_zoo/tree/47534e27c9851bb1128ccc0102f1145e27f23f98/models/handpose_estimation_mediapipe)

模型 SHA-256：

```text
palm_detection_mediapipe_2023feb.onnx
78ff51c38496b7fc8b8ebdb6cc8c1abb02fa6c38427c6848254cdaba57fcce7c
handpose_estimation_mediapipe_2023feb.onnx
db0898ae717b76b075d9bf563af315b29562e11f8df5027a1ef07b02bef6d81c
```

仅对两个官方包装类的模型加载行做本地兼容修改：`readNet(path)` 改为 `readNetFromONNX(np.fromfile(path, dtype=np.uint8))`，解决 Windows 中文路径；推理与模型后处理数学逻辑保持官方版本。`download_models.py` 会检查并应用此补丁。

## 验证

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m pip check
```

测试照片不随仓库提交。可先运行以下命令下载（仅入站下载公开测试图片）：

```powershell
New-Item -ItemType Directory -Force outputs | Out-Null
Invoke-WebRequest https://storage.googleapis.com/mediapipe-tasks/hand_landmarker/woman_hands.jpg -OutFile outputs/test_hands.jpg
```

回归测试使用 [Google 官方示例中的双手照片](https://github.com/google-ai-edge/mediapipe-samples/blob/main/examples/hand_landmarker/python/hand_landmarker.ipynb)，本机已保存为 `outputs/test_hands.jpg`。测试涵盖两只手、21×3 有限坐标、归一化一致性、连续帧 ID、无手清空和非镜像模式。重建时可从 `https://storage.googleapis.com/mediapipe-tasks/hand_landmarker/woman_hands.jpg` 下载到该路径。

2026-09-18 原始手部版本的实测报告位于 `outputs/verification_report.json`。验证得到 3,515 帧、其中双手帧 1,514 帧，CSV 共 99,813 行；平均处理速度约 25 FPS，双手帧约 22 FPS，3 项回归测试通过。`outputs/` 整体不提交，摄像头录制、测试照片、截图、日志及验证原始数据仅保留在本机。新克隆可按上述步骤重新验证。

## 常见问题

- 长时间启动中：C270 的 MSMF 初始化约 30 秒；检查运行日志。若始终无画面，关闭占用摄像头的软件、重新插拔后重启程序。
- 显示 4/4 但 LOST：先查看页面的布局诊断。0 与 3 应位于对角，1 与 2 应位于对角；改变 Marker 自身旋转角度不能修复 ID 位置排错。不要按镜像网页的绝对左右重新定义 ID。
- 左右手暂时跳变：保持手掌完整可见；模型在遮挡、手背和特殊角度下仍可能误判。
- 帧率降低：当前逐帧进行手掌检测与关键点推理，默认 CPU、640×480；双手推理比单手慢，录制也有少量开销。
- 浏览器连接断开：检查后台进程与日志；重新运行 start.cmd。端口被其他应用占用时可通过 start.ps1 的 `-Port` 参数更换。
