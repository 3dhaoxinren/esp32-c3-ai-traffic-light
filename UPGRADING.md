# AI 指示灯 3.1.5 更新指南

## Windows 10 / 11 x64

完整解压 [3.1.5 Windows 更新包](https://github.com/3dhaoxinren/esp32-c3-ai-traffic-light/releases/tag/v3.1.5) 后，双击 **双击升级.cmd**。默认安装到 `%LOCALAPPDATA%\Programs\AI-Lamp`，备份该目录的原统一版组件、可识别的 2.7.0 程序及指向它的快捷方式，并迁移识别到的本项目开机自启项。不会清理其它 EXE、接线图或工具，保留连接设置和 C3 配对资料，不刷固件、不改已存录音。如果提示剪辑区有内容，先保存需要的剪辑，再从托盘退出并重试。

**需要回退时**：先从托盘退出新版。升级完成时会显示回退备份路径；里面的 **回退记录.json** 给出了每个旧文件的原位置和备份位置。确认路径后再还原；固件和用户设置不需要随桌面版本回退。

如需便携运行，从托盘退出旧版，再双击解压后 **Windows** 目录内的 **AI-Lamp.exe**，保留旁边的 **_internal**。若自启项指向另一个目录，请在新版重新保存；不需要安装 Python。

**这个更新包仅含 Windows 程序，Nano 和已经在用 v3.1.0 配套固件的 C3 都不需要刷机。** 以下固件步骤只供仍在使用更旧 C3 固件、且要启用局域网回读等设备侧功能的用户。所需镜像在 [v3.1.0 完整更新包](https://github.com/3dhaoxinren/esp32-c3-ai-traffic-light/releases/download/v3.1.0/AI-Lamp-3.1.0-update-kit.zip) 中。

## 已在使用的 C3：只更新程序区

适用于本项目 **ESP32-C3 / 4 MB Flash、默认分区布局、运行 app0** 的 MAX98357A 版本。不要给 Nano、其他芯片或不同分区的 C3 刷此镜像。不确定布局时请先按 [源码编译说明](README.md#编译上传和串口验证) 检查，不要直接写入。

以下命令在完整更新包的根目录执行。COMx 替换为确认过的 **C3 原生 USB** 串口；如果桌面程序占用该串口，先断开灯控。分区表和 OTA 初始选择必须都校验通过才继续：

~~~powershell
python -m esptool --chip esp32c3 --port COMx verify-flash 0x8000 firmware/fresh-install/partitions.bin 0xe000 firmware/fresh-install/boot_app0.bin
~~~

先自留备份，不要上传或公开；它可能包含 Wi-Fi 密码和录音。

~~~powershell
python -m esptool --chip esp32c3 --port COMx read-flash 0x0 0x400000 private-backup.bin
python -m esptool --chip esp32c3 --port COMx write-flash 0x10000 firmware/app.bin
python -m esptool --chip esp32c3 --port COMx verify-flash 0x10000 firmware/app.bin
~~~

只写 **app.bin**，不要执行整片擦除或重写分区表。这样会保留原分区中的录音、Wi-Fi、音量和绑定。**firmware/fresh-install** 里的其它镜像是给空白板的，不是老设备升级时要一起写入的。

## 空白 C3（不是老设备升级）

配套构建为 Arduino-ESP32 3.3.12、ESP32C3 Dev Module、USB CDC On Boot、DIO、4 MB、默认分区。只在确认是同规格空白板时使用以下全套镜像：

~~~powershell
python -m esptool --chip esp32c3 --port COMx write-flash --flash-mode dio --flash-freq 80m --flash-size 4MB 0x0 firmware/fresh-install/bootloader.bin 0x8000 firmware/fresh-install/partitions.bin 0xe000 firmware/fresh-install/boot_app0.bin 0x10000 firmware/app.bin
~~~

## 电脑试听 C3 已存录音

1. C3 与电脑在同一局域网。在「灯控调试」通过 Wi-Fi 连接 C3。
2. 首次给同一块 C3 接原生 USB 数据线，点「C3 音频」→「配对局域网回读」。灯控不需要从 Wi-Fi 切回 USB。
3. 配对成功后，只要 C3 继续供电，USB 数据线可拔。在「C3 已存录音」点 **电脑试听**，状态会标明「局域网回读」。换电脑需重新做一次 USB 配对。

「直接播报到 C3」和「电脑试听文字」都不占设备录音空间，也不改已有的状态绑定。Windows 离线语音不发送文字；选择在线神经音色并主动生成、试听或播报时，文字会发送到对应语音服务。
