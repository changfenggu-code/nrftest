# Zephyr Tester、PCA10059 与 AutoPTS `pybtp` 本地源码审计

## 1. 状态

- 日期：2026-09-06
- 性质：Phase 0 本地源码审计、firmware build 与 DFU package spike
- 2026-09-06 审计时点结论：**固定 upstream Tester 已为 PCA10059 成功构建并生成可重复 DFU package；当时尚未通过 flash、USB、BTP 或 RF 验证**
- 该时点已执行：只读源码审计、最小 config/overlay、Windows pristine firmware build、生成配置和 HEX 地址边界验证、Nordic DFU 工具固定、可重复 package 和 Nordic parser 复验
- 该时点未执行：刷写、串口通信、BTP direct probe、BLE RF
- 未修改：固定 Zephyr/AutoPTS checkout、归档实现、BleHub

后续 T8–T12 在 NCS 对照环境定位并闭环证明 zero-backend logging process thread assertion；T13 验证 `CONFIG_LOG=n` 后，no-logging 语义已迁入正式 upstream `v4.4.2` 配置。2026-09-08 正式候选完成 build/package/flash/re-enumeration/Win32 serial/BTP Core direct probe，Phase 0 Windows 控制面通过；BLE RF 仍未执行。详见
[《Zephyr Bluetooth Tester zero-backend logging assertion 根因报告》](2026-09-08-zephyr-tester-zero-backend-logging-assertion.md)。

审计基线：

| 上游 | 固定版本 |
|---|---|
| Zephyr | `v4.4.2` / `dccb09599635bdff17633fa7e9dab014b91dce90` |
| AutoPTS | `54e81c7f3495bce72e5f688e9c996b85b8272799` |
| Zephyr SDK | `1.0.1`，GNU target `arm-zephyr-eabi` |

审计使用当前机器由 `nrftest.local.toml` 指向的固定 checkout。本文中的 Zephyr 路径均相对于 `zephyr_root`，AutoPTS 路径均相对于 `autopts_root`。

## 2. 总结

1. PCA10059 可运行完整 Zephyr Bluetooth Host、片上 Controller、GATT Server 和 BTP Server；它不是供 PC 接管的 HCI adapter。
2. nRF52840 的 Bluetooth Controller 是片上 `zephyr,bt-hci-ll-sw-split`，因此不需要第二路物理 HCI UART。PCA10059 只需一路 BTP serial。
3. PCA10059 board 已提供原生 USB CDC ACM UART；BTP 可把它当作串口使用，不需要 PC 通过 libusb claim Bluetooth HCI interface。
4. stock `tests/bluetooth/tester` 不能原样为 `nrf52840dongle/nrf52840` 构建：`CONFIG_UART_PIPE=y` 要求 `zephyr,uart-pipe` chosen，而 Dongle DTS 只提供 `zephyr,console` 等 chosen。
5. 最小确定性修复是项目 overlay 将 `zephyr,uart-pipe` 指向既有 `board_cdc_acm_uart`。这不修改 BTP、不增加私有 transport，也不新增第二个 CDC interface。
6. 必须使用普通 `nrf52840dongle/nrf52840` target 保留原厂 nRF5 SDK USB bootloader；不得静默切换到 `/bare`。
7. AutoPTS 与固定 Zephyr 在 BTP header、字节序以及当前所需 Core/GAP/GATT opcode 上静态一致，并已经实现所需 parser、worker 和命令 wrapper。
8. AutoPTS `pybtp` 不是独立 SDK；它与 `IutCtl`、`Stack`、进程全局事件处理和外部 `socat` 强耦合。当前仍有可行的原生复用路径，不构成转向自研 BTP Client 的理由。
9. BTP 没有 request ID，现有 wrapper 也不普遍保证完整事务并发安全。`nrftest` 必须保持单 session、单 command owner、最多一个 pending response；timeout 后应重建 session。
10. 动态 GATT 可以使用上游 sequential mode 建库，但数据库清理、CCC 事实、Indication confirmation 和 `set_value` 的弱 success 语义仍需后续硬件验证。

## 3. Zephyr Tester 与 BTP transport

### 3.1 Tester 构建入口

Tester 是普通 Zephyr application：

```text
tests/bluetooth/tester/
├── CMakeLists.txt
├── Kconfig
├── prj.conf
├── testcase.yaml
└── src/
```

`CMakeLists.txt:13-19` 无条件编译 `main.c`、`btp.c`、`btp_core.c`、`btp_gap.c` 和 `btp_gatt.c`；L2CAP、Mesh、IAS、OTS 和 Audio 等额外服务按 Kconfig 编译。

`src/main.c:17-20` 只调用 `tester_init()`。`src/btp.c:238-260` 初始化 UART、注册 Core service，并发送 `BTP_CORE_EV_IUT_READY`。

`testcase.yaml` 只列出 `qemu_x86`、`native_sim` 和 `nrf52840dk/nrf52840`，没有 PCA10059。这不禁止手工指定 Dongle board build，但说明上游没有为其建立 Tester CI 覆盖。

### 3.2 BTP serial 实现

stock `prj.conf` 启用：

```text
CONFIG_UART_PIPE=y
CONFIG_UART_CONSOLE=n
CONFIG_BT=y
CONFIG_BT_CENTRAL=y
CONFIG_BT_PERIPHERAL=y
CONFIG_BT_GATT_CLIENT=y
CONFIG_BT_GATT_DYNAMIC_DB=y
```

`src/btp.c:189-199` 在 `CONFIG_UART_PIPE=y` 时通过 `uart_pipe_register()` 和 `uart_pipe_send()` 收发 BTP。`drivers/serial/uart_pipe.c:24-25` 将设备硬绑定到：

```text
DT_CHOSEN(zephyr_uart_pipe)
```

BTP frame 上限为 1024 bytes，header 为 5 bytes。接收端等待完整 header/payload 后入队；发送端使用 mutex 防止不同 response/event 交错。

关闭 `CONFIG_UART_PIPE` 时，`src/btp.c:200-235` 可回退到 `zephyr,console` 和 10 ms polling。该路径仍是标准 BTP，但延迟和吞吐较差，只保留为 spike 备选，不作为首选设计。

### 3.3 为什么不需要第二路 HCI serial

nRF52840 SoC DTS：

- `dts/arm/nordic/nrf52840.dtsi:12-17` 将 `zephyr,bt-hci` 指向片上 controller；
- `dts/arm/nordic/nrf52840.dtsi:128-134` 的 controller compatible 是 `zephyr,bt-hci-ll-sw-split`；
- `subsys/bluetooth/controller/hci/hci_driver.c` 将其注册成进程内 Zephyr HCI device。

所以这里的两条链路是：

```text
PC Python → serial/USB CDC → BTP Server
Zephyr Host → in-process HCI driver → nRF52840 Controller/Radio
```

Tester README 的“两路 serial”适用于外置 controller 等通用拓扑，不适用于这个 integrated controller 配置。若需要文本调试日志，应使用独立 UART/RTT，或保持日志关闭；不能把日志混进 BTP CDC。

## 4. PCA10059 board、USB 与 bootloader

### 4.1 USB CDC ACM 已存在

`boards/nordic/nrf52840dongle/nrf52840dongle_nrf52840_common.dtsi:184-189` 启用 nRF USBD，并 include `boards/common/usb/cdc_acm_serial.dtsi`。公共 include 创建 `board_cdc_acm_uart`，并将 console、shell、mcumgr 等 chosen 指向它，但没有定义：

```dts
zephyr,uart-pipe = &board_cdc_acm_uart;
```

因此第一版项目 overlay 应只补该 chosen：

```dts
/ {
    chosen {
        zephyr,uart-pipe = &board_cdc_acm_uart;
    };
};
```

这是已由源码确定的 build 前置条件；是否还需调整 CDC FIFO 或 flow control 必须由连续 BTP traffic 和断开恢复实测决定。

### 4.2 日志隔离

stock Tester 设置 `CONFIG_UART_CONSOLE=n`。当前 Kconfig 组合下，minimal log 最终走 `printk()`，而没有 UART console hook 时字符被丢弃，因此静态上不会把日志写入 BTP CDC。

首次 build/package 审计阶段曾因 stock `prj.conf` 先选择 `CONFIG_BTTESTER_LOG_LEVEL_DBG=y`、随后禁用 logging 会留下 Kconfig warning，而选择保留 logging framework、关闭实际等级和 UART backend：

```text
CONFIG_UART_CONSOLE=n
CONFIG_BOOT_BANNER=n
CONFIG_TEST_LOGGING_DEFAULTS=n
CONFIG_LOG=y
CONFIG_LOG_DEFAULT_LEVEL=0
CONFIG_BTTESTER_LOG_LEVEL_OFF=y
CONFIG_LOG_BACKEND_UART=n
```

该组合完成了当时无 Kconfig warning 的真实 build，但后续实机证明它形成了“assertions 启用 + deferred process thread + zero backend”的非法运行时组合。T11 捕获 `log_core.c:956` assertion，T12 最小关闭 thread 后通过，T13 完整 `CONFIG_LOG=n` 后也通过。活动正式配置现采用 `CONFIG_TEST_LOGGING_DEFAULTS=n`、`CONFIG_LOG=n`；保留 stock Tester source，因此已知 debug choice warning 仍会在 merge 时出现，但最终 `.config` 已确认 logging 不编译。Zephyr 还会提示 `drivers__console` 没有 source 并排除该 library；这是禁用 console backend 后的通用 CMake 提示。BTP header 没有 magic/escape/checksum，任何文本污染都可能破坏 frame 对齐。

### 4.3 board variant 与地址范围

普通 target `nrf52840dongle/nrf52840` 保留原厂 MBR 和 nRF5 SDK USB bootloader。关键布局来自 board DTS/Kconfig：

- direct application 默认 load offset：`0x1000`；
- 原厂 USB bootloader 起始：`0xe0000`；
- application 不能覆盖 bootloader/settings pages。

`nrf52840dongle/nrf52840/bare` 明确不保留 onboard USB bootloader，只适合外部 SWD/debug probe，不属于当前默认路径。

`firmware-build` 必须检查最终 HEX 的实际地址范围，不能只依赖 link success。

### 4.4 USB identity

board 的默认 CDC VID/PID 和字符串属于 Zephyr 示例值，不应未经评审直接作为正式发布 identity。`CONFIG_HWINFO=y` 可让 serial-number descriptor 使用 nRF FICR device ID，有利于 reset/re-enumeration 后稳定发现设备。

Phase 0 build spike 可以验证该机制，但在发布前必须单独决定合法 VID/PID、manufacturer 和 product string；不得擅自使用 Nordic VID。

## 5. 固件最小配置边界

第一轮构建候选坚持 stock Tester source，只维护项目 overlay/config。实际生成配置已经验证保持：

```text
board = nrf52840dongle/nrf52840
CONFIG_BOOTLOADER_MCUBOOT=n
CONFIG_USE_DT_CODE_PARTITION=n
CONFIG_UART_PIPE=y
CONFIG_UART_CONSOLE=n
CONFIG_HWINFO=y
CONFIG_BT_GATT_DYNAMIC_DB=y
CONFIG_BT_PERIPHERAL=y
```

由于 `btp_gap.c`/`btp_gatt.c` 无条件引用 Central/GATT Client API，在不修改上游 C source 时暂时保留：

```text
CONFIG_BT_CENTRAL=y
CONFIG_BT_GATT_CLIENT=y
```

可以通过已有编译门评估关闭 L2CAP dynamic channel、OTS、EATT、EAD、ISO 和当前测试不需要的 extended/periodic advertising 能力，但必须以无 ignored assignment/dependency warning 的构建为准。

此处不批准复制 Tester 源码、修改 BTP framing/opcode 或新建私有 transport。

## 6. 原厂 USB DFU 路径

本地官方 board 文档 `boards/nordic/nrf52840dongle/doc/index.rst:65-114` 说明：

1. 使用普通 `nrf52840dongle/nrf52840` target；
2. 用 `nrfutil nrf5sdk-tools pkg generate` 将 `zephyr.hex` 打包成 DFU ZIP；
3. 用 `nrfutil nrf5sdk-tools dfu usb-serial` 向明确端口刷写。

这与 Zephyr `west flash` 的 `nrfutil device` runner 不是同一命令路径。因此正式流程应分离为：

```text
firmware-build
firmware-package
firmware-flash <explicit port/device>
```

当前已固定并在 Windows 受管目录验证：

```text
nRF Util CLI:                8.2.1
nrf5sdk-tools command:       1.1.0
command 内部兼容引擎:         pc-nrfutil 6.1.7
安装根:                       E:\dev\nrftest-upstream\host-tools\nrfutil-8.2.1
```

这里的 `6.1.7` 不是旧的全局 CLI，也不是项目绕过新工具后主动选择的版本。项目调用的是 Nordic 当前 nRF Util CLI `8.2.1` 中的官方 `nrf5sdk-tools 1.1.0` command；该 command 为 PCA10059 原厂 bootloader 使用的旧 nRF5 SDK Secure DFU package/protocol 内嵌兼容引擎。这个分层也解释了为什么新 CLI 可以继续执行固定 Zephyr board 文档要求的 `pkg` 和 `dfu usb-serial`。

CLI 和 command package 均报告 `LicenseRef-Nordic-1-Clause`：允许用于 Nordic 芯片，但禁止本项目重新分发这些工具二进制。`firmware-tools.lock.toml` 固定三个 Host 平台的下载 URL、版本、commit 和 SHA-256；`setup-firmware-tools` 只执行显式机器本地安装。普通 setup/build/test 不安装 programmer、不请求权限、不刷写。

## 7. AutoPTS `pybtp` 复用审计

### 7.1 模块和初始化

关键模块：

| 路径 | 职责 |
|---|---|
| `autopts/pybtp/parser.py` | BTP header/frame 编解码 |
| `autopts/pybtp/defs.py` | service/opcode/event/status 常量 |
| `autopts/pybtp/types.py` | UUID、address、property、permission 编码 |
| `autopts/pybtp/iutctl_common.py` | socket、worker、response FIFO |
| `autopts/pybtp/btp/btp.py` | Core API、全局初始化、事件分发 |
| `autopts/pybtp/btp/gap.py` | GAP API/event |
| `autopts/pybtp/btp/gatt.py` | GATT Server API/event |
| `autopts/ptsprojects/iutctl.py` | serial↔socat↔socket transport 生命周期 |

`btp.init(get_iut_method)` 保存进程级 IUT getter 并安装全局 event handler。IUT 至少需要 `.btp_socket` 和 `.get_stack()`；AutoPTS 正常路径由 `IutCtl` 提供 transport 和 `Stack`。

### 7.2 协议静态兼容性

AutoPTS parser 使用 5-byte little-endian header：

```text
uint8 service
uint8 opcode
uint8 controller_index
uint16 payload_length
```

`parser.py` decoder 使用 `<BBBH`；固定 Zephyr 的 packed header 和 `sys_le16_to_cpu()`/`sys_cpu_to_le16()` 与其一致。

当前需求涉及的 Core/GAP/GATT service 和 opcode 抽查与固定 Zephyr 一致。但 `defs.py` 不是针对当前 Zephyr lock 自动生成，静态匹配不能替代真实 Core response probe。

### 7.3 已存在的所需 API

Core：

- `read_supp_svcs()`
- `read_supported_commands(service)`
- `core_reg_svc_gap()` / `core_unreg_svc_gap()`
- `core_reg_svc_gatt()` / `core_unreg_svc_gatt()`

GAP：

- controller info
- power on/off
- start/stop advertising
- disconnect/reset
- settings、connected、disconnected events

GATT Server：

- add service/included service/characteristic/descriptor
- set value
- start server
- get attributes/value/handle
- remove handle/service
- attribute value changed event

因此当前没有理由在 `nrftest` 中实现 framing/parser/Core/GAP/GATT Client。

### 7.4 transport 不是 pyserial 直连

AutoPTS 的真实路径是：

```text
Tester serial
→ external socat
→ Unix socket（Linux/macOS）或 TCP socket（Windows）
→ BTPSocketSrv
→ BTPWorker
→ response FIFO / global event handler
```

Windows 分支还调用 `mode COMx`，再由 `socat.exe` 桥接到 TCP。非 Windows 分支使用 AF_UNIX 和固定 `/tmp/bt-stack-tester`。

`pyserial` 主要用于 flush 和可选 logger，并不承担 BTP framing transport。

需要运行验证的障碍包括：

- Windows `socat.exe` 版本、下载来源、COM 参数、hostname bind、防火墙和清理；
- Linux serial 权限、固定 socket path 和并行进程冲突；
- macOS `/dev/cu.*`/`/dev/tty.*`、socat 参数、重枚举和恢复；
- import graph 对 MMI、boards、RTT、`pylink-square` 等非最小组件的耦合。

### 7.5 command ownership 与 timeout

BTP 没有 request ID。`BTPWorker` 使用单个 response FIFO；部分 wrapper 将 send/read 分开，不能视为一般并发安全 API。

正式约束：

```text
one HIL session
one command owner
at most one pending response
```

外层 adapter 最终必须串行化完整事务。timeout 之后晚到 response 可能使 FIFO 失步，安全恢复边界是销毁并重建 transport/session，而不是继续复用原 FIFO。

### 7.6 动态 GATT 语义限制

固定 Zephyr sequential mode 要求 add characteristic 的 `svc_id=0`、add descriptor 的 `char_id=0`。AutoPTS 已按这种方式调用；server start 后再按 UUID 查询动态 handle。

已确认限制：

- add API 的 response ID/count 被 wrapper 丢弃；
- UUID 重复时 first-match handle lookup 有歧义；
- `gatts_set_val()` 的 bytes 参数仍按 ASCII hex 解码，不是任意 raw bytes API；
- Central write 会产生 Attribute Value Changed event；
- CCC 变化没有独立结构化 BTP event；
- outgoing Indication completion 只写日志，没有 BTP confirmation event；
- Zephyr `set_value` 部分内部错误仍可能返回 BTP success；
- GATT unregister success 不等于动态 DB 已清空；同 boot rebuild 必须实测。

这些缺口已在计划的语义边界/后续 Phase 中预留，不应在 Phase 0 先增加私有 opcode。

## 8. Phase 0 direct probe 路径

首选完整复用 AutoPTS 原生链路：

```text
fixed AutoPTS checkout
→ IutCtl
→ BTPSocketSrv + socat
→ BTPWorker
→ Stack
→ autopts.pybtp.btp
→ fixed Zephyr Tester
```

第一条探针只验证 Core：

1. 从固定 checkout source-path import，不复制或 vendor 源码；
2. 一个进程、一个 IUT、一个 command owner；
3. 初始化 `IutCtl`、Core stack 和 `btp.init(lambda: ctl)`；
4. 启动现有 transport/worker；
5. 调用 `read_supp_svcs()`；
6. 检查 `Stack.supported_svcs` 至少包含目标固件预期的 Core/GAP/GATT；
7. 无论成功失败都关闭 `IutCtl`；
8. timeout 后销毁 session，不继续消费原 FIFO。

只有 Core probe 通过后，才依次注册 GAP/GATT 并验证 controller、advertising 和动态 GATT。

如果完整 `IutCtl` 只被无关 board/RTT import 耦合阻塞，可评估组合 AutoPTS 自带的 `BTPSocketSrv`、`BTPWorker`、`Stack` 和 `btp.init()`，但这仍需项目拥有 `socat` 生命周期薄胶水。采用前应确认没有滑向第二套 BTP Client。

## 9. 必须停止并重新评审的条件

以下情况属于已批准计划的停止条件：

1. 固定 AutoPTS 无法在隔离环境 import/init，且不能通过安装其现有依赖解决；
2. 现有 `IutCtl`/`BTPWorker` 无法读取固定 Zephyr 的合法 Core response；
3. 真实 probe 显示固定双方 BTP 不兼容；
4. transport 要求复制、重写或平行实现 framing、parser、response worker、Core、GAP 或 GATT Client；
5. 必须 patch/vendor AutoPTS GPL 源码才能继续；
6. PCA10059 需要改变协议、硬件角色或默认 bootloader/partition 策略才能构建；
7. 发布方式无法满足 GPL 或其他第三方许可证边界。

## 10. 首次构建与 package 证据

Windows 执行：

```text
pixi run just firmware-build
```

已通过以下门：

- 从固定 Zephyr `v4.4.2`/commit 和 SDK `1.0.1` pristine build；
- board 精确为 `nrf52840dongle/nrf52840`；
- 项目 overlay 被 CMake/DTS 接受；
- 无 Kconfig warning；
- generated `.config` 确认原厂 bootloader、direct app、USB BTP、动态 GATT 和 integrated controller 配置；
- Flash `398820 B / 1020 KiB`（38.18%）；
- RAM `98712 B / 256 KiB`（37.66%）；
- HEX 内部 Flash segments 为 `0x1000..0x5a5dc` 和 `0x5a5e0..0x625e4`，未覆盖 MBR 或 `0xe0000` 起始的原厂 bootloader；
- `zephyr.hex` SHA-256 为 `eafe56e6bc8a38f1a26077654d176eb00727ed2d1e2d7993f0856fc15f665b86`；
- `.work/build/pca10059-tester/nrftest-build-manifest.json` 记录输入和 ELF/HEX/BIN identity。

Windows 随后执行：

```text
pixi run just firmware-package
```

已取得以下证据：

- package 配置固定为 nRF5 SDK Secure DFU、hardware version `52`、SoftDevice requirement `0x00`、application version `1` 和 `signed = false`；
- 生成包包含 `manifest.json`、`zephyr.bin` 和 `zephyr.dat`；
- Nordic 内嵌兼容引擎会给 ZIP entries 写入当前时间，导致未经处理的 ZIP container SHA-256 漂移，但两次生成的三个 entry payload 逐字节一致；
- 项目仅将 ZIP entry 顺序和时间规范化为固定值 `1980-01-01T00:00:00`，不修改 Nordic 生成的 payload；
- 规范化后再次通过同一受管 Nordic `pkg display` 解析验证；
- 连续两次规范化 DFU ZIP SHA-256 均为 `3a3b623aef6012b4c0bdcb0b94cbc3c304f703fd60c14317effc53edd077d9f3`；
- package identity、参数、内容、embedded manifest、工具版本和许可证边界写入 `nrftest-build-manifest.json`；
- `firmware-flash` 要求同时提供当前 bootloader port 和精确 package SHA-256，验证目标端口当前唯一存在后才调用 `dfu usb-serial`，不会选择“第一个设备”，也不会自动 reset/erase/recover。

这个 unsigned package 只用于当前开发/HIL PCA10059 和允许 signature-less package 的原厂 bootloader，不是生产安全更新。以上仍是 2026-09-06 首次 build/package 时点证据，不应用后续结果倒改其 artifact identity。

2026-09-08 正式后续候选改为 `CONFIG_LOG=n`：Flash `393484 B`、RAM `95256 B`，HEX SHA-256 `4a1a79fc123082a3ec987df10e566b4432a401aeee0bda351f7a50d9dff20308`，规范化 DFU ZIP SHA-256 `d53fa63143ad862de23cff4dcc3af68e538f94feafa1fcadb37e295029b25326`。正式 package 入口使用独立 `generate`/`normalize-validate` 子进程处理 Windows legacy Nordic ZIP handle 时序，并通过 Nordic parser 复验。该包已完成显式 DFU、稳定 application identity、Win32 serial 和固定 AutoPTS BTP Core direct probe；仍不能称为通过 BLE RF。

## 11. 下一步

Phase 0 控制面已通过。下一步严格进入 Phase 1：

1. 保持固定 upstream firmware、AutoPTS 和 PCA10059 不变，验证 GAP controller info/power；
2. 通过现有 BTP GAP API 启停 advertising；
3. 由 BleHub Windows Central 通过自己的 controller 和真实 BLE RF 完成 scan/connect/disconnect；
4. 两条路径分别记录 BTP 控制事实与 BleHub/RF 结果；
5. Phase 1 通过后再推进归档 JSON Profile 语义迁移和动态 GATT。
