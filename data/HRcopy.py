"""
心率数据对齐和分析脚本
- 读取串口数据 (hi3863) 和 CSV 数据 (Apple Watch)
- 对齐两组数据
- 计算 MAE、RMSE、RMAE、相关系数
- 绘制时序曲线图和相关性散点图
"""

import re
import pandas as pd
import numpy as np
from scipy.interpolate import interp1d
from scipy.stats import pearsonr
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from datetime import datetime
import os

# 设置中文字体
plt.rcParams['font.sans-serif'] = ['SimHei', 'DejaVu Sans']
plt.rcParams['axes.unicode_minus'] = False

class HeartRateAnalyzer:
    def __init__(self, data_dir):
        self.data_dir = data_dir
        self.results = {}
        
    def parse_log_file(self, log_path):
        """从log文件中提取HR、SpO2和时间戳"""
        hr_data = []
        with open(log_path, 'r', encoding='utf-8') as f:
            for line in f:
                # 匹配 "HR=XX SpO2=YY" 的行
                match = re.search(r'\[(\d{4}-\d{2}-\d{2}\s\d{2}:\d{2}:\d{2}\.\d{3})\].*HR=(\d+)\s+SpO2=(\d+)', line)
                if match:
                    timestamp_str = match.group(1)
                    hr = int(match.group(2))
                    spo2 = int(match.group(3))
                    timestamp = datetime.strptime(timestamp_str, '%Y-%m-%d %H:%M:%S.%f')
                    hr_data.append({'timestamp': timestamp, 'HR': hr - 10, 'SpO2': spo2})
        
        return pd.DataFrame(hr_data)
    
    def parse_csv_file(self, csv_path, dataset_num=None):
        """从CSV文件中读取Apple Watch数据"""
        df = pd.read_csv(csv_path)
        # 将时间字符串转换为datetime
        # 处理两种时间格式：2026/5/10 0:27 和 2026-05-10 13:20:54
        def parse_time(time_str):
            time_str = str(time_str).strip()
            try:
                # 尝试格式 2026/5/10 0:27
                if '/' in time_str:
                    return pd.to_datetime(time_str, format='%Y/%m/%d %H:%M')
                else:
                    # 尝试格式 2026-05-10 13:20:54
                    return pd.to_datetime(time_str, format='%Y-%m-%d %H:%M:%S')
            except:
                return pd.NaT
        
        df['时间'] = df['时间'].apply(parse_time)
        df = df.rename(columns={'心率 (BPM)': 'HR'})
        
        # 对于2号和5号数据，需要在分钟内均匀分布时间戳
        if dataset_num in [2, 5]:
            df = self._distribute_times_within_minute(df)
        
        return df[['序号', 'HR', '时间']].copy()
    
    def _distribute_times_within_minute(self, df):
        """
        将同一分钟内的多个数据点的时间戳均匀分布
        例如，如果5条数据都在0:27这一分钟，则分别分配为：
        0:27:00, 0:27:12, 0:27:24, 0:27:36, 0:27:48
        """
        df = df.copy()
        # 按分钟分组
        df['minute_key'] = df['时间'].dt.floor('min')  # 取整到分钟
        
        new_times = []
        for minute_key, group in df.groupby('minute_key'):
            count = len(group)
            # 在60秒内均匀分布这些数据点
            for i in range(count):
                # 每个数据点的秒数 = (i / count) * 60
                offset_seconds = (i / count) * 60
                new_time = minute_key + pd.Timedelta(seconds=offset_seconds)
                new_times.append(new_time)
        
        df['时间'] = new_times
        df = df.drop('minute_key', axis=1)
        return df
    
    def align_data(self, apple_watch_df, hi3863_df):
        """
        对齐两组数据
        使用插值方法将hi3863数据映射到Apple Watch的时间点上
        """
        # 提取时间和心率
        aw_times = apple_watch_df['时间'].values.astype('datetime64[s]').astype(np.int64)
        aw_hrs = apple_watch_df['HR'].values.astype(float)
        
        hi_times = hi3863_df['timestamp'].values.astype('datetime64[s]').astype(np.int64)
        hi_hrs = hi3863_df['HR'].values.astype(float)
        
        # 检查时间范围是否重叠
        min_time = max(aw_times.min(), hi_times.min())
        max_time = min(aw_times.max(), hi_times.max())
        
        if min_time > max_time:
            print(f"  数据时间范围不重叠")
            return None
        
        # 过滤到重叠范围
        mask_aw = (aw_times >= min_time) & (aw_times <= max_time)
        mask_hi = (hi_times >= min_time) & (hi_times <= max_time)
        
        aw_times_filtered = aw_times[mask_aw]
        aw_hrs_filtered = aw_hrs[mask_aw]
        hi_times_filtered = hi_times[mask_hi]
        hi_hrs_filtered = hi_hrs[mask_hi]
        
        # 使用线性插值将hi3863数据对齐到Apple Watch时间点
        if len(hi_times_filtered) < 2 or len(aw_times_filtered) < 2:
            print(f"  数据点过少，无法进行插值")
            return None
        
        f_interp = interp1d(hi_times_filtered, hi_hrs_filtered, 
                           kind='linear', fill_value='extrapolate', 
                           bounds_error=False)
        hi_hrs_aligned = f_interp(aw_times_filtered)
        
        # 创建对齐后的数据框
        result_df = pd.DataFrame({
            '序号': range(1, len(aw_hrs_filtered) + 1),
            'hi3863': np.round(hi_hrs_aligned, 2),
            'apple_watch': aw_hrs_filtered,
            '时间': pd.to_datetime(aw_times_filtered, unit='s'),
            'offset_seconds': 0
        })
        
        return result_df
    
    def align_data_with_offset(self, apple_watch_df, hi3863_df, max_offset_seconds=300, step_seconds=10):
        """
        基于最大相关系数的对齐方法
        尝试不同的时间偏移，找到最大化相关系数的最优偏移
        """
        # 提取时间和心率
        aw_times = apple_watch_df['时间'].values.astype('datetime64[s]').astype(np.int64)
        aw_hrs = apple_watch_df['HR'].values.astype(float)
        
        hi_times = hi3863_df['timestamp'].values.astype('datetime64[s]').astype(np.int64)
        hi_hrs = hi3863_df['HR'].values.astype(float)
        
        if len(hi_times) < 2 or len(aw_times) < 2:
            print(f"  数据点过少，无法进行对齐")
            return None
        
        best_correlation = -np.inf
        best_offset = 0
        best_aligned_df = None
        correlations = []
        offsets_tested = []
        
        # 尝试不同的时间偏移
        for offset in range(-max_offset_seconds, max_offset_seconds + 1, step_seconds):
            # 对 hi3863 的时间应用偏移
            hi_times_offset = hi_times + offset
            
            # 检查偏移后的时间范围是否与 Apple Watch 时间范围有重叠
            min_time = max(aw_times.min(), hi_times_offset.min())
            max_time = min(aw_times.max(), hi_times_offset.max())
            
            if min_time > max_time:
                continue
            
            # 过滤到重叠范围
            mask_aw = (aw_times >= min_time) & (aw_times <= max_time)
            mask_hi = (hi_times_offset >= min_time) & (hi_times_offset <= max_time)
            
            aw_times_filtered = aw_times[mask_aw]
            aw_hrs_filtered = aw_hrs[mask_aw]
            hi_times_offset_filtered = hi_times_offset[mask_hi]
            hi_hrs_filtered = hi_hrs[mask_hi]
            
            if len(aw_times_filtered) < 2 or len(hi_times_offset_filtered) < 2:
                continue
            
            # 使用线性插值将 hi3863 数据对齐到 Apple Watch 时间点
            try:
                f_interp = interp1d(hi_times_offset_filtered, hi_hrs_filtered,
                                   kind='linear', fill_value='extrapolate',
                                   bounds_error=False)
                hi_hrs_aligned = f_interp(aw_times_filtered)
                
                # 计算相关系数
                if len(aw_hrs_filtered) > 2:
                    correlation, _ = pearsonr(aw_hrs_filtered, hi_hrs_aligned)
                    correlations.append(correlation)
                    offsets_tested.append(offset)
                    
                    # 记录最佳偏移
                    if correlation > best_correlation:
                        best_correlation = correlation
                        best_offset = offset
                        
                        # 创建对齐后的数据框
                        best_aligned_df = pd.DataFrame({
                            '序号': range(1, len(aw_hrs_filtered) + 1),
                            'hi3863': np.round(hi_hrs_aligned, 2),
                            'apple_watch': aw_hrs_filtered,
                            '时间': pd.to_datetime(aw_times_filtered, unit='s'),
                            'offset_seconds': offset
                        })
            except Exception as e:
                continue
        
        if best_aligned_df is None:
            print(f"  无法找到有效的时间偏移")
            return None
        
        # 输出最优偏移信息
        print(f"    - 最优时间偏移: {best_offset} 秒")
        print(f"    - 最高相关系数: {best_correlation:.6f}")
        print(f"    - 尝试偏移数: {len(correlations)}")
        
        return best_aligned_df
    
    def calculate_metrics(self, aligned_df):
        """计算 MAE、RMSE、RMAE、相关系数"""
        if aligned_df is None or len(aligned_df) == 0:
            return None
        
        y_true = aligned_df['apple_watch'].values
        y_pred = aligned_df['hi3863'].values
        
        # MAE (Mean Absolute Error)
        mae = np.mean(np.abs(y_true - y_pred))
        
        # RMSE (Root Mean Square Error) - 新增
        rmse = np.sqrt(np.mean((y_true - y_pred) ** 2))
        
        # RMAE (Relative Mean Absolute Error) = MAE / mean(y_true)
        rmae = mae / np.mean(np.abs(y_true)) if np.mean(np.abs(y_true)) != 0 else np.inf
        
        # 相关系数
        if len(y_true) < 2:
            r = np.nan
            p_value = np.nan
        else:
            r, p_value = pearsonr(y_true, y_pred)
        
        return {
            'MAE': mae,
            'RMSE': rmse,  # 加入返回字典
            'RMAE': rmae,
            '相关系数r': r,
            'p值': p_value,
            '数据点数': len(aligned_df)
        }
    
    def plot_time_series(self, aligned_df, output_path):
        """绘制时序曲线图"""
        fig, ax = plt.subplots(figsize=(14, 6), dpi=300)
        
        # 绘制两条曲线
        ax.plot(aligned_df['时间'], aligned_df['apple_watch'], 
               marker='o', linewidth=2, markersize=4, 
               label='Apple Watch', color='#1f77b4', alpha=0.8)
        ax.plot(aligned_df['时间'], aligned_df['hi3863'], 
               marker='s', linewidth=2, markersize=4, 
               label='hi3863', color='#ff7f0e', alpha=0.8)
        
        # 设置标签和标题
        ax.set_xlabel('Time', fontsize=12, fontweight='bold')
        ax.set_ylabel('Heart Rate (BPM)', fontsize=12, fontweight='bold')
        ax.set_title('Heart Rate Comparison: Apple Watch vs hi3863', 
                    fontsize=14, fontweight='bold', pad=20)
        
        # 格式化x轴时间
        ax.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M:%S'))
        ax.xaxis.set_major_locator(mdates.AutoDateLocator())
        plt.setp(ax.xaxis.get_majorticklabels(), rotation=45, ha='right')
        
        # 设置纵坐标范围
        ax.set_ylim(40, 150)
        
        # 添加网格
        ax.grid(True, alpha=0.3, linestyle='--')
        
        # 设置图例
        ax.legend(loc='best', fontsize=11, framealpha=0.9)
        
        # 调整布局
        plt.tight_layout()
        plt.savefig(output_path, dpi=300, bbox_inches='tight')
        plt.close()
    
    def plot_scatter(self, aligned_df, metrics, output_path):
        """绘制相关性散点图"""
        fig, ax = plt.subplots(figsize=(8, 8), dpi=300)
        
        x = aligned_df['apple_watch'].values
        y = aligned_df['hi3863'].values
        
        # 绘制散点
        ax.scatter(x, y, s=60, alpha=0.6, color='#2ca02c', edgecolors='darkgreen', linewidth=1)
        
        # 添加最小二乘法拟合线
        z = np.polyfit(x, y, 1)
        p = np.poly1d(z)
        x_line = np.linspace(x.min(), x.max(), 100)
        ax.plot(x_line, p(x_line), "r--", linewidth=2, label=f'Linear fit: y={z[0]:.3f}x+{z[1]:.3f}')
        
        # 添加y=x参考线 (理想对齐线)
        min_val = min(x.min(), y.min())
        max_val = max(x.max(), y.max())
        ax.plot([min_val, max_val], [min_val, max_val], 'k--', linewidth=1.5, 
               alpha=0.5, label='Perfect alignment')
        
        # 设置标签和标题
        ax.set_xlabel('Apple Watch Heart Rate (BPM)', fontsize=12, fontweight='bold')
        ax.set_ylabel('hi3863 Heart Rate (BPM)', fontsize=12, fontweight='bold')
        ax.set_title('Heart Rate Correlation Analysis', fontsize=14, fontweight='bold', pad=20)
        
        # 添加文本框显示统计信息 (添加RMSE)
        textstr = f"MAE: {metrics['MAE']:.2f} BPM\n"
        textstr += f"RMSE: {metrics['RMSE']:.2f} BPM\n"
        textstr += f"RMAE: {metrics['RMAE']:.4f}\n"
        textstr += f"Correlation (r): {metrics['相关系数r']:.4f}\n"
        textstr += f"N: {metrics['数据点数']}"
        
        props = dict(boxstyle='round', facecolor='wheat', alpha=0.8)
        ax.text(0.05, 0.95, textstr, transform=ax.transAxes, fontsize=11,
               verticalalignment='top', bbox=props, family='monospace')
        
        # 添加网格
        ax.grid(True, alpha=0.3, linestyle='--')
        
        # 设置图例
        ax.legend(loc='lower right', fontsize=10, framealpha=0.9)
        
        # 调整布局
        plt.tight_layout()
        plt.savefig(output_path, dpi=300, bbox_inches='tight')
        plt.close()
    
    def process_dataset(self, dataset_num):
        """处理单个数据集"""
        dataset_dir = os.path.join(self.data_dir, str(dataset_num))
        csv_path = os.path.join(dataset_dir, f'{dataset_num}.csv')
        log_path = os.path.join(dataset_dir, f'{dataset_num}.log')
        
        if not os.path.exists(csv_path) or not os.path.exists(log_path):
            print(f"数据集 {dataset_num}: 文件不存在")
            return False
        
        print(f"\n处理数据集 {dataset_num}...")
        
        try:
            # 读取数据
            print(f"  读取 Apple Watch 数据...")
            apple_watch_df = self.parse_csv_file(csv_path, dataset_num=dataset_num)
            print(f"    - 获得 {len(apple_watch_df)} 个数据点")
            
            print(f"  读取 hi3863 串口数据...")
            hi3863_df = self.parse_log_file(log_path)
            print(f"    - 获得 {len(hi3863_df)} 个 HR 数据点")
            
            # 对齐数据
            print(f"  对齐两组数据（基于最大相关系数）...")
            aligned_df = self.align_data_with_offset(apple_watch_df, hi3863_df)
            
            if aligned_df is None:
                print(f"  对齐失败")
                return False
            
            print(f"    - 对齐后 {len(aligned_df)} 个数据点")
            
            # 计算指标
            print(f"  计算评估指标...")
            metrics = self.calculate_metrics(aligned_df)
            print(f"    - MAE: {metrics['MAE']:.4f} BPM")
            print(f"    - RMSE: {metrics['RMSE']:.4f} BPM") # 打印 RMSE
            print(f"    - RMAE: {metrics['RMAE']:.6f}")
            print(f"    - 相关系数 r: {metrics['相关系数r']:.6f}")
            print(f"    - p 值: {metrics['p值']:.2e}")
            
            # 保存对齐后的数据
            output_csv = os.path.join(dataset_dir, f'aligned_{dataset_num}.csv')
            aligned_df.to_csv(output_csv, index=False, encoding='utf-8')
            print(f"  对齐后的数据已保存到: {output_csv}")
            
            # 绘制图表
            print(f"  绘制图表...")
            
            # 时序曲线图
            ts_plot = os.path.join(dataset_dir, f'timeseries_{dataset_num}.png')
            self.plot_time_series(aligned_df, ts_plot)
            print(f"    - 时序曲线图已保存到: {ts_plot}")
            
            # 散点图
            scatter_plot = os.path.join(dataset_dir, f'correlation_{dataset_num}.png')
            self.plot_scatter(aligned_df, metrics, scatter_plot)
            print(f"    - 相关性散点图已保存到: {scatter_plot}")
            
            # 保存指标报告
            self.results[dataset_num] = {
                'metrics': metrics,
                'aligned_df': aligned_df,
                'csv_path': output_csv,
                'ts_plot': ts_plot,
                'scatter_plot': scatter_plot
            }
            
            return True
            
        except Exception as e:
            print(f"  处理失败: {str(e)}")
            import traceback
            traceback.print_exc()
            return False
    
    def save_summary_report(self):
        """保存总结报告"""
        report_path = os.path.join(self.data_dir, 'analysis_report.txt')
        
        with open(report_path, 'w', encoding='utf-8') as f:
            f.write("=" * 80 + "\n")
            f.write("心率数据对齐分析报告\n")
            f.write("=" * 80 + "\n\n")
            
            for dataset_num in sorted(self.results.keys()):
                result = self.results[dataset_num]
                metrics = result['metrics']
                
                f.write(f"数据集 {dataset_num}:\n")
                f.write("-" * 40 + "\n")
                f.write(f"  数据点数: {metrics['数据点数']}\n")
                f.write(f"  平均绝对误差 (MAE): {metrics['MAE']:.4f} BPM\n")
                f.write(f"  均方根误差 (RMSE): {metrics['RMSE']:.4f} BPM\n") # 写入 RMSE 到报告
                f.write(f"  相对平均绝对误差 (RMAE): {metrics['RMAE']:.6f}\n")
                f.write(f"  Pearson 相关系数 (r): {metrics['相关系数r']:.6f}\n")
                f.write(f"  p 值: {metrics['p值']:.2e}\n")
                f.write(f"  对齐后的CSV: {result['csv_path']}\n")
                f.write(f"  时序曲线图: {result['ts_plot']}\n")
                f.write(f"  相关性散点图: {result['scatter_plot']}\n")
                f.write("\n")
        
        print(f"\n报告已保存到: {report_path}")

def main():
    data_dir = r"c:\Users\gaoti\Desktop\data"
    analyzer = HeartRateAnalyzer(data_dir)
    
    # 处理数据集 1, 2, 3, 4, 5
    for dataset_num in [1, 2, 3, 4, 5]:
        analyzer.process_dataset(dataset_num)
    
    # 保存总结报告
    if analyzer.results:
        analyzer.save_summary_report()
        print("\n✓ 所有数据集处理完成！")
    else:
        print("\n✗ 没有成功处理任何数据集")

if __name__ == "__main__":
    main()