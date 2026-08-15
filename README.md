# mac-4g-modem

让 Mac 插上 4G 模块，就能收发短信、打电话、监控网速与流量的本地 Web 应用。

无需安装内核驱动，无需 `.kext`，纯 Python + pyusb 直接操作 USB 端点，浏览器打开即用。

## ✨ 功能特性

- 📨 **短信收发**：文本模式收发、已发短信持久化留存、按号码分组对话视图
- 📞 **语音通话**：拨号 / 接听 / 挂断 / DTMF 按键（需开启模块 USB 音频）
- 📋 **通话记录**：拨出 / 接入 / 未接，带通话时长，类手机通话记录逻辑
- 📊 **实时网速**：上下行速率实时监控 + 折线图
- 📈 **流量统计**：累计 / 今日 / 本月消耗，可清零
- 🔄 **自动重连**：模块拔插后自动恢复

## 🔧 硬件要求

- **Quectel EG25-G** 模块（或兼容的 EG2x / EC2x 系列）
  - 本项目开发测试使用「百旺 QDC507」，`VID=0x2CA3, PID=0x4006`
  - AT 指令端口为 **Interface 2**，端点 `0x03`（写）/ `0x84`（读）
- 一张可用的 SIM 卡（测试使用中国电信）
- macOS（本项目的核心场景；理论上 Linux 也可运行）

> ⚠️ 其他型号模块的 `VID/PID/接口号/端点` 可能不同，需调整 `sms_server.py` 顶部的常量。

## 🚀 快速开始

### 1. 安装依赖

```bash
# macOS 安装 libusb（pyusb 依赖的系统库）
brew install libusb

# 安装 Python 依赖
pip install -r requirements.txt
```

### 2. 启动服务

```bash
DYLD_LIBRARY_PATH=/opt/homebrew/lib python3 sms_server.py
```

或直接运行启动脚本（会自动打开浏览器）：

```bash
bash start_web.sh
```

### 3. 使用

浏览器打开 <http://localhost:8080>，即可收发短信、拨打电话。

## 📁 项目结构

```
mac-4g-modem/
├── sms_server.py          # Flask 后端核心（USB 通信 + 短信 + 通话 + 网速 + 流量）
├── index.html             # 前端单页应用（毛玻璃风格 UI）
├── sms_tool.py            # 命令行短信工具（status/send/list/read/delete）
├── sms.sh                 # CLI 快捷入口
├── probe_eg25g.py         # USB 设备探测脚本
├── diagnose_network.py    # 4G 网络诊断脚本
├── fix_network.py         # 4G 网络修复脚本（重启数据连接）
├── probe_data_usage.py    # 流量统计能力探测
├── test_call_manager.py   # 通话状态机测试
├── start_web.sh           # 一键启动脚本
├── requirements.txt       # Python 依赖
└── LICENSE
```

## ⚙️ 工作原理

macOS 缺少 CDC-ACM 串口驱动，无法识别 EG25-G 的 vendor-specific（class `0xFF`）接口。本项目通过 **pyusb + libusb 直接读写 USB bulk 端点**，在用户态发送 AT 指令，完全绕过内核驱动：

```
枚举 USB 设备 → 找到 AT 指令接口 → write 端点发 AT → read 端点读响应
```

后台用 Flask 包一层 REST API，前端通过 HTTP 轮询实现实时交互。

## 🔌 开启语音通话（USB 音频）

EG25-G 的 USB 音频接口**默认关闭**，需先开启一次（配置会持久化）：

```python
AT+QCFG="usbcfg",0x2CA3,0x4006,1,1,1,1,1,0,1   # 最后一个参数 audio=1
AT&W                                            # 保存
AT+CFUN=1,1                                     # 重启模组生效
```

重启后 macOS 会识别出 2 个 USB 音频设备（8000Hz 电话音质），通话时在「系统设置 → 声音」选择即可。

## 🐛 常见问题（踩坑记录）

### 1. 模块能拿到 IP，但 Mac 上不了 IPv4

**现象**：浏览器能开网页（走 IPv6），大量 App 连不上（只依赖 IPv4）。

**原因**：EG25-G 的 RNDIS 网卡 DHCP 服务可能卡死，模块自己在 PDP 上下文里拿到了 IPv4，却没通过 DHCP 分给 Mac，Mac 网卡只剩 `169.254.x.x` 无效地址。

**解决**：给 Mac 网卡配静态 IP 绕开 DHCP，或运行 `fix_network.py` 重启模块数据连接。详见 `diagnose_network.py` 与 `fix_network.py`。

### 2. 挂断后仍显示「通话中」

模块固件会残留空号码的 `active` 状态（phantom call），且挂断后短时间仍报 `dialing`。后端已加入 phantom call 过滤 + 挂断冷却期 + 号码过滤三重保护。

### 3. 挂断后无法重拨

固件在挂断后 2 秒内仍报告 `dialing`，会覆盖本地 idle 状态。已通过 5 秒冷却期解决，拨号前会自动强制重置残留状态。

## 🤝 贡献

欢迎提交 Issue 和 Pull Request！任何改进、新功能、Bug 修复都很欢迎。

提交前请确认：
- 不要提交任何包含个人隐私的文件（`call_history.json`、`sent_sms.json` 等运行时数据已被 `.gitignore` 排除）
- 新功能请附带简要说明

## 📄 License

[MIT](./LICENSE) © 2026 zxd
