# BS
心率血氧监测算法

## 项目简介

硬件平台为海思Hi3863
算力平台为基于OpenHarmony LiteOS内核的kaihong OS
使用USB转串口烧录，理论上该芯片的开发板或设备都可以直接烧录fwpkg文件

算法在Maxim基础上改进，参考CEEDMAN + VS-LMS文献，基于嵌入式平台优化，使用DWT+VS-LMS评估并在嵌入式平台部署

BS 仓库包含与心率（HR）与血氧（SpO2）检测相关的算法和示例实现，主要模块：

- `hr_spo2/`：与 MAX30102 等光电容积脉搏波传感器配合的算法与驱动代码（算法实现、传感器读取等）。
- `hr_spo2/evaluation`：基于BIDMC数据集的离线测试脚本
- `HealthMonitor/`：健康监测的示例程序，演示如何使用相关算法模块进行数据处理与监控。
- `data/`：实测数据和处理脚本


## 主要特性

- 基于 MAX30102 等传感器的数据采集与驱动代码
- 心率 / 血氧（SpO2）算法
- 心率血氧体温实时监测和报警，OLED显示

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
  - evaluation                — 离线数据库评估
-data/                        — 实测数据评估
-DWT+VS-LMS.fwpkg             — 烧录固件
