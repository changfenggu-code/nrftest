# nrftest 跨平台源码与配置审计

## 1. 结论

| 对象 | 当前判定 | 证据边界 |
|---|---|---|
| Windows Host 功能源码 | **完成并回归通过** | 正式 API、132 项单元测试、静态检查及当前 PCA10059 `host-doctor` 通过 |
| Windows clean-machine 配置 | **部分通过** | 固定 upstream/Host tools/firmware tools 和 Pixi Runtime 通过；Nordic managed driver provisioning 尚未执行 |
| Linux x64 源码与 lock | **结构成立，未实机** | 依赖可从同一 `pixi.lock` 解析；udev、串口权限、socat teardown 和 RF 未执行 |
| macOS Apple Silicon 源码与 lock | **结构成立，未实机** | 依赖可从同一 `pixi.lock` 解析；USB CDC 枚举、设备权限、socat teardown 和 RF 未执行 |
| Phase 5 整体 | **进行中，未通过** | Windows gate 已通过，Linux/macOS gate 尚未执行 |
| “源码彻底完成” | **不能作此声明** | 固定 AutoPTS 的无界 stop/pending-command 回收仍需设计决策；两个非 Windows 平台仍需真实设备验证 |

本轮没有改变 BTP wire protocol、固件 Profile、nRF Peripheral 角色或 BleHub 独立 DUT 拓扑，也没有实现第二套 BTP Client。修改集中在 Host 独占、来源绑定、刷写安全、安装安全、报告持久化和跨平台命令入口。

## 2. 三平台静态基线

| 项目 | Windows x64 | Linux x64 | macOS Apple Silicon |
|---|---|---|---|
| Pixi target | `win-64` | `linux-64` | `osx-arm64` |
| 同一 `pixi.lock` 解析 | PASS | PASS | PASS |
| Python | `3.12` lock | `3.12` lock | `3.12` lock |
| DTC | 项目固定 portable `1.6.1` | Pixi `1.7.2` | Pixi `1.7.2` |
| socat | 项目固定 portable `1.7.3.2` | Pixi `1.8.1.3` | Pixi `1.8.1.3` |
| nRF Util | 固定 `8.2.1`/`nrf5sdk-tools 1.1.0` | 同版本平台 artifact | 同版本平台 artifact |
| AutoPTS serial path | `COMn → /dev/ttyS(n-1)` | `/dev/...` 原样 | `/dev/...` 原样 |
| 应用设备筛选 | `2FE3:0004` + optional serial | 相同 | 相同 |
| DFU 设备筛选 | `1915:521F` + 必选 bootloader serial | 相同 | 相同 |
| Host/RF 实机门 | PASS | 未执行 | 未执行 |

`pixi.lock` 可解析只证明依赖求解成立，不证明目标 Host 能访问 USB CDC、具备设备权限、正确终止 socat/AutoPTS 或完成 BLE RF 场景。

Windows 与 Linux/macOS 的 DTC、socat 版本不完全相同。当前这对 Host transport 设计是允许的，但跨 Host 固件产物是否字节一致只能通过 Phase 7 artifact 对比确认，不能由版本范围推断。

## 3. 本轮确认正确的既有结构

| 边界 | 静态事实 |
|---|---|
| 项目依赖 | `pixi.toml` 声明 `win-64`、`linux-64`、`osx-arm64`；Windows-only `pywin32`/`windows-curses` 和直接 `vc14_runtime` 正确分流 |
| 固件目标 | 三个平台都使用 Host 原生 Zephyr SDK 加 `arm-zephyr-eabi` 交叉工具链；这里的 `arm` 指 nRF52840 目标，不是 Host OS |
| 源码来源 | upstream Zephyr `v4.4.2`/固定 commit；AutoPTS 固定 commit；NCS 不参与活动构建 |
| 本机路径 | 安装根、工具、设备 serial 和输出目录来自 CLI/环境变量/ignored local TOML；活动源码无固定 COM 号或生效的 Windows 绝对路径 |
| RF 拓扑 | nrftest 只控制 nRF Peripheral；BleHub 使用自己的 Central Controller；两项目运行时只通过 BLE RF 交互 |
| Profile | JSON 继续定义 topology/properties/permissions/初值；runtime value 继续使用固定 AutoPTS BTP `Set Value` |
| 正常生命周期 | resident Profile attach/restore/remap；普通 testcase 不 remove/rebuild，不依赖人工拔插或 J-Link |

## 4. 已修复问题

| 级别 | 问题 | 修复 |
|---:|---|---|
| P0 | `_PROCESS_SESSION_LOCK` 只约束单 Python 进程 | 新增标准库 OS 文件锁；Windows 使用 `msvcrt.locking`，POSIX 使用 `fcntl.flock`；固定全 Host lock，不按会重枚举的 COM/tty；进程异常退出由 OS 释放 |
| P0 | 多个 Host 进程可竞争固定 AutoPTS socket/串口 | `AutoPtsSession.start()` 在 PATH 修改、controller 创建和串口打开前 fail-fast 获取全 Host lease；关闭和启动失败路径释放 |
| P0 | `load_autopts()` 可复用错误 `sys.modules` | root 必须 `resolve(strict=True)`；拒绝来源不在固定 root 的既有 `autopts.*`；导入后复核所有模块 origin；失败回滚本次模块；恢复原 `sys.path` |
| P0 | DFU 只按 COM/tty 名选择 | 强制 `VID:PID=1915:521F`、非空且精确匹配的 bootloader serial；nRF Util 启动前再次枚举并比较 port/VID/PID/serial |
| P0 | application serial 与 bootloader serial 混用风险 | 配置新增 `device.bootloader_serial`/`NRFTEST_BOOTLOADER_SERIAL`/CLI override；保留 `device.serial` 专用于应用态 `2FE3:0004` |
| P0 | flash 只验证 ZIP SHA，未验证 manifest 语义 | 同时验证 schema、board、format、`signed=false`、DFU 参数、安全相对路径和规范 lowercase SHA |
| P0 | elevated driver installer 真实 exit code 丢失 | PowerShell 显式 `exit $process.ExitCode`；非零禁止写 receipt |
| P0 | driver marker 任意文件即可通过 | 改为原子 JSON receipt，并严格绑定 `schema_version`、平台、固定版本和 installer SHA |
| P0 | platform setup 可能在配置失败前下载/安装 | Windows 在下载前要求 downloads/host-tools root；Linux 在下载前检查发行版及 sudo/dpkg；setup 结束调用 managed provisioning verify |
| P1 | 系统 VC Runtime 门与 Pixi 重复 | 从系统 lock、注册表探测和 UAC installer 中移除；在 `pixi.toml` 中声明 Windows `vc14_runtime` 直接依赖，由联合 lock 固定 |
| P1 | receipt 被误读为连接设备功能状态 | 输出和 recipe 改称 managed provisioning；receipt 只证明固定 installer 成功退出，实际 CDC/BTP/DFU 功能分别由 `host-doctor`/`firmware-flash` 证明 |
| P1 | 同秒报告覆盖、部分 probe 直接写最终 JSON | 公共 Telemetry 使用微秒 + UUID no-clobber 名称、同目录临时文件、flush/fsync、atomic replace；全部活动 BTP probe、target reset 和 flash 迁入公共 writer |
| P1 | 默认报告保存原始 Bluetooth address | 公共 Telemetry 默认递归脱敏 `address`/`peer_address`/`peripheral_address`，保留 address type、hardware serial、Profile identity 和 run ID；仅显式 debug 参数可保留原值 |
| P1 | 默认 Profile 依赖调用者 CWD | 正式 CLI 和 GATT tools 的默认 Profile 改为项目根绝对路径；显式 CLI path 语义不变 |
| P1 | stock recipe 使用 POSIX `NAME=value command` | build/package/flash 增加 Python `--build-dir`/`--cache-dir` override；Just 不再依赖 POSIX 环境前缀 |
| P1 | firmware setup 与系统提权边界不清 | `setup-firmware`/`firmware-reproduce` 只准备 build/package/flash 工具；driver/udev 仅由显式 `setup-platform-provisioning` 处理 |
| P1 | Host/firmware tool setup 先安装后发现 selector 错误 | Windows DTC/socat 和 nRF Util 在副作用前要求当前配置精确选择 managed executable；verify 使用相同边界 |
| P1 | 多个带 timeout 的 helper 泄漏 `TimeoutExpired` traceback | config、DTC、socat、nRF Util 和 package helper 转换为稳定领域错误 |
| P2 | advertising 命令已生效但 response 失败时 close 可能跳过 stop | start 调用前即标记为需要清理；失败 close 会 best-effort 查询/停止 |
| P2 | J-Link native close 抛错前丢失句柄 | 仅在确认 native close 成功后清除句柄，失败状态仍可诊断/重试 |

全 Host lock 文件默认位于：

```text
Windows: %TEMP%/nrftest/autopts-session.lock
POSIX:   <tempdir>/nrftest/autopts-session.lock
```

锁文件本身不会删除；锁定的是 OS handle，而不是文件是否存在。进程崩溃后 OS 释放 lock，不会因 stale 文件永久阻塞。

当前 ignored `nrftest.local.toml` 已分别记录本机 application serial 与 bootloader serial，没有记录固定 COM 号。

## 5. Windows 回归结果

### 5.1 软件和工具门

| 命令 | 结果 |
|---|---|
| `pixi run just fmt` | PASS |
| `pixi run just lint` | PASS |
| `pixi run just test` | PASS，`132 passed` |
| project diagnostics | PASS，0 errors / 0 warnings |
| `pixi run just verify-upstream` | PASS，68 个 West project、固定 Zephyr revision 和 ARM SDK |
| `pixi run just verify-host-tools` | PASS，DTC `1.6.1`、socat `1.7.3.2` |
| `pixi run just verify-firmware-tools` | PASS，nRF Util `8.2.1` + `nrf5sdk-tools 1.1.0` |
| `pixi run just check-tools` | PASS |
| Windows `vc14_runtime` direct dependency | PASS，manifest 显式声明且 lock 固定 `14.51.36247` |
| `pixi tree --platform win-64 --locked` | PASS |
| `pixi tree --platform linux-64 --locked` | PASS |
| `pixi tree --platform osx-arm64 --locked` | PASS |
| `just --dry-run setup-firmware` | PASS；只进入 source/SDK/Host tools 和 managed firmware tools，不包含 provisioning/UAC |
| `just --dry-run firmware-reproduce` | PASS；不包含 platform provisioning |
| stock build/package dry-run | PASS；使用 Python CLI override，无 POSIX env prefix |

单元测试包含真实第二 Python 进程对同一 lockfile 的竞争：持有者存在时第二进程失败；释放后可重新获取。它证明 OS 跨进程锁生效，但不替代 Linux/macOS 上真实 AutoPTS/socat teardown 门。

### 5.2 当前 PCA10059 实机 doctor

执行：

```text
pixi run just host-doctor
```

结果：

```text
outcome=pass
platform=win32
port=COM13 (identity discovery，未硬编码)
device_serial=DBDBE94A2CED8C63
profile_action=attached
profile_id=blehub-nrf52840-basic-gatt-v1
service_handle=33
attribute_count=10
cleanup_classification=clean
```

报告：

```text
.work/reports/host-doctor/
20260910T113257.344685Z-c889f2fb34f74d4bb82b40a8abb52c9c.json
```

报告中的 controller `address` 已验证为 `<redacted>`，address type、hardware serial、Profile identity 和 mapping 保留。本轮没有 remove/rebuild、target reset、重新刷写或人工拔插。既有 Windows basic GATT、Notification、Indication 和 10-cycle RF 证据仍有效；本轮修改未触及 BTP opcode、Profile topology 或固件字节。由于完整 RF 场景需要独立 BleHub DUT 进程配合，本次审计没有把单边 nrftest 等待命令伪装成新的双侧 RF rerun。

### 5.3 Runtime、managed provisioning 与设备功能已分层

Windows VC++ Runtime 现在由 `pixi.toml` 的 `win-64` 直接依赖和联合 `pixi.lock` 管理；系统 prerequisite lock、注册表检查、`vc_redist.x64.exe` 下载及 UAC 安装均已移除。`pixi lock` 报告 lock 已是最新状态，现有 lock 固定 `vc14_runtime 14.51.36247`。

只读状态：

```text
pixi run just platform-provisioning-status
```

结果：

```text
Platform: win-64
VC++ runtime: managed by the Pixi win-64 environment
nRF device-lib managed-install receipt: missing-or-invalid
Connected-device functionality: verify with host-doctor or firmware-flash
```

Nordic installer 仍按固定 commit URL/SHA-256 保留，用于未经配置的新 Windows Host、`nrfutil device` 和恢复路径；本轮没有运行会触发 UAC 的 `setup-platform-provisioning`。`verify-platform-provisioning` 因 receipt 不存在而按预期返回 exit 2；这只表示当前机器没有由这版 nrftest setup 建立受管安装来源记录，不表示驱动或设备功能缺失。同一轮当前应用态 `2FE3:0004` 已实测绑定 Microsoft `usbser.inf`，BTP `host-doctor` 仍以 resident attach、10 attributes 和 clean cleanup 通过；DFU 能力继续以 bootloader identity 校验和实际 `firmware-flash` 结果为最终证据。

## 6. 尚未解决或必须实机验证

| 级别 | 项目 | 当前边界与下一步 |
|---:|---|---|
| P1 | AutoPTS close 无硬截止时间 | 固定上游 worker join/BTP response 和 socat wait 可能无界。可靠解决需要上游 bounded stop 或把整个 AutoPTS 放入可 terminate/kill 的专用子进程；后者改变 Host 进程结构，实施前需用户批准 |
| P1 | command/start/close 跨线程竞态 | 当前公开 API 是同步 API，但没有声明或强制多线程安全。Phase 5 前应明确单调用线程约束，或在有界 teardown 方案确定后实现 lifecycle state machine |
| P1 | failure report envelope 不统一 | 持久化已统一，但 preflight 失败分类和“报告目录本身不可用”的机器可读 stderr 仍未统一；不影响 BTP 功能，影响 Phase 7 runner 集成质量 |
| P1 | Zephyr SDK 项目级 artifact receipt | 固定 Zephyr `west sdk install` 本地源码会下载官方 `sha256.sum` 并在解压前校验 minimal SDK archive；但项目 lock 没有固定该 checksum manifest 自身的摘要，安装后 verify 也只核对 version/compiler presence。Phase 7 需补项目级 artifact/receipt 身份 |
| P2 | Linux udev provisioning verify 过宽 | 当前仍按 `*nrf*.rules`/`*nordic*.rules` 判断；Linux 实机应改为精确 package/version/rule identity，串口 open/close 和 BTP 功能由独立 Host gate 验证 |
| P2 | 新 GATT build 失败的事务边界 | partial mutation 后无法总是安全回滚；需故障注入后决定 remove 或明确 `RECOVERY_REQUIRED`，不得在无证据时自动 destructive cleanup |
| P2 | Python dependency 范围较宽 | 当前 `pixi.lock` 精确，但多个 PyPI manifest 约束为 `*`；Phase 7 重新求解策略应增加兼容上界 |
| 实机 | Linux x64 | 从 lock 建环境、udev/current-user access、`/dev/ttyACM*`、socat orphan/SIGTERM、doctor/basic/Notification/Indication/reopen/10-cycle |
| 实机 | macOS Apple Silicon | 从 lock 建环境、`/dev/cu.usbmodem*`/`tty.usbmodem*`、quarantine/权限、CDC 重枚举、socat orphan、同一 Host/RF gate |
| 显式系统操作 | Windows managed driver provisioning | 新机或需要 Nordic device-lib/恢复能力时，经用户批准运行 `setup-platform-provisioning`；receipt 只证明固定 installer 成功退出，不能替代设备功能门 |

## 7. 最终判定

当前代码已经具备进入 Linux/macOS Phase 5 实机验证的结构基础，且 Windows 正式 Host API 仍然健康。不能把这一结论表述成“三个平台已经支持”或“全部源码彻底完成”：两个目标 Host 尚未执行，固定 AutoPTS 的无界关闭仍是已知生命周期风险。

建议执行顺序：

1. 先在 Linux x64 使用完全相同的 lock、固件、Profile 和 Just 入口跑 Phase 5；
2. 再在 macOS Apple Silicon 跑同一矩阵，只针对真实差异做最小适配；
3. 在新 Windows Host 或确需 Nordic device-lib/恢复能力时，经用户批准执行 managed provisioning，不把 receipt 当成功能测试；
4. 在跨平台实机门之前单独决定 AutoPTS 子进程隔离是否进入计划，不把它夹带进普通修复。
