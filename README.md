# PPG 算法评估基准测试

对比三种心率/血氧算法的准确度和性能：

| 算法 | 说明 |
|------|------|
| **Maxim Original** | MAX30102 参考代码 (MAXREFDES117#) |
| **VS-LMS Improved** | 当前部署版本 (VS-LMS 自适应滤波 + Maxim 核心) |
| **CEEMDAN-MPE+VS-LMS** | 论文完整方法 (仅 PC 可运行) |

## 快速开始

```bash
# 1. 安装依赖
pip install numpy scipy matplotlib

# 2. 运行评估（合成数据，含 CEEMDAN，较慢约 10~30 分钟）
python benchmark.py

# 3. 快速模式（跳过 CEEMDAN）
python benchmark.py --no-ceemdan

# 4. 查看结果
#    results/metrics.csv            - 逐样本详细数据
#    results/hr_scatter.png         - HR 散点图
#    results/hr_bland_altman.png    - HR Bland-Altman 图
#    results/accuracy_vs_motion.png - 准确度随运动强度变化
#    results/execution_time.png     - 执行时间对比
#    results/valid_rate_vs_motion.png - 有效检测率对比
```

## 使用真实数据集

支持 [BIDMC PPG and Respiration Dataset](https://physionet.org/content/bidmc/1.0.0/)（PhysioNet 开源数据集）：

```bash
# 1. 从 PhysioNet 下载数据
#    https://physionet.org/content/bidmc/1.0.0/
#    需要注册 PhysioNet 帐号

# 2. 将 CSV 文件放入 data/bidmc/ 目录
#    bidmc_01_Signals.csv, bidmc_01_Numerics.csv
#    bidmc_02_Signals.csv, bidmc_02_Numerics.csv
#    ...

# 3. 运行
python benchmark.py
```

## 评估指标

- **MAE** (Mean Absolute Error) - 平均绝对误差
- **RMSE** (Root Mean Square Error) - 均方根误差
- **r** (Pearson correlation) - 皮尔逊相关系数
- **Valid%** - 有效检测率（算法返回有效结果的比例）
- **Time** - 每 500 点窗口(5秒)的平均处理时间

## 测试场景

合成数据覆盖以下场景：

| 场景 | HR 范围 | SpO2 范围 | 运动强度 |
|------|---------|-----------|----------|
| 静息 | 50~140 BPM | 88~98% | 0 |
| 轻度运动 | 72~100 BPM | 95~97% | 0.3 |
| 中度运动 | 72~100 BPM | 95~97% | 0.6 |
| 剧烈运动 | 72~100 BPM | 95~97% | 1.0 |
| 低血氧 | 80 BPM | 88~92% | 0~0.5 |

## 文件结构

```
evaluation/
├── benchmark.py      # 主评估程序
├── algorithms.py     # 三种算法的 Python 实现
├── dataset.py        # 数据集加载器（BIDMC + 合成数据）
├── README.md         # 本文件
├── requirements.txt  # Python 依赖
├── data/             # 数据集目录（需手动下载）
│   └── bidmc/
└── results/          # 评估结果输出
    ├── metrics.csv
    ├── hr_scatter.png
    ├── hr_bland_altman.png
    ├── accuracy_vs_motion.png
    ├── execution_time.png
    └── valid_rate_vs_motion.png
```
