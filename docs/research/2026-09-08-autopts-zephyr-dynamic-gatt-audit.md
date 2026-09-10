# AutoPTS 与 Zephyr Tester 动态 GATT 审计

## 1. 报告状态与结论摘要

| 项目 | 内容 |
|---|---|
| 日期 | 2026-09-08 |
| 阶段 | Phase 2：动态 GATT Profile 审计与实机 follow-up |
| Zephyr | `v4.4.2` / `dccb09599635bdff17633fa7e9dab014b91dce90` |
| AutoPTS | `54e81c7f3495bce72e5f688e9c996b85b8272799` |
| 结论 | 固定上游已提供 fresh-database basic GATT 所需命令，nrftest 不需要实现第二套 BTP |
| 关键实现边界 | add/start helper 不返回可用 handle；Start Server 后必须查询并交叉验证 actual handle |
| CCC | notify/indicate characteristic 不会自动获得 CCC，必须显式 Add Descriptor `0x2902` |
| 写事件 | Central 写入会生成 Attribute Value Changed；Central read 没有 Peripheral 侧 read event |
| 生命周期 | remove 只注销整个动态 service，不回收 Tester 构建资源；不是完整 reset/rebuild |
| 已决策缺口 | 用户批准 active canonical `write-only.initial_value_hex="00"`，使用 1-byte 固定 value；schema 仍为 v1 |

本报告结论来自固定本地 AutoPTS 和 Zephyr source，并补充 PCA10059 dynamic database 与 BleHub GATT discovery 实机结果。actual handles、可读初值和公开 RF discovery 已验证；2026-09-09 follow-up 又完成 1-byte read、两种 write 和 Attribute Value Changed 双侧对账。removal/rebuild 仍未验证，不能外推。

## 2. 现成 AutoPTS API

固定 AutoPTS 通过 `autopts.pybtp.btp` 暴露以下 API：

| API | 主要参数 | Python 返回 | 固定 Tester 语义 |
|---|---|---|---|
| `gatts_add_svc(svc_type, uuid)` | `0` primary、`1` secondary；UUID | `None` | 增加动态 service；wire response ID 被 pybtp 丢弃 |
| `gatts_add_char(hdl, prop, perm, uuid)` | 固定 Tester 要求 `hdl=0`；property/permission bit；UUID | `None` | 连续创建 declaration 与 value 两个 attributes |
| `gatts_add_desc(hdl, perm, uuid)` | 固定 Tester 要求 `hdl=0`；permission；UUID | `None` | descriptor 附到最近 characteristic；CCC 需 UUID `2902` |
| `gatts_set_val(hdl, val)` | `hdl=0` 表示最后一个 attribute；`val` 是 hex 文本 | `None` | 首次调用分配固定长度 value buffer |
| `gatts_start_server()` | 无 | `None` | 调用 `bt_gatt_service_register()`，此后才有 actual handles |
| `gatts_get_attrs(start, end, type_uuid)` | handle 范围；可选 UUID filter | attribute tuple list | 枚举已注册 database，是 actual handle 的主要来源 |
| `gatts_get_attr_val(addr_type, addr, handle)` | peer address/type；actual handle | ATT status、长度、value bytes | 回读 declaration/value/descriptor |
| `gatts_get_handle_from_uuid(uuid)` | UUID | `int` handle | 固定 handler 对 128-bit UUID 使用尺寸不足的 `struct bt_uuid`；实机返回 failure，active 128-bit Profile 不调用 |
| `remove_handle_from_db(handle)` | service 内任一 actual handle | `None` | 注销包含该 handle 的整个 service，不是移除单 attribute |
| `gatts_change_database(0, 0, operation)` | 仅固定参数 | `None` | 只操作 Tester 内建 PTS test service，不操作动态 Profile |

固定 Zephyr `btp2bt_uuid()` 只接受 16-bit 或 128-bit UUID。AutoPTS helper 虽能编码 32-bit UUID，但当前 Tester 会拒绝；活动 schema v1 使用 128-bit UUID，不受影响，后续 validator 不得把 32-bit helper 能力误写为 target capability。

## 3. Property 与 permission 映射

| Role | GATT properties | `prop` | Derived permissions | `perm` |
|---|---|---:|---|---:|
| `read-write` | read/write/write without response | `0x0e` | read/write | `0x03` |
| `updates` | read/notify/indicate | `0x32` | read | `0x01` |
| `read-only` | read | `0x02` | read | `0x01` |
| `write-only` | write/write without response | `0x0c` | write | `0x02` |
| CCC | descriptor `2902` | — | read/write | 调用值可用 `0x03`；Tester CCC helper 实际固定 read/write |

该映射只使用 AutoPTS 现有 `Prop`/`Perm` 定义。nrftest 薄层可以根据 schema properties 派生 bits，不需要在 JSON 中加入 BTP opcode 或 provider class。

## 4. Database 构建与 handle 映射

### 4.1 构建期不能依赖返回 handle

AutoPTS 的 add/start helper 只检查 status，不返回 wire payload。固定 Tester 在 service 注册前也没有 final attribute handle，因此构建期使用上游规定的 `0` 相对引用：

```text
Add Service(root)

对每个 characteristic：
    Add Characteristic(0, properties, permissions, uuid)
    Set Value(0, initial value hex)
    如果需要 notify/indicate：
        Add Descriptor(0, read|write, 2902)

Start Server
```

带 CCC 的 characteristic 必须先 Set Value、再 Add CCC。若先加 CCC 再执行 `Set Value(0, ...)`，`0` 会指向最后添加的 CCC；Tester 对 CCC Set Value 可能返回 success 但不设置 characteristic value。

### 4.2 Start Server 后查询 actual handles

推荐通过三类证据构建 role→actual handle map：

1. `gatts_get_attrs(type_uuid=root_uuid)` 获取 service handle；
2. `gatts_get_attrs(type_uuid="2803")` 获取 declaration handles；
3. `gatts_get_attr_val(..., declaration_handle)` 解析 declaration value 中的：

```text
properties:u8
value_handle:u16 little-endian
characteristic_uuid:2 or 16 bytes little-endian
```

然后以 `gatts_get_attrs()` 枚举出的 value attribute 与 declaration 内显式 value handle/UUID 交叉验证，并在 characteristic handle 区间内定位 `0x2902` CCC。固定 Tester 的 128-bit `GET_HANDLE_FROM_UUID` 已实机失败且源码使用尺寸不足的 `struct bt_uuid`，因此不作为 active 128-bit Profile 证据。

当前实现通常满足 `value_handle = declaration_handle + 1`，但 nrftest 不应把 `+1` 固化为跨版本契约；declaration 内显式 value handle 才是权威关系。

活动 basic Profile 预期布局为：

```text
root service                                      1
4 × characteristic declaration/value             8
updates CCC                                       1
                                                   ─
total                                             10 attributes
```

actual handles 必须非零、唯一并写入报告，不能硬编码。

### 4.3 Set Value success 不是充分证据

stock Zephyr `set_value()` 内部会计算分配/设置 status，但 handler 最终无条件返回 BTP success，同时 `alloc_value()` 丢弃 Notification/Indication native return。故每个 initial value 在 Start Server 后必须通过 `gatts_get_attr_val()` 回读；RF delivery 再由独立 Central 侧证据证明。Phase 3 的 project-managed patch 已把 allocation/native send 的 immediate success/failure 纳入现有 BTP status，但仍不把 response 提升为异步 RF delivery 事实。

## 5. CCC 与订阅边界

CCC 不会因 characteristic 具有 notify/indicate properties 自动生成。固定 Tester 的 CCC helper：

- 要求最近 characteristic 至少具有 Notify 或 Indicate；
- 每个 characteristic 最多一个 CCC；
- 使用 `BT_GATT_CCC` 创建 descriptor；
- CCC 权限固定为 read/write；
- 当前 `MAX_CCC_COUNT=2`，basic Profile 使用 1 个；
- 在 Tester 自有 tracking array 中没有完整的 remove/rebuild 回收路径。

Phase 2 只验证 CCC attribute 已构建并被 BleHub discovery 发现。Phase 3 随后分别以 `0100`、`0200` 和 `0000` 完成 Notification、Indication、disable 和取消后静默双侧 RF；固定 Tester 仍没有机器可读的逐条 Indication confirmation event。不得用“CCC 存在”或 Set Value response 替代对应 Central delivery 事实。

## 6. Read、write 与 Peripheral 事实

| 操作 | nRF/Tester 行为 | AutoPTS Host 可见事实 | 证据限制 |
|---|---|---|---|
| Central read | `read_value()` 返回当前 value | 无 server-side read event | 必须使用 BleHub read 结果证明 |
| Write with response | `write_value()` 成功后发 Attribute Value Changed | handle、最新 value、累计 changed count | event 不含 peer address |
| Write without response | 同样发 Attribute Value Changed | 与有响应写相同 | 一次写一次等待可作为 Phase 2 证据 |
| Host `gatts_set_val()` | 更新本地 value；订阅后还可能触发 update | 不产生 Attribute Value Changed | 不能把 Host set 当 Central write；RF delivery 需 Central 证据 |
| Central 写 CCC | CCC helper 更新订阅状态 | 可按实际 peer 读取两字节 CCC payload | 固定 metadata 路径可能同时返回伪 ATT `0x0c`；仅 CCC reader 接受并严格核对 payload |

Attribute Value Changed event 格式为：

```text
handle:u16
value_length:u16
value:bytes
```

AutoPTS worker 已负责 event parsing，并提供：

```text
stack.gatt.wait_attr_value_changed(handle, timeout)
stack.gatt.attr_value_get(handle)
stack.gatt.attr_value_get_changed_cnt(handle)
stack.gatt.attr_value_clr_changed(handle)
```

value 在 AutoPTS state 中保存为 hexlify 后的 bytes。它只保留最新值和累计次数，不保留逐包有序 ledger；Phase 2 应使用“一次写→一次等待→核对 handle/value/count”，不能用它证明吞吐或逐包无损。

## 7. Value 长度约束与 write-only 缺口

固定 Tester 的动态 value buffer 不是可增长容量：

1. 首次 `gatts_set_val()` 按该次 value 长度分配；
2. 后续 `gatts_set_val()` 要求长度完全相同；
3. Central write 要求 `offset + len <= 当前 value length`；
4. 未调用 `gatts_set_val()` 的 characteristic value 长度为 0，任何非空 Central write 预计返回 Invalid Attribute Length。

归档并已迁移的 canonical JSON 中，`write-only` 没有 `initial_value_hex`。schema v1 parser 按既有语义将缺省值解释为 `b""`。因此若原样构建，该 characteristic 没有非空写容量，和 role 的“写入/无响应写”用途冲突。

可选处理：

| 方案 | schema 影响 | 能否测非空 write-only | 评价 |
|---|---|---:|---|
| A. 为 canonical `write-only` 增加 `initial_value_hex: "00"` | 仍是 schema v1；改变 active Profile 数据和 signature | 是，先验证 1-byte | **最小、显式、推荐** |
| B. 保持 JSON 不变，Phase 2 只写 `read-write` | 不变 | 否 | 可通过部分命令门，但不能兑现 write-only role 用途 |
| C. BTP mapping 暗中为空值填充 1 byte | JSON/signature 不反映真实 target | 是 | 隐式 provider policy，不推荐 |
| D. schema v2 增加 capacity/max length | 新 schema 与计划评审 | 是，可表达可变容量 | 当前过度设计；只有真实需求证明后再考虑 |

用户已明确选择方案 A。active canonical JSON 已增加 `write-only.initial_value_hex: "00"`，source SHA-256 更新为 `7c6a51d45f928b645b9e3e7c68ecabb26070da5c63fe6ffef37d1300a0b30de1`；归档 JSON 保持不变。

## 8. Dynamic removal 不是完整 reset

真正对应动态 service 的 API 是：

```text
remove_handle_from_db(actual_handle)
```

它注销包含该 handle 的整个 service，但固定 Tester 不回收完整构建器状态：

| 状态 | remove 后行为 |
|---|---|
| Zephyr 已注册 database | service 被注销，不再由正常 attribute 枚举返回 |
| `server_db` / value buffer | 静态尾指针不回退，内存不回收 |
| `attr_count` / `svc_count` | 不减少 |
| Tester CCC slots | 不完整回收 |
| 旧 Zephyr handles | 注销后归零 |
| UUID lookup | 遍历历史 `server_db`，同 UUID rebuild 可能先命中 stale zero handle |
| rollback | Add/Set/Start 失败没有完整事务回滚 |

固定实现也没有可调用的完整 Reset Server：header 虽定义 reset opcode，但 Tester handlers 与 AutoPTS wrapper 未实现；`tester_unregister_gatt()` 也不释放动态 database。

因此 Phase 2 正常路径必须先验证 target database 是 fresh 或已是同一已知 Profile，不能盲目重复 Add。Profile removal/rebuild、同 UUID 重建、无泄漏循环和自动 target reset 仍属于 Phase 4。

## 9. Phase 2 推荐实施顺序

write-only 决策完成后，仍全部复用 AutoPTS：

1. 检查实际 GATT supported-command mask 是否包含 Add Service/Characteristic/Descriptor、Set Value、Start Server、Get Attributes/Get Attribute Value、Remove Handle；
2. 加载 active Profile，记录 profile identity、semantic signature 与 source SHA-256；
3. 确认 target 没有冲突的已注册 root Profile；
4. 按本报告顺序 Add Service、四个 Characteristics、initial values 和一个显式 CCC；
5. Start Server；
6. 枚举并交叉验证 10 个 actual attributes；
7. 回读全部 initial values；
8. 再启动 provider-specific advertising；
9. 独立 BleHub 执行 discovery/read/write with response/write without response；
10. nrftest 对每次 Central write 核对 Attribute Value Changed handle/value/count；
11. 正常结束时停止 advertising、结束连接、关闭 Host transport，保留 BTP service，不把 dynamic removal 当作完整 reset。

## 10. 源码证据索引

```text
E:/dev/nrftest-upstream/auto-pts/
  autopts/pybtp/btp/gatt.py
  autopts/pybtp/btp/btp.py
  autopts/pybtp/types.py
  autopts/ptsprojects/stack/layers/gatt.py
  autopts/wid/gatt.py

E:/dev/nrftest-upstream/zephyrproject/zephyr/
  tests/bluetooth/tester/src/btp_gatt.c
  tests/bluetooth/tester/src/btp/btp_gatt.h
  tests/bluetooth/tester/src/btp_core.c

D:/projects/nrftest/
  docs/PLAN.md
  profiles/blehub-nrf-basic-v1.json
  host/nrftest/profile.py
  tests/unit/test_profile.py
```

## 11. 当前判断

固定 upstream 足以实现 fresh-database、单 Profile、单次写逐项确认的 Phase 2 basic GATT，不需要自研 BTP。四-role dynamic mapping、actual handles、本地可读初值、BleHub RF discovery/read/two-mode write 与 nRF Attribute Value Changed 双侧证据均已通过。Phase 3 进一步证明 stock Notification 可用，但 stock Indication 的发送选择和 Set Value status 需要 fixed-revision 最小 patch；标准 BTP wire format 仍然足够。

## 12. 实机 follow-up

### 12.1 Profile build 与 resident attach

第一次 `btp-gatt-profile` 已完成 Add/Set/Start，但随后在 metadata 验证阶段失败；dynamic service 因此保留在 target。后续没有重复 Add，而是按 registered database 识别同一 Profile 并 attach。两个独立进程最终报告均通过：

```text
.work/reports/btp-gatt-profile-probe/20260908T152659Z.json
.work/reports/btp-gatt-profile-probe/20260908T152745Z.json
```

稳定 mapping：

| 对象 | Handle |
|---|---:|
| Primary service | 33 |
| `read-write` declaration/value | 34 / 35 |
| `updates` declaration/value/CCC | 36 / 37 / 38 |
| `read-only` declaration/value | 39 / 40 |
| `write-only` declaration/value | 41 / 42 |

两次均为 `GAP=attached/GATT=attached`、`attribute_count=10`、cleanup clean；可读 characteristic 的本地初值回读与 active Profile 一致。

### 12.2 固定 Tester 的三个查询表示边界

| 现象 | 固定源码根因 | active 处理 |
|---|---|---|
| Service/Characteristic Declaration 本地读可能返回 ATT `0x0c`，同时 payload 有效 | `get_attr_val_rp()` 对所有 dynamic `server_db` attributes 都把不同类型的 `user_data` 强转成 `gatt_value` 并读取 `enc_key_size` | 仅 metadata reader 接受 `0x00/0x0c` 且仍严格解码 payload；普通 value read 只接受 success |
| read-write value permission 枚举为 `0x43` 而请求是 `0x03` | `alloc_characteristic()` 无条件 OR Zephyr native `BT_GATT_PERM_PREPARE_WRITE (0x40)`，`GET_ATTRIBUTES` 原样返回 native `attr->perm` | BTP add 仍传 `0x03`；注册后验证预期 native permission 为 `0x43` |
| 128-bit `GET_HANDLE_FROM_UUID` 返回 BTP failure | handler 用 `struct bt_uuid search_uuid`，而 `btp2bt_uuid()` 对 128-bit 写入 `BT_UUID_128(...)->val[16]`；同文件其他安全调用使用 `union uuid` | active 128-bit mapping 不调用该 helper，使用 registered enumeration + declaration value handle/UUID 交叉验证 |

这些处理没有关闭 assertions、修改 upstream 或复制 BTP parser。

### 12.3 BleHub 公开 GATT discovery

外部执行环境并行运行：

```text
nrftest:
pixi run just btp-gatt-rf-fixture "" profiles/blehub-nrf-basic-v1.json 120

BleHub:
pixi run just windows gatt-smoke 120 fd000000-0000-4000-8000-000000000000
```

结果：

```text
nRF dynamic GATT RF fixture: PASS
Windows GATT smoke: PASS
```

Peripheral 报告：

```text
.work/reports/btp-gatt-profile-probe/20260908T153219Z.json
```

该结果证明 BleHub 通过自己的 Windows Central Controller 发现 primary root service、四个 characteristic 的预期 properties 和 updates CCC；同时 nRF 侧记录 connected/disconnected。该次 discovery 运行本身不证明 read/write 或 Notification/Indication。

### 12.4 BleHub read/write 与 Attribute Value Changed

2026-09-09 外部执行环境使用 BleHub 自有的通用参数化 Windows HIL consumer，分别执行 1-byte write with response `10` 和 write without response `ab`。两轮 BleHub 均先读到初值 `00`、收到对应写终端、再读回目标值并输出 `Windows single-write smoke: PASS`；nRF 均报告 handle `35`、精确 value、`changed_count=1`，随后观察断开且 cleanup clean。

Peripheral 原始报告：

```text
.work/reports/btp-gatt-profile-probe/20260908T231550Z.json
.work/reports/btp-gatt-profile-probe/20260908T231654Z.json
```

首次轮次在设备重新插入后的 fresh boot 上执行，mapping action 为 `built`；第二轮为独立 Host 进程的 `attached`。详细双侧证据与限制见 [`2026-09-09-phase2-dynamic-gatt-rf.md`](2026-09-09-phase2-dynamic-gatt-rf.md)。

### 12.5 AutoPTS changed-state 本地初始化

固定 AutoPTS 的 `attr_value_clr_changed()` 在新 Host 本地 `server_db` 尚无目标 handle 时会记录 `No attribute with 35 handle`；同一实现的 `wait_attr_value_changed()` 才会按需创建该本地槽。这不是 BTP/ATT/nRF failure。活动 adapter 在 clear 前增加一次 `timeout=0` 的本地 wait，以非阻塞方式确保槽存在，再严格清零 value/count。单元测试与实机回归均通过；回归报告如下，日志已消失：

```text
.work/reports/btp-gatt-profile-probe/20260908T232348Z.json
```

该兼容处理不发送 BTP command、不修改 upstream，也不改变 Attribute Value Changed 的 handle/value/count 判定。

### 12.6 Phase 3 Notification/Indication follow-up

stock Tester 的 Notification 首轮双侧通过；stock Indication 在 CCC=`0200` 后表面返回 BTP success 并更新本地 value，但 BleHub 没有收到 update。只传播 status 的诊断候选把同一路径暴露为 immediate BTP failure，证明 stock 隐藏了 `bt_gatt_indicate(NULL, ...)` 的同步负返回。indication-only 候选显式选择 Indication 连接后完成一轮 Notification/Indication，但后续 Notification 的 `conn=NULL` 路径稳定返回 BTP failure，Host off/on 后仍未恢复。

最终 update-single-subscriber patch 对两种模式都通过 `bt_gatt_is_subscribed()` 选择唯一当前连接，再调用标准 `bt_gatt_notify(conn, ...)` 或 `bt_gatt_indicate(conn, params)`，并传播 allocation/native send status；BTP wire format、AutoPTS Client 和 Profile 均未改变。active candidate 连续通过 Notification `11`、Indication `21`、Notification `31`，每轮都完成 CCC disable、after-disable readback 和 2 秒 Central 静默。固定 Tester 的 Indication callback 没有 BTP event，因此本轮不声称 Peripheral Host 机器可读地观察到 ATT Confirmation。完整对照、固件身份和双侧证据见 [`2026-09-09-phase3-notification-indication-rf.md`](2026-09-09-phase3-notification-indication-rf.md)。
