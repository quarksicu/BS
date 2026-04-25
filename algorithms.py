"""
PPG heart-rate / SpO2 algorithms implemented in Python for evaluation.

1. maxim_original   – faithful port of the Maxim MAXREFDES117# reference code
2. maxim_fixed      – Maxim with improved peak detection + IQR interval filter
3. vslms_improved   – Maxim + VS-LMS adaptive filter (deployed on Hi3863)
4. dwt_improved     – Maxim + DWT (Daubechies-4, 4-level) denoising filter
5. ceemdan_vslms    – full CEEMDAN-MPE + VS-LMS (paper method, PC only)
"""

import numpy as np
import pywt
from scipy.signal import find_peaks
from scipy.interpolate import CubicSpline

# ============================================================================
# SpO2 look-up table (same as C code)
# uch_spo2_table:  -45.060*R^2 + 30.354*R + 94.845
# ============================================================================
SPO2_TABLE = np.array([
    95, 95, 95, 96, 96, 96, 97, 97, 97, 97, 97, 98, 98, 98, 98, 98,
    99, 99, 99, 99, 99, 99, 99, 99, 100, 100, 100, 100, 100, 100, 100,
    100, 100, 100, 100, 100, 100, 100, 100, 100, 100, 100, 100, 100, 99,
    99, 99, 99, 99, 99, 99, 99, 98, 98, 98, 98, 98, 98, 97, 97, 97, 97,
    96, 96, 96, 96, 95, 95, 95, 94, 94, 94, 93, 93, 93, 92, 92, 92, 91,
    91, 90, 90, 89, 89, 89, 88, 88, 87, 87, 86, 86, 85, 85, 84, 84, 83,
    82, 82, 81, 81, 80, 80, 79, 78, 78, 77, 76, 76, 75, 74, 74, 73, 72,
    72, 71, 70, 69, 69, 68, 67, 66, 66, 65, 64, 63, 62, 62, 61, 60, 59,
    58, 57, 56, 56, 55, 54, 53, 52, 51, 50, 49, 48, 47, 46, 45, 44, 43,
    42, 41, 40, 39, 38, 37, 36, 35, 34, 33, 31, 30, 29, 28, 27, 26, 25,
    23, 22, 21, 20, 19, 17, 16, 15, 14, 12, 11, 10, 9, 7, 6, 5, 3, 2, 1
], dtype=np.int32)

FS = 100          # sampling rate
BUFFER_SIZE = 500 # 5 seconds


# ============================================================================
# Algorithm 1: Maxim Original (faithful C port)
# ============================================================================
def _maxim_core(ir_buf, red_buf, max_peaks=15, min_distance=25, use_iqr=True):
    """Core Maxim algorithm.  Returns (hr, hr_valid, spo2, spo2_valid).
    
    max_peaks: max detected peaks (was 5 in original → missed beats → HR bias high)
    min_distance: min samples between peaks (25 = 240BPM upper limit)
    use_iqr: apply IQR outlier rejection on HR intervals
    """
    n = len(ir_buf)
    ir = ir_buf.astype(np.float64)
    red = red_buf.astype(np.float64)

    # --- DC removal on IR ---
    ir_mean = np.mean(ir)
    x = ir - ir_mean

    # --- 4-pt moving average ---
    x_ma = np.convolve(x, np.ones(4) / 4, mode='valid')

    # --- 1st-order difference ---
    dx = np.diff(x_ma)

    # --- 2-pt moving average on diff ---
    dx = np.convolve(dx, np.ones(2) / 2, mode='valid')

    # --- Hamming window convolution (flip sign for valley→peak) ---
    hamm = np.array([41, 276, 512, 276, 41], dtype=np.float64)
    hamm_sum = hamm.sum()  # 1146
    dx_hamm = np.zeros(len(dx) - len(hamm) + 1)
    for i in range(len(dx_hamm)):
        dx_hamm[i] = -np.dot(dx[i:i + len(hamm)], hamm) / hamm_sum

    # --- adaptive threshold: 1.5x mean absolute value ---
    th = np.mean(np.abs(dx_hamm)) * 1.5
    th = max(th, 30.0 / hamm_sum)

    # --- find peaks ---
    peak_locs, props = find_peaks(dx_hamm, height=th, distance=min_distance)
    peak_locs = peak_locs[:max_peaks]
    n_peaks = len(peak_locs)

    # --- heart rate ---
    hr, hr_valid = -999, False
    if n_peaks >= 2:
        intervals = np.diff(peak_locs)
        # Range filter: keep intervals corresponding to 40-180 BPM
        valid_intervals = intervals[(intervals >= 33) & (intervals <= 150)]
        if use_iqr and len(valid_intervals) >= 4:
            q1 = np.percentile(valid_intervals, 25)
            q3 = np.percentile(valid_intervals, 75)
            iqr = q3 - q1
            lower = q1 - 1.5 * iqr
            upper = q3 + 1.5 * iqr
            valid_intervals = valid_intervals[(valid_intervals >= lower) & (valid_intervals <= upper)]
        if len(valid_intervals) >= 1:
            median_interval = np.median(valid_intervals)
            if median_interval > 0:
                hr = int(6000 / median_interval)
                hr_valid = True

    # --- valley locations ---
    valley_locs = peak_locs + 2  # HAMMING_SIZE//2

    # --- SpO2: AC/DC ratio ---
    spo2, spo2_valid = -999, False
    ratios = []
    for k in range(len(valley_locs) - 1):
        v1, v2 = int(valley_locs[k]), int(valley_locs[k + 1])
        if v2 >= n or v1 < 0 or (v2 - v1) <= 10:
            continue
        seg_ir = ir[v1:v2].copy()
        seg_red = red[v1:v2].copy()

        ir_max_idx = np.argmax(seg_ir)
        red_max_idx = np.argmax(seg_red)

        # linear DC baseline for IR
        ir_dc_line = np.linspace(seg_ir[0], seg_ir[-1], len(seg_ir))
        ir_ac = seg_ir[ir_max_idx] - ir_dc_line[ir_max_idx]
        ir_dc = seg_ir[ir_max_idx]

        # linear DC baseline for Red
        red_dc_line = np.linspace(seg_red[0], seg_red[-1], len(seg_red))
        red_ac = seg_red[red_max_idx] - red_dc_line[red_max_idx]
        red_dc = seg_red[red_max_idx]

        if ir_ac != 0 and red_dc > 0 and ir_dc > 0:
            r = (red_ac / red_dc) / (ir_ac / ir_dc)
            ratio_idx = int(r * 20)
            if 2 < ratio_idx < 184:
                ratios.append(ratio_idx)

    if len(ratios) >= 1:
        ratios.sort()
        mid = len(ratios) // 2
        if len(ratios) >= 2 and mid > 0:
            ratio_avg = (ratios[mid - 1] + ratios[mid]) // 2
        else:
            ratio_avg = ratios[mid]
        if 2 < ratio_avg < 184:
            spo2 = int(SPO2_TABLE[ratio_avg])
            spo2_valid = True

    return hr, hr_valid, spo2, spo2_valid


def maxim_original(ir_buf, red_buf):
    """Algorithm 1: Maxim reference (original parameters, known HR-high bias)."""
    return _maxim_core(ir_buf, red_buf, max_peaks=5, min_distance=8, use_iqr=False)


def maxim_fixed(ir_buf, red_buf):
    """Algorithm 1b: Maxim with fixed peak detection + IQR interval filtering."""
    return _maxim_core(ir_buf, red_buf, max_peaks=15, min_distance=25, use_iqr=True)


# ============================================================================
# Algorithm 2: VS-LMS improved (deployed version)
# ============================================================================
def _vslms_filter(ir, red, order=8, mu_init=0.005, mu_min=0.001, mu_max=0.1,
                  alpha=0.95, gamma=0.01):
    """Variable Step-size LMS adaptive filter using IR-Red synthetic ref."""
    n = len(ir)
    ir_f = ir.astype(np.float64)
    red_f = red.astype(np.float64)

    ir_mean = np.mean(ir_f)
    red_mean = np.mean(red_f)
    scale = ir_mean / red_mean if red_mean > 0 else 1.0

    # Compute reference signal (motion artifact estimate)
    ref_raw = (red_f - red_mean) * scale - (ir_f - ir_mean)

    # Check if reference has significant power (motion artifact present)
    # If reference power is low relative to signal, skip filtering
    ir_ac_power = np.var(ir_f - ir_mean)
    ref_power = np.var(ref_raw)
    if ir_ac_power > 0 and ref_power / ir_ac_power < 0.15:
        # Reference too weak → no significant motion artifact, bypass
        return ir.copy()

    # Normalize signals to prevent overflow
    ir_std = np.std(ir_f - ir_mean)
    if ir_std < 1.0:
        ir_std = 1.0

    w = np.zeros(order)
    ref_buf = np.zeros(order)
    mu = mu_init
    out = np.zeros(n)

    for k in range(n):
        ir_ac = (ir_f[k] - ir_mean) / ir_std
        ref = ref_raw[k] / ir_std

        ref_buf = np.roll(ref_buf, 1)
        ref_buf[0] = ref

        y = np.dot(w, ref_buf)
        e = ir_ac - y

        # VS-LMS step update with clamping
        e_clamp = np.clip(e, -1.0, 1.0)
        mu = alpha * mu + gamma * e_clamp * e_clamp
        mu = np.clip(mu, mu_min, mu_max)

        # NLMS-style weight update
        ref_norm = np.dot(ref_buf, ref_buf) + 1e-8
        w += (mu / ref_norm) * e_clamp * ref_buf
        w = np.clip(w, -10.0, 10.0)

        out[k] = e * ir_std + ir_mean

    return np.clip(out, 0, None).astype(ir.dtype)


def vslms_improved(ir_buf, red_buf):
    """Algorithm 2: VS-LMS filter + Maxim core + improved peak detection.
    
    Key fix: VS-LMS filtered IR is used only for HR peak detection.
    SpO2 uses original (unfiltered) IR to preserve AC/DC ratio.
    """
    ir_clean = _vslms_filter(ir_buf.astype(np.float64),
                             red_buf.astype(np.float64))
    ir_clean = np.clip(ir_clean, 0, None).astype(np.uint32)
    # Use filtered IR for peak detection (HR), but original IR for SpO2
    hr, hr_valid, _, _ = _maxim_core(ir_clean, red_buf, max_peaks=15, min_distance=25, use_iqr=True)
    _, _, spo2, spo2_valid = _maxim_core(ir_buf, red_buf, max_peaks=15, min_distance=25, use_iqr=True)
    return hr, hr_valid, spo2, spo2_valid


# ============================================================================
# Algorithm 3: DWT denoising + Maxim core
# ============================================================================
def _dwt_filter(ir, level=4, wavelet='db4'):
    """Discrete Wavelet Transform denoising for PPG motion artifact removal.

    At 100 Hz, frequency bands for 4-level db4 decomposition:
        cA4: 0 – 3.125 Hz   (PPG fundamental + DC trend)    → keep
        cD4: 3.125 – 6.25 Hz (PPG 2nd harmonic)             → keep
        cD3: 6.25 – 12.5 Hz (PPG 3rd/4th harmonic)          → keep
        cD2: 12.5 – 25 Hz   (mostly noise, light threshold)  → soft threshold
        cD1: 25 – 50 Hz     (electronic noise)               → zero out

    Zeroing D1 and lightly thresholding D2 removes high-freq noise while
    preserving all PPG harmonic content needed for derivative peak detection.
    DC mean is restored after reconstruction.
    """
    ir_f = ir.astype(np.float64)
    ir_mean = np.mean(ir_f)
    ir_ac = ir_f - ir_mean

    # Decompose: coeffs = [cA4, cD4, cD3, cD2, cD1]
    coeffs = pywt.wavedec(ir_ac, wavelet, level=level)

    # Zero out D1 (25-50 Hz) — pure electronic noise
    coeffs[-1] = np.zeros_like(coeffs[-1])

    # Soft-threshold D2 (12.5-25 Hz) with MAD-based universal threshold
    cD2 = coeffs[-2]
    sigma = np.median(np.abs(cD2)) / 0.6745
    if sigma > 0:
        threshold = sigma * np.sqrt(2 * np.log(max(len(cD2), 1)))
        coeffs[-2] = pywt.threshold(cD2, threshold, mode='soft')

    # Reconstruct (keeps A4, D4, D3, thresholded D2, zeroed D1)
    ir_clean = pywt.waverec(coeffs, wavelet)
    ir_clean = ir_clean[:len(ir_f)]  # waverec may add 1 extra sample

    return (ir_clean + ir_mean).astype(ir.dtype)


def dwt_improved(ir_buf, red_buf):
    """Algorithm 3: DWT (db4, 4-level) denoising + improved Maxim core.

    Filtered IR is used for HR peak detection only.
    Original IR is preserved for SpO2 AC/DC ratio calculation.
    """
    ir_clean = _dwt_filter(ir_buf.astype(np.float64))
    ir_clean = np.clip(ir_clean, 0, None).astype(np.uint32)
    # HR from DWT-filtered IR, SpO2 from original IR
    hr, hr_valid, _, _ = _maxim_core(ir_clean, red_buf, max_peaks=15, min_distance=25, use_iqr=True)
    _, _, spo2, spo2_valid = _maxim_core(ir_buf, red_buf, max_peaks=15, min_distance=25, use_iqr=True)
    return hr, hr_valid, spo2, spo2_valid


def dwt_vslms(ir_buf, red_buf):
    """Algorithm 4: DWT pre-denoising + VS-LMS motion removal + improved Maxim core.

    Two-stage filtering pipeline:
      Stage 1 – DWT (db4, 4-level): removes fixed high-frequency electronic noise
                (D1 zeroed, D2 soft-thresholded). Fast, O(n).
      Stage 2 – VS-LMS: uses Red channel as motion reference to adaptively cancel
                remaining broadband motion artifacts from the DWT-cleaned IR.
    HR uses the doubly-filtered IR; SpO2 uses original IR (AC/DC ratio preserved).
    """
    # Stage 1: DWT pre-filter (removes high-freq noise)
    ir_dwt = _dwt_filter(ir_buf.astype(np.float64))
    ir_dwt = np.clip(ir_dwt, 0, None)

    # Stage 2: VS-LMS on DWT-cleaned IR (removes motion artifacts)
    ir_clean = _vslms_filter(ir_dwt, red_buf.astype(np.float64))
    ir_clean = np.clip(ir_clean, 0, None).astype(np.uint32)

    # HR from doubly-filtered IR, SpO2 from original IR
    hr, hr_valid, _, _ = _maxim_core(ir_clean, red_buf, max_peaks=15, min_distance=25, use_iqr=True)
    _, _, spo2, spo2_valid = _maxim_core(ir_buf, red_buf, max_peaks=15, min_distance=25, use_iqr=True)
    return hr, hr_valid, spo2, spo2_valid


# ============================================================================
# Algorithm 5: Full CEEMDAN-MPE + VS-LMS (paper method)
# ============================================================================
def _emd(signal, max_imfs=12, max_sift=10, stop_threshold=0.05):
    """Empirical Mode Decomposition with cubic spline envelope."""
    imfs = []
    residual = signal.copy()
    t = np.arange(len(signal))

    for _ in range(max_imfs):
        h = residual.copy()
        for _ in range(max_sift):
            # find local maxima and minima
            max_idx = np.where((h[1:-1] > h[:-2]) & (h[1:-1] >= h[2:]))[0] + 1
            min_idx = np.where((h[1:-1] < h[:-2]) & (h[1:-1] <= h[2:]))[0] + 1

            if len(max_idx) < 2 or len(min_idx) < 2:
                break

            # add boundary conditions (mirror)
            max_idx_ext = np.concatenate([[0], max_idx, [len(h) - 1]])
            min_idx_ext = np.concatenate([[0], min_idx, [len(h) - 1]])
            max_val = h[max_idx_ext]
            min_val = h[min_idx_ext]

            # cubic spline interpolation
            upper = CubicSpline(max_idx_ext, max_val)(t)
            lower = CubicSpline(min_idx_ext, min_val)(t)

            mean_env = (upper + lower) / 2.0
            h_new = h - mean_env

            # stop criterion
            if np.sum(h ** 2) > 0:
                sd = np.sum((h - h_new) ** 2) / np.sum(h ** 2)
                h = h_new
                if sd < stop_threshold:
                    break
            else:
                h = h_new
                break

        imfs.append(h)
        residual = residual - h

        # stop if residual is monotonic
        if len(np.where(np.diff(residual) > 0)[0]) == 0 or \
           len(np.where(np.diff(residual) < 0)[0]) == 0:
            break

    imfs.append(residual)
    return imfs


def _ceemdan(signal, num_trials=50, noise_std=0.2, max_imfs=12):
    """
    Complete Ensemble EMD with Adaptive Noise.
    Each trial: add adaptive noise → EMD → collect IMFs → average.
    """
    rng = np.random.default_rng(42)
    sig_std = np.std(signal)
    if sig_std == 0:
        sig_std = 1.0

    # First: compute IMFs of original signal as template for #IMFs
    template_imfs = _emd(signal, max_imfs=max_imfs)
    n_imfs = len(template_imfs)

    accumulated = [np.zeros_like(signal, dtype=np.float64) for _ in range(n_imfs)]
    counts = np.zeros(n_imfs)

    for trial in range(num_trials):
        noise = rng.normal(0, noise_std * sig_std, len(signal))
        noisy_sig = signal + noise
        imfs = _emd(noisy_sig, max_imfs=max_imfs)

        for j in range(min(len(imfs), n_imfs)):
            accumulated[j] += imfs[j]
            counts[j] += 1

    result = []
    for j in range(n_imfs):
        if counts[j] > 0:
            result.append(accumulated[j] / counts[j])
    return result


def _permutation_entropy(x, m=3, delay=1):
    """Permutation entropy of a 1-D signal."""
    from math import factorial
    n = len(x)
    perms = {}
    count = 0
    for i in range(n - (m - 1) * delay):
        pattern = tuple(np.argsort(x[i:i + m * delay:delay]))
        perms[pattern] = perms.get(pattern, 0) + 1
        count += 1
    if count == 0:
        return 0.0
    probs = np.array(list(perms.values())) / count
    pe = -np.sum(probs * np.log2(probs + 1e-12))
    return pe / np.log2(factorial(m))  # normalised [0, 1]


def _multiscale_pe(x, scales=range(1, 11), m=3):
    """Multi-scale Permutation Entropy (MPE)."""
    mpe_values = []
    for s in scales:
        # coarse-graining
        n = len(x) // s
        if n < (m + 1):
            mpe_values.append(1.0)
            continue
        coarse = np.mean(x[:n * s].reshape(n, s), axis=1)
        mpe_values.append(_permutation_entropy(coarse, m=m))
    return np.mean(mpe_values)


def ceemdan_vslms(ir_buf, red_buf, mpe_threshold=0.6):
    """
    Algorithm 3: full CEEMDAN-MPE + VS-LMS.

    1. CEEMDAN decomposes IR signal into IMFs
    2. MPE identifies motion-artifact-contaminated IMFs
    3. VS-LMS filters the contaminated IMFs using Red as reference
    4. Reconstructed clean IR → Maxim core → HR / SpO2
    """
    ir_f = ir_buf.astype(np.float64)
    red_f = red_buf.astype(np.float64)
    ir_mean = np.mean(ir_f)

    # Step 1: CEEMDAN decomposition
    ir_ac = ir_f - ir_mean
    imfs = _ceemdan(ir_ac, num_trials=50, noise_std=0.2)

    # Step 2: MPE-based IMF classification
    clean_signal = np.zeros_like(ir_ac)
    artifact_signal = np.zeros_like(ir_ac)

    for imf in imfs:
        mpe = _multiscale_pe(imf, scales=range(1, 8), m=3)
        if mpe > mpe_threshold:
            # high entropy → motion artifact contaminated
            artifact_signal += imf
        else:
            # low entropy → PPG or trend
            clean_signal += imf

    # Step 3: VS-LMS on artifact-contaminated component
    if np.std(artifact_signal) > 1e-6:
        # Use red channel AC as reference
        red_ac = red_f - np.mean(red_f)
        # VS-LMS to extract PPG component from artifact IMFs
        n = len(artifact_signal)
        order = 16  # higher order for better separation
        w = np.zeros(order)
        ref_buf_arr = np.zeros(order)
        mu = 0.005
        recovered = np.zeros(n)

        for k in range(n):
            # reference is red_ac (correlated with both PPG and motion)
            ref_buf_arr = np.roll(ref_buf_arr, 1)
            ref_buf_arr[0] = red_ac[k] if k < len(red_ac) else 0

            y = np.dot(w, ref_buf_arr)
            e = artifact_signal[k] - y

            # NLMS update with normalization
            norm = np.dot(ref_buf_arr, ref_buf_arr) + 1e-8
            mu = 0.95 * mu + 0.01 * e * e / (norm + 1e-8)
            mu = np.clip(mu, 0.0005, 0.05)
            w += mu * e * ref_buf_arr / norm
            w = np.clip(w, -10.0, 10.0)

            recovered[k] = e

        clean_signal += recovered

    # Step 4: Reconstruct and run Maxim core
    ir_clean = (clean_signal + ir_mean).astype(np.float64)
    ir_clean = np.clip(ir_clean, 0, None).astype(np.uint32)
    return _maxim_core(ir_clean, red_buf)
