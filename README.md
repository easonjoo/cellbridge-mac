# DJiPhone Kit

让 Mac 插上大疆 QDC507 4G 模块，变成一台真正的"蜂窝版 Mac"：收发短信、拨打电话、实时网速与流量统计，外加 macOS 菜单栏常驻信号/网速显示。

无需内核驱动、无需 `.kext`，纯 Python + pyusb 直接操作 USB 端点；内置 macOS 原生 App（pywebview）与手机端 PWA，底部四 Tab（状态 / 短信 / 通话 / 更多）交互。

## ✨ 功能特性

- 📨 **短信收发**：文本模式收发、会话式聊天视图、已发短信持久化留存
- 📞 **语音通话**：拨号 / 接听 / 挂断 / DTMF 按键、通话记录（含时长）、来电浮层
- 📊 **实时网速**：基于基带计数器 `AT+QGDCNT` 的真实上下行速率 + 折线图
- 📈 **流量统计**：今日 / 本月 / 模块累计消耗，可一键清零
- 🖥 **macOS 菜单栏**：常驻信号格数 + ↓↑ 实时网速，菜单含今日/本月流量
- 📱 **手机端 PWA**：局域网内 iPhone/iPad 通过浏览器访问，iOS 原生风 UI，支持 PIN 鉴权
- 🛰 **GPS 定位**：开关模块 GPS、读取经纬度并在地图展示
- ⌨️ **AT 控制台**：内置 AT 指令调试台，附常用快捷指令
- 🔄 **自动重连**：模块拔插后自动恢复

## 🔧 硬件要求

- **大疆（DJI Cellular / 百旺 QDC507）4G 模块**
  - `VID=0x2CA3, PID=0x4006`，固件实测 `QDC507GLEFM21`
  - AT 指令端口为 **Interface 2**，端点 `0x03`（写）/ `0x84`（读）
- 一张可用的 SIM 卡
- macOS（Intel / Apple Silicon 均可）

> ⚠️ 其他型号模块的 `VID/PID/接口号/端点` 可能不同，需调整 `sms_server.py` 顶部的常量。

## 🚀 快速开始

### 方式一：源码运行

```bash
# macOS 安装 libusb（pyusb 依赖的系统库）
brew install libusb

# 安装 Python 依赖
pip install -r requirements.txt

# 启动（会自动打开浏览器）
bash start_web.sh
```

### 方式二：打包为 App

```bash
bash build_app.sh   # 生成 ~/Applications/DJiPhone Kit.app
```

浏览器打开 <http://localhost:8080>（手机端 <http://<Mac 的 IP>>:8080/m），即可收发短信、拨打电话。

## 📁 项目结构

```
DJiPhone Kit/
├── sms_server.py          # Flask 后端核心（USB 通信 + 短信 + 通话 + 流量 + GPS + 局域网鉴权）
├── app.py                 # pywebview 桌面壳（加载本地 UI + 菜单栏状态项）
├── menubar.py             # macOS 菜单栏常驻项（pyobjc NSStatusItem：信号格 + 网速）
├── index.html             # 桌面端前端（底部四 Tab，macOS 原生设计语言）
├── mobile.html            # 手机端 PWA（底部四 Tab，iOS 原生设计语言）
├── voice_runtime.py       # 通话音频辅助
├── build_app.sh           # 一键打包 macOS App 脚本
├── sms_tool.py            # 命令行短信工具（status/send/list/read/delete）
├── probe_eg25g.py         # USB 设备探测脚本
├── diagnose_network.py    # 4G 网络诊断脚本
├── fix_network.py         # 4G 网络修复脚本（重启数据连接）
├── PROCESS.md             # 开发过程记录（含固件踩坑）
├── requirements.txt       # Python 依赖
└── LICENSE
```

## ⚙️ 工作原理

macOS 缺少 CDC-ACM 串口驱动，无法识别模块的 vendor-specific（class `0xFF`）接口。本项目通过 **pyusb + libusb 直接读写 USB bulk 端点**，在用户态发送 AT 指令，完全绕过内核驱动：

```
枚举 USB 设备 → 找到 AT 指令接口 → write 端点发 AT → read 端点读响应
```

后台用 Flask 包一层 REST API，前端通过 HTTP 轮询实现实时交互；流量统计直接读模块基带计数器 `AT+QGDCNT`（注意：该固件返回的是**字节**而非文档标注的 KB）。

## 🔌 开启语音通话（USB 音频）

模块的 USB 音频接口**默认关闭**，需先开启一次（配置会持久化）：

```python
AT+QCFG="usbcfg",0x2CA3,0x4006,1,1,1,1,1,0,1   # 最后一个参数 audio=1
AT&W                                            # 保存
AT+CFUN=1,1                                     # 重启模组生效
```

重启后 macOS 会识别出 USB 音频设备（8000Hz 电话音质），通话时在「系统设置 → 声音」选择即可。

## 🐛 常见问题（踩坑记录）

更多固件怪癖与调试记录见 [PROCESS.md](./PROCESS.md)。

### 1. 模块能拿到 IP，但 Mac 上不了 IPv4

EG25-G 的 RNDIS/ECM 网卡 DHCP 服务可能卡死。给 Mac 网卡配静态 IP 绕开 DHCP，或运行 `fix_network.py` 重启模块数据连接。

### 2. 挂断后仍显示「通话中」/ 无法重拨

固件会残留空号码的 `active` 状态（phantom call），且挂断后 2 秒内仍报 `dialing`。后端已加入 phantom call 过滤 + 挂断冷却期双重保护。

### 3. AT+QGPS=0 关不掉 GPS

该固件返回 `+CME ERROR: 501`，属固件行为；开启（`AT+QGPS=1`）与读数（`AT+QGPSLOC?`）均正常。

## 🙏 致谢

本项目站在以下优秀项目的肩膀上，感谢这些作者的开源分享：

| 项目 | 作者 | 借鉴内容 |
| --- | --- | --- |
| [DJOneHub](https://github.com/ZenGeekLabs/DJOneHub) | ZenGeekLabs | 模块 USB 模式识别与设置面板设计思路 |
| [CellDock](https://github.com/celldock/celldock-for-mac) | celldock | 菜单栏状态项、短信/通话交互设计参考 |
| [NasAnySim](https://github.com/mccding/NasAnySim) | @mccding | 移动端 PWA UI 风格与自托管蜂窝网关思路 |
| [mac-4g-modem](https://github.com/zxd-sudo/mac-4g-modem) | zxd | pyusb 用户态 AT 通信的最初实现基础 |

同时感谢社区里所有研究 QDC507 / VoHive / MaVo 生态的玩家，你们公开的踩坑记录让后来者少走了很多弯路。

## 🤝 贡献

欢迎提交 Issue 和 Pull Request！提交前请确认不要包含任何个人隐私数据（`call_history.json`、`sent_sms.json` 等运行时数据已被 `.gitignore` 排除）。

## ⚖️ 免责声明

本项目是**独立开发的非官方开源项目**，**未获得 DJI 的授权、赞助或认可**，与 DJI、Quectel、运营商或 eSIM 设备厂商**不存在隶属或合作关系**。

DJI 及相关产品名称是其各自权利人的商标，**仅用于说明兼容**，不代表任何商业关联或背书。请遵守当地法律法规使用短信/通话功能，录制通话前请征得对方同意。

## 📄 License

[MIT](./LICENSE)
