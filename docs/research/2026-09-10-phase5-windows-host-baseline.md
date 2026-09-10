# Phase 5 Windows Host 正式 API 基线

## 1. 状态

| 项目 | 状态 | 证据边界 |
|---|---|---|
| Windows x64 Phase 5 Host gate | **PASS** | doctor、basic GATT、Notification、Indication、10-cycle normal close/reopen 均由正式 API 实机复验 |
| Linux x64 | **未执行** | 不由 Windows 结果推断 |
| macOS Apple Silicon | **未执行** | 不由 Windows 结果推断 |
| Phase 5 整体 | **未通过** | 必须等三个 Host 平台分别完成第 17 节退出门 |

本报告记录的是 Windows 上第一套正式 `nrftest` Host 实现基线，不改变固件、Profile、BTP wire protocol 或 BleHub production API。

## 2. 固定对象

| 对象 | 固定值 |
|---|---|
| Host | Windows 11 x64，Python `3.12.14` |
| Board | Nordic nRF52840 Dongle，PCA10059 |
| 应用态 USB identity | `VID:PID=2FE3:0004`，serial `DBDBE94A2CED8C63` |
| 本轮枚举端口 | `COM13`；由 identity discovery 得到，未硬编码 |
| AutoPTS Windows tty 映射 | `/dev/ttyS12`，由 `COM13 → /dev/ttyS(N-1)` 规则计算 |
| Zephyr | upstream `v4.4.2`，commit `dccb09599635bdff17633fa7e9dab014b91dce90` |
| Zephyr SDK | `1.0.1`，`arm-zephyr-eabi` |
| AutoPTS | commit `54e81c7f3495bce72e5f688e9c996b85b8272799` |
| Windows socat | `1.7.3.2`，由项目 host-tools 管理和校验 |
| Profile | `profiles/blehub-nrf-basic-v1.json` |
| Profile SHA-256 | `7c6a51d45f928b645b9e3e7c68ecabb26070da5c63fe6ffef37d1300a0b30de1` |
| Root Service UUID | `fd000000-0000-4000-8000-000000000000` |
| Resident mapping | Service `33`；value handles `35/37/40/42`；CCC `38` |

固件仍使用 Phase 4 已验证的项目 patch：

```text
firmware/patches/
zephyr-v4.4.2-tester-single-subscriber-database-lifecycle.patch
```

本轮没有 remove/rebuild database、target reset、重新刷写或人工拔插。

## 3. 正式 Host 分层

| 文件 | 责任 | 明确不负责 |
|---|---|---|
| `host/nrftest/autopts_adapter.py` | 固定 AutoPTS transport/worker、串行化命令、response/event snapshot | 不重新定义 BTP framing、opcode 或 parser |
| `host/nrftest/profile.py` | JSON schema v1 校验、role 和语义签名 | 不保存运行时 value/CCC/connection 状态 |
| `host/nrftest/gatt_profile.py` | resident Profile attach/provision、初值恢复、实际 handle mapping | 不把固定 handle 写入 JSON |
| `host/nrftest/subscription.py` | CCC 精确值等待和 Set Value 后本地 readback | 不把 BTP success 解释为 Central 已收到 RF payload |
| `host/nrftest/fixture.py` | 面向 HIL runner 的同步 Peripheral 生命周期 API | 不启动、import 或配置 BleHub |
| `host/nrftest/telemetry.py` | UTF-8 JSON 原子报告写入 | 不合并 Peripheral 与 DUT 两侧事实 |
| `host/nrftest/cli.py` | 稳定 `doctor` 命令入口 | 当前不承载完整 scenario runner |
| `tools/btp_gatt_subscription_fixture.py` | 使用正式 Fixture API 编排一轮订阅 RF 场景 | 不直接访问 `pybtp`、裸 handle 或 AutoPTS 全局对象 |

正式 API 保持同步调用，不引入 `asyncio`、线程池或新的 runtime。一个 `PeripheralFixture` 独占一个 AutoPTS process-global client，并由 `AutoPtsSession` 保证同一 session 的 BTP command 串行化。

## 4. 本轮正式 API

| API | 语义和约束 |
|---|---|
| `PeripheralFixture.open(environment)` | 按 port override 或 USB serial identity 选择设备，加载固定 AutoPTS |
| `doctor()` | 完成 Core/GAP/GATT capability handshake 并读取 controller information |
| `load_profile(profile)` | provision 或 attach resident Profile，恢复 JSON 初值并重新核对 mapping；同一 session 禁止切换 topology |
| `start_advertising()` / `stop_advertising()` | 根据 JSON local name/root service UUID 控制 advertising；连接残留时拒绝启动 |
| `snapshot()` | 读取并同步当前 GAP settings、advertising 和单 peer 状态；发现多 peer 时失败 |
| `wait_for_connection(timeout)` | 等待一个真实 GAP connected event，并保存 peer identity |
| `wait_for_subscription_state(mode, enabled, timeout)` | 对当前 peer 和 `updates` CCC handle 等待精确 `0100`、`0200` 或 `0000` |
| `set_value(role, payload)` | 通过既有 BTP Set Value 更新 role，并验证 nRF 本地 attribute readback；不声称 Central 已收到 |
| `clear_write_events(role)` / `wait_for_write(...)` | 按实际 value handle 管理 Central write event，支持精确 payload 断言 |
| `wait_for_disconnection(timeout)` | 等待当前 peer 的 GAP disconnected event，确认 peer 已从状态中消失并清除 fixture connection |
| `disconnect_peer(trigger, timeout)` | 默认使用已验证的 radio-stack restart；direct GAP disconnect 必须显式选择，且当前实机已知失败 |
| `close()` | 幂等停止本 session 所有的 advertising 并释放 Host transport；不 unregister resident BTP services/database |

`reset()` 尚未并入正式 `PeripheralFixture`。现有 J-Link target reset 是 suite-boundary 可选灾难恢复工具，不是正常 testcase 或 Phase 5 必需依赖；在没有跨平台使用证据前不把它伪装成统一逻辑 reset。

## 5. 旧订阅夹具迁移

`tools/btp_gatt_subscription_fixture.py` 的 CLI、Just recipe、marker 和报告 schema 保持兼容，但场景编排已从底层调用迁移为：

```text
PeripheralFixture.open
→ doctor
→ load_profile
→ start_advertising
→ wait_for_connection
→ wait_for_subscription_state(enabled)
→ set_value
→ wait_for_subscription_state(disabled)
→ set_value after disable
→ wait_for_disconnection
→ close
```

保留的结构化 marker：

```text
NRFTEST_GATT_SUBSCRIPTION_READY
NRFTEST_GATT_SUBSCRIPTION_CONNECTED
NRFTEST_GATT_SUBSCRIPTION_ENABLED
NRFTEST_GATT_SUBSCRIPTION_UPDATE
NRFTEST_GATT_SUBSCRIPTION_DISABLED
NRFTEST_GATT_SUBSCRIPTION_DISCONNECTED
```

报告改用 `host/nrftest/telemetry.py` 的临时文件 + atomic replace 写入；JSON schema、目录、scope 和 confirmation 文案不变。

## 6. Windows 实机证据

### 6.1 正式 doctor

执行：

```text
pixi run just host-doctor
```

结果：

```text
.work/reports/host-doctor/20260909T175508Z.json
outcome=pass
platform=win32
GAP=attached
GATT=attached
profile_action=attached
service_handle=33
attribute_count=10
cleanup_classification=clean
```

这证明当前正式 API 能从一个新 Host 进程识别 resident BTP services、恢复 Profile 初值并核对实际 mapping。

### 6.2 Basic GATT read/write

`tools/btp_gatt_profile_probe.py` 保留原 CLI、Just recipe、marker 和报告 schema，但已迁移为正式 `PeripheralFixture` 生命周期。两种 write mode 分别运行，以同时获得 BleHub command 类型和 nRF Attribute Value Changed 两侧事实。

| 模式 | nrftest 报告 | Peripheral 事实 | BleHub 事实 |
|---|---|---|---|
| With Response，`75` | `.work/reports/btp-gatt-profile-probe/20260910T072030Z.json` | handle `35`、value `75`、count `1`；connected/disconnected；cleanup clean | initial `00`、written/readback `75`；`Windows single-write smoke: PASS` |
| Without Response，`76` | `.work/reports/btp-gatt-profile-probe/20260910T072153Z.json` | handle `35`、value `76`、count `1`；connected/disconnected；cleanup clean | initial `00`、written/readback `76`；`Windows single-write smoke: PASS` |

BleHub 每轮通过自己的公开 Central API 完成 scan/filter、connect、GATT discovery、read、指定 write mode、readback 和 disconnect。nrftest 不根据 BTP event 推断 write mode；它只证明 nRF 的动态 attribute 收到目标 payload。

### 6.3 Notification

并行执行：

```text
nrftest:
pixi run just btp-gatt-subscription-fixture \
  notification 71 72 "" profiles/blehub-nrf-basic-v1.json 120

BleHub:
pixi run just windows subscription-once-smoke \
  120 fd000000-0000-4000-8000-000000000000 \
  notification 71 72 2
```

结果：

| 观察方 | 结果 |
|---|---|
| nrftest | `.work/reports/btp-gatt-subscription-fixture/20260909T175254Z.json`，`subscription-rf-pass` |
| nRF CCC | enable `0100`，disable `0000` |
| nRF value | enabled `71`、after-disable `72`，BTP Set Value 后本地 readback 精确 |
| nRF lifecycle | connected/disconnected 均观察到；`attached`；cleanup `clean` |
| BleHub | `Windows single-subscription smoke: PASS`；收到 `71`，disable 后对 `72` 保持 2 秒 stale-route silence |

### 6.4 Indication

并行执行：

```text
nrftest:
pixi run just btp-gatt-subscription-fixture \
  indication 73 74 "" profiles/blehub-nrf-basic-v1.json 120

BleHub:
pixi run just windows subscription-once-smoke \
  120 fd000000-0000-4000-8000-000000000000 \
  indication 73 74 2
```

结果：

| 观察方 | 结果 |
|---|---|
| nrftest | `.work/reports/btp-gatt-subscription-fixture/20260909T175349Z.json`，`subscription-rf-pass` |
| nRF CCC | enable `0200`，disable `0000` |
| nRF value | enabled `73`、after-disable `74`，BTP Set Value 后本地 readback 精确 |
| nRF lifecycle | connected/disconnected 均观察到；`attached`；cleanup `clean` |
| BleHub | `Windows single-subscription smoke: PASS`；收到 `73`，disable 后对 `74` 保持 2 秒 stale-route silence |

固定 Tester 不提供机器可读的逐条 ATT Indication confirmation event。本轮 PASS 证明 Central 收到 strict Indication、CCC 精确状态和场景生命周期，不新增更强的 Peripheral confirmation 声明。

### 6.5 10-cycle resident lifecycle 与 Host reopen

随后由仓库外临时父进程顺序启动 10 组独立子进程。每轮 nrftest 和 BleHub 仍是两个独立进程；父进程只负责同时启动和收集退出状态，不进入任一项目运行时。

| 轮次 | 模式 | Payload | nrftest 报告 | 双侧结果 |
|---:|---|---|---|---|
| 1 | Notification | `81 → 82` | `20260910T072341Z.json` | PASS |
| 2 | Indication | `83 → 84` | `20260910T072424Z.json` | PASS |
| 3 | Notification | `85 → 86` | `20260910T072512Z.json` | PASS |
| 4 | Indication | `87 → 88` | `20260910T072554Z.json` | PASS |
| 5 | Notification | `89 → 8a` | `20260910T072636Z.json` | PASS |
| 6 | Indication | `8b → 8c` | `20260910T072718Z.json` | PASS |
| 7 | Notification | `8d → 8e` | `20260910T072803Z.json` | PASS |
| 8 | Indication | `8f → 90` | `20260910T072842Z.json` | PASS |
| 9 | Notification | `91 → 92` | `20260910T072923Z.json` | PASS |
| 10 | Indication | `93 → 94` | `20260910T073003Z.json` | PASS |

10 轮共同满足：

- 每轮创建新的 nrftest `PeripheralFixture`/AutoPTS Host session；
- 每轮 Profile `attached`，service handle `33`，没有 remove/rebuild/reset/拔插；
- 每轮 BleHub consumer 都是独立新进程，并通过自己的 controller 重新 scan/connect/discover；
- Notification/Indication CCC 分别精确为 `0100`/`0200`，disable 后为 `0000`；
- enabled payload 到达 BleHub，after-disable payload 通过本地 readback 且 BleHub 保持 2 秒静默；
- 每轮 nRF 均观察到 connected/disconnected；
- 每轮 nrftest 与 BleHub 都以 PASS 退出。

这组结果是正式 API 的 normal close/reopen 和 repeated resident lifecycle 证据；它不替代既有 Host force-kill 异常恢复或 1000-packet soak。

### 6.6 软件验证

| 命令 | 结果 |
|---|---|
| `pixi run pytest -q tests/unit/test_fixture.py tests/unit/test_subscription.py` | `15 passed` |
| `pixi run just fmt` | PASS，53 个文件无需改动 |
| `pixi run just lint` | PASS |
| `pixi run just test` | PASS，`89 passed` |

新增单元边界覆盖：

- Notification/Indication 到 exact CCC value 的映射；
- disable 必须等待 `0000`；
- CCC 读取使用当前真实 peer address/type；
- 未观察到连接时拒绝订阅和 peer write 等待；
- write event 使用 role 对应的实际 handle 并支持精确 payload 断言；
- 普通远端断连后清理 fixture connection；
- 默认 radio-stack restart 与显式 direct GAP disconnect 不混淆；
- closed fixture 拒绝继续读取状态。

## 7. 与既有 Phase 2–4 证据的关系

| 能力 | 既有证据 | 本轮变化 |
|---|---|---|
| basic GATT read/write | `docs/research/2026-09-09-phase2-dynamic-gatt-rf.md` | profile/write fixture 已迁移；两种 write mode 已通过正式 API 重跑 |
| Notification/Indication | `docs/research/2026-09-09-phase3-notification-indication-rf.md` | 两种模式已通过正式 Fixture API 重跑 |
| 10-cycle resident lifecycle | `docs/research/2026-09-09-phase4-database-lifecycle-and-reset.md` | 不重复破坏性 database 实验；正式 API 10-cycle 已整组重跑 |
| passive disconnect/recovery | 同一 Phase 4 报告 | 默认故障语义已进入正式 API；本轮未重复故障 RF |
| Host crash/reopen | Phase 1/4 报告 | force-kill 证据不变；本轮 doctor、两轮 write、两轮 subscription 和后续 10-cycle 均由新进程 clean attach/close |

既有 probe 保留为回归基线，不因正式 API 出现而删除。后续按场景逐条迁移，避免一次重写全部已验证工具。

## 8. 跨平台边界

当前代码结构已经避免把 `COM13`、Windows安装目录或 BleHub checkout 写入 Host API，但“代码可解析”不等于“平台已验证”：

| 边界 | Windows 当前实现 | macOS/Linux 待验证项 |
|---|---|---|
| serial discovery | `pyserial` 枚举并按 USB serial/VID/PID 选择 | 实际 device path、USB identity 字段稳定性 |
| AutoPTS tty 参数 | `COMN` 映射为 socat `/dev/ttyS(N-1)` | 原样使用 `/dev/tty.*` 或 `/dev/ttyACM*` 是否满足固定 AutoPTS |
| socat | 项目校验的 portable `1.7.3.2` | Pixi `socat` 可执行路径、启动和回收 |
| permission | Windows COM 当前可打开 | Linux udev/group；macOS CDC 权限和重枚举 |
| firmware/Profile | 当前固定候选 | 应保持相同，不因 Host OS 另建协议或 Profile |
| RF 结果 | Windows BleHub Central 已通过 | 必须由各平台独立 DUT/Controller 运行并保存两侧事实 |

Linux 和 macOS 适配只允许针对实测差异做最小修改；不得预先分叉另一套 BTP client、JSON schema 或 fixture 生命周期。

## 9. 仍未完成

1. 在 Linux x64 运行同一 Pixi/Just/firmware/Profile 入口；
2. 在 macOS Apple Silicon 运行同一入口；
3. 根据两个平台的实测差异做最小 transport/permission 适配；
4. 在需要统一暴露 suite-boundary recovery 时再决定可选 `reset()` API；正常 testcase 不依赖它；
5. 三个平台都完成 Phase 5 退出门后，才能把 Phase 5 标记通过。

## 10. 结论

Windows x64 的 Phase 5 Host gate 已通过：正式 API 继续复用固定 AutoPTS `pybtp`，保持 JSON Profile、resident database 和两项目 RF-only 边界，并已由独立 BleHub Central 实机证明 doctor、basic GATT、Notification、Indication、normal Host reopen 和 10-cycle resident lifecycle。当前结果不证明 Linux/macOS，也不代表 Phase 5 整体完成；下一步应在另外两个 Host 平台运行同一入口，只针对真实平台差异做最小适配。