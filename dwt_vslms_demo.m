%% =========================================================================
%  DWT + VS-LMS PPG 信号去噪处理过程可视化
%  适用于 MATLAB R2017a（不使用 sgtitle / xline / tiledlayout）
%
%  功能：
%    1. 合成含运动伪影与高频电子噪声的双通道（IR/Red）PPG信号
%    2. 逐级展示 DWT 预滤波 → VS-LMS 自适应滤波 各中间波形
%    3. 频域分析（各阶段功率谱对比）
%    4. VS-LMS 步长自适应曲线与权重收敛过程
%    5. 交流分量叠加对比与残差分析
%
%  参数设置与嵌入式 C 代码 algorithm.c 保持一致：
%    采样率 100 Hz，窗口 5 s（500 点），Q15 参数浮点等效值
%
%  Author:  Generated for paper validation
%  Version: 1.0  (2026-04)
% =========================================================================

clear; close all; clc;
rng(42);   % 固定随机种子，结果可完全复现

%% =========================================================================
%  1. 基本参数
% =========================================================================
Fs     = 100;           % 采样率 (Hz)，与嵌入式系统一致
T      = 5;             % 信号窗口时长 (s)
N      = Fs * T;        % 采样点数 = 500
t      = (0:N-1) / Fs;  % 时间轴

HR_bpm = 75;            % 仿真真实心率 (BPM)
f0     = HR_bpm / 60;   % PPG 基频 (Hz) ≈ 1.25 Hz

%% =========================================================================
%  2. 合成双通道 PPG 信号
% =========================================================================

% --- 2a. 纯净 PPG 波形（基波 + 3次谐波，模拟真实 PPG 形态）---
ppg_clean = 1.00 * sin(2*pi*1*f0*t)        ...
          + 0.40 * sin(2*pi*2*f0*t - 0.30) ...
          + 0.15 * sin(2*pi*3*f0*t - 0.50) ...
          + 0.08 * sin(2*pi*4*f0*t - 0.70);

% --- 2b. 运动伪影（IR 和 Red 通道高度相关）---
%  模拟场景：1.5~3.5 秒区间内有中度手腕晃动
motion_artifact = 0.65 * sin(2*pi*0.9*t + 0.3)                        ...
                + 0.45 * (t>1.5 & t<3.5) .* sin(2*pi*1.8*t + 1.1)    ...
                + 0.20 * sin(2*pi*0.35*t);

% --- 2c. 高频电子噪声（ADC 量化噪声 + 电磁干扰，>25 Hz）---
noise_hf_ir  = 0.07 * randn(1, N);
noise_hf_red = 0.06 * randn(1, N);

% --- 2d. DC 偏置（对应 MAX30102 LED 直流分量）---
dc_ir  = 5.0;
dc_red = 4.5;

% --- 2e. 构造 IR 与 Red 通道原始信号 ---
%  Beer-Lambert 模型：
%    IR  通道：PPG幅值较大（血红蛋白 880nm 吸收）
%    Red 通道：PPG幅值约 85%（血红蛋白 660nm 吸收率不同）
%    运动伪影：两通道相关系数约 0.92（皮肤机械运动相同，光路略异）
ir_raw  = dc_ir  + 1.00 * ppg_clean + motion_artifact + noise_hf_ir;
red_raw = dc_red + 0.85 * ppg_clean + 0.92 * motion_artifact + noise_hf_red;

fprintf('信号合成完成：HR=%d BPM，窗口=%ds，采样率=%dHz\n', HR_bpm, T, Fs);
fprintf('  运动伪影峰值：%.2f (PPG幅值的%.0f%%)\n', ...
        max(abs(motion_artifact)), max(abs(motion_artifact))*100);

%% =========================================================================
%  3. Stage 1：DWT 预滤波
%     5-tap 二项式 FIR [1,4,6,4,1]/16
%     频率响应：|H(f)| = cos^4(pi*f/Fs)
%     效果：D1(25-50Hz) 衰减 >12dB（等效置零），D2(12.5-25Hz) 衰减 >3dB
% =========================================================================
h_fir = [1, 4, 6, 4, 1] / 16;

% conv('same') 处理边界：对应嵌入式中的常量延拓（clamp）
ir_dwt  = conv(ir_raw,  h_fir, 'same');
red_dwt = conv(red_raw, h_fir, 'same');

fprintf('\nDWT 预滤波完成（5-tap 二项式 FIR）\n');
fprintf('  高频噪声抑制：%.1f dB (>25Hz)\n', ...
        20*log10(abs(freqz_at(h_fir, 25, Fs))));

%% =========================================================================
%  4. Stage 2：VS-LMS 自适应滤波
%     参数与 algorithm.c 中 Q15 定点参数完全对应
% =========================================================================
% 对应 C 代码中的宏定义（浮点等效值）
vslms_order   = 8;       % VSLMS_FILTER_ORDER
mu_init       = 0.005;   % VSLMS_MU_INIT / 32768
mu_min        = 0.001;   % VSLMS_MU_MIN  / 32768
mu_max        = 0.100;   % VSLMS_MU_MAX  / 32768
alpha_vslms   = 0.95;    % VSLMS_ALPHA   / 32768
gamma_vslms   = 0.01;    % VSLMS_GAMMA   / 32768
bypass_thresh = 0.15;    % VSLMS_BYPASS_RATIO / 32768

% --- 4a. 计算 DC 均值 ---
ir_mean  = mean(ir_dwt);
red_mean = mean(red_dwt);

% --- 4b. 构造合成运动参考信号 ---
%  scale = ir_mean / red_mean：将 Red 幅值归一化到与 IR 相同量级
%  ref = scaled_Red_AC - IR_AC：两通道 PPG 相抵，剩余运动分量
scale_s  = ir_mean / red_mean;
ir_ac    = ir_dwt  - ir_mean;
red_ac_n = (red_dwt - red_mean) * scale_s;
ref_sig  = red_ac_n - ir_ac;   % 合成运动参考信号

% --- 4c. 旁路判断（对应 VSLMS_BYPASS_RATIO 判据）---
ref_power_ratio = var(ref_sig) / max(var(ir_ac), 1e-10);
bypass_active   = (ref_power_ratio < bypass_thresh);

fprintf('\nVS-LMS 参数：order=%d, mu=[%.3f,%.3f], alpha=%.2f, gamma=%.3f\n', ...
        vslms_order, mu_min, mu_max, alpha_vslms, gamma_vslms);
fprintf('  参考功率/IR功率 = %.4f（旁路阈值=%.2f）\n', ...
        ref_power_ratio, bypass_thresh);

% --- 4d. VS-LMS 主循环 ---
w_vec     = zeros(1, vslms_order);  % 初始权重全零
ref_buf   = zeros(1, vslms_order);  % 参考延迟缓冲
mu_cur    = mu_init;

ir_filt_ac = zeros(1, N);      % VS-LMS 滤波后的交流分量
w_history  = zeros(vslms_order, N);  % 权重历史（用于可视化）
mu_history = zeros(1, N);            % 步长历史
y_history  = zeros(1, N);            % 自适应器输出（估计的运动分量）

if ~bypass_active
    for k = 1:N
        % 更新参考信号移位寄存器
        ref_buf = [ref_sig(k), ref_buf(1:end-1)];

        % FIR 滤波器输出：估计的运动伪影分量
        y = w_vec * ref_buf';

        % 误差信号 = 干净 PPG 估计
        e = ir_ac(k) - y;

        % 误差钳位（对应 Q15 边界，防止步长更新溢出）
        e_c = min(max(e, -0.99), 0.99);

        % VS-LMS 步长更新：mu = alpha*mu + gamma*e^2，双侧钳位
        mu_cur = alpha_vslms * mu_cur + gamma_vslms * e_c^2;
        mu_cur = min(max(mu_cur, mu_min), mu_max);

        % NLMS 归一化：除以参考缓冲功率（防止大幅值导致发散）
        ref_pow = max(ref_buf * ref_buf', 1e-10);
        w_vec   = w_vec + (mu_cur / ref_pow) * e_c * ref_buf;

        % 权重钳位（对应 VSLMS_W_CLAMP = 10.0）
        w_vec = min(max(w_vec, -10), 10);

        % 记录各量
        ir_filt_ac(k)    = e;
        y_history(k)     = y;
        w_history(:, k)  = w_vec';
        mu_history(k)    = mu_cur;
    end
    fprintf('  状态：自适应滤波激活\n');
    fprintf('  最终步长 mu = %.6f，权重收敛范数 = %.4f\n', ...
            mu_history(end), norm(w_history(:,end)));
else
    ir_filt_ac  = ir_ac;
    mu_history(:) = mu_init;
    fprintf('  状态：旁路激活（低运动场景，跳过自适应滤波）\n');
end

% 恢复 DC 分量
ir_vslms = ir_filt_ac + ir_mean;

%% =========================================================================
%  5. 频谱计算（单侧幅值谱）
% =========================================================================
NFFT  = 1024;
f_vec = (0:NFFT/2-1) * Fs / NFFT;

sp_raw   = calc_spectrum(ir_raw   - mean(ir_raw),   NFFT, N);
sp_dwt   = calc_spectrum(ir_dwt   - mean(ir_dwt),   NFFT, N);
sp_vslms = calc_spectrum(ir_vslms - mean(ir_vslms), NFFT, N);
sp_clean = calc_spectrum(ppg_clean,                  NFFT, N);

%% =========================================================================
%  6. 图1：时域处理流水线（5个子图）
% =========================================================================
clr_raw   = [0.85, 0.20, 0.10];  % 红色  — 原始信号
clr_dwt   = [0.15, 0.45, 0.85];  % 蓝色  — DWT后
clr_ref   = [0.75, 0.40, 0.00];  % 橙色  — 参考信号
clr_out   = [0.05, 0.65, 0.25];  % 绿色  — 最终输出
clr_mu    = [0.55, 0.00, 0.75];  % 紫色  — 步长曲线
clr_ref2  = [0.00, 0.00, 0.00];  % 黑虚线 — 真实参考

figure('Name', '图1：DWT+VS-LMS 两级处理流程（时域）', ...
       'NumberTitle', 'off', 'Position', [30, 30, 1250, 950]);

% ---- 子图1：原始 IR 信号 ----
subplot(5, 1, 1);
plot(t, ir_raw,       'Color', clr_raw, 'LineWidth', 1.2); hold on;
plot(t, ppg_clean + dc_ir, 'k--',      'LineWidth', 0.9); hold off;
title(['(a)  原始 IR 信号  ——  含运动伪影（', ...
       sprintf('%.2f', max(abs(motion_artifact))), ...
       ' 峰值）与高频 ADC 噪声'], 'FontSize', 11);
ylabel('幅值'); xlim([0, T]); grid on; set(gca, 'FontSize', 10);
legend('原始 IR 信号', '纯净 PPG（参考）', 'Location', 'northeast');

% ---- 子图2：DWT 预滤波后 ----
subplot(5, 1, 2);
plot(t, ir_dwt,            'Color', clr_dwt, 'LineWidth', 1.2); hold on;
plot(t, ppg_clean + dc_ir, 'k--',            'LineWidth', 0.9); hold off;
title('(b)  DWT 预滤波后  ——  5-tap 二项式 FIR，高频电子噪声（>25 Hz）已抑制', ...
      'FontSize', 11);
ylabel('幅值'); xlim([0, T]); grid on; set(gca, 'FontSize', 10);
legend('DWT 滤波后', '纯净 PPG（参考）', 'Location', 'northeast');

% ---- 子图3：合成运动参考信号 ----
subplot(5, 1, 3);
plot(t, ref_sig,         'Color', clr_ref,  'LineWidth', 1.2); hold on;
plot(t, motion_artifact, 'k--',             'LineWidth', 0.9); hold off;
title('(c)  合成运动参考信号  ——  归一化 (IR − Red) 差分，对应真实运动伪影分量', ...
      'FontSize', 11);
ylabel('幅值'); xlim([0, T]); grid on; set(gca, 'FontSize', 10);
legend('合成参考信号', '真实运动伪影', 'Location', 'northeast');

% ---- 子图4：DWT + VS-LMS 输出 ----
subplot(5, 1, 4);
plot(t, ir_vslms,          'Color', clr_out, 'LineWidth', 1.4); hold on;
plot(t, ppg_clean + dc_ir, 'k--',            'LineWidth', 0.9); hold off;
title('(d)  DWT + VS-LMS 两级滤波后  ——  运动伪影消除，PPG 波形恢复', ...
      'FontSize', 11);
ylabel('幅值'); xlim([0, T]); grid on; set(gca, 'FontSize', 10);
legend('DWT+VS-LMS 输出', '纯净 PPG（参考）', 'Location', 'northeast');

% ---- 子图5：VS-LMS 步长自适应曲线 ----
subplot(5, 1, 5);
plot(t, mu_history, 'Color', clr_mu, 'LineWidth', 1.2); hold on;
plot([0, T], [mu_min, mu_min], '--', 'Color', [0.5, 0.5, 0.5], 'LineWidth', 0.8);
plot([0, T], [mu_max, mu_max], '--', 'Color', [0.5, 0.5, 0.5], 'LineWidth', 0.8);
hold off;
title(['\mu(n) 自适应步长过程  ——  运动段（1.5\sim3.5s）步长增大（误差大），', ...
       '稳态段步长衰减至 \mu_{min}'], 'FontSize', 11);
xlabel('时间 (s)'); ylabel('\mu(n)');
xlim([0, T]); ylim([0, mu_max * 1.3]); grid on; set(gca, 'FontSize', 10);
legend('\mu(n)', '\mu_{min}', '\mu_{max}', 'Location', 'northeast');

% 整体标题（R2017a 兼容方式，用 annotation 替代 sgtitle）
annotation('textbox', [0, 0.965, 1, 0.035], ...
    'String', 'DWT + VS-LMS 两级级联 PPG 去噪：各阶段中间信号（时域）', ...
    'HorizontalAlignment', 'center', 'VerticalAlignment', 'middle', ...
    'FontSize', 13, 'FontWeight', 'bold', 'EdgeColor', 'none', ...
    'BackgroundColor', 'none');

%% =========================================================================
%  7. 图2：频域分析（各阶段功率谱）
% =========================================================================
figure('Name', '图2：各处理阶段频谱对比', ...
       'NumberTitle', 'off', 'Position', [60, 60, 1150, 720]);

% --- DWT 滤波器理论频率响应 ---
f_resp = linspace(0, 50, 500);
H_dwt  = abs(cos(pi * f_resp / Fs)).^4;  % 5-tap 二项式 FIR 频率响应

subplot(2, 2, 1);
plot(f_vec, sp_raw, 'Color', clr_raw, 'LineWidth', 1.2); hold on;
vline_plot(f0 * (1:4), [0, max(sp_raw)*1.1], [0.3, 0.3, 0.3]);
hold off;
title('原始 IR 信号（黑虚线：PPG 各次谐波频率）', 'FontSize', 11);
xlabel('频率 (Hz)'); ylabel('幅值');
xlim([0, 50]); grid on; set(gca, 'FontSize', 10);

subplot(2, 2, 2);
plot(f_vec, sp_dwt, 'Color', clr_dwt, 'LineWidth', 1.2); hold on;
yl2 = [0, max(sp_dwt) * 1.15];
vline_plot(f0 * (1:4), yl2, [0.3, 0.3, 0.3]);
% 标注 D1/D2 频段边界（12.5 Hz 和 25 Hz）
plot([12.5, 12.5], yl2, 'r--', 'LineWidth', 1.0);
plot([25.0, 25.0], yl2, 'r--', 'LineWidth', 1.0);
text(13, yl2(2)*0.85, 'D_2边界', 'Color', 'r', 'FontSize', 9);
text(25.5, yl2(2)*0.85, 'D_1边界', 'Color', 'r', 'FontSize', 9);
hold off;
title('DWT 预滤波后（红虚线：D_1/D_2 频段边界）', 'FontSize', 11);
xlabel('频率 (Hz)'); ylabel('幅值');
xlim([0, 50]); grid on; set(gca, 'FontSize', 10);

subplot(2, 2, 3);
plot(f_vec, sp_vslms, 'Color', clr_out, 'LineWidth', 1.4); hold on;
vline_plot(f0 * (1:4), [0, max(sp_vslms)*1.1], [0.3, 0.3, 0.3]);
hold off;
title('DWT + VS-LMS 两级滤波后', 'FontSize', 11);
xlabel('频率 (Hz)'); ylabel('幅值');
xlim([0, 50]); grid on; set(gca, 'FontSize', 10);

subplot(2, 2, 4);
% dB 幅值对比（0~15 Hz，突出 PPG 谐波与运动伪影分离）
fi  = f_vec <= 15;
eps_v = 1e-6;
plot(f_vec(fi), 20*log10(sp_raw(fi)   + eps_v), 'Color', clr_raw,  'LineWidth', 1.2); hold on;
plot(f_vec(fi), 20*log10(sp_dwt(fi)   + eps_v), 'Color', clr_dwt,  'LineWidth', 1.2);
plot(f_vec(fi), 20*log10(sp_vslms(fi) + eps_v), 'Color', clr_out,  'LineWidth', 1.4);
plot(f_vec(fi), 20*log10(sp_clean(fi) + eps_v), 'k--',             'LineWidth', 1.0);
hold off;
title('低频段（0–15 Hz）dB 幅值对比', 'FontSize', 11);
xlabel('频率 (Hz)'); ylabel('幅值 (dB)'); xlim([0, 15]); grid on;
legend('原始信号', 'DWT 后', 'DWT+VS-LMS 后', '纯净 PPG', ...
       'Location', 'northeast', 'FontSize', 9);
set(gca, 'FontSize', 10);

annotation('textbox', [0, 0.965, 1, 0.035], ...
    'String', '频域分析：DWT 与 VS-LMS 各阶段功率谱对比', ...
    'HorizontalAlignment', 'center', 'VerticalAlignment', 'middle', ...
    'FontSize', 13, 'FontWeight', 'bold', 'EdgeColor', 'none', ...
    'BackgroundColor', 'none');

%% =========================================================================
%  8. 图3：DWT 滤波器频率响应与分析
% =========================================================================
figure('Name', '图3：DWT 近似滤波器分析', ...
       'NumberTitle', 'off', 'Position', [90, 90, 1100, 500]);

subplot(1, 2, 1);
f_resp_full = linspace(0, Fs/2, 1000);
H_full = abs(cos(pi * f_resp_full / Fs)).^4;
plot(f_resp_full, 20*log10(H_full + 1e-6), 'Color', clr_dwt, 'LineWidth', 1.5); hold on;
% 标注各频段
x1 = [0, 12.5]; x2 = [12.5, 25]; x3 = [25, 50];
fill([x1, fliplr(x1)], [-40, -40, 5, 5], [0.8, 0.95, 0.8], 'EdgeColor', 'none', 'FaceAlpha', 0.4);
fill([x2, fliplr(x2)], [-40, -40, 5, 5], [0.95, 0.95, 0.8], 'EdgeColor', 'none', 'FaceAlpha', 0.4);
fill([x3, fliplr(x3)], [-40, -40, 5, 5], [0.95, 0.85, 0.85], 'EdgeColor', 'none', 'FaceAlpha', 0.4);
plot([12.5, 12.5], [-40, 5], 'r--', 'LineWidth', 0.9);
plot([25.0, 25.0], [-40, 5], 'r--', 'LineWidth', 0.9);
text(4,    2, 'A_4/D_3/D_4', 'HorizontalAlignment', 'center', 'FontSize', 9);
text(18.7, 2, 'D_2 衰减',    'HorizontalAlignment', 'center', 'FontSize', 9);
text(37.5, 2, 'D_1 置零',    'HorizontalAlignment', 'center', 'FontSize', 9, 'Color', 'r');
hold off;
title('5-tap 二项式 FIR 频率响应（dB）', 'FontSize', 11);
xlabel('频率 (Hz)'); ylabel('增益 (dB)');
xlim([0, 50]); ylim([-40, 5]); grid on; set(gca, 'FontSize', 10);

subplot(1, 2, 2);
% 各阶段噪声能量对比（柱状图）
band_labels = {'<12.5Hz', '12.5~25Hz', '>25Hz'};
f_bounds = [0, 12.5, 25, 50];
eng_raw   = zeros(1, 3);
eng_dwt   = zeros(1, 3);
eng_vslms = zeros(1, 3);
for bi = 1:3
    idx_b = f_vec >= f_bounds(bi) & f_vec < f_bounds(bi+1);
    eng_raw(bi)   = sum(sp_raw(idx_b).^2);
    eng_dwt(bi)   = sum(sp_dwt(idx_b).^2);
    eng_vslms(bi) = sum(sp_vslms(idx_b).^2);
end
bar_data = [eng_raw; eng_dwt; eng_vslms]';
b = bar(bar_data, 1.0);
b(1).FaceColor = clr_raw;
b(2).FaceColor = clr_dwt;
b(3).FaceColor = clr_out;
set(gca, 'XTickLabel', band_labels);
title('各频段信号能量对比', 'FontSize', 11);
ylabel('能量'); legend('原始', 'DWT后', 'DWT+VS-LMS后', 'Location', 'north');
grid on; set(gca, 'FontSize', 10);

annotation('textbox', [0, 0.965, 1, 0.035], ...
    'String', 'DWT 近似滤波器分析：频率响应与各频段能量对比', ...
    'HorizontalAlignment', 'center', 'VerticalAlignment', 'middle', ...
    'FontSize', 13, 'FontWeight', 'bold', 'EdgeColor', 'none', ...
    'BackgroundColor', 'none');

%% =========================================================================
%  9. 图4：VS-LMS 权重收敛与 SNR 改善
% =========================================================================
figure('Name', '图4：VS-LMS 权重收敛与信噪比', ...
       'NumberTitle', 'off', 'Position', [120, 120, 1150, 520]);

subplot(1, 2, 1);
cmap_lines = lines(vslms_order);
for j = 1:vslms_order
    plot(t, w_history(j, :), 'Color', cmap_lines(j,:), 'LineWidth', 1.0); hold on;
end
hold off;
title('VS-LMS 滤波器权重 w_k(n) 收敛过程', 'FontSize', 11);
xlabel('时间 (s)'); ylabel('权重值');
wleg = cell(1, vslms_order);
for j = 1:vslms_order; wleg{j} = sprintf('w_%d', j); end
legend(wleg, 'Location', 'northeast', 'FontSize', 8);
xlim([0, T]); grid on; set(gca, 'FontSize', 10);

subplot(1, 2, 2);
% 滑动窗口 SNR 计算（0.5 s 窗口，以纯净 PPG 为基准）
win_len  = 50;
n_wins   = floor(N / win_len);
snr_raw  = zeros(1, n_wins);
snr_vslm = zeros(1, n_wins);
t_wins   = zeros(1, n_wins);
for wi = 1:n_wins
    idx_r = (wi-1)*win_len + 1 : wi*win_len;
    s_ref = ppg_clean(idx_r);
    n_r   = (ir_raw(idx_r) - mean(ir_raw(idx_r))) - s_ref;
    n_v   = ir_filt_ac(idx_r)                      - s_ref;
    ps    = sum(s_ref.^2);
    if sum(n_r.^2) > 0;  snr_raw(wi)  = 10*log10(ps / sum(n_r.^2));  end
    if sum(n_v.^2) > 0;  snr_vslm(wi) = 10*log10(ps / sum(n_v.^2));  end
    t_wins(wi) = mean(t(idx_r));
end

plot(t_wins, snr_raw,  'o-', 'Color', clr_raw, 'LineWidth', 1.2, ...
     'MarkerSize', 5, 'MarkerFaceColor', clr_raw); hold on;
plot(t_wins, snr_vslm, 's-', 'Color', clr_out, 'LineWidth', 1.4, ...
     'MarkerSize', 5, 'MarkerFaceColor', clr_out); hold off;
title('信噪比随时间变化（0.5 s 分析窗口）', 'FontSize', 11);
xlabel('时间 (s)'); ylabel('SNR (dB)');
legend('原始 IR  SNR', 'DWT+VS-LMS 后 SNR', 'Location', 'best');
grid on; set(gca, 'FontSize', 10);

annotation('textbox', [0, 0.965, 1, 0.035], ...
    'String', 'VS-LMS 权重收敛过程与信噪比改善（运动段1.5~3.5s效果最显著）', ...
    'HorizontalAlignment', 'center', 'VerticalAlignment', 'middle', ...
    'FontSize', 13, 'FontWeight', 'bold', 'EdgeColor', 'none', ...
    'BackgroundColor', 'none');

%% =========================================================================
%  10. 图5：交流分量叠加对比与残差分析
% =========================================================================
figure('Name', '图5：交流分量与残差分析', ...
       'NumberTitle', 'off', 'Position', [150, 150, 1150, 620]);

ir_ac_raw_plot  = ir_raw   - mean(ir_raw);
ir_ac_vslm_plot = ir_vslms - mean(ir_vslms);
ir_ac_dwt_plot  = ir_dwt   - mean(ir_dwt);

subplot(2, 1, 1);
plot(t, ir_ac_raw_plot,  'Color', clr_raw, 'LineWidth', 0.9, ...
     'DisplayName', '原始 IR 交流分量'); hold on;
plot(t, ir_ac_dwt_plot,  'Color', clr_dwt, 'LineWidth', 1.0, ...
     'DisplayName', 'DWT 预滤波后');
plot(t, ir_ac_vslm_plot, 'Color', clr_out, 'LineWidth', 1.4, ...
     'DisplayName', 'DWT+VS-LMS 输出');
plot(t, ppg_clean,       'k--',            'LineWidth', 1.0, ...
     'DisplayName', '纯净 PPG 参考'); hold off;
title('各处理阶段交流分量叠加对比', 'FontSize', 11);
ylabel('幅值'); xlim([0, T]); grid on; set(gca, 'FontSize', 10);
legend('Location', 'northeast');

subplot(2, 1, 2);
res_raw  = ir_ac_raw_plot  - ppg_clean;
res_dwt  = ir_ac_dwt_plot  - ppg_clean;
res_vslm = ir_ac_vslm_plot - ppg_clean;
rms_raw  = sqrt(mean(res_raw.^2));
rms_dwt  = sqrt(mean(res_dwt.^2));
rms_vslm = sqrt(mean(res_vslm.^2));

plot(t, res_raw,  'Color', clr_raw, 'LineWidth', 0.9, ...
     'DisplayName', sprintf('原始残差    (RMS=%.4f)', rms_raw)); hold on;
plot(t, res_dwt,  'Color', clr_dwt, 'LineWidth', 1.0, ...
     'DisplayName', sprintf('DWT后残差   (RMS=%.4f)', rms_dwt));
plot(t, res_vslm, 'Color', clr_out, 'LineWidth', 1.2, ...
     'DisplayName', sprintf('两级后残差  (RMS=%.4f)', rms_vslm)); hold off;
title('与纯净 PPG 的残差（含运动伪影剩余量 + 滤波引入的失真）', 'FontSize', 11);
xlabel('时间 (s)'); ylabel('残差幅值'); xlim([0, T]); grid on;
legend('Location', 'northeast'); set(gca, 'FontSize', 10);

annotation('textbox', [0, 0.965, 1, 0.035], ...
    'String', '交流分量叠加对比与残差分析：量化各阶段滤波效果', ...
    'HorizontalAlignment', 'center', 'VerticalAlignment', 'middle', ...
    'FontSize', 13, 'FontWeight', 'bold', 'EdgeColor', 'none', ...
    'BackgroundColor', 'none');

%% =========================================================================
%  11. 控制台性能指标输出
% =========================================================================
fprintf('\n============================================================\n');
fprintf('  DWT + VS-LMS 信号处理性能评估\n');
fprintf('  真实HR: %d BPM | Fs: %d Hz | 窗口: %ds | 样本数: %d\n', ...
        HR_bpm, Fs, T, N);
fprintf('============================================================\n');

sig_pow = sum(ppg_clean.^2);
fprintf('\n--- 全帧 SNR（以纯净 PPG 为基准）---\n');
fprintf('  原始 IR 信号    : %+6.2f dB\n', ...
        10*log10(sig_pow / max(sum((ir_ac_raw_plot  - ppg_clean).^2), 1e-10)));
fprintf('  DWT 预滤波后    : %+6.2f dB\n', ...
        10*log10(sig_pow / max(sum((ir_ac_dwt_plot  - ppg_clean).^2), 1e-10)));
fprintf('  DWT + VS-LMS 后 : %+6.2f dB\n', ...
        10*log10(sig_pow / max(sum((ir_ac_vslm_plot - ppg_clean).^2), 1e-10)));

fprintf('\n--- RMS 残差 ---\n');
fprintf('  原始 IR 信号    : %.6f\n', rms_raw);
fprintf('  DWT 预滤波后    : %.6f\n', rms_dwt);
fprintf('  DWT + VS-LMS 后 : %.6f\n', rms_vslm);
fprintf('  RMS 改善比       : %.1f%%\n', (1 - rms_vslm/rms_raw) * 100);

fprintf('\n--- VS-LMS 统计 ---\n');
fprintf('  参考信号功率/IR功率 : %.4f\n', ref_power_ratio);
if bypass_active
    fprintf('  旁路状态            : 激活（低运动场景）\n');
else
    fprintf('  旁路状态            : 未激活（自适应滤波有效）\n');
    fprintf('  mu 最大值（运动段） : %.6f\n', max(mu_history));
    fprintf('  mu 最终值（稳态）   : %.6f\n', mu_history(end));
    fprintf('  权重收敛范数        : %.6f\n', norm(w_history(:,end)));
end
fprintf('============================================================\n');


%% =========================================================================
%  辅助函数区（MATLAB R2016b+ 支持脚本内局部函数）
% =========================================================================

function sp = calc_spectrum(x, nfft, n_pts)
% 计算单侧幅值谱（归一化）
    sp_full = abs(fft(x, nfft));
    sp = 2 / n_pts * sp_full(1:nfft/2);
end

function vline_plot(x_vals, y_range, color_rgb)
% 在当前 axes 绘制垂直虚线（替代 R2018b 引入的 xline）
    for xi = 1:length(x_vals)
        plot([x_vals(xi), x_vals(xi)], y_range, '--', ...
             'Color', color_rgb, 'LineWidth', 0.7);
    end
end

function val = freqz_at(b, freq_hz, Fs_hz)
% 计算 FIR 滤波器在指定频率处的复数增益
    omega = 2 * pi * freq_hz / Fs_hz;
    n_taps = length(b);
    val = sum(b .* exp(-1j * omega * (0:n_taps-1)));
end
