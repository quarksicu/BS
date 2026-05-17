#!/usr/bin/env python3
"""
PPG Algorithm Benchmark: compare three HR/SpO2 algorithms.

  1. Maxim Original   – reference code from MAXREFDES117#
  2. VS-LMS Improved  – deployed on Hi3863 (VS-LMS + Maxim core)
  3. CEEMDAN-MPE+VS-LMS – full paper method (PC only)

Usage:
    python benchmark.py                  # run with synthetic data
    python benchmark.py --data bidmc     # run with BIDMC dataset
    python benchmark.py --no-ceemdan     # skip slow CEEMDAN (fast mode)

Output:
    - Console table with MAE, RMSE, correlation for each algorithm
    - results/ folder with comparison plots (PNG)
    - results/metrics.csv with detailed per-sample results
"""

import os
import sys
import time
import argparse
import numpy as np
from pathlib import Path

# Add parent to path
sys.path.insert(0, str(Path(__file__).parent))

from algorithms import maxim_original, maxim_fixed, vslms_improved, dwt_improved, dwt_vslms, ceemdan_vslms
from dataset import load_dataset


def compute_metrics(predictions, references, name=""):
    """Compute MAE, RMSE, and Pearson correlation."""
    preds = np.array(predictions, dtype=np.float64)
    refs = np.array(references, dtype=np.float64)

    # Filter valid pairs
    valid = np.isfinite(preds) & np.isfinite(refs) & (preds > 0) & (refs > 0)
    if np.sum(valid) < 2:
        return {'mae': np.nan, 'rmse': np.nan, 'corr': np.nan,
                'valid_ratio': 0.0, 'n_valid': 0}

    p = preds[valid]
    r = refs[valid]

    mae = np.mean(np.abs(p - r))
    rmse = np.sqrt(np.mean((p - r) ** 2))
    if np.std(p) > 0 and np.std(r) > 0:
        corr = np.corrcoef(p, r)[0, 1]
    else:
        corr = np.nan

    return {
        'mae': mae,
        'rmse': rmse,
        'corr': corr,
        'valid_ratio': np.sum(valid) / len(preds),
        'n_valid': int(np.sum(valid))
    }


def run_benchmark(args):
    # ---- Load data ----
    print("=" * 70)
    print("  PPG Algorithm Benchmark")
    print("  Comparing: Maxim Original / VS-LMS Improved / CEEMDAN-MPE+VS-LMS")
    print("=" * 70)

    dataset = load_dataset()
    n_samples = len(dataset)
    print(f"\nTotal test samples: {n_samples}\n")

    # ---- Prepare result storage ----
    results_dir = Path(__file__).parent / "results"
    results_dir.mkdir(exist_ok=True)

    algo_names = ["Maxim_Original", "Maxim_Fixed", "VSLMS_Improved", "DWT_Improved", "DWT_VSLMS"]
    algo_funcs = [maxim_original, maxim_fixed, vslms_improved, dwt_improved, dwt_vslms]
    if not args.no_ceemdan:
        algo_names.append("CEEMDAN_VSLMS")
        algo_funcs.append(ceemdan_vslms)

    # Storage: algo → { 'hr': [], 'spo2': [], 'hr_ref': [], 'spo2_ref': [],
    #                    'time': [], 'motion': [], 'label': [] }
    results = {name: {'hr': [], 'spo2': [], 'hr_ref': [], 'spo2_ref': [],
                       'time': [], 'motion': [], 'label': []}
               for name in algo_names}

    # ---- Run all algorithms ----
    for idx, sample in enumerate(dataset):
        ir = sample['ir']
        red = sample['red']
        hr_ref = sample['hr_ref']
        spo2_ref = sample['spo2_ref']
        label = sample['label']
        motion = sample['motion']

        progress = f"[{idx + 1}/{n_samples}]"

        for aname, afunc in zip(algo_names, algo_funcs):
            ir_copy = ir.copy()
            red_copy = red.copy()

            t0 = time.perf_counter()
            try:
                hr, hr_valid, spo2, spo2_valid = afunc(ir_copy, red_copy)
            except Exception as e:
                hr, hr_valid, spo2, spo2_valid = -999, False, -999, False
                print(f"  {progress} {aname} ERROR on {label}: {e}")
            elapsed = time.perf_counter() - t0

            hr_out = float(hr) if hr_valid else np.nan
            spo2_out = float(spo2) if spo2_valid else np.nan

            results[aname]['hr'].append(hr_out)
            results[aname]['spo2'].append(spo2_out)
            results[aname]['hr_ref'].append(hr_ref)
            results[aname]['spo2_ref'].append(spo2_ref)
            results[aname]['time'].append(elapsed)
            results[aname]['motion'].append(motion)
            results[aname]['label'].append(label)

        # progress
        if (idx + 1) % 10 == 0 or idx == n_samples - 1:
            times_str = " | ".join(
                f"{n}:{np.mean(results[n]['time'][-10:]) * 1000:.1f}ms"
                for n in algo_names)
            print(f"  {progress} {label:40s} [{times_str}]")

    # ---- Compute metrics ----
    print("\n" + "=" * 70)
    print("  RESULTS")
    print("=" * 70)

    # Determine grouping strategy based on data type
    is_real_data = any('BIDMC' in s['label'] for s in dataset)

    if is_real_data:
        # Group by HR range for real data (since all are motion=0.0 resting)
        def _make_hr_groups():
            groups = {'All': lambda idx, r: True}
            # HR range groups
            groups['HR < 70 BPM'] = lambda idx, r: r[r['algo']][_k('hr_ref', idx)] < 70
            groups['HR 70-90 BPM'] = lambda idx, r: 70 <= r[r['algo']][_k('hr_ref', idx)] < 90
            groups['HR 90-110 BPM'] = lambda idx, r: 90 <= r[r['algo']][_k('hr_ref', idx)] < 110
            groups['HR >= 110 BPM'] = lambda idx, r: r[r['algo']][_k('hr_ref', idx)] >= 110
            # SpO2 range groups
            groups['SpO2 >= 97%'] = lambda idx, r: r[r['algo']][_k('spo2_ref', idx)] >= 97
            groups['SpO2 94-97%'] = lambda idx, r: 94 <= r[r['algo']][_k('spo2_ref', idx)] < 97
            groups['SpO2 < 94%'] = lambda idx, r: r[r['algo']][_k('spo2_ref', idx)] < 94
            return groups

        # Simpler approach: use hr_ref from first algo to group
        ref_algo = algo_names[0]
        hr_refs_all = results[ref_algo]['hr_ref']
        spo2_refs_all = results[ref_algo]['spo2_ref']

        groups = {
            'All': lambda i: True,
            'HR < 70 BPM': lambda i: hr_refs_all[i] < 70,
            'HR 70-90 BPM': lambda i: 70 <= hr_refs_all[i] < 90,
            'HR 90-110 BPM': lambda i: 90 <= hr_refs_all[i] < 110,
            'HR >= 110 BPM': lambda i: hr_refs_all[i] >= 110,
            'SpO2 >= 97%': lambda i: spo2_refs_all[i] >= 97,
            'SpO2 94-97%': lambda i: 94 <= spo2_refs_all[i] < 97,
            'SpO2 < 94%': lambda i: spo2_refs_all[i] < 94,
        }
    else:
        # Group by motion level for synthetic data
        ref_algo = algo_names[0]
        motion_all = results[ref_algo]['motion']
        groups = {
            'All': lambda i: True,
            'Stationary (0.0)': lambda i: motion_all[i] < 0.1,
            'Light motion (0.3)': lambda i: 0.1 <= motion_all[i] < 0.5,
            'Moderate motion (0.6)': lambda i: 0.5 <= motion_all[i] < 0.8,
            'Heavy motion (1.0)': lambda i: motion_all[i] >= 0.8,
        }

    summary_rows = []

    for group_name, group_filter in groups.items():
        n_total_ref = len(results[ref_algo]['hr'])
        mask = [group_filter(i) for i in range(n_total_ref)]

        # Skip empty groups
        if sum(mask) == 0:
            continue

        print(f"\n--- {group_name} (n={sum(mask)}) ---")
        header = f"{'Algorithm':<22s} | {'HR MAE':>7s} {'HR RMSE':>8s} {'HR r':>6s} {'HR%':>5s} | " \
                 f"{'SpO2 MAE':>9s} {'SpO2 RMSE':>10s} {'SpO2 r':>7s} {'SpO2%':>6s} | {'Time(ms)':>9s}"
        print(header)
        print("-" * len(header))

        for aname in algo_names:
            hr_pred = [results[aname]['hr'][i] for i, ok in enumerate(mask) if ok]
            hr_ref = [results[aname]['hr_ref'][i] for i, ok in enumerate(mask) if ok]
            spo2_pred = [results[aname]['spo2'][i] for i, ok in enumerate(mask) if ok]
            spo2_ref = [results[aname]['spo2_ref'][i] for i, ok in enumerate(mask) if ok]
            times = [results[aname]['time'][i] for i, ok in enumerate(mask) if ok]

            if len(hr_pred) == 0:
                continue

            hr_m = compute_metrics(hr_pred, hr_ref)
            spo2_m = compute_metrics(spo2_pred, spo2_ref)
            avg_time = np.mean(times) * 1000

            print(f"{aname:<22s} | "
                  f"{hr_m['mae']:7.2f} {hr_m['rmse']:8.2f} {hr_m['corr']:6.3f} {hr_m['valid_ratio']:5.1%} | "
                  f"{spo2_m['mae']:9.2f} {spo2_m['rmse']:10.2f} {spo2_m['corr']:7.3f} {spo2_m['valid_ratio']:6.1%} | "
                  f"{avg_time:9.2f}")

            if group_name == 'All':
                summary_rows.append({
                    'algo': aname,
                    'hr_mae': hr_m['mae'], 'hr_rmse': hr_m['rmse'],
                    'hr_corr': hr_m['corr'], 'hr_valid': hr_m['valid_ratio'],
                    'spo2_mae': spo2_m['mae'], 'spo2_rmse': spo2_m['rmse'],
                    'spo2_corr': spo2_m['corr'], 'spo2_valid': spo2_m['valid_ratio'],
                    'avg_time_ms': avg_time
                })

    # ---- Save detailed CSV ----
    csv_path = results_dir / "metrics.csv"
    with open(csv_path, 'w') as f:
        f.write("label,motion,algorithm,hr_pred,hr_ref,spo2_pred,spo2_ref,time_ms\n")
        for aname in algo_names:
            for i in range(len(results[aname]['label'])):
                f.write(f"{results[aname]['label'][i]},"
                        f"{results[aname]['motion'][i]:.2f},"
                        f"{aname},"
                        f"{results[aname]['hr'][i]:.1f},"
                        f"{results[aname]['hr_ref'][i]:.1f},"
                        f"{results[aname]['spo2'][i]:.1f},"
                        f"{results[aname]['spo2_ref'][i]:.1f},"
                        f"{results[aname]['time'][i] * 1000:.3f}\n")
    print(f"\n[SAVED] Detailed results: {csv_path}")

    # ---- Generate plots ----
    try:
        _generate_plots(results, algo_names, results_dir)
    except ImportError:
        print("\n[WARN] matplotlib not installed, skipping plots.")
        print("       Install with: pip install matplotlib")

    print(f"\n[DONE] Results saved to {results_dir}/")


def _generate_plots(results, algo_names, results_dir):
    """Generate comparison plots."""
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt

    colors = {'Maxim_Original': '#e74c3c', 'VSLMS_Improved': '#2ecc71',
              'CEEMDAN_VSLMS': '#3498db'}

    # ---- Plot 1: HR scatter ----
    fig, axes = plt.subplots(1, len(algo_names), figsize=(6 * len(algo_names), 5))
    if len(algo_names) == 1:
        axes = [axes]
    for ax, aname in zip(axes, algo_names):
        hr_p = np.array(results[aname]['hr'])
        hr_r = np.array(results[aname]['hr_ref'])
        valid = np.isfinite(hr_p) & (hr_p > 0)
        ax.scatter(hr_r[valid], hr_p[valid], alpha=0.4, s=15, c=colors.get(aname, 'gray'))
        ax.plot([30, 180], [30, 180], 'k--', alpha=0.5)
        m = compute_metrics(hr_p, hr_r)
        ax.set_title(f"{aname}\nMAE={m['mae']:.1f} RMSE={m['rmse']:.1f} r={m['corr']:.3f}")
        ax.set_xlabel("Reference HR (BPM)")
        ax.set_ylabel("Estimated HR (BPM)")
        ax.set_xlim(30, 180)
        ax.set_ylim(30, 180)
        ax.set_aspect('equal')
    plt.tight_layout()
    plt.savefig(results_dir / "hr_scatter.png", dpi=150)
    plt.close()

    # ---- Plot 2: Bland-Altman for HR ----
    fig, axes = plt.subplots(1, len(algo_names), figsize=(6 * len(algo_names), 5))
    if len(algo_names) == 1:
        axes = [axes]
    for ax, aname in zip(axes, algo_names):
        hr_p = np.array(results[aname]['hr'])
        hr_r = np.array(results[aname]['hr_ref'])
        valid = np.isfinite(hr_p) & (hr_p > 0)
        mean_val = (hr_p[valid] + hr_r[valid]) / 2
        diff_val = hr_p[valid] - hr_r[valid]
        ax.scatter(mean_val, diff_val, alpha=0.4, s=15, c=colors.get(aname, 'gray'))
        md = np.mean(diff_val)
        sd = np.std(diff_val)
        ax.axhline(md, color='k', linestyle='-', alpha=0.5)
        ax.axhline(md + 1.96 * sd, color='r', linestyle='--', alpha=0.5)
        ax.axhline(md - 1.96 * sd, color='r', linestyle='--', alpha=0.5)
        ax.set_title(f"{aname}\nBias={md:.1f} LoA=[{md - 1.96 * sd:.1f}, {md + 1.96 * sd:.1f}]")
        ax.set_xlabel("Mean HR (BPM)")
        ax.set_ylabel("Difference (Est - Ref)")
    plt.tight_layout()
    plt.savefig(results_dir / "hr_bland_altman.png", dpi=150)
    plt.close()

    # ---- Plot 3: Performance grouped plot ----
    is_real_data = any('BIDMC' in l for l in results[algo_names[0]]['label'])

    if is_real_data:
        # Group by HR reference range for real data
        hr_bins = [(0, 70, '<70'), (70, 90, '70-90'), (90, 110, '90-110'), (110, 250, '>=110')]
        fig, axes = plt.subplots(1, 2, figsize=(14, 5))

        for aname in algo_names:
            hr_maes, spo2_maes, xlabels = [], [], []
            hr_r_all = np.array(results[aname]['hr_ref'])
            for lo, hi, lbl in hr_bins:
                mask = (hr_r_all >= lo) & (hr_r_all < hi)
                if np.sum(mask) < 2:
                    hr_maes.append(np.nan)
                    spo2_maes.append(np.nan)
                else:
                    hr_p = np.array(results[aname]['hr'])[mask]
                    hr_r = hr_r_all[mask]
                    spo2_p = np.array(results[aname]['spo2'])[mask]
                    spo2_r = np.array(results[aname]['spo2_ref'])[mask]
                    hr_maes.append(compute_metrics(hr_p, hr_r)['mae'])
                    spo2_maes.append(compute_metrics(spo2_p, spo2_r)['mae'])
                xlabels.append(lbl)

            c = colors.get(aname, 'gray')
            x = np.arange(len(xlabels))
            axes[0].plot(x, hr_maes, 'o-', label=aname, color=c, linewidth=2)
            axes[1].plot(x, spo2_maes, 'o-', label=aname, color=c, linewidth=2)

        axes[0].set_xticks(np.arange(len(xlabels)))
        axes[0].set_xticklabels(xlabels)
        axes[0].set_xlabel("Reference HR Range (BPM)")
        axes[0].set_ylabel("HR MAE (BPM)")
        axes[0].set_title("Heart Rate Accuracy by HR Range")
        axes[0].legend()
        axes[0].grid(True, alpha=0.3)

        axes[1].set_xticks(np.arange(len(xlabels)))
        axes[1].set_xticklabels(xlabels)
        axes[1].set_xlabel("Reference HR Range (BPM)")
        axes[1].set_ylabel("SpO2 MAE (%)")
        axes[1].set_title("SpO2 Accuracy by HR Range")
        axes[1].legend()
        axes[1].grid(True, alpha=0.3)
    else:
        # Group by motion level for synthetic data
        motion_levels = sorted(set(results[algo_names[0]]['motion']))
        fig, axes = plt.subplots(1, 2, figsize=(12, 5))

        for aname in algo_names:
            hr_maes = []
            spo2_maes = []
            for ml in motion_levels:
                mask = [m == ml for m in results[aname]['motion']]
                hr_p = [results[aname]['hr'][i] for i, ok in enumerate(mask) if ok]
                hr_r = [results[aname]['hr_ref'][i] for i, ok in enumerate(mask) if ok]
                spo2_p = [results[aname]['spo2'][i] for i, ok in enumerate(mask) if ok]
                spo2_r = [results[aname]['spo2_ref'][i] for i, ok in enumerate(mask) if ok]
                hr_maes.append(compute_metrics(hr_p, hr_r)['mae'])
                spo2_maes.append(compute_metrics(spo2_p, spo2_r)['mae'])

            c = colors.get(aname, 'gray')
            axes[0].plot(motion_levels, hr_maes, 'o-', label=aname, color=c, linewidth=2)
            axes[1].plot(motion_levels, spo2_maes, 'o-', label=aname, color=c, linewidth=2)

        axes[0].set_xlabel("Motion Artifact Level")
        axes[0].set_ylabel("HR MAE (BPM)")
        axes[0].set_title("Heart Rate Accuracy vs Motion")
        axes[0].legend()
        axes[0].grid(True, alpha=0.3)

        axes[1].set_xlabel("Motion Artifact Level")
        axes[1].set_ylabel("SpO2 MAE (%)")
        axes[1].set_title("SpO2 Accuracy vs Motion")
        axes[1].legend()
        axes[1].grid(True, alpha=0.3)

    plt.tight_layout()
    plt.savefig(results_dir / "accuracy_vs_motion.png", dpi=150)
    plt.close()

    # ---- Plot 4: Execution time comparison ----
    fig, ax = plt.subplots(figsize=(8, 5))
    avg_times = []
    for aname in algo_names:
        t = np.mean(results[aname]['time']) * 1000
        avg_times.append(t)
    bars = ax.bar(algo_names, avg_times, color=[colors.get(n, 'gray') for n in algo_names])
    ax.set_ylabel("Average Time per Window (ms)")
    ax.set_title("Computation Time Comparison (500 samples @ 100Hz)")
    for bar, val in zip(bars, avg_times):
        ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height(),
                f'{val:.1f}ms', ha='center', va='bottom', fontsize=10)
    ax.set_yscale('log')
    plt.tight_layout()
    plt.savefig(results_dir / "execution_time.png", dpi=150)
    plt.close()

    # ---- Plot 5: Valid detection rate ----
    fig, axes = plt.subplots(1, 2, figsize=(12, 5))

    if is_real_data:
        hr_bins = [(0, 70, '<70'), (70, 90, '70-90'), (90, 110, '90-110'), (110, 250, '>=110')]
        for aname in algo_names:
            hr_rates, spo2_rates, xlabels = [], [], []
            hr_r_all = np.array(results[aname]['hr_ref'])
            for lo, hi, lbl in hr_bins:
                mask = (hr_r_all >= lo) & (hr_r_all < hi)
                hr_p = np.array(results[aname]['hr'])[mask]
                spo2_p = np.array(results[aname]['spo2'])[mask]
                total = max(len(hr_p), 1)
                hr_rates.append(sum(1 for h in hr_p if np.isfinite(h) and h > 0) / total)
                spo2_rates.append(sum(1 for s in spo2_p if np.isfinite(s) and s > 0) / total)
                xlabels.append(lbl)

            c = colors.get(aname, 'gray')
            x = np.arange(len(xlabels))
            axes[0].plot(x, hr_rates, 'o-', label=aname, color=c, linewidth=2)
            axes[1].plot(x, spo2_rates, 'o-', label=aname, color=c, linewidth=2)

        axes[0].set_xticks(np.arange(len(xlabels)))
        axes[0].set_xticklabels(xlabels)
        axes[0].set_xlabel("Reference HR Range (BPM)")
        axes[1].set_xticks(np.arange(len(xlabels)))
        axes[1].set_xticklabels(xlabels)
        axes[1].set_xlabel("Reference HR Range (BPM)")
    else:
        motion_levels = sorted(set(results[algo_names[0]]['motion']))
        for aname in algo_names:
            hr_rates = []
            spo2_rates = []
            for ml in motion_levels:
                mask = [m == ml for m in results[aname]['motion']]
                hr_p = [results[aname]['hr'][i] for i, ok in enumerate(mask) if ok]
                spo2_p = [results[aname]['spo2'][i] for i, ok in enumerate(mask) if ok]
                hr_valid = sum(1 for h in hr_p if np.isfinite(h) and h > 0) / max(len(hr_p), 1)
                spo2_valid = sum(1 for s in spo2_p if np.isfinite(s) and s > 0) / max(len(spo2_p), 1)
                hr_rates.append(hr_valid)
                spo2_rates.append(spo2_valid)

            c = colors.get(aname, 'gray')
            axes[0].plot(motion_levels, hr_rates, 'o-', label=aname, color=c, linewidth=2)
            axes[1].plot(motion_levels, spo2_rates, 'o-', label=aname, color=c, linewidth=2)

        axes[0].set_xlabel("Motion Level")
        axes[1].set_xlabel("Motion Level")
    axes[0].set_ylabel("Valid Detection Rate")
    axes[0].set_title("HR Valid Output Rate vs Motion")
    axes[0].legend()
    axes[0].grid(True, alpha=0.3)
    axes[0].set_ylim(-0.05, 1.05)

    axes[1].set_xlabel("Motion Level")
    axes[1].set_ylabel("Valid Detection Rate")
    axes[1].set_title("SpO2 Valid Output Rate vs Motion")
    axes[1].legend()
    axes[1].grid(True, alpha=0.3)
    axes[1].set_ylim(-0.05, 1.05)

    plt.tight_layout()
    plt.savefig(results_dir / "valid_rate_vs_motion.png", dpi=150)
    plt.close()

    print(f"[SAVED] Plots: {results_dir}/hr_scatter.png")
    print(f"               {results_dir}/hr_bland_altman.png")
    print(f"               {results_dir}/accuracy_vs_motion.png")
    print(f"               {results_dir}/execution_time.png")
    print(f"               {results_dir}/valid_rate_vs_motion.png")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="PPG Algorithm Benchmark")
    parser.add_argument("--no-ceemdan", action="store_true",
                        help="Skip CEEMDAN algorithm (much faster)")
    parser.add_argument("--data", type=str, default="auto",
                        help="Dataset: 'auto', 'bidmc', or 'synthetic'")
    args = parser.parse_args()
    run_benchmark(args)
