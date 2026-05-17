"""
Data loader for BIDMC PPG + SpO2 dataset with auto-download via wfdb.

Dataset: BIDMC PPG and Respiration Dataset (PhysioNet)
    https://physionet.org/content/bidmc/1.0.0/

This is a widely-used open-source dataset containing:
- 53 recordings of ~8 minutes each
- PPG waveforms (125 Hz)
- Reference SpO2 from pulse oximeter
- Reference heart rate from ECG

The dataset is freely available from PhysioNet under the
Open Data Commons Attribution License v1.0.

Uses wfdb library to auto-download if not cached locally.
Falls back to synthetic data if download fails.
"""

import os
import numpy as np
import csv
from pathlib import Path

FS_BIDMC = 125   # BIDMC native sampling rate
FS_TARGET = 100  # MAX30102 sampling rate
WINDOW_SEC = 5   # seconds per analysis window
WINDOW_SAMPLES = FS_TARGET * WINDOW_SEC  # 500 samples at 100 Hz

DATA_DIR = Path(__file__).parent / "data" / "bidmc-ppg-and-respiration-dataset-1.0.0"
DATA_DIR_CSV = DATA_DIR / "bidmc_csv"


def _resample(sig, fs_in, fs_out):
    """Simple linear resampling."""
    n_in = len(sig)
    n_out = int(n_in * fs_out / fs_in)
    x_in = np.linspace(0, 1, n_in)
    x_out = np.linspace(0, 1, n_out)
    return np.interp(x_out, x_in, sig)


def _ppg_to_ir_red(ppg, spo2_ref=97.0, rng=None):
    """
    Convert single-channel PPG to dual-channel IR + Red with realistic
    ratio corresponding to the reference SpO2 value.

    The MAX30102 measures tissue absorption at 660nm (Red) and 880nm (IR).
    The ratio R = (AC_red/DC_red) / (AC_ir/DC_ir) determines SpO2:
        SpO2 ≈ -45.060 * R^2 + 30.354 * R + 94.845

    We synthesise realistic dual channels by:
    1. Extracting AC/DC from PPG
    2. Applying different AC modulation depths for Red vs IR
    """
    if rng is None:
        rng = np.random.default_rng(42)

    ppg_f = ppg.astype(np.float64)

    # Extract DC (baseline) and AC (pulsatile)
    # Use a moving average as DC estimate
    kernel_len = max(int(FS_TARGET * 0.5), 1)
    kernel = np.ones(kernel_len) / kernel_len
    dc_component = np.convolve(ppg_f, kernel, mode='same')
    ac_component = ppg_f - dc_component

    # Solve for R from SpO2 reference:
    # SpO2 = -45.060*R^2 + 30.354*R + 94.845
    # => 45.060*R^2 - 30.354*R + (SpO2 - 94.845) = 0
    spo2_val = np.clip(spo2_ref, 70, 100)
    a, b, c = 45.060, -30.354, (spo2_val - 94.845)
    disc = b * b - 4 * a * c
    if disc < 0:
        R = 0.5
    else:
        R = (-b - np.sqrt(disc)) / (2 * a)
    R = np.clip(R, 0.2, 2.0)

    # IR channel: DC ~ 100000, AC modulation ~ 2%
    dc_ir = 100000.0
    ac_scale_ir = 2000.0  # typical AC amplitude for IR

    # Red channel: DC ~ 80000, AC modulation adjusted by R
    dc_red = 80000.0
    ac_scale_red = R * (dc_red / dc_ir) * ac_scale_ir

    # Normalise AC component
    ac_std = np.std(ac_component)
    if ac_std > 0:
        ac_norm = ac_component / ac_std
    else:
        ac_norm = ac_component

    ir_signal = dc_ir + ac_scale_ir * ac_norm
    red_signal = dc_red + ac_scale_red * ac_norm

    # Add realistic sensor noise
    ir_signal += rng.normal(0, 30, len(ppg))
    red_signal += rng.normal(0, 40, len(ppg))

    ir_signal = np.clip(ir_signal, 1000, 262143).astype(np.uint32)
    red_signal = np.clip(red_signal, 1000, 262143).astype(np.uint32)

    return ir_signal, red_signal


def _load_bidmc_wfdb():
    """
    Load BIDMC records from local wfdb files.
    Reads bidmcXX (signals) and bidmcXXn (numerics) records.
    Returns list of dicts with per-window data.
    """
    try:
        import wfdb
    except ImportError:
        print("[DATA] wfdb not installed. Install with: pip install wfdb")
        return []

    # Check local data exists
    test_file = DATA_DIR / "bidmc01.hea"
    if not test_file.exists():
        print(f"[DATA] BIDMC wfdb files not found in {DATA_DIR}")
        return []

    dataset = []
    rng = np.random.default_rng(2026)

    print(f"[DATA] Loading BIDMC from {DATA_DIR} ...")

    for rid in range(1, 54):
        rec_name = f"bidmc{rid:02d}"
        rec_path = str(DATA_DIR / rec_name)

        # --- Read PPG signal ---
        try:
            record = wfdb.rdrecord(rec_path)
            pleth_idx = None
            for i, name in enumerate(record.sig_name):
                if 'PLETH' in name.upper():
                    pleth_idx = i
                    break
            if pleth_idx is None:
                continue

            ppg = record.p_signal[:, pleth_idx]
            fs_native = record.fs
            ppg = np.nan_to_num(ppg, nan=0.0)
            ppg_100 = _resample(ppg, fs_native, FS_TARGET)
        except Exception as e:
            print(f"  [WARN] Failed to load {rec_name}: {e}")
            continue

        # --- Read numerics from bidmcXXn record ---
        hr_times = np.array([])
        hr_vals = np.array([])
        spo2_times = np.array([])
        spo2_vals = np.array([])

        num_path = str(DATA_DIR / f"{rec_name}n")
        try:
            num_rec = wfdb.rdrecord(num_path)
            # sig_name: ['HR,', 'PULSE,', 'RESP,', 'SpO2,'], fs=1Hz
            hr_idx, spo2_idx = None, None
            for i, name in enumerate(num_rec.sig_name):
                nm = name.upper().rstrip(',')
                if nm == 'HR':
                    hr_idx = i
                elif nm == 'SPO2':
                    spo2_idx = i

            n_num = num_rec.p_signal.shape[0]
            t_arr = np.arange(n_num) / num_rec.fs  # seconds

            if hr_idx is not None:
                raw_hr = num_rec.p_signal[:, hr_idx]
                valid = (raw_hr > 20) & (raw_hr < 250) & ~np.isnan(raw_hr)
                hr_times = t_arr[valid]
                hr_vals = raw_hr[valid]

            if spo2_idx is not None:
                raw_spo2 = num_rec.p_signal[:, spo2_idx]
                valid = (raw_spo2 > 50) & (raw_spo2 <= 100) & ~np.isnan(raw_spo2)
                spo2_times = t_arr[valid]
                spo2_vals = raw_spo2[valid]
        except Exception:
            pass

        # --- Extract 5-second windows ---
        n_total = len(ppg_100)
        n_windows = n_total // WINDOW_SAMPLES
        n_added = 0

        for w in range(min(n_windows, 10)):
            start = w * WINDOW_SAMPLES
            end = start + WINDOW_SAMPLES
            ppg_win = ppg_100[start:end]

            if np.std(ppg_win) < 1e-6:
                continue

            t_start = start / FS_TARGET
            t_end = end / FS_TARGET

            # Per-window HR reference
            if len(hr_times) > 0:
                mask = (hr_times >= t_start) & (hr_times < t_end)
                hr_ref = np.mean(hr_vals[mask]) if np.any(mask) else (
                    np.mean(hr_vals) if len(hr_vals) > 0 else np.nan)
            else:
                hr_ref = np.nan

            # Per-window SpO2 reference
            if len(spo2_times) > 0:
                mask = (spo2_times >= t_start) & (spo2_times < t_end)
                spo2_ref = np.mean(spo2_vals[mask]) if np.any(mask) else (
                    np.mean(spo2_vals) if len(spo2_vals) > 0 else np.nan)
            else:
                spo2_ref = np.nan

            if np.isnan(hr_ref) or np.isnan(spo2_ref):
                continue

            # Normalise PPG to MAX30102 ADC range and create dual channels
            ppg_norm = ppg_win - np.min(ppg_win)
            ppg_max = np.max(ppg_norm)
            if ppg_max > 0:
                ppg_norm = ppg_norm / ppg_max
            ppg_adc = (ppg_norm * 200000 + 30000).astype(np.float64)

            ir, red = _ppg_to_ir_red(ppg_adc, spo2_ref=spo2_ref,
                                     rng=np.random.default_rng(rng.integers(0, 100000)))

            dataset.append({
                'ir': ir,
                'red': red,
                'hr_ref': float(hr_ref),
                'spo2_ref': float(spo2_ref),
                'label': f"BIDMC-{rid:02d}-w{w}",
                'motion': 0.0  # BIDMC is resting ICU data
            })
            n_added += 1

        if n_added > 0:
            hr_info = f", HR ref: {hr_vals.mean():.0f} bpm" if len(hr_vals) > 0 else ""
            print(f"  [{rec_name}] {n_added} windows{hr_info}")

    return dataset


def _load_bidmc_csv():
    """Legacy CSV loader (fallback if wfdb fails but CSVs exist)."""
    dataset = []
    rng = np.random.default_rng(2026)

    for rid in range(1, 54):
        signals_file = DATA_DIR_CSV / f"bidmc_{rid:02d}_Signals.csv"
        numerics_file = DATA_DIR_CSV / f"bidmc_{rid:02d}_Numerics.csv"
        if not signals_file.exists() or not numerics_file.exists():
            continue

        ppg = []
        with open(signals_file, 'r') as f:
            reader = csv.DictReader(f)
            for row in reader:
                for key in row:
                    if 'PLETH' in key.upper():
                        try:
                            ppg.append(float(row[key]))
                        except ValueError:
                            pass
                        break

        hr_refs, spo2_refs = [], []
        with open(numerics_file, 'r') as f:
            reader = csv.DictReader(f)
            for row in reader:
                for key in row:
                    if 'HR' in key.upper() or 'PULSE' in key.upper():
                        try:
                            hr_refs.append(float(row[key]))
                        except (ValueError, KeyError):
                            pass
                    if 'SPO2' in key.upper():
                        try:
                            spo2_refs.append(float(row[key]))
                        except (ValueError, KeyError):
                            pass

        if len(ppg) < 1000:
            continue

        ppg = np.array(ppg)
        ppg_100 = _resample(ppg, FS_BIDMC, FS_TARGET)

        ppg_norm = ppg_100 - np.min(ppg_100)
        if np.max(ppg_norm) > 0:
            ppg_norm = ppg_norm / np.max(ppg_norm)
        ppg_adc = (ppg_norm * 200000 + 30000).astype(np.float64)

        hr_ref = np.mean(hr_refs) if hr_refs else np.nan
        spo2_ref = np.mean(spo2_refs) if spo2_refs else np.nan

        if np.isnan(hr_ref) or np.isnan(spo2_ref):
            continue

        n_windows = min(len(ppg_adc) // WINDOW_SAMPLES, 5)
        for w in range(n_windows):
            start = w * WINDOW_SAMPLES
            win = ppg_adc[start:start + WINDOW_SAMPLES]
            ir, red = _ppg_to_ir_red(win, spo2_ref=spo2_ref,
                                     rng=np.random.default_rng(rng.integers(0, 100000)))
            dataset.append({
                'ir': ir,
                'red': red,
                'hr_ref': float(hr_ref),
                'spo2_ref': float(spo2_ref),
                'label': f"BIDMC-{rid:02d}-w{w}",
                'motion': 0.0
            })

    return dataset


# ============================================================================
# Synthetic PPG generator (fallback when real dataset not available)
# ============================================================================
def _generate_synthetic_ppg(hr_bpm=72, spo2=97, duration_s=5, fs=100,
                            motion_level=0.0, rng=None):
    """
    Generate synthetic PPG IR and Red signals with known HR and SpO2.

    Parameters
    ----------
    hr_bpm : float       - heart rate in BPM
    spo2 : float         - target SpO2 (%)
    duration_s : float   - signal duration in seconds
    fs : int             - sampling rate
    motion_level : float - motion artifact amplitude [0, 1]
    rng : np.Generator   - random number generator

    Returns
    -------
    ir, red : np.ndarray(uint32) - simulated ADC values
    """
    if rng is None:
        rng = np.random.default_rng(42)

    n = int(duration_s * fs)
    t = np.arange(n) / fs
    hr_hz = hr_bpm / 60.0

    # --- PPG pulse waveform (sum of harmonics) ---
    pulse = (0.5 * np.sin(2 * np.pi * hr_hz * t - np.pi / 3) +
             0.25 * np.sin(2 * np.pi * 2 * hr_hz * t - np.pi / 4) +
             0.1 * np.sin(2 * np.pi * 3 * hr_hz * t))

    # --- DC and AC components ---
    # R = (AC_red/DC_red) / (AC_ir/DC_ir)
    # From SpO2 table: SpO2 ≈ -45.060*R^2 + 30.354*R + 94.845
    # Solve for R given SpO2 (approximate)
    # For SpO2=97 → R≈0.5, SpO2=95→R≈0.7, SpO2=90→R≈1.0
    if spo2 >= 100:
        R = 0.4
    elif spo2 >= 90:
        R = 0.4 + (100 - spo2) * 0.06
    else:
        R = 1.0 + (90 - spo2) * 0.08

    dc_ir = 100000.0
    ac_ir = 2000.0
    dc_red = 80000.0
    ac_red = R * (dc_red / dc_ir) * ac_ir

    ir_signal = dc_ir + ac_ir * pulse
    red_signal = dc_red + ac_red * pulse

    # --- Add physiological noise ---
    ir_signal += rng.normal(0, 50, n)
    red_signal += rng.normal(0, 50, n)

    # --- Add motion artifacts ---
    if motion_level > 0:
        # low-frequency motion artifact (0.5-3 Hz)
        motion_freq = rng.uniform(0.5, 3.0)
        motion = motion_level * 5000 * np.sin(2 * np.pi * motion_freq * t)
        # add random bursts
        burst_start = rng.integers(0, n // 2)
        burst_len = rng.integers(n // 4, n // 2)
        burst_env = np.zeros(n)
        burst_env[burst_start:burst_start + burst_len] = 1.0
        burst_env = np.convolve(burst_env, np.ones(20) / 20, mode='same')
        # motion affects both channels similarly
        ir_signal += motion * burst_env
        red_signal += motion * burst_env * rng.uniform(0.8, 1.2)
        # high-frequency spike artifact
        n_spikes = int(motion_level * 10)
        for _ in range(n_spikes):
            spike_loc = rng.integers(0, n)
            spike_amp = rng.uniform(-3000, 3000) * motion_level
            width = rng.integers(1, 5)
            ir_signal[spike_loc:spike_loc + width] += spike_amp
            red_signal[spike_loc:spike_loc + width] += spike_amp * rng.uniform(0.9, 1.1)

    ir_signal = np.clip(ir_signal, 0, 262143).astype(np.uint32)
    red_signal = np.clip(red_signal, 0, 262143).astype(np.uint32)

    return ir_signal, red_signal


def load_dataset():
    """
    Load evaluation dataset.

    Priority: wfdb streaming from PhysioNet → CSV fallback → synthetic fallback.

    Returns list of dicts: ir, red, hr_ref, spo2_ref, label, motion.
    """
    dataset = []

    # --- Try wfdb streaming from PhysioNet ---
    dataset = _load_bidmc_wfdb()
    if len(dataset) > 0:
        print(f"[DATA] Loaded {len(dataset)} windows from BIDMC (PhysioNet streaming)")
        return dataset

    # --- Try CSV fallback ---
    dataset = _load_bidmc_csv()
    if len(dataset) > 0:
        print(f"[DATA] Loaded {len(dataset)} windows from BIDMC (CSV)")
        return dataset

    # --- Fallback: Synthetic dataset ---
    print("[DATA] BIDMC not available, generating synthetic PPG dataset...")
    print("[DATA] To use real data: pip install wfdb (requires network access)")

    rng = np.random.default_rng(2026)

    # Test cases: (hr_bpm, spo2, motion_level, count, description)
    test_cases = [
        # Stationary (no motion)
        (60,  98, 0.0, 5,  "rest_60bpm"),
        (72,  97, 0.0, 5,  "rest_72bpm"),
        (85,  96, 0.0, 5,  "rest_85bpm"),
        (100, 95, 0.0, 5,  "rest_100bpm"),
        (120, 94, 0.0, 3,  "rest_120bpm"),
        # Light motion
        (72,  97, 0.3, 5,  "light_motion_72bpm"),
        (85,  96, 0.3, 5,  "light_motion_85bpm"),
        (100, 95, 0.3, 5,  "light_motion_100bpm"),
        # Moderate motion
        (72,  97, 0.6, 5,  "moderate_motion_72bpm"),
        (85,  96, 0.6, 5,  "moderate_motion_85bpm"),
        (100, 95, 0.6, 5,  "moderate_motion_100bpm"),
        # Heavy motion
        (72,  97, 1.0, 5,  "heavy_motion_72bpm"),
        (85,  96, 1.0, 5,  "heavy_motion_85bpm"),
        (100, 95, 1.0, 5,  "heavy_motion_100bpm"),
        # Low SpO2 scenarios
        (80,  92, 0.0, 3,  "low_spo2_92"),
        (80,  88, 0.0, 3,  "low_spo2_88"),
        (80,  92, 0.5, 3,  "low_spo2_92_motion"),
        # High HR
        (140, 96, 0.0, 3,  "high_hr_140bpm"),
        (140, 96, 0.5, 3,  "high_hr_140bpm_motion"),
        # Low HR
        (50,  98, 0.0, 3,  "low_hr_50bpm"),
        (50,  98, 0.5, 3,  "low_hr_50bpm_motion"),
    ]

    for hr, spo2, motion, count, desc in test_cases:
        for i in range(count):
            ir, red = _generate_synthetic_ppg(
                hr_bpm=hr, spo2=spo2, duration_s=5, fs=100,
                motion_level=motion,
                rng=np.random.default_rng(rng.integers(0, 100000))
            )
            dataset.append({
                'ir': ir,
                'red': red,
                'hr_ref': float(hr),
                'spo2_ref': float(spo2),
                'label': f"{desc}_{i}",
                'motion': motion
            })

    print(f"[DATA] Generated {len(dataset)} synthetic test cases")
    return dataset
