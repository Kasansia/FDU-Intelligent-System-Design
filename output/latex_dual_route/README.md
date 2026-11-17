# MotionMuse 双路线阶段汇报

共 13 页，16:9，使用 XeLaTeX + Beamer。视觉参考为桌面的《汇报模板.pdf》：保留蓝白风格、校徽及封面斜切蓝色区域，内容和排版重建为可编辑 LaTeX。

## 修改文字

编辑 MotionMuse.tex。源码按 01–13 页分段标记。
- 封面团队成员、日期以及第 12 页负责人需要填写。
- 第 10 页训练数据和结果仍为预留，不应在获得实际数据前填写推测指标。
- 无标记路线区分当前传统视觉实现与后续四角模型方案。

## 添加图片

将 PNG 图片放入 figures 文件夹并使用下列文件名。重新编译后，已有文件自动替换占位框，图片保持比例，不裁切。暂缺图片仍显示占位框。

| 页码 | 文件名 | 建议内容 |
|---|---|---|
| 2 | 02-progress.png | 上阶段识别画面与当前工作台对照 |
| 3 | 03-architecture.png | 总体系统与两条定位路线 |
| 4 | 04-aruco.png | 标记角点、板面坐标、手指投影 |
| 5 | 05-performance.png | 七区工作台与模式控件 |
| 6 | 06-open.png | 实体模型展开 |
| 6 | 06-folded.png | 实体模型收纳 |
| 6 | 06-parts.png | 拆解与装配 |
| 8 | 08-markerless.png | 无标记四角初始化及定位 |
| 9 | 09-training-flow.png | 数据准备与训练流程 |
| 10 | 10-training.png | 实际训练曲线或指标 |
| 10 | 10-cases.png | 成功与失败案例 |

如果使用 JPG/PDF，在源码中对应的 figureplaceholder 调用处修改文件后缀即可。宽度随列宽自动适配，高度是该调用的第二个参数。全幅流程图适合横向图片，单栏大图建议约 3:2，三联图建议约 16:10。

## 编译

本机：在 PowerShell 中执行 `powershell -ExecutionPolicy Bypass -File .\build.ps1`。
脚本优先寻找 PATH 中的 xelatex，找不到则使用本机 D:/texlive/2025/bin/windows/xelatex.exe。

其他平台或 Overleaf：上传本目录文件，主文件选择 MotionMuse.tex，编译器选择 XeLaTeX。中文使用 TeX Live 自带 Fandol 字体，无需安装 Windows 字体。命令行连续运行两次 `xelatex MotionMuse.tex`。

## 来源与验证范围

- 排版参考及校徽资产：C:/Users/kehan/Desktop/汇报模板.pdf。仅有 PDF 参考，未使用其原始主题源码。
- ArUco 项目：D:/智能系统，origin 为 Loong-C/FDU-Intelligent-System-Design，基于 8abfcc5 及本地后续修改。
- 无标记项目：SMF-an/MotionMuse，cda1d54fd05f763aa1d6d902529ef4cf4082a220。
- 88 项测试来自 2026-11-08 既有验收记录。另一仓库 3 项合成测试在本次代码阅读时验证。
- 本演示稿不声明已经完成真人演奏验收、实体打印、板角模型训练或 Jetson 部署。

桌面交付的 PDF 可直接放映；文字及图示应在源码中修改后重新编译。

## 2026-11-11 实体模型补图

第 6 页已加入 Blender 原模型第 60、1、120 帧的新渲染图，依次为四角展开、圆片收纳和三板拆解。图示属于数字结构原型，不表示已经打印或试装。原 .blend 文件未修改。其余图示仍为占位。
