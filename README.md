# BS

简体中文说明（README）

## 项目简介

BS 仓库包含与心率（HR）与血氧（SpO2）检测相关的算法和示例实现，主要模块：

- `hr_spo2/`：与 MAX30102 等光电容积脉搏波传感器配合的算法与驱动代码（算法实现、传感器读取等）。
- `HealthMonitor/`：健康监测的示例程序，演示如何使用相关算法模块进行数据处理与监控。

该仓库适合作为嵌入式设备或原型平台上血氧与心率检测算法的参考实现与快速原型。

## 主要特性

- 基于 MAX30102 等传感器的数据采集与驱动代码
- 心率 / 血氧（SpO2）算法实现
- 简单的示例程序用于演示数据流与处理流程

## 仓库结构

- BUILD.gn                    — GN 构建描述文件（若使用 GN/ninja 构建）
- HealthMonitor/              — 健康监测示例程序
  - HealthMonitor.c
  - BUILD.gn
- hr_spo2/                    — 心率/血氧算法与驱动实现
  - algorithm.c
  - algorithm.h
  - max30102.c
  - max30102.h
  - BUILD.gn

## 构建说明

本项目包含 GN 构建脚本（BUILD.gn）。如果你已经安装了 GN 与 Ninja，可以尝试：

```bash
# 生成构建文件并构建（在项目根目录）
gn gen out
ninja -C out
```

如果你不使用 GN，也可以直接用 gcc 编译示例（仅作示例，视平台与编译器选项可能需要调整）：

```bash
# 创建输出目录
mkdir -p bin

# 编译 hr_spo2 模块（示例）
gcc hr_spo2/*.c -Ihr_spo2 -lm -o bin/hr_spo2

# 编译 HealthMonitor 示例（示例）
gcc HealthMonitor/HealthMonitor.c -I. -Ihr_spo2 -L. -lm -o bin/HealthMonitor
```

注意：真实嵌入式平台通常需要交叉编译、指定目标架构、链接硬件平台 SDK 或 HAL，请根据目标设备调整编译器和链接选项。

## 运行

构建成功后，执行生成的可执行文件：

```bash
# 运行示例（根据实际生成的可执行文件名）
./bin/HealthMonitor
./bin/hr_spo2
```

这些示例程序通常需要实际的传感器硬件或预录制/仿真数据输入才能产生有效输出。

## 贡献

欢迎 issue、PR 和建议：

- 提交 bug 报告或功能请求请使用 GitHub Issues
- 如果提交代码，请遵循简明的 commit 信息，并在 PR 描述中说明变更目的

## 许可证

仓库当前未包含许可证文件（LICENSE）。在使用或分发本仓库代码前，请与仓库维护者确认许可条款，或在仓库中添加合适的许可证（例如 MIT/Apache-2.0 等）。

## 联系

如需更多信息或讨论实现细节，请在仓库中打开 Issue 或直接联系仓库维护者。
