# OpenCV Zoo 手部节点实验台

本项目基于 OpenCV Zoo 的手掌检测与手部关键点模型，首次验证使用 Logitech C270。原始本地部署位置为 `D:\智能系统`，也支持克隆到其他目录。完成摄像头采集、双手关键点提取、骨架可视化和节点数据导出。本阶段不包含音高映射、拨弦判定或音频合成。

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

运行 `.\.venv\Scripts\python.exe example_client.py` 可以持续读取食指尖坐标，Ctrl+C 结束客户端。

| 字段 | 含义 |
| --- | --- |
| `schema_version` | 当前为 1 |
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
| `landmark_names`, `connections` | 节点名和骨架连接关系 |

二维坐标原点在左上，x 向右、y 向下；边缘外预测可能小于 0 或大于 1，不进行截断。z 越小通常表示越靠近摄像头，**不是手到摄像头的距离**。世界坐标也不是经相机/板子标定的场景绝对坐标；不同手的局部原点不能直接用于测量双手间距离。

后续动作判断应只消费 `status == running`、时间戳足够新、`frame_id` 未重复的包，使用时间戳计算速度。示例客户端采用 0.5 秒过期阈值。HTTP 返回最新帧，低频轮询可能跳帧；JSONL 录制包含每个处理帧（包括空手帧），CSV 每帧每只手每节点一行，无手帧没有 CSV 节点行。

节点编号：0 手腕；1–4 拇指；5–8 食指；9–12 中指；13–16 无名指；17–20 小指。指尖为 4 / 8 / 12 / 16 / 20。

目前保留原始关键点，不做平滑、不推测遮挡后运动。track_id 使用 0.5 秒失踪保留和 0.2 归一化距离门限；交叉、遮挡、快速移动时可能换 ID，不应将它视为永久身份。握板时遮挡关键点仍会降低可靠性；板子坐标标定、滤波和动作判定留待下一阶段。

## 文件与环境

- `app.py`：摄像头线程、本地 HTTP 服务、录制。
- `hand_tracking.py`：官方模型调用、输出结构、短时 ID 关联、绘制。
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

原始机器上的实测报告位于 `outputs/verification_report.json`。验证得到 3,515 帧、其中双手帧 1,514 帧，CSV 共 99,813 行；平均处理速度约 25 FPS，双手帧约 22 FPS，3 项回归测试通过。`outputs/` 整体不提交，摄像头录制、测试照片、截图、日志及验证原始数据仅保留在本机。新克隆可按上述步骤重新验证。

## 常见问题

- 长时间启动中：C270 的 MSMF 初始化约 30 秒；检查运行日志。若始终无画面，关闭占用摄像头的软件、重新插拔后重启程序。
- 左右手暂时跳变：保持手掌完整可见；模型在遮挡、手背和特殊角度下仍可能误判。
- 帧率降低：当前逐帧进行手掌检测与关键点推理，默认 CPU、640×480；双手推理比单手慢，录制也有少量开销。
- 浏览器连接断开：检查后台进程与日志；重新运行 start.cmd。端口被其他应用占用时可通过 start.ps1 的 `-Port` 参数更换。
