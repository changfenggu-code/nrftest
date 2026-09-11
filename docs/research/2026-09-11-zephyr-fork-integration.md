# `VIDLG/zephyr` Tester 集成报告

## 1. 目的与结论范围

本次变更将 nrftest 的 Zephyr 固件来源从官方 Zephyr v4.4.2 checkout 切换为项目专用的公开 fork：

```text
https://github.com/VIDLG/zephyr.git
commit b64b351b49c027a1c56cb234f99538e168be315a
```

目标是把已验证的 Zephyr Bluetooth Tester 修复直接集成进 fork，使 nrftest 的默认构建直接使用集成源码，不在每台机器构建时复制 Tester 或应用本地 patch。

本报告记录源码、供应链和构建/package 事实。切换基线后尚未刷写 PCA10059，因此本报告不宣称新 fork 候选已经通过 BTP runtime 或 BLE RF 回归；此前 v4.4.2 的 RF 结果只作历史对照。

## 2. 版本和工作区

| 项目 | 本次固定值/事实 |
|---|---|
| Zephyr repository | `https://github.com/VIDLG/zephyr.git` |
| Zephyr commit | `b64b351b49c027a1c56cb234f99538e168be315a` |
| Zephyr reported version | `4.4.99` |
| west projects | 69，`west list` 精确 revision 验证通过 |
| Zephyr SDK | `1.0.1` |
| target toolchain | `arm-zephyr-eabi` |
| AutoPTS | `54e81c7f3495bce72e5f688e9c996b85b8272799` |
| Board | `nrf52840dongle/nrf52840` / PCA10059 |
| Firmware source | `tests/bluetooth/tester`，来自锁定 fork workspace |
| nrftest patch at build time | 无；build manifest 为 `tester_patch: null` |

fork 的源码 checkout 位于独立 west workspace，不复用或修改原官方 v4.4.2 workspace。nrftest 的 machine-local `zephyr_root` 已切换到该独立 workspace；路径仍由 `nrftest.local.toml` 选择，不写入 tracked 配置。

## 3. 当前 main 的源码审计

直接应用 v4.4.2 组合 patch 失败，原因不是 patch 文件损坏，而是 fork 当前 main 的 `btp_gatt.c` 已经改变了 `set_value()` 的实现。当前 main 已经具备：

- 按真实 attribute handle 遍历 `server_db`；
- `set_value()` 保留 `alloc_value()` 返回的 BTP status。

因此没有把旧版 `set_value()` hunk 机械复制到 fork。

审计确认当前 main 仍缺少以下 nrftest 必需修复：

1. `register_service()` 在 `add_service()` 递增 `svc_count` 后仍使用 `server_svcs[svc_count]`；
2. `add_service()` 在递增前没有保护 `SERVER_MAX_SERVICES`；
3. `alloc_value()` 对 `bt_gatt_notify()` 和 `bt_gatt_indicate()` 使用 `conn=NULL`，并丢弃 native immediate return；
4. `alloc_value()` 的旧 indication fallback 不能为 nrftest 提供唯一订阅者策略。

## 4. 已集成的最小改动

集成 commit 只修改：

```text
tests/bluetooth/tester/src/btp_gatt.c
```

改动包括：

| 改动 | 作用 |
|---|---|
| `server_svcs[svc_count - 1U]` | 使已递增的 service count 对应实际待注册槽位，修正 removal/register 的槽位一致性 |
| `svc_count >= ARRAY_SIZE(server_svcs)` | 在 service count 递增前阻止 service 数组越界 |
| `get_single_subscriber()` | 用 `bt_conn_foreach()` 和 `bt_gatt_is_subscribed()` 取得唯一匹配 LE connection，并正确管理 reference |
| 显式 `bt_gatt_notify(conn, ...)` | Notification 只对唯一 Notify subscriber 发送，并传播 native immediate status |
| 显式 `bt_gatt_indicate(conn, ...)` | Indication 只对唯一 Indicate subscriber 发送，并传播 native immediate status |
| `return status` | 保留 fork 当前 main 已有的 Set Value status 传播 |
| CCC 位测试 | 支持合法的 `NOTIFY|INDICATE` 组合值；没有把 CCC bitmap 错当作枚举值 |

`conn=NULL` 的广播到所有匹配 peer 语义没有被误称为随机选择。唯一 subscriber 是 nrftest 的测试夹具策略，不是 BLE 协议限制。native return 只表示本地 API 接受/拒绝，不表示 RF 已经被 Central 收到；最终交付仍由 BleHub Central 的观察证明。

该 commit 没有修改 BTP header、service/opcode、payload 或 wire framing，也没有修改 AutoPTS、Bluetooth Host core 或 Controller。

## 5. 构建流程变更

默认命令现在是：

```text
pixi run just firmware-build
pixi run just firmware-package
```

执行路径为：

```text
固定 VIDLG/zephyr commit
        ↓
west workspace/module verification
        ↓
直接构建 tests/bluetooth/tester
        ↓
合并 firmware/app/pca10059.conf 与 overlay
        ↓
生成并校验 ELF/HEX/BIN
        ↓
生成并规范化验证 PCA10059 DFU ZIP
```

已删除默认流程中的：

- Tester staging copy；
- local patch application；
- patch cache；
- `firmware-build-stock`、`firmware-build-patched` 及对应 package/flash 入口。

历史 patch（包括组合 patch）保留在 `firmware/patches/archive/`，只用于 provenance 和历史对照，不参与正式构建。构建 manifest 保留 `inputs.tester_patch = null` 以保持 schema 兼容，并明确声明没有应用 local patch。

`setup-upstream-sources` 不再把任意 SHA 直接传给 `west init --mr`，因为 west 的该参数在底层按 Git branch/ref 克隆。当前实现先用 fork 的 `main` 初始化 manifest，再 fetch 并 detached-checkout 锁定 commit，最后运行 `west update` 和完整 revision 验证。已有 origin、HEAD 或本机变更不匹配时直接失败，不 reset、不 checkout 用户已有 workspace。

## 6. 已执行验证

### 6.1 源码与工具链

```text
setup-upstream-sources    完成（独立 fork workspace）
verify-upstream           通过（69 West projects + SDK）
```

### 6.2 Host 静态/单元验证

```text
pixi run just fmt          通过
pixi run just lint         通过
pixi run just test         通过，136 passed
```

### 6.3 Firmware build

```text
pixi run just firmware-build    通过
```

实际使用：

```text
Zephyr 4.4.99
Board nrf52840dongle/nrf52840
CONFIG_LOG=n
CONFIG_UART_PIPE=y
CONFIG_BT_GATT_DYNAMIC_DB=y
Flash used 404472 B
RAM used 96760 B
```

build manifest：

```text
.work/build/pca10059-tester/nrftest-build-manifest.json
```

当前候选产物 SHA-256：

| 产物 | SHA-256 |
|---|---|
| `zephyr.elf` | `8db01483ac3b8330aea622642df9028c4733e7e0a9dddeaa786de4a116d3356f` |
| `zephyr.hex` | `0d85777ae7cbefc187ca93dfb7368349e1fe332283c330beaddf2564d5e3f567` |
| `zephyr.bin` | `7264f1c533f02da18cfabccbca3d96e9cc75d924b0cd74f7c8207402776af694` |

### 6.4 DFU package

```text
pixi run just firmware-package    通过
```

nRF Util `8.2.1` / `nrf5sdk-tools 1.1.0` 生成、时间规范化和 Nordic parser 验证均通过。

```text
.work/build/pca10059-tester/nrftest-pca10059-tester-v1.zip
DFU ZIP SHA-256:
916cab5e7c9a2f1a0cf0f038275fb6b9de24aae6f6e84d994011df462fe1730c
```

nRF Util 输出的 unsigned package 警告是预期的：该 DFU 只适用于当前无签名原厂 bootloader 的开发/测试路径，不是生产签名包。

## 7. 构建警告边界

当前 fork build 仍输出一个 Kconfig warning：Tester 的 `prj.conf` 选择了 `BTTESTER_LOG_LEVEL_DBG`，但 nrftest 通过 `CONFIG_LOG=n` 关闭 logging 后，该 choice 没有有效 selection。它不进入固件功能，也没有改变生成的 `CONFIG_LOG=n`；尝试在 extra config 中单独写 `CONFIG_BTTESTER_LOG_LEVEL_DBG=n` 不能覆盖 choice 的来源，因此已移除这条无效配置。

另一个 `BT_SUBRATING` capability warning 已通过 nrftest 的：

```text
CONFIG_BT_SUBRATING=n
```

消除。CMake 的 `drivers__console` empty-library warning 来自关闭 console 的预期结果，不影响链接；构建已成功完成。

## 8. 尚未执行的硬件门

切换 Zephyr 基线后，下列结果必须用新 fork 固件重新建立：

- 刷写与 application re-enumeration；
- BTP Core capability probe；
- GAP power/advertising；
- dynamic GATT discovery/read/write；
- Notification、Indication、CCC disable 和 stale-route silence；
- passive disconnect/recovery；
- 10-cycle resident lifecycle。

本次没有自动刷写、reset 或修改 bootloader。旧 v4.4.2 固件的 Windows RF 报告不能证明本 commit 的 RF 行为；只有新 package 刷入并完成上述矩阵后，才能把 fork 集成候选标记为通过。
