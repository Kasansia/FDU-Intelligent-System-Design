# 真实乐器音源接入路线

当前使用原始加法合成测试音，不含第三方录音。音乐映射在 `music.py`，输出
`mode/tonic/degree/notes_midi/duration_ms/direction/velocity`；视觉线程只提交事件。
`SynthSoundBank.get_sound(event)` 是预加载 PCM 音源查找接口，`AudioEngine` 管理
有界队列、去重、配置版本、复音与播放。后续音源应保持这些边界。

## WAV 采样：建议的第一步

可以为每个完整和弦准备 WAV，也可以按单音采样。双模式和 12 调需要灵活组合，
建议用单音采样，以 MIDI 音高为索引，启动时加载、统一采样率并去除头部静音，
播放时组合和弦。缺少音高可做移调；跨度大时应增加采样以减少音色失真。
可继续使用 sounddevice 混音输出，不要每扫一次启动外部播放器或临时读文件。

Python SoundFile 可将 WAV 等格式读为 NumPy 数组：[官方文档](https://python-soundfile.readthedocs.io/)。
后续增强包括多力度层、同音轮换采样、释放音、立体声与不同拨弦技法。

## SF2 / SoundFont

FluidSynth 可以通过 MIDI 音符驱动 SoundFont，支持 Windows、macOS、Linux，
提供可嵌入其他程序的共享库：[官方介绍](https://www.fluidsynth.org/)。
适合需要更换多种乐器或部署到 Linux 的路线。工程上仍需测量目标机器延迟，
并实现音符关闭和切换时的全部停音。

## NKI / Kontakt

`.nki` 是 Kontakt 乐器文件，不是普通 PCM 音频。需要 Kontakt 加载乐器及关联采样，
再发送 MIDI 触发：[Kontakt 乐器及 MIDI 文档](https://docs.native-instruments.com/ni-tech-manuals/kontakt-manual/en/classic-view)。

推荐先使用外部宿主路线：

`本程序 → MIDI 输出 → 虚拟 MIDI 端口 → Kontakt 独立程序或 DAW 内 Kontakt → 声卡`

Windows 可用 [loopMIDI](https://www.tobias-erichsen.de/software/loopmidi.html)连接应用。
输出适配器需要调度 Note On / Note Off、力度和扫弦音序；切换模式、调性、设备或
退出时停止旧音符。特定吉他音色还可能要求 keyswitch、专用扫弦触发键或 MIDI CC，
应按所选音色库说明映射，不能假定所有 NKI 接口相同。

免费 Kontakt Player 是否适用取决于音色库；部分第三方库需要完整版 Kontakt：
[官方说明](https://support.native-instruments.com/support/solutions/articles/69000879739-third-party-kontakt-libraries-licensed-vs-unlicensed)。
若以后要求程序内嵌 Kontakt/VST3，还需实现插件宿主、音频时钟和插件生命周期，
复杂度高于外部 MIDI 路线。

本轮仅实现模拟音源和通用事件接口；未安装 MIDI 驱动、Kontakt 或第三方音色库。
