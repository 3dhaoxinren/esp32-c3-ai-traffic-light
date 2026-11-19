# AI 指示灯 3.0.0 · Nano / ESP32-C3 统一版

## Windows 便携版

在 [v3.0.0 Release](https://github.com/3dhaoxinren/esp32-c3-ai-traffic-light/releases/tag/v3.0.0) 下载 `AI-Lamp-3.0.0-win-x64.zip`。适用于 Windows 10 / 11 x64。**完整解压**，双击 `AI-Lamp.exe`；不要单独移动 EXE，旁边的 `_internal` 是运行库。不需要安装 Python。界面使用系统浏览器，关网页不退出后台，退出请用 Windows 托盘菜单。首次使用登录启动、自动连接、跟随请在「AI指示灯」页保存开关。

- **灯控调试**：USB 选自己的 Nano/ESP32-C3 串口；Wi-Fi 可点「搜索局域网灯板」或手填 IP。搜索只检查本电脑的私有直连网段（最多两个 /24），验证 TCP 8080 的灯控回执，不自动建立控制连接、不修改灯态。发现灯板后仍需点击「连接灯控」。用自定义端口的旧灯请手填。跨网段、访客隔离或防火墙阻止时不一定能发现。
- **C3 音频**：本地文字合成、导入、波形选段、电脑试听、USB 读回、USB/Wi-Fi 推送、音量和按键曲目数。空闲、运行、等待、报错、完成、离线可分别绑不同编号；续期不重播。灯内读回与分状态绑定需要 [本仓库新版固件](../C3_MaxPreview)。
- **附近 Wi-Fi**：C3 音频页展开 USB 配网，可以搜索并填入 Windows 看得到的网络；只填名称，不读取或自动填入密码。C3 需要 2.4 GHz。搜索受网卡和系统位置权限限制，也可以手动输入。
- **高级 AT**：默认隐藏，需手动展开。Nano 接线页不再显示，Nano USB 和旧灯 Wi-Fi 灯控仍可用。AT、透传、OLED 等旧设备相关功能保留。
- 一次只接管一块灯板。若还在运行旧版，请从旧版托盘退出，再启动统一版。统一版不会调用旧 EXE。

常用录音格式尝试由系统浏览器解码；特殊格式可另装 FFmpeg 并放进 PATH。文字合成是 Windows SAPI，中文需系统已安装中文语音。部分 Nano 需要 CH340 驱动。发布包不是烧录工具，不包含设备配置、音频或固件备份。

## 从源码运行和复现

在本目录运行：

```powershell
python -m pip install -r requirements.txt
python server.py
```

要复现 Windows 便携版，安装 PyInstaller 6.21.0 后执行：

```powershell
.\build-windows.ps1 -OutputDirectory .\dist -WorkDirectory .\build\pyinstaller
```

软件回归：`python -m unittest discover -s tests -p "test_*.py"`。固件和 TCP 实验需用 [仓库的 C++ 主机测试桩](../tests/host)，设置 `FIRMWARE_HOST_QA` 指向测试程序。运行离线包检查：`AI-Lamp.exe --self-test report.json`（生成回执，只在演示模式检查本机页面、托盘资源和音频裁切，不接触灯板）。

后台只监听 127.0.0.1。接入第三方 AI 可使用 `ai_signal.py` 或 `claude_hook.py`，也可从命令提示符运行 `AI-Lamp.exe --signal running --source custom-ai --session task-1`。
