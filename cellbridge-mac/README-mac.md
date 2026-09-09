# CellBridge-mac — 让 iPhone 用蜂窝号码打电话/收短信（SIP）

把 [mccding/CellBridge](https://github.com/mccding/CellBridge) 的 Go 网关移植到 macOS，
配合 DJiPhone Kit 的模块语音运行时：iPhone 装 SIP 客户端（如 YakPhone），经局域网或
Tailscale 连到 Mac，即可用 SIM 卡号码收发短信、拨打/接听 VoLTE 电话。

## 组成

| 组件 | 作用 |
|---|---|
| `at_pty_bridge.py` | 把模块 USB AT 通道桥成 PTY 串口（CellBridge 只认 tty） |
| `voice-audio-bridge --fifo-*` | 蜂窝 UAC 音频 ↔ FIFO（s16le 8k mono，对应 raw-pcm 后端） |
| `cellbridge-gateway` | Go 编译的 SIP 服务器 + 短信引擎 + Web 控制面 |

## 前提

1. DJiPhone Kit 已完成 QADBKEY 解锁 + 模块语音运行时部署（语音路由 ready）。
   **刚重启过模块的话，先打开 DJiPhone Kit.app 让自愈线程部署一次再启动本栈。**
2. `cellbridge-gateway`（darwin/amd64）与 `voice-audio-bridge` 已编译就位：

```bash
# Go 1.25+：https://go.dev/dl/（Apple Silicon 用 darwin-arm64 包）
git clone --depth 1 https://github.com/mccding/CellBridge.git /tmp/CellBridge
cd /tmp/CellBridge/gateway
GOPROXY=https://goproxy.cn,direct go build -o cellbridge-gateway ./cmd/cellbridge-gateway
cp cellbridge-gateway /path/to/cellbridge-mac/

# 音频桥
cd /path/to/mac-4g-modem
swiftc -O voice_audio_bridge.swift -o cellbridge-mac/voice-audio-bridge
```

## 启动 / 停止

```bash
./start_cellbridge.sh        # 启动（自动退出 DJiPhone Kit App，两者互斥）
./start_cellbridge.sh stop   # 停止全部组件
```

启动后：

- **SIP**：`0.0.0.0:5060`，账号 `iphone` / `cellbridge-<用户名>`
  （可用环境变量 `SIP_USER` / `SIP_PASS` 覆盖）
- **控制台**：http://127.0.0.1:8787
- **日志**：`~/.cellbridge/run/logs/`

iPhone 端（YakPhone）配置：SIP 服务器填 Mac 的局域网 IP 或 Tailscale 地址 `:5060`，
用户名密码如上。默认 SMS dry-run=true（只入库不发送）；要真实发短信，在启动前
`export CELLBRIDGE_SMS_DRY_RUN=false`。

## 与 DJiPhone Kit 的关系

- **互斥**：App 与本栈都独占 USB AT 接口（interface 2），启动脚本会先退出 App。
- **依赖**：模块侧语音运行时（qdc507_aprv3.ko / qdc507_voice.ko / mavo-pcm-bridge）
  仍由 App 的自愈线程部署；本栈只消费它建好的 UAC 音频流与语音路由。
- 电话音频路径：蜂窝 → AC Interface → FIFO → 网关 → SIP/RTP → iPhone。

## 已验证（2026-09-09）

- AT PTY 桥：AT/CPIN?/CEREG? 透传正常，URC 可达
- 音频桥 FIFO 模式：tx 8kHz 持续流、rx 待通话激活
- 网关：SIP 5060 监听、modem probe 无告警、控制面 HTTP 响应
- 待真机验证：iPhone SIP 注册与实际通话
