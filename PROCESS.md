# DjiPhone 开发日志（PROCESS.md）

> 本文件记录 DjiPhone（原 mac-4g-modem / 4G SMS Hub）的开发过程与决策，作为开源发布的一部分持续更新。

## 项目演进

| 阶段 | 内容 |
|---|---|
| Day 1 上午 | 部署 zxd-sudo/mac-4g-modem：libusb + pyusb 用户态驱动 EG25-G/QDC507，Flask Web 界面，短信/通话/流量监控 |
| Day 1 下午 | 用 pywebview 打包为原生 macOS App「4G SMS Hub」，配置 LaunchAgent 开机自启，AT 指令开启 USB 音频接口 |
| Day 1 晚 | SMS Hub 2.0：修复中文短信乱码（UCS2）、macOS 风格 UI、设置面板、通讯录（pyobjc Contacts）、局域网访问 + 二维码、PIN 保护 |

## 2026-09-04 晚 · DjiPhone 版本（本次）

### 目标
1. 更换用户提供的微信风格玻璃图标，App 更名 **DjiPhone**
2. 修复通话无声音 + 接通 15-18 秒自动挂断
3. 设置面板参考 DJOneHub-mac-enhanced 重做
4. 借鉴参考项目新增功能（见下方参考清单）

### 通话故障根因分析（研究阶段）

**现象**：接通后 15-18 秒自动挂断，全程无声音。

**线索链**：
- 本机 AT+COPS 返回 `CHN-UNICOM, AcT=7`（LTE）。中国联通 2G/3G 已退网，**不开 VoLTE/IMS 的模块无法完成 CSFB 语音回落** → 呼叫被网络释放，正好对应十几秒挂断
- DJOneHub-mac-enhanced 文档确认：双向通话依赖「模块侧语音运行时 + USB Audio + IMS/VoLTE」三要素；旧配置 `…1,1,1,1,1,0,1` 已有 USB Audio，需要「补 IMS/VoLTE」
- dji-4g-connect 源码给出可操作配方：读 `AT+QCFG="usbcfg"` 9 字段 → **末两位强制 1,1** → `AT+QCFG="ims",1` → `AT+CFUN=1,1` 重启；并指出部分固件 `AT+QPCMV?` 返回 ERROR，这类固件无法把通话媒体路由到 Mac（需模块侧运行时）
- 若 IMS 修复后 QPCMV 仍 ERROR：需 MaVo 模块侧运行时（`qdc507_aprv3.ko`/`qdc507_voice.ko`/`mavo-pcm-bridge.armv7`，经 ADB 装入模块），上游为 moluncn/mavo 固定 commit，留作后续阶段

**决策**：先实施 AT 层修复（IMS + usbcfg 末两位），在 App 内做「语音诊断」面板展示每一步状态；模块侧运行时作为后续里程碑。

### 参考项目（致谢与借鉴）

| 项目 | 借鉴内容 |
|---|---|
| [DJOneHub-mac-enhanced](https://github.com/rogerbush007-a11y/DJOneHub-mac-enhanced) | 设置面板结构、语音三要素分析、LaunchAgent 管理方式 |
| [dji-4g-connect](https://github.com/437609368/dji-4g-connect) | IMS + USB 音频配置 AT 配方、QPCMV 能力检测、通话轮询容错（语音承载建立时 AT 通道会短暂失联） |
| [MacCellular](https://github.com/yuexiazhuojiu-byte/MacCellular) | 远程语音 WebRTC/TURN 架构（路线图）、PWA 移动端思路 |
| [NasAnySim](https://github.com/mccding/NasAnySim) | NAS/Docker 部署思路（路线图）、AT 串口探测 |
| [CellDock](https://github.com/celldock/celldock-for-mac) | 短信转发 Bark/飞书/钉钉、转发自测 |
| [MaVo (moluncn/mavo)](https://github.com/moluncn/mavo) | 模块侧语音运行时的上游来源（未再分发，按需获取） |

### 变更记录
- （进行中）语音诊断/修复 API、App 更名 DjiPhone、新图标、设置面板重构、短信转发

## 2026-09-04 晚：ADB 死胡同定论 + 界面 3.1 改版

### ADB 结论（重要）
- `AT+QCFG="usbcfg",…,1,1,1,1,1,1,1` 写入返回 OK，**立即读回仍为 `1,1,1,1,1,0,1`**——ADB 位（倒数第 2 位）被固件硬锁为 0，与是否带 AT&W/重启无关；UAC 位（末位）可正常写 0/1。
- 对照 DJOneHub `module_setup.go`：其 `isLegacyUACTarget()` 恰好定义 `1,1,1,1,1,0,1` 为"旧 UAC 已具备通话音频"，并注明"部分 QDC507 固件确认但保留旧位布局，重写 ADB 位对通话音频无必要"。与我们实测一致。
- 模块侧 Linux（ECM 网卡 192.168.225.1）无任何开放 TCP 端口（22/23/5555/80 等全关），无法绕道网络装语音运行时。
- **定论：本台 QDC507（QDC507GLEFM21）无法安装模块侧语音运行时（MaVo），后续语音方案需走纯 UAC 路线或等待其他固件入口。**
- VoLTE 配置链路核实正常：`ims=1,1`、`volte_disable=0`、MBN `CU-VoLTE` 已激活（List index 13, flags=1,1）。上次通话释放码 `+CEER: 6,259`，待真实拨号复测。

### 界面 3.1
- 全局更名 **DJiPhone Kit**（窗口标题/页面标题/顶栏/Info.plist CFBundleName+DisplayName，bundle 路径仍为 ~/Applications/DjiPhone.app）。
- 顶栏 logo 换为图标本体（128px base64 内联，LAN 端 iPhone 也能看到），品牌标题改苹果系实体深色字。
- 实时网速 + 流量统计从左侧列表底部挪到**右侧独立信息栏**（300px 玻璃卡片，窗口 <1180px 自动隐藏），sparkline 画布 330→264。
- App 图标重做：对原图从四边 flood-fill 近白背景为透明（保留玻璃质感软边缘），彻底去白边；按 Big Sur 规范 824/1024 居中，重出 iconset/icns。

## 2026-09-04 深夜：v3.2 — 手机端 PWA + GPS + 局域网鉴权重构

- **品牌统一 DJiPhone Kit**（窗口/页面/Info.plist/关于面板/转发测试文案）。
- **局域网连不上的根因**：设置里启用了 LAN PIN，非本机请求一律 401 且返回裸 JSON；且 settings.json 存在 App 包内，每次 rebuild 被 rm -rf 清空（PIN/转发配置/流量统计全丢）。
- **修复**：
  - 鉴权重构：页面与静态资源免 token，API 需 token（query/X-Auth-Token/cookie 三通道）；新增 `/api/pin-login`（POST 验 PIN → 写 `dj_token` cookie，30 天）。
  - 数据持久化迁移：settings.json / sent_sms.json / call_history.json 全部迁到 `~/Library/Application Support/DJiPhoneKit/`（带旧位置自动迁移），App 重建不再丢数据。
- **手机端 `/m`（NasAnySim 风 PWA）**：底部 4 Tab（状态/短信/通话/更多）；信号条 + 运营商、实时网速、流量、GPS 卡片；会话式短信 + 发送；拨号盘 + 通话状态轮询（拨/接/挂）；manifest + apple-touch-icon 支持添加到主屏幕。
- **CallKit 结论**：iOS 网页无法直接调用 CallKit（需原生 App）；手机端提供 `tel:` 跳转 iOS 电话应用拨打，模块侧 Mac 拨号仍可用。
- **设置新增**：GPS 定位组（AT+QGPS 开关 + QGPSLOC 解析 + OSM 内嵌地图）；AT 控制台组（任意 AT 指令 + 历史 + 快捷查询）。
- **GPS 固件怪癖**：`AT+QGPS=0` 返回 `+CME ERROR: 501` 且关不掉（开正常，`AT+QGPS?` 读数准确），留待后续调研。

## 2026-09-04 22:50 补丁：包名彻底改为 DJiPhone Kit
- 根因：之前只改了 Info.plist 的 CFBundleName/DisplayName，App 文件本身仍是 DjiPhone.app，Finder/Dock 显示的是文件名，用户看到名字没变。
- 修复：bundle 改为 `~/Applications/DJiPhone Kit.app`，可执行文件 `Contents/MacOS/DJiPhone Kit`，CFBundleExecutable 同步；sms_server.py 的 APP_BUNDLE_PATH 与 LaunchAgent 指向更新，旧 DjiPhone.app 在构建和自启动写入时自动清理。
- 顺带修复：LaunchAgent plist 此前因重建丢失，已通过设置接口重新生成（指向新包路径）。

## 2026-09-04 23:50：v3.3 — 手机端重做 + 流量统计改用模块基带计数
### 用户反馈
手机端不是移动 UI（仍是桌面布局、无法新建发短信）；桌面实时网速/流量"完全没有用"。
### 根因
1. 旧 mobile.html 实际生成质量差（残留编辑器属性、布局桌面化、无发短信入口）。
2. 流量统计用 psutil 读 Mac 网卡——模块 ECM 网卡只承载 AT/管理流量，恒为 0，纯属摆设。
### 方案
- **后端**：`SpeedMonitor`/`DataUsageMonitor`(psutil) 整体替换为 `ModuleTrafficMonitor`，轮询 `AT+QGDCNT?`（基带真实蜂窝计数），后台 2s 采样；按天/月增量持久化；`/api/speed` `/api/data-usage` 统一返回新结构；reset 同步清模块计数器。
- **固件怪癖①**：QDC507GLEFM21 的 QGDCNT 返回**字节**而非文档标注的 KB（按 KB 解释寿命累计数百 TB，不可能）→ 统一 /1024。
- **固件怪癖②**：模块初始化瞬间计数器跳变（单次增量达 GB 级）→ 加 200 Mbps 合理性钳制，异常采样丢弃。
- **桌面**：右侧栏重设计为单张「4G 网络」卡：下行/上行大数字 + sparkline + 今日/本月/模块累计 + 清零；去掉 <1180px 隐藏（改为收窄 240px）。
- **手机端**：mobile.html 全部重写（干净 iOS 风，无残留属性）：底部 4 Tab（状态/短信/通话/更多）；状态页=信号/运营商/实时速率/流量/SIM/IMEI；短信页=新建短信（输入手机号）+ 会话列表 + 气泡聊天 + 发送（/api/sms/send）；通话页=iOS 拨号盘 + 来电浮层（接听/挂断/时长）+ tel: 备选 + 最近通话；更多页=GPS 开关/定位 + AT 控制台。
- 顺手修复：v3.1 挪面板时丢失的 `.app` 闭合 div（HTMLParser 校验配平）。

## 2026-09-05 00:30：v4.0 — 桌面/手机 UI 全面重做 + 菜单栏状态项
### 需求
电脑 UI 和手机 UI 全部重做成微信式底部选项模式；电脑任务栏（macOS 菜单栏）加入 4G 信号强度、网速、流量显示；UI 更贴近 macOS 原生。
### 实现
- **桌面 index.html 全部重写**：顶栏（毛玻璃+红绿灯让位）+ 底部四 Tab（状态/短信/通话/更多），SF 字体栈、系统色、hairline 分组卡片、12px 圆角、macOS 工具栏式顶栏；状态页含信号条/速率大数字/流量统计/SIM/IMEI；短信页会话+气泡聊天；通话页拨号盘+横幅+来电浮层+记录；更多页 GPS/AT/局域网/转发/设置。
- **手机 mobile.html 全部重写**：与桌面同款底部四 Tab，iOS 原生设计语言（large-nav、分组卡、开关、iOS 拨号盘、来电全屏浮层、聊天覆盖页），safe-area 适配，PIN 锁保留。
- **菜单栏状态项 menubar.py（新增）**：pyobjc NSStatusItem，绿色四格信号条（attributed string）+ 等宽数字 ↓↑ 网速，每 2s 轮询本机 /api/status /api/speed；下拉菜单：今日/本月流量、打开主窗口、清零流量、退出；pyobjc 选择器命名坑：尾下划线=参数占位（setBarText_signal_down_up_ 被解析成 4 参数），改 setBarData_ 传 tuple 解决。
- **顺手修复**：`/api/at` 服务端只认 `command`，桌面端一直发 `cmd`（AT 控制台恒报"command 不能为空"）→ 服务端兼容两个键 + 前端统一发 `command`。
- mobile.html 缺 #pages 闭合 div（HTMLParser 校验发现并修复）；build_app.sh 拷贝清单加入 menubar.py，版本 4.0。
### 验证
桌面/mobile 均 200，两份 HTML 标签配平 OK、JS 语法 node --check OK；App（PID 运行中）v4.0。注意：当前 USB 模块未插入（/dev/cu.* 无模组端口），/api/status 返回 connected:false 属正常——插回模块即恢复。

## 2026-09-09：QADBKEY 解锁 + 模块语音运行时部署成功，15-18 秒挂断根治
### 根因闭环
此前"usbcfg ADB 位写不进"并非固件硬锁——缺 QADBKEY 解锁步骤。流程：`AT+QADBKEY?` 得挑战值 → 密码=`openssl passwd -1 -salt <挑战> SH_adb_quectel` 的第 4 段（22 字符）→ `AT+QADBKEY="<密码>"`（返回 OK，持久）。之后 usbcfg 写 `…,1,1,1,1,1,1,1` 读回保持，CFUN 重启后 USB 枚举出 ADB 接口（if#6, sub0x42/proto0x01）。
### 运行时部署（adb root, mdm9607/3.18.44/armv7）
来源 moluncn/mavo `Resources/ModuleVoice`（SHA256 与多项目 pin 一致）。推 /tmp/mavo-call → insmod qdc507_aprv3.ko + qdc507_voice.ko → 声卡 mdm9607-tomtom-i2s-snd-card 出现 → /usr/bin/alsaucm_test 校准（verb VoLTE, Auxpcm Rx/Tx，等待 "ACDB -> Sent VocProc Cal!"）→ mavo-pcm-bridge.armv7 --voice-route-session 激活 hw:0,4。运行时已备份至模块 /data/mavo-call。
### 实测
ATD10010; 通话 ACTIVE >60s（旧 bug 15-18s 必挂），CHUP 后 CEER 6,256 正常。macOS 出现 AC Interface / AS Interface（BAIWANG USB Audio 8kHz）。
### 待办
模块每次重启后需重跑 insmod+校准+route session（可集成进 DJiPhone Kit 启动自愈）；Mac 侧 8kHz 音频路由（AC→扬声器、麦克风→AS）待接入 App；CellBridge SIP 网关移植评估继续。

## 2026-09-09（晚）：语音管线自动化收尾
- voice_runtime.py 修复后 ensure_voice_route 全链路可独立运行（纯 Python ADB，不依赖外部 adb）。
- 新增 voice_audio_bridge.swift（CoreAudio 8kHz 双工桥），构建时 swiftc 编译进 App。
- 通话钩子：dial/来电 active → ensure_voice_route + 音频桥启动；挂断 → 自动拆除。
- CLCC 解析过滤 mode!=0 幽灵条目（EG25 挂断后固件残留），避免状态机卡死。
- 已提交并推送 GitHub（3f7a330）。
