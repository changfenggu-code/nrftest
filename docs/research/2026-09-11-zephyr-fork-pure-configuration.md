# Zephyr Tester 纯净配置验证

## 1. 变更目标

在不改变 BTP、USB CDC/UART_PIPE 和 BLE 行为的前提下，移除正式 Tester 固件中没有实际作用的日志与 Console 配置，避免文本输出污染 BTP 二进制通道，并消除构建 warning。

## 2. 变更位置

| 位置 | 变更 |
|---|---|
| `VIDLG/zephyr/tests/bluetooth/tester/prj.conf` | 删除 `CONFIG_BTTESTER_LOG_LEVEL_DBG=y` |
| `nrftest/firmware/app/pca10059.conf` | 增加 `CONFIG_CONSOLE=n`、`CONFIG_PRINTK=n` |
| `nrftest/tools/build_firmware.py` | 将 `CONFIG_CONSOLE=n`、`CONFIG_PRINTK=n` 纳入构建不变量校验 |
| `nrftest/tests/unit/test_build_firmware.py` | 增加最终配置的 Console/printk 关闭回归样例 |

`CONFIG_LOG=n` 原本已经存在并继续保留。没有修改 Zephyr logging、Console、USB CDC 或 UART 驱动实现；唯一的 fork 源码改动是移除 Tester 配置中在 `CONFIG_LOG=n` 下无效的 log-level choice。

## 3. 最终控制面配置

```text
CONFIG_UART_PIPE=y
CONFIG_UART_CONSOLE=n
CONFIG_CONSOLE=n
CONFIG_PRINTK=n
CONFIG_BOOT_BANNER=n
CONFIG_TEST_LOGGING_DEFAULTS=n
CONFIG_LOG=n
```

BTP 通道仍为：

```text
USB CDC ACM → board_cdc_acm_uart → UART_PIPE → BTP Server
```

生成配置已确认 `CONFIG_USBD_CDC_ACM_CLASS=y`、CDC 自动初始化/enable 和 `CONFIG_UART_PIPE=y`；关闭 Console/printk 没有关闭 USB CDC 或 BTP transport。

代价是普通 `printk`、assertion/fatal 的文本诊断不再可见。`CONFIG_ASSERT=y`、`CONFIG_ASSERT_LEVEL=2` 仍然保留，但 `CONFIG_ASSERT_VERBOSE=n`。需要故障文本时必须使用单独的诊断固件/输出通道；不能将没有日志当作没有故障，也不能推断所有内部格式化函数都已从镜像中删除。

## 4. 为什么不能只在 nrftest extra config 中写 log level

Tester 的 log-level choice 在 Zephyr Kconfig 中依赖 `LOG`。当 `CONFIG_LOG=n` 时，`CONFIG_BTTESTER_LOG_LEVEL_DBG=y` 没有有效 choice selection；在 nrftest extra config 中再写 `...DBG=n` 不能可靠消除上游 `prj.conf` 对该 choice 的选择警告。

因此，将无效设置从专用 fork 的 Tester `prj.conf` 删除是正确层级的修复，而不是重新打开 logging 或增加第三套 patch。

## 5. 验证结果

fork 最终 commit：

```text
f530afbe09cb3b3d96dee432676aaafd56a8a93d
```

执行：

```text
pixi run just update-upstream-sources
pixi run just verify-upstream
pixi run just firmware-build
pixi run just firmware-package
```

结果：

- 69 个 west project 和 Zephyr SDK `1.0.1` 验证通过；
- Kconfig 的 `BTTESTER_LOG_LEVEL_DBG` warning 消失；
- `drivers__console` 空库 warning 消失；
- `zephyr.elf` 正常链接；
- 生成配置确认 `CONFIG_CONSOLE`、`CONFIG_PRINTK`、`CONFIG_LOG` 均未设置；
- `CONFIG_UART_PIPE=y` 保持；
- `CONFIG_UART_CONSOLE=n` 保持；
- DFU package 生成和 Nordic parser 验证通过。

### 最终产物

| 产物 | 大小 | SHA-256 |
|---|---:|---|
| `zephyr.elf` | 5,646,332 B | `ec4e6f07f1a13bd24f6122b268eee3209c98064dcc3148bb3647cfbfd8584167` |
| `zephyr.hex` | 1,030,908 B | `1b2f478a83b5ec7508cce67ef8c62c740a4e5fb87319896d2916e8a160f9784d` |
| `zephyr.bin` | 366,460 B | `08495c32959adbc01abc81f3283cdfd0136ae080b491f8604ea473a968e347f7` |
| DFU ZIP | — | `20b8cb8c7285de12d585b637bf972ccf897f20b1dc08872dbaa441ecd3724b00` |

相对于纯净配置前的 fork 候选：

```text
Flash: 404472 B → 366460 B
减少: 38012 B
```

RAM 使用保持为：

```text
96760 B
```

### Host 工具回归

`pixi run just fmt`、`pixi run just lint` 和 `pixi run just test` 均通过（140 tests）。文档 UTF-8、围栏配对、尾部空白和冲突标记检查通过。测试包含生成配置的禁用项校验，以及 Console/printk/UART Console/log 被意外重新启用时必须拒绝构建的回归用例。

## 6. 结论边界

本次已经证明纯净配置可以成功构建和打包，并且不会再产生之前两个 logging/console warning。它尚未证明新固件已经通过硬件 BTP 或 BLE RF 回归；由于 fork commit 和固件配置都发生了变化，仍需刷写新 DFU 后重新执行 Core、GAP、GATT、Notification、Indication、恢复和生命周期矩阵。
