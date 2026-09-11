# nrftest

`nrftest` 是一个独立、跨平台、PC 可控制的 BLE Peripheral 硬件测试夹具，用于通过真实 BLE RF 测试 BleHub Central。

## 架构

```text
外部 HIL Runner
├── nrftest Host → BTP serial → nRF52840 Peripheral
└── BleHub Central → 自己的 Central Controller
                              ↕ BLE RF
                         nRF Peripheral
```

当前主线使用：

- Nordic nRF52840 Dongle（PCA10059）；
- `VIDLG/zephyr` fork 中固定集成 commit 的 Zephyr Bluetooth Tester/BTP Server；
- BTP over USB CDC ACM；
- 固定 AutoPTS `pybtp` Client；
- Pixi 管理的 Windows x64、Linux x64 和 macOS Apple Silicon Host 环境；
- Just 提供稳定的 setup、build、package、flash 和硬件测试入口。

nrftest 与 BleHub 不存在仓库、代码或进程依赖，两者只通过真实 BLE RF 交互。

## 当前状态

- Phase 0–4 已完成；
- Windows x64 Phase 5 Host gate 已通过；
- Linux x64 和 macOS Apple Silicon 实机验证尚未执行；
- 当前固定 AutoPTS 的无界 teardown 风险仍待单独设计决策。

未经实机验证的平台不会标记为已支持。详细状态、架构决策和验证边界见 [`docs/PLAN.md`](docs/PLAN.md)。

## 常用入口

所有项目命令通过 Pixi 运行：

```text
pixi run just <recipe>
```

首次使用先复制并填写本机配置：

```text
nrftest.local.example.toml → nrftest.local.toml
```

常用命令：

```text
pixi run just setup
pixi run just setup-firmware
pixi run just firmware-reproduce
pixi run just host-doctor
pixi run just fmt
pixi run just lint
pixi run just test
```

普通 setup、build、test 和 `host-doctor` 不会刷写设备。只有显式 `firmware-flash` 会执行刷写；driver/udev 安装由可能请求 UAC/sudo 的 `setup-platform-provisioning` 单独处理。

## 目录

```text
firmware/   Zephyr 配置、历史 patch provenance 和 DFU package 规则
host/       nrftest Python Host API
profiles/   可复用的 Peripheral Profile JSON
tools/      setup、build、flash 和诊断工具
tests/      非硬件单元测试
docs/       计划与调查/验证报告
archive/    已退出主线的完整 Connectivity/Blatann 历史实现
```

`archive/` 中的历史结果不能证明当前 BTP 主线通过；其中第三方二进制仍归各自权利人所有，并保留来源与摘要说明。
