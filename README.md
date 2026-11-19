# 4 元玩具红绿灯 → AI 任务指示灯

## Windows 统一版下载

- [3.1.0 Windows x64 ZIP 便携版（不需要 Python）](https://github.com/3dhaoxinren/esp32-c3-ai-traffic-light/releases/download/v3.1.0/AI-Lamp-3.1.0-win-x64.zip)
- [3.1.0 完整更新包（Windows + ESP32-C3 固件）](https://github.com/3dhaoxinren/esp32-c3-ai-traffic-light/releases/download/v3.1.0/AI-Lamp-3.1.0-update-kit.zip) · [更新与配对说明](UPGRADING.md)
- [Release 说明、校验文件](https://github.com/3dhaoxinren/esp32-c3-ai-traffic-light/releases/tag/v3.1.0) · [统一版源码、局域网搜索和使用说明](desktop-unified/README.md)

统一版支持 Nano 和 C3，保留托盘、自启、自动跟随与 C3 分段播报。AT 默认隐藏；Nano 接线页已取消。可搜索附近 Wi-Fi 和局域网灯板。下载后完整解压，双击文件夹内的 `AI-Lamp.exe`。


这是视频里 **ESP32-C3 + MAX98357A + 单只 1609 铝膜喇叭** 的固件源码，以及后续重做的 AI 指示灯桌面客户端。原玩具的外壳、灯和顶部按键保留；改造前由纽扣电池供电，没有 USB 口，改造后才从 C3 的 USB 供电。

- 红灯快闪＝报错；黄灯呼吸＝运行中；绿灯常亮＝空闲。
- C3 接收主机发来的状态指令，不是在 C3 上运行 AI，也不自动监控桌面软件。
- 可以按原键播放已存入的语音；本仓库不含语音素材和旧工作台。
- 出厂默认 **7 档**，桌面端保存的新档位会跨重启保留。7 / 8 / 9 档分别输出原 PCM 振幅的 50% / 75% / 100%。9 档只是数字信号不衰减，出现破音就退一档。

## 接线

下面的原板点位只对应视频中那块 **HY-035**。先拆电池及原黑色八脚芯片，保留焊盘；按视频中原板照片朝向区分 A/B/C/D、K1/K2，其他板子不要照搬。

| 原板 / 外设 | 接到 |
| --- | --- |
| 旧芯片上排焊盘 A | 留空 |
| 旧芯片上排焊盘 B / C / D | 各串 **220 Ω**，依次接 C3 GPIO5 / GPIO6 / GPIO7 |
| 顶部左 K1 / 右 K2 | C3 GPIO10 / GND；断电确认按下近 0 Ω |
| 原电池 B+ / B− | 留空，不接 C3 |
| 原喇叭的两根黄色 SPK 线 | 脱焊，不复用 |
| C3 开发板 5V / GND | MAX98357A VIN / GND |
| GPIO4 / GPIO0 / GPIO3 | MAX98357A BCLK / LRC / DIN |
| 单只 1609 的红线 / 黑线 | MAX98357A OUT+ / OUT−；**任一喇叭端均不接地** |

## 编译、上传和串口验证

源文件入口是 [C3_MaxPreview/C3_MaxPreview.ino](C3_MaxPreview/C3_MaxPreview.ino)。使用 [Arduino-ESP32](https://docs.espressif.com/projects/arduino-esp32/en/latest/installing.html) **3.3.12**，板型 ESP32C3 Dev Module，Flash 4 MB、DIO、默认分区、USB CDC 开机启用。仅对确认是你这块 C3 的串口执行上传。已有设备若更换分区或清空 Flash，先备份。

```sh
arduino-cli core install esp32:esp32@3.3.12 --additional-urls https://espressif.github.io/arduino-esp32/package_esp32_index.json
arduino-cli compile --fqbn esp32:esp32:esp32c3:CDCOnBoot=cdc,FlashSize=4M,FlashMode=dio,PartitionScheme=default --export-binaries C3_MaxPreview
arduino-cli board list
```

可以在 Arduino IDE 内选择 C3 串口上传。首次连接用 115200、换行结尾，经 USB 串口逐行发送：

```text
LIGHT STATUS
LIGHT GREEN
LIGHT AI RUNNING
PCM HELLO
PCM STATUS
```

AI 状态有约 15 秒续期窗口，需要持续显示时由主机定期重发状态；单纯试灯用 `LIGHT RED`、`LIGHT YELLOW`、`LIGHT GREEN` 或 `LIGHT OFF`。语音需事先灌入 **16 kHz、16-bit signed little-endian、单声道原始 PCM（不带 WAV 文件头）**。1 号是完成播报，顶部按键默认从 2～4 号的已配置范围随机选；只存 2 号时，通过串口发 `SOUND COUNT 1`。可以用状态指令确认功放已初始化，不需要安装旧工作台。Wi‑Fi 在没有写入网络配置时不加入网络，建议入门先走 USB；局域网控制无认证，不要映射到公网。

## 旧版 C3 专用桌面端（保留源码）

需要 Python 3.10+；建议 Python 3.12。安装依赖后，从仓库根目录启动：

```powershell
python -m pip install -r desktop/requirements.txt
python -m desktop.app
```

1. **连接**：用 C3 原生 USB 直连。「通过 USB 配 Wi-Fi」把 2.4 GHz Wi-Fi 配置写入灯里；获得 IP 后可切到 Wi-Fi。支持 USB 和可信同网段 Wi-Fi 推送、试听和绑定；灯内音频读回只允许物理 USB。
2. **准备音频**：输入文字用 Windows 本地 SAPI 生成（中文需已安装中文语音），或者导入录音。标准 16 kHz 单声道 WAV 不需要 FFmpeg，MP3 等格式需装 FFmpeg。导入源最长 5 分钟，每段推送上限 32 秒；还可填写编号，通过 USB 读回已存音频后再剪。
3. **波形剪辑**：拖波形或输入起止时间，先试听选段，再按编号加入分段；一批可包含多段，也可将选段直接绑定某个状态。推送会替换相同编号的语音。
4. **分状态播报**：空闲、运行、等待、报错、完成、离线分别绑定。编号为 0 不播；状态真正变化才播一次，续期不重播。绑定保存在设备上。更新到本仓库固件后才支持状态绑定、完成状态和 USB 音频读回。

### 主机状态示例

```text
SOUND STATE RUNNING 42
SOUND STATE DONE 43
SOUND STATE STATUS
LIGHT AI RUNNING
LIGHT AI DONE
```

`LIGHT AI DONE` 对应绿色常亮，播报编号可与 `IDLE` 分开配置。兼容原 `SOUND FINISH`，它仍显式播放曲目 1。设备并不会自行发现电脑上 AI 任务的开始和结束，由应用或主机发送状态事件。

### 验证源码

`python tests/test_integration.py` 可测试采样点剪辑。其余测试使用同一份 C++ 固件源码的主机测试入口，模拟 GPIO、文件系统/I²S 与串口并跑本机 TCP 回环；需要 C++17 编译器：

```sh
c++ -std=c++17 -Itests/host -IC3_MaxPreview tests/host/main.cpp -o indicator-host
FIRMWARE_HOST_QA="$PWD/indicator-host" python tests/test_integration.py
```

Windows 可用 Zig 或 MinGW 生成 `indicator-host.exe`，再用 PowerShell 的 `$env:FIRMWARE_HOST_QA` 指定其绝对路径。

项目原创源码按 [MIT](LICENSE) 许可发布。构建时下载的 Arduino-ESP32、ESP-IDF 等依赖保留各自许可证。
