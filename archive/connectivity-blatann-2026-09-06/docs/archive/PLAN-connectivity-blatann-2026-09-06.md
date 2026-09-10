# nrftest 计划

## 1. 计划定位

`nrftest` 是一个独立的、PC 控制的 nRF52840 BLE Peripheral 测试设备项目。

它的目标是为 BleHub Central 提供一个真实 BLE RF 对端，替代日常测试中对 BLESS Peripheral 的依赖。`nrftest` 不实现 BleHub，不实现 BleHub backend，也不进入 BleHub 发布物；它只负责提供可编程的硬件 Peripheral、USB 控制面和 Peripheral ground-truth telemetry。

本计划只约束 `nrftest`。BleHub 的公共契约、backend、HIL runner 和现有文档不在本计划中修改。

当前状态：**方案已确定，Connectivity 固件、官方驱动、Python 3.10、USB CDC/Blatann 链路和 Phase 0 doctor 已验证；Phase 1 host 控制骨架、profile load、GATT database 构建、广播启动/停止和 shutdown 已通过 COM11 实机探针；Phase 3 的确定性 burst payload、Notification/Indication paced burst、completion correlation、失败分类和 fake-driver 测试已实现，25 个默认测试通过；Central 连接、GATT 操作、真实 burst RF 和完整 Peripheral RF parity smoke 尚未完成。**

---

## 2. 目标与非目标

### 2.1 目标

`nrftest` 第一阶段应提供：

- nRF52840 Dongle 通过 USB CDC 接入 Windows PC；
- PC host 使用 Nordic Connectivity firmware 控制 nRF52840；
- nRF52840 作为 BLE Peripheral 广播并提供 GATT Server；
- 根据 profile 构建 service、characteristic 和初始值；
- 支持 Central 的 scan、connect、GATT discovery、read、write；
- 支持 Notification 和 Indication，并暴露发送/确认 telemetry；
- 支持主动停止广播、主动断连、重置和恢复；
- 支持一次一个 scenario 的可重复执行；
- 控制命令与 BLE 数据路径分离；
- 输出结构化的控制响应和 Peripheral ground-truth telemetry；
- 支持 BleHub Windows/Android Central 的真实 RF smoke、反复连接和稳定 soak；
- 在不依赖 BLESS/BlueZ/QEMU 的情况下提供独立 GATT Server/controller 实现。

### 2.2 非目标

第一阶段不做：

- 把 nRF52840 做成 BleHub 的生产 backend；
- 让 BleHub 通过 COM port 直接驱动 nRF52840；
- 把 Connectivity firmware 改造成 HCI controller firmware；
- 同时支持 standalone Peripheral 和 Linux P-Controller 两种固件角色；
- 实现通用 BLE Peripheral 产品 SDK；
- 实现 Web service、远程云控制或多租户调度；
- 替代 `FakeBackend` 的确定性 contract/conformance 测试；
- 覆盖 BlueZ、D-Bus、QEMU guest 或 Linux host 专项测试；
- 模拟 malformed ATT PDU、任意非法 BLE controller 行为或任意 RF 时序；
- 以 nRF provider 的结果推断 BLESS/BlueZ provider 已通过，反之亦然。

---

## 3. 角色和总体拓扑

正式术语使用 BLE `Central` 和 `Peripheral`，不使用“主机/从机”描述 BLE 角色。

### 3.1 角色

- **DUT Central**：被测的 BleHub Central，例如 Windows 或 Android。
- **nRF Peripheral**：运行 Nordic Connectivity firmware 的 nRF52840 Dongle。
- **nrftest Host**：运行在 PC 上的 Python 控制程序，使用 `pc_ble_driver_py` 控制 nRF。
- **Control Plane**：HIL runner 到 nrftest Host、再到 nRF 的 USB 控制和 telemetry 路径。
- **BLE Data Plane**：DUT Central 与 nRF Peripheral 之间的真实 BLE RF 路径。

### 3.2 推荐拓扑

```text
                              HIL Runner
                         测试编排和结果关联
                         /                 \
                        /                   \
        控制/准备 Peripheral                 驱动被测 DUT
                      /                       \
                     v                         v
          nrftest Host ── USB CDC ──> nRF52840 Peripheral
                                                ^
                                                │ BLE RF
                                                │ 被测真实空口链路
                                                v
                         BleHub Central ── Central Controller
```

上图中的 HIL Runner 是测试协调者，不是把 nrftest 调用链嵌入 BleHub。它分别操作两个独立对象：

1. **先控制 nRF**：通过 `nrftest Host` 和 USB CDC 设置 profile、启动广播、修改值、触发 Notification/Indication 或请求断连；
2. **再驱动 BleHub**：单独调用 BleHub 的 Central API，验证 BleHub 对当前 nRF 行为的观察和处理；
3. **只通过 BLE RF 关联结果**：当 nRF 作为 Peripheral、BleHub 作为 Central 时，二者在运行时通过真实 BLE 空口通信。这是被测系统的物理关系，不是 Hub 调用 nrftest 的软件 API 关系。

因此，严格来说不存在下面这种调用链：

```text
BleHub → nrftest Host → USB → nRF
```

实际关系是两个并行控制分支，以及一个被测 RF 连接：

```text
HIL Runner ──控制──> nrftest Host ──USB──> nRF Peripheral
     │                                      ^
     │                                      │ BLE RF
     └──────驱动/观察──> BleHub Central ─────┘
```

两条控制/观察路径必须保持独立：

- **nRF Control/Telemetry Path**：测试 runner 改变 nRF 状态，并读取 nRF 侧实际 callback/发送/确认事实；
- **BleHub DUT Path**：测试 runner 调用 BleHub API，并读取 BleHub 自己产生的事件和结果；
- **BLE Data Path**：nRF 和 BleHub 之间的真实 BLE RF 通信，是被测链路，不由 nrftest Host 直接转发给 BleHub。

一个典型测试顺序是：

```text
1. runner 让 nRF 广播，并把 read characteristic 设置为 0x42
2. runner 调用 BleHub scan/connect/discover/read
3. BleHub 通过自己的 Central controller 从 BLE RF 读取 0x42
4. runner 对比 BleHub 结果、nRF telemetry 和测试预期
```

测试 Notification 时则是：

```text
1. runner 让 nRF 发出 Notification
2. nRF 通过 BLE RF 发送数据
3. BleHub Central 收到并产生自己的 Notification event
4. runner 对比 nRF 的发送事实和 BleHub 的接收事实
```

Control Plane 可以改变 Peripheral 状态，但不得：

- 伪造 BleHub event；
- 调用 BleHub event ingress；
- 绕过 RF 直接回答 BleHub 的被测命令；
- 以 host 端预期代替 nRF 端实际 callback；
- 让 BleHub 通过 COM port 直接调用 nRF。

当 Windows 是 DUT 时，Windows 内置 Bluetooth controller 或独立 Central controller 承担 Central 角色，nRF 作为另一个独立的 RF 对端。nRF 不是 Windows BLE adapter，也不应被 Windows BLE backend 通过 COM port 直接使用。

---

## 4. 固定硬件和软件基线

当前仓库已经固定了以下基线：

| 组件 | 基线 |
|---|---|
| Hardware | Nordic nRF52840 Dongle，PCA10059 |
| USB 模式 | Nordic Connectivity firmware，USB CDC/serial |
| Connectivity image | 4.1.4 |
| SoftDevice | S132 5.1.0 |
| Python | 3.10.x |
| `pc-ble-driver-py` | 0.17.0，CPython 3.10，Windows x64 |
| `Blatann` | 0.6.0，已通过源码/API 静态验证，待项目 host 与 BleHub RF smoke 验证 |
| nRF Util recipe | 8.2.0 |
| 已安装 nRF Util | 当前现场为 8.2.1 |
| nRF Util device command | 2.20.1 |

已归档资产：

- `firmware/verified/connectivity_4.1.4_usb_with_s132_5.1.0.hex`
- `firmware/verified/connectivity_4.1.4_usb_with_s132_5.1.0_dfu_pkg.zip`
- `firmware/vendor/pc_ble_driver_py-0.17.0-cp310-cp310-win_amd64.whl`
- `firmware/SHA256SUMS.txt`

刷写规则：

- 默认使用 DFU ZIP，不由普通启动命令隐式刷写；
- 刷写必须是单独、显式、人工确认的操作；
- 刷写前确认 serial number、board 和 USB port；
- host 不得在启动失败时自动 erase、recover 或覆盖 firmware；
- 报告记录实际 firmware、SoftDevice、driver、Python 和 nRF Util 版本。

当前现场状态：已确认 PCA10059、serial `F3CDDDE125C5` 和 Connectivity application `COM11`；官方 nRF device driver 已安装，`nrfutil device list` 报告 `broken: false`。Blatann scanner 已通过该 USB CDC 链路运行。设备与 host 的 Peripheral GATT Server/advertising 以及 BleHub Central RF 交互仍未标记为通过。

---

## 5. 软件架构

### 5.1 模块结构

第一阶段预计在 `host/` 下建立：

```text
host/
└── nrftest/
    ├── __init__.py
    ├── cli.py                 # CLI 入口和退出码
    ├── protocol.py            # NDJSON 控制命令/响应
    ├── transport.py           # COM port/driver session
    ├── connectivity.py        # Connectivity 初始化和 observer 适配
    ├── profile.py             # profile 读取、校验和 GATT 构建
    ├── peripheral.py          # 广播、连接、GATT Server 操作
    ├── telemetry.py           # 结构化 ground-truth 输出
    └── recovery.py            # reset、reopen、恢复和 doctor
```

测试代码单独放在：

```text
tests/
├── test_protocol.py
├── test_profile.py
├── test_state_machine.py
├── test_fake_driver.py
└── test_hardware_smoke.py     # 显式硬件测试，不作为普通默认测试
```

### 5.2 Host 控制模型

Host 只允许一个活动 driver session 和一个命令 owner：

- 同一 COM port 不能被多个 nrftest 进程同时使用；
- 所有外部命令经过单一控制循环；
- Nordic callback 只做最小事件捕获和 owned payload 拷贝；
- 不在 driver callback 中执行长时间阻塞操作；
- callback 事件进入内部事件队列后由控制循环处理；
- Notification 使用 TX-complete 或等价发送进度控制节奏；
- Indication 必须等待 confirmation 后才能发送下一条；
- `shutdown`、`reset` 和连接清理必须是幂等的；
- driver/USB 错误必须区分为 environment failure，不得伪装为 BLE scenario failure。

`pc_ble_driver_py` 是第一阶段的直接硬件适配底层。`Blatann` 只有在实际验证能完整提供本计划所需的 Peripheral Server、write callback、Notification、Indication confirmation 和 reset/recovery 语义后，才可作为便利层使用。无论选择哪一层，外部 control protocol 不暴露其内部 API。

### 5.3 状态机

```text
Absent
  ↓ device detected
Bootloader / Unknown
  ↓ explicit firmware program and USB re-enumeration
ConnectivityReady
  ↓ profile loaded
ProfileLoaded
  ↓ advertising started
Advertising
  ↓ Central connects
Connected
  ├─ GATT operations / subscriptions / telemetry
  ├─ force disconnect → Advertising or ProfileLoaded
  ├─ reset → ConnectivityReady
  └─ driver/USB fault → Recovering
Recovering
  ↓ doctor + reopen + profile rebuild
ConnectivityReady or Failed
```

状态转换必须有明确的响应或 telemetry，不依靠日志文本猜测状态。

---

## 6. Profile 设计

### 6.1 Provider identity

nRF provider 使用独立的 profile identity：

- 独立 `profile_id`；
- 独立 local name；
- 独立 advertised/root service UUID；
- characteristic role、属性和初始值与共享基础 profile 保持一致；
- 运行 ID 通过 control session 传递，并在可行时编码进 manufacturer data；
- Central 发现必须使用 provider-specific service UUID，不能依赖 RSSI、发现顺序或固定 Bluetooth address。

nRF profile 的最终 UUID 不在本计划中提前随意指定，由实现阶段统一登记并固化。

### 6.2 基础 GATT 语义

nRF 第一版至少支持以下 role：

| Role | Properties | 目的 |
|---|---|---|
| `read-write` | read/write/write without response | 读写、回读和 write ledger |
| `updates` | read/notify/indicate | Notification、Indication 和 disable 后静默 |
| `read-only` | read | 固定设备信息和只读路径 |
| `write-only` | write/write without response | 写入路径和无响应写压力 |

Profile 构建器负责：

1. 读取 profile；
2. 校验 schema、UUID、属性、初始值；
3. 创建 service；
4. 创建 characteristic；
5. 建立逻辑 role 到 attribute handle 的映射；
6. 记录实际 handle 到 telemetry；
7. 在 profile 不合法时拒绝启动广播。

### 6.3 Profile drift 防护

nRF 不在实现代码中重新手写 Android/QEMU 的 GATT 语义。Profile 输入应来自共享 profile 的明确路径或经过 source commit/checksum 固化的 provider profile。

实现阶段必须增加：

- profile schema validation；
- provider identity isolation check；
- GATT semantic signature check；
- profile source revision/checksum 记录；
- 缺失 characteristic、属性漂移和初始值漂移的失败测试。

---

## 7. Control Protocol

第一阶段使用本地 NDJSON，不引入 web service。

### 7.1 控制命令

```text
 doctor
 load_profile {profile, run_id}
 start_advertising
 stop_advertising
 set_value {characteristic, bytes}
 emit_notification {characteristic, bytes}
 emit_notification_burst {burst_id, count, interval_us, payload_bytes}
 emit_indication {characteristic, bytes}
 emit_indication_burst {burst_id, count, interval_us, payload_bytes}
 force_disconnect
 reset
 status
 shutdown
```

每条命令都包含 `request_id`。命令响应只表示该控制操作已接受、拒绝或完成，不代替 BLE Central 侧结果。

### 7.2 推荐交互

```json
{"request_id":"r1","op":"doctor"}
{"request_id":"r2","op":"load_profile","profile":"nrf52840-basic-gatt-v1.json","run_id":"run-001"}
{"request_id":"r3","op":"start_advertising"}
{"request_id":"r4","op":"emit_notification","characteristic":"updates","bytes":"deadbeef"}
{"request_id":"r5","op":"force_disconnect"}
{"request_id":"r6","op":"shutdown"}
```

建议约定：

- stdin 接收控制命令；
- stdout 输出响应和 telemetry NDJSON；
- stderr 输出人类可读日志；
- stdout 不混入普通日志；
- 每条消息带 `protocol_version`；
- 每个 scenario 带 `run_id`；
- 退出码区分参数错误、环境错误、scenario 失败和正常关闭。

### 7.3 Telemetry

至少输出以下事件：

```text
ready {run_id, profile_id, serial, com_port, firmware, softdevice, service_uuids}
central_connected {connection_handle}
read {characteristic, value, connection_handle}
write {characteristic, write_type, value, connection_handle, sequence}
subscribed {characteristic, mode, connection_handle}
unsubscribed {characteristic, mode, connection_handle}
notification_accepted {sequence, payload_length}
notification_sent {sequence, payload_length}
indication_accepted {sequence, payload_length}
indication_confirmed {sequence, payload_length}
disconnected {reason, connection_handle}
reset_started
reset_completed
error {category, message}
```

Telemetry 必须区分：

- host 接受了发送请求；
- SoftDevice 接受了发送请求；
- Peripheral 侧发送进度 callback；
- Indication 收到 Central confirmation；
- Central 实际观察到的事件。

不能把 `notification_accepted` 当成 Central 已收到 Notification，也不能把 `indication_accepted` 当成 Indication 已确认。

---

## 8. Scenario 设计

每个 scenario 开始前必须完成：

```text
doctor
→ profile load
→ clean state
→ ready
→ advertising
```

每个 scenario 结束时必须完成：

```text
stop advertising
→ disconnect if needed
→ clear/rebuild state
→ preserve telemetry
```

### 8.1 `basic`

```text
advertise
→ Central scan/filter
→ connect
→ GATT discovery
→ read
→ write with response
→ write without response
→ verify telemetry
→ disconnect
```

### 8.2 `notifications`

```text
connect
→ subscribe Notification
→ send one payload
→ send paced burst
→ verify sequence
→ unsubscribe
→ verify disable 后无 stale delivery
```

### 8.3 `indications`

```text
connect
→ require Indication
→ send one indication
→ wait HVC confirmation
→ send paced burst
→ verify every sequence
→ unsubscribe
```

### 8.4 `passive_disconnect`

```text
connect
→ subscribe
→ force disconnect or reset Peripheral
→ observe Central passive Disconnected
→ clean recovery
→ advertise again
```

### 8.5 `restart`

```text
scenario running
→ stop advertising
→ close/reopen driver or reset device
→ doctor
→ rebuild profile
→ advertise
→ new run_id
→ Central reconnect
```

### 8.6 `stress`

至少覆盖：

- 10 次 scan/connect/disconnect cycle；
- 1000 个 Notification packet；
- 1000 个 Indication packet；
- 多批次 write ledger；
- host process restart；
- nRF reset/reopen；
- subscription disable 后没有 stale packet；
- 每批数据的 missing、duplicate、out-of-order、foreign-batch 和 malformed 检查。

---

## 9. BLESS 替代范围

### 9.1 nRF 作为主力替代 provider

完成本计划的 parity gate 后，nRF 作为日常测试的默认 Peripheral：

- 普通 scan/connect smoke；
- GATT discovery/read/write；
- Notification/Indication；
- 主动断连和恢复；
- 独立硬件 controller/GATT Server 互操作；
- 跨 Central 平台的真实 RF smoke；
- 稳定的重复连接和应用层 soak。

### 9.2 不属于替代范围的测试

nRF 不承担：

- Linux BlueZ/D-Bus 行为；
- QEMU USB passthrough；
- guest reboot 或 BlueZ daemon restart；
- BLESS/BlueZ 特有的 callback 和对象生命周期；
- `FakeBackend` 可精确注入的任意事件顺序；
- 非法 ATT/协议包和 controller firmware fault。

这些测试是否继续保留其他 provider，由使用它们的测试项目自行决定；`nrftest` 不复制这些实现，也不声称覆盖它们。

### 9.3 Provider parity gate

nRF 成为主力 provider 前，必须通过：

1. profile identity 和 GATT semantic signature 校验；
2. doctor/readiness；
3. scan/filter/advertisement；
4. connect/disconnect；
5. GATT discovery；
6. read；
7. write with response；
8. write without response；
9. Notification；
10. Indication confirmation；
11. subscription disable 后 stale-route silence；
12. 主动断连和恢复；
13. host restart/reopen；
14. 10-cycle lifecycle；
15. 1000-packet Notification/Indication soak；
16. telemetry 与 Central observation 的独立保存和关联。

任何未执行项都不能标记为通过。

---

## 10. 开发阶段

### Phase 0：设备与固件基线

- 找回并确认 PCA10059 设备；
- 记录 serial、USB port 和 board；
- 明确当前是 bootloader 还是 Connectivity image；
- 显式刷写已校验的 DFU ZIP（如确有需要）；
- 验证 USB 重新枚举；
- 通过 `pc_ble_driver_py` 完成 open、BLE enable 和版本 handshake；
- 固化实际环境报告。

退出条件：`doctor` 能可靠判断设备存在、firmware 可用和 driver 可用。

### Phase 1：最小 Peripheral

- 打开 driver；
- 创建单个 service；
- 创建 read/write characteristic；
- 启动广播；
- 接受 Central 连接；
- 接收 write callback；
- 发送单条 Notification；
- 发送并确认单条 Indication；
- 主动断连；
- 幂等关闭。

退出条件：完成一次真实 RF basic smoke。

### Phase 2：Profile 和控制协议

- 加入 profile parser/builder；
- 加入完整基础 GATT profile；
- 加入 NDJSON command/response；
- 加入 ready/status/telemetry；
- 加入 run ID 和 provider identity；
- 加入本地 unit tests 和 fake driver tests。

退出条件：host 不依赖手工 Python 交互即可由命令驱动完整 basic scenario。

### Phase 3：完整测试场景

- Notification/Indication burst；
- callback-driven pacing；
- write ledger；
- force disconnect；
- reset/reopen/recovery；
- 10-cycle scan/connect；
- 1000-packet delivery soak；
- 明确 environment failure 和 scenario failure。

退出条件：通过 provider parity gate。

### Phase 4：BleHub HIL 接入

- 提供稳定 CLI/NDJSON entrypoint；
- 由外部 HIL runner 启动和停止 nrftest；
- 保存 profile、run ID、control log、telemetry、Central event 和环境报告；
- 完成 Windows Central smoke；
- 完成 Android Central smoke（nRF 仍由 PC host 控制）；
- 运行独立硬件 reference report。

退出条件：nRF 可以作为 BleHub 日常 RF HIL 的默认 Peripheral，而不需要人工操作手机或 BLESS。

### Phase 5：维护和发布基线

- 固定 firmware/driver/Python/nRF Util manifest；
- 增加 firmware 和 wheel checksum 检查；
- 增加 doctor 诊断报告；
- 记录实际设备 serial 和 firmware revision；
- 明确每次 provider 运行的 capability 和 skip；
- 保留显式刷写、reset 和 recovery recipe；
- 不把硬件测试放入普通默认测试命令。

---

## 11. 验收报告

每次硬件运行至少保存：

```text
scenario/profile
run_id
nrftest source revision
firmware image and SHA-256
SoftDevice version
pc-ble-driver-py version
Blatann version if used
Python version
nRF Util version
serial number
COM port
Central OS and version
Central controller and driver
control commands
nrftest telemetry
BleHub observations
start/end timestamps
recovery actions
failure classification
```

报告必须分别标记：

- **test failure**：环境正常但 BleHub 或 scenario 语义失败；
- **environment failure**：设备、USB、driver、firmware 或 host 环境失败；
- **capability skip**：当前固件/控制器明确不支持某项能力；
- **not executed**：没有执行，不得折算为通过。

---

## 12. 主要风险和应对

| 风险 | 应对 |
|---|---|
| 设备处于 bootloader 或无法枚举 | `doctor` 先行；刷写单独显式执行；不自动 erase/recover |
| `nrfutil`、firmware 和 driver 版本不一致 | manifest 固定版本，报告记录实际版本，先做 handshake |
| `pc_ble_driver_py` 为 CPython 3.10 Windows x64 native wheel | 保持 Python 3.10 和 Windows x64 基线，不随意升级 Python |
| Blatann Peripheral Server API 不完整 | 先以 `pc_ble_driver_py` 直接 API 完成 M1；Blatann 只作为可选便利层 |
| Notification burst 丢包 | 使用 TX-complete、明确 pacing 和 sequence ledger，不把请求接受当成送达 |
| Indication 未确认就继续发送 | 以 HVC confirmation 作为下一次发送门槛 |
| Host 进程崩溃后 nRF 保留旧状态 | 下次启动先 doctor、清理、reset/reopen；必要时报告需要物理重插 |
| nRF profile 与共享 profile 漂移 | semantic signature、provider identity 和 source/checksum 校验 |
| 把 nRF 结果误认为覆盖 BlueZ/BLESS | 报告按 provider 分类，明确 nRF 只覆盖独立硬件 Peripheral 路径 |
| 过度依赖固定 Bluetooth address | 使用独立 service UUID、profile ID 和 run ID |
| 复杂协议使测试本身成为新故障源 | 第一阶段使用本地 stdin/stdout NDJSON，不引入 web service |

---

## 13. 当前下一步

按以下顺序执行，不先写复杂 scenario：

1. 保留已确认的 nRF52840 Dongle、serial、`COM11`、Connectivity application 和官方 driver 状态；
2. 对照 `firmware/SHA256SUMS.txt` 检查固件和 wheel；
3. 在 `host/` 实现最小 `pc_ble_driver_py`/Blatann Peripheral open/enable/advertise 探针；
4. 用一个 service、一个 characteristic 完成 BleHub Central 的第一次真实 RF connect/read/write；
5. 再加入 Notification、Indication 和 Peripheral telemetry；
6. 最后实现 profile、NDJSON、recovery 和完整 scenario；
7. 用 BleHub 的 Windows/Android Central HIL 记录独立的 RF 验收报告。

在 Phase 1 的真实 RF smoke 通过前，不将 nRF 标记为 BleHub 测试的默认 Peripheral，也不把当前固件资产存在视为硬件 provider 已通过。
