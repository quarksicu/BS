# PPG 心率与血氧 算法评估（evaluation）

本目录包含用于评估与对比多种 PPG（光电容积描记）算法的脚本与示例数据加载器。目标是对心率（HR）和血氧饱和度（SpO2）算法在准确度与运行性能上的差异进行量化评估。

支持比较的算法示例：

- **Maxim Original**：MAX30102 参考实现（MAXREFDES117#）。
- **VS-LMS Improved**：在嵌入式部署中使用的改进版（DWT+VS-LMS 自适应滤波 + Maxim 核心）。
- **CEEMDAN-MPE+VS-LMS**：论文提出的完整流程（含 CEEMDAN 分解，主要用于离线高精度评估）。

## 目录结构

```
evaluation/
├── algorithms.py     # 算法实现与工具函数
├── benchmark.py      # 评估脚本（入口）
├── dataset.py        # 数据加载与合成数据生成
├── README.md         # 本文件
├── requirements.txt  # Python 依赖
├── .gitignore        # 忽略大文件（data/ 等）
├── data/             # 数据（请勿提交到仓库，需手动下载）
│   └── bidmc/
└── results/          # 评估输出（CSV / 图片）
```

## 快速开始

1. 建议创建并激活虚拟环境：

```bash
python -m venv .venv
source .venv/bin/activate    # macOS / Linux
.venv\Scripts\activate.ps1  # Windows PowerShell
```

2. 安装依赖：

```bash
pip install -r requirements.txt
```

3. 运行评估（默认包含 CEEMDAN，运行较慢）：

```bash
python benchmark.py
```

4. 快速模式（跳过 CEEMDAN，加速测试）：

```bash
python benchmark.py --no-ceemdan
```

5. 评估结果保存在 `results/` 目录，常见输出：

- `results/metrics.csv` — 每个样本的详细指标
- `results/hr_scatter.png` — 心率散点图
- `results/hr_bland_altman.png` — Bland-Altman 分析图

## 使用真实数据集（BIDMC）

项目支持使用 [BIDMC PPG and Respiration Dataset](https://physionet.org/content/bidmc/1.0.0/)（PhysioNet）。下载并将原始文件放到 `data/bidmc/` 下，具体格式见 `dataset.py` 的加载说明。注意：PhysioNet 可能需要注册账户并同意使用条款。

注意：`data/` 目录默认被 `.gitignore` 排除，避免将大型原始数据提交到 Git 仓库。

## 评估指标说明

- MAE — 平均绝对误差（Mean Absolute Error）
- RMSE — 均方根误差（Root Mean Square Error）
- r — 皮尔逊相关系数（Pearson r）
- Valid% — 有效检测率（算法返回有效估计的比例）
- Time — 处理时间（用于对比算法效率）
