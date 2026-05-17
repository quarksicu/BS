import re
import csv
from pathlib import Path

def process_ecg_log(log_file, output_csv):
    """
    处理ECG log文件，只保留HR和SpO2的行，转换为CSV
    """
    # 正则表达式用于匹配格式: [timestamp] HR=XX SpO2=YY (raw:...)
    pattern = r'\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}\] HR=(\d+) SpO2='
    
    data = []
    second_number = 1
    
    with open(log_file, 'r', encoding='utf-8') as f:
        for line in f:
            if re.search(pattern, line):
                # 提取HR值
                match = re.search(r'HR=(\d+)', line)
                if match:
                    hr_value = int(match.group(1)) - 10
                    data.append([second_number, hr_value])
                    second_number += 1
    
    # 保存为CSV文件
    with open(output_csv, 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        # 写入标题行
        writer.writerow(['second', 'HR'])
        # 写入数据
        writer.writerows(data)
    
    print(f"处理完成: {log_file} -> {output_csv}")
    print(f"共提取 {len(data)} 条记录")
    return len(data)

if __name__ == '__main__':
    base_path = Path(r'c:\Users\gaoti\Desktop\data\ECG')
    
    # 处理ECG1.log
    process_ecg_log(
        str(base_path / 'ECG1.log'),
        str(base_path / 'ECG1_processed.csv')
    )
    
    # 处理ECG2.log
    process_ecg_log(
        str(base_path / 'ECG2.log'),
        str(base_path / 'ECG2_processed.csv')
    )
