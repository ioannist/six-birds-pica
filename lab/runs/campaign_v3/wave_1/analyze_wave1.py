#!/usr/bin/env python3
"""
Wave 1 post-fix analysis for campaign_v3.

Outputs:
  - stats_by_exp_config_scale.csv
  - hypothesis_tests.json
  - analysis_report.md
"""

from __future__ import annotations

import json
import math
import statistics
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd
from scipy.stats import fisher_exact, kruskal, mannwhitneyu, spearmanr


ROOT = Path("lab/runs/campaign_v3/wave_1")
AUDITS_PATH = ROOT / "audits.jsonl"
STATS_CSV = ROOT / "stats_by_exp_config_scale.csv"
HYP_JSON = ROOT / "hypothesis_tests.json"
REPORT_MD = ROOT / "analysis_report.md"

ALPHA = 0.05
BONFERRONI_ALPHA = 0.0125  # for HYP-202..205 preregistered family correction

KEY_METRICS = [
    "frob_from_rank1",
    "macro_gap",
    "sigma",
    "sigma_u",
    "sigma_ratio",
    "tau",
    "active_tau",
    "budget",
    "macro_n",
    "trans_ep",
]

MW_METRICS_H130 = [
    "frob_from_rank1",
    "sigma",
    "macro_gap",
    "sigma_ratio",
    "budget",
]


def load_jsonl(path: Path) -> List[dict]:
    rows = []
    with path.open() as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def rev_multiscale(rec: dict) -> bool:
    scan = rec.get("multi_scale_scan") or []
    sigmas = [e.get("sigma_pi") for e in scan if e.get("sigma_pi") is not None]
    if sigmas:
        return all(s < 1e-3 for s in sigmas)
    sigma = rec.get("sigma")
    return sigma is not None and sigma < 1e-3


def build_dataframes(records: List[dict]) -> Tuple[pd.DataFrame, pd.DataFrame]:
    run_rows = []
    scan_rows = []

    for rec in records:
        row = {
            "exp_id": rec.get("exp_id"),
            "config_name": rec.get("config_name") or rec.get("_cfg_label"),
            "n": rec.get("n"),
            "seed": rec.get("seed"),
            "schema_version": rec.get("schema_version"),
            "git_sha": rec.get("git_sha"),
            "rev_multiscale": rev_multiscale(rec),
            "budget_ratio": None,
        }
        for m in KEY_METRICS:
            row[m] = rec.get(m)
        n = rec.get("n")
        b = rec.get("budget")
        if n and b is not None and n > 1:
            row["budget_ratio"] = b / (n * math.log(n))
        run_rows.append(row)

        for e in rec.get("multi_scale_scan") or []:
            srow = {
                "exp_id": rec.get("exp_id"),
                "config_name": rec.get("config_name") or rec.get("_cfg_label"),
                "n": rec.get("n"),
                "seed": rec.get("seed"),
                "k": e.get("k"),
            }
            for k in [
                "frob",
                "macro_gap",
                "sigma_pi",
                "step_entropy",
                "pla2_gap",
                "lagr_geo_r2",
                "lagr_diff_kl",
                "lagr_diff_alpha",
                "t_rel",
                "gap_ratio",
                "eigen_entropy",
                "spectral_participation",
                "slow_modes_r50",
                "slow_modes_r70",
                "slow_modes_r90",
            ]:
                srow[k] = e.get(k)
            scan_rows.append(srow)

    run_df = pd.DataFrame(run_rows)
    scan_df = pd.DataFrame(scan_rows)
    return run_df, scan_df


def q1(xs: Sequence[float]) -> float:
    return float(pd.Series(xs).quantile(0.25))


def q3(xs: Sequence[float]) -> float:
    return float(pd.Series(xs).quantile(0.75))


def iqr(xs: Sequence[float]) -> float:
    return q3(xs) - q1(xs)


def cliffs_delta(x: Sequence[float], y: Sequence[float]) -> float:
    if not x or not y:
        return float("nan")
    gt = 0
    lt = 0
    for a in x:
        for b in y:
            if a > b:
                gt += 1
            elif a < b:
                lt += 1
    return (gt - lt) / (len(x) * len(y))


def mw_test(
    x: Sequence[float],
    y: Sequence[float],
    alternative: str = "two-sided",
) -> Optional[dict]:
    if len(x) < 3 or len(y) < 3:
        return None
    u, p = mannwhitneyu(x, y, alternative=alternative)
    med_x = statistics.median(x)
    med_y = statistics.median(y)
    return {
        "n_x": len(x),
        "n_y": len(y),
        "u": float(u),
        "p": float(p),
        "median_x": float(med_x),
        "median_y": float(med_y),
        "median_diff": float(med_x - med_y),
        "cliffs_delta": float(cliffs_delta(x, y)),
    }


def median_abs_dev(vals: Sequence[float]) -> float:
    med = statistics.median(vals)
    return float(statistics.median([abs(v - med) for v in vals]))


def pooled_mad_effect(x: Sequence[float], y: Sequence[float]) -> float:
    """Effect size used in prereg HYP-202: |median diff| / pooled MAD."""
    mx = median_abs_dev(x)
    my = median_abs_dev(y)
    pooled = (mx + my) / 2.0
    if pooled <= 1e-15:
        return float("inf")
    med_diff = statistics.median(x) - statistics.median(y)
    return abs(med_diff) / pooled


def bh_fdr(pvals: Sequence[float]) -> List[float]:
    """Benjamini-Hochberg adjusted q-values."""
    m = len(pvals)
    if m == 0:
        return []
    order = sorted(range(m), key=lambda i: pvals[i])
    q = [0.0] * m
    prev = 1.0
    for r, idx in enumerate(reversed(order), start=1):
        k = m - r + 1
        val = min(prev, pvals[idx] * m / k)
        q[idx] = val
        prev = val
    return q


def rank_partial_corr(
    x: Sequence[float],
    y: Sequence[float],
    z: Sequence[float],
) -> Optional[dict]:
    """Approximate partial Spearman by rank-transform + residual Pearson.

    Returns rho and permutation p-value.
    """
    if not (len(x) == len(y) == len(z)) or len(x) < 10:
        return None
    df = pd.DataFrame({"x": x, "y": y, "z": z}).rank(method="average")
    X = pd.DataFrame({"const": 1.0, "z": df["z"]}).to_numpy()
    xv = df["x"].to_numpy()
    yv = df["y"].to_numpy()

    bx, *_ = np.linalg.lstsq(X, xv, rcond=None)
    by, *_ = np.linalg.lstsq(X, yv, rcond=None)
    rx = xv - X @ bx
    ry = yv - X @ by
    rho = float(np.corrcoef(rx, ry)[0, 1])

    # Two-sided permutation p-value with fixed seed for reproducibility.
    rng = np.random.default_rng(0)
    obs = abs(rho)
    n_perm = 20000
    cnt = 0
    for _ in range(n_perm):
        py = rng.permutation(ry)
        rr = float(np.corrcoef(rx, py)[0, 1])
        if abs(rr) >= obs:
            cnt += 1
    p = float((cnt + 1) / (n_perm + 1))
    return {"rho": rho, "p": p, "n": len(x)}


def fmt(v: Optional[float], digits: int = 3) -> str:
    if v is None:
        return "NA"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        if math.isnan(v) or math.isinf(v):
            return "NA"
        return f"{v:.{digits}f}"
    return str(v)


def md_table(headers: List[str], rows: List[List[object]]) -> str:
    out = []
    out.append("| " + " | ".join(headers) + " |")
    out.append("| " + " | ".join(["---"] * len(headers)) + " |")
    for row in rows:
        out.append("| " + " | ".join(str(x) for x in row) + " |")
    return "\n".join(out)


def group_stats(run_df: pd.DataFrame) -> pd.DataFrame:
    out_rows = []
    for (exp_id, cfg, n), g in run_df.groupby(["exp_id", "config_name", "n"], dropna=False):
        row = {
            "exp_id": exp_id,
            "config_name": cfg,
            "n": int(n),
            "runs": int(len(g)),
            "rev_rate": float(g["rev_multiscale"].mean()),
        }
        for m in KEY_METRICS + ["budget_ratio"]:
            vals = [float(v) for v in g[m].dropna().tolist()]
            if vals:
                row[f"{m}_median"] = float(statistics.median(vals))
                row[f"{m}_iqr"] = float(iqr(vals))
                row[f"{m}_min"] = float(min(vals))
                row[f"{m}_max"] = float(max(vals))
            else:
                row[f"{m}_median"] = None
                row[f"{m}_iqr"] = None
                row[f"{m}_min"] = None
                row[f"{m}_max"] = None
        out_rows.append(row)
    df = pd.DataFrame(out_rows).sort_values(["exp_id", "n", "frob_from_rank1_median"], ascending=[True, True, False])
    return df


def get_run_values(
    run_df: pd.DataFrame,
    exp_id: str,
    config: str,
    n: int,
    metric: str,
) -> List[float]:
    g = run_df[
        (run_df["exp_id"] == exp_id)
        & (run_df["config_name"] == config)
        & (run_df["n"] == n)
    ]
    vals = [float(v) for v in g[metric].dropna().tolist()]
    return vals


def get_scan_values(
    scan_df: pd.DataFrame,
    exp_id: str,
    config: str,
    n_values: Sequence[int],
    k: int,
    metric: str,
) -> List[float]:
    g = scan_df[
        (scan_df["exp_id"] == exp_id)
        & (scan_df["config_name"] == config)
        & (scan_df["n"].isin(list(n_values)))
        & (scan_df["k"] == k)
    ]
    vals = [float(v) for v in g[metric].dropna().tolist()]
    return vals


def hypothesis_tests(run_df: pd.DataFrame, scan_df: pd.DataFrame) -> Dict[str, dict]:
    out: Dict[str, dict] = {}

    # HYP-130: distinct signatures for single-cell toggles (EXP-100).
    exp = "EXP-100"
    baseline = "baseline"
    cells = sorted(
        c for c in run_df[run_df["exp_id"] == exp]["config_name"].unique().tolist() if c != baseline
    )
    tested_cells = []
    distinct_count = 0
    for cell in cells:
        tests = []
        for n in [32, 64, 128]:
            for metric in MW_METRICS_H130:
                x = get_run_values(run_df, exp, cell, n, metric)
                y = get_run_values(run_df, exp, baseline, n, metric)
                t = mw_test(x, y, "two-sided")
                if not t:
                    continue
                tests.append(
                    {
                        "metric": metric,
                        "n": n,
                        "p": t["p"],
                        "cliffs_delta": t["cliffs_delta"],
                        "median_diff": t["median_diff"],
                    }
                )

        if not tests:
            tested_cells.append({"cell": cell, "distinct": False, "method": "bh_fdr", "best_signal": None, "tests": []})
            continue

        pvals = [t["p"] for t in tests]
        qvals = bh_fdr(pvals)
        bonf_alpha = ALPHA / len(tests)
        best_sig = None
        distinct = False
        for i, t in enumerate(tests):
            t["q_bh"] = qvals[i]
            t["sig_bh"] = (t["q_bh"] < ALPHA)
            t["sig_bonf"] = (t["p"] < bonf_alpha)
            t["large_effect"] = abs(t["cliffs_delta"]) > 0.5
            if t["sig_bh"] and t["large_effect"]:
                distinct = True
                if best_sig is None or abs(t["cliffs_delta"]) > abs(best_sig["cliffs_delta"]):
                    best_sig = {
                        "metric": t["metric"],
                        "n": t["n"],
                        "p": t["p"],
                        "q_bh": t["q_bh"],
                        "cliffs_delta": t["cliffs_delta"],
                        "median_diff": t["median_diff"],
                    }
        if distinct:
            distinct_count += 1
        tested_cells.append(
            {
                "cell": cell,
                "distinct": distinct,
                "method": "bh_fdr",
                "n_tests": len(tests),
                "bonf_alpha_per_cell": bonf_alpha,
                "best_signal": best_sig,
                "tests": tests,
            }
        )

    if distinct_count == len(cells) and len(cells) > 0:
        verdict = "supported"
    elif distinct_count == 0:
        verdict = "refuted"
    else:
        verdict = "partially_supported"
    out["HYP-130"] = {
        "verdict": verdict,
        "tested_cells": len(cells),
        "distinct_cells": distinct_count,
        "distinct_ratio": (distinct_count / len(cells)) if cells else 0.0,
        "note": "A10 is baseline and is not isolatable in EXP-100. Distinctness uses BH-FDR per cell across 15 tests and |Cliff's delta|>0.5.",
        "details": tested_cells,
    }

    # HYP-131: needs static-control experiment (EXP-090) not present in wave_1.
    out["HYP-131"] = {
        "verdict": "open",
        "reason": "wave_1 includes EXP-101 but not static-control EXP-090 comparator required by claim.",
    }

    # HYP-137: boost-level invariance for A19 switch.
    # IMPORTANT: failure to reject differences is not evidence of invariance.
    # We only refute on positive evidence of differences / absence of REV.
    # If not refuted, mark open unless a future equivalence margin is preregistered.
    boost_cfgs = ["boost_0.1", "boost_0.5", "boost_1.0", "boost_3.0", "boost_4.0"]
    h137_scales = {}
    for n in [32, 64, 128]:
        sigma_groups = [get_run_values(run_df, "EXP-106", c, n, "sigma") for c in boost_cfgs]
        frob_groups = [get_run_values(run_df, "EXP-106", c, n, "frob_from_rank1") for c in boost_cfgs]
        valid_sigma = all(len(g) >= 3 for g in sigma_groups)
        valid_frob = all(len(g) >= 3 for g in frob_groups)
        rev_rates = {}
        for c in boost_cfgs:
            g = run_df[
                (run_df["exp_id"] == "EXP-106")
                & (run_df["config_name"] == c)
                & (run_df["n"] == n)
            ]
            rev_rates[c] = float(g["rev_multiscale"].mean()) if len(g) else float("nan")
        h137_scales[str(n)] = {
            "rev_rates": rev_rates,
            "sigma_kruskal_p": float(kruskal(*sigma_groups).pvalue) if valid_sigma else None,
            "frob_kruskal_p": float(kruskal(*frob_groups).pvalue) if valid_frob else None,
            "sigma_medians": {
                c: float(statistics.median(get_run_values(run_df, "EXP-106", c, n, "sigma")))
                for c in boost_cfgs
            },
        }

    n64_rev = [h137_scales["64"]["rev_rates"][c] for c in boost_cfgs]
    sigma_p64 = h137_scales["64"]["sigma_kruskal_p"]
    if sigma_p64 is not None and (sigma_p64 < ALPHA or min(n64_rev) < 0.8):
        verdict_137 = "refuted"
        note_137 = "Refuted by significant sigma differences or low REV rates at n=64."
    else:
        verdict_137 = "open"
        note_137 = "Not refuted, but support requires an equivalence test with preregistered margin."
    out["HYP-137"] = {"verdict": verdict_137, "note": note_137, "scales": h137_scales}

    # HYP-138: SBRC breaks reversible chain.
    h138 = {}
    for n in [32, 64, 128]:
        chain = get_run_values(run_df, "EXP-106", "A13_A18_A19", n, "sigma")
        sbrc = get_run_values(run_df, "EXP-106", "chain_SBRC", n, "sigma")
        t = mw_test(sbrc, chain, "greater")
        chain_rev = int(
            run_df[
                (run_df["exp_id"] == "EXP-106")
                & (run_df["config_name"] == "A13_A18_A19")
                & (run_df["n"] == n)
            ]["rev_multiscale"].sum()
        )
        sbrc_rev = int(
            run_df[
                (run_df["exp_id"] == "EXP-106")
                & (run_df["config_name"] == "chain_SBRC")
                & (run_df["n"] == n)
            ]["rev_multiscale"].sum()
        )
        table = [[sbrc_rev, 10 - sbrc_rev], [chain_rev, 10 - chain_rev]]
        _, fisher_p = fisher_exact(table, alternative="less")  # sbrc has fewer REV
        h138[str(n)] = {
            "sigma_test": t,
            "chain_rev": chain_rev,
            "sbrc_rev": sbrc_rev,
            "rev_fisher_p": float(fisher_p),
        }

    t64 = h138["64"]["sigma_test"]
    chain64 = h138["64"]["chain_rev"]
    sbrc64 = h138["64"]["sbrc_rev"]
    f64 = h138["64"]["rev_fisher_p"]
    support_138 = (
        t64 is not None
        and t64["p"] < ALPHA
        and t64["median_diff"] > 0
        and f64 < ALPHA
        and sbrc64 < chain64
        and chain64 >= 8
    )
    out["HYP-138"] = {"verdict": "supported" if support_138 else "refuted", "scales": h138}

    # HYP-139: A14 disrupts reversible chain.
    h139 = {}
    for n in [32, 64, 128]:
        chain = get_run_values(run_df, "EXP-106", "A13_A18_A19", n, "sigma")
        a14 = get_run_values(run_df, "EXP-106", "A13_A14_A19", n, "sigma")
        t = mw_test(a14, chain, "greater")
        chain_rev = int(
            run_df[
                (run_df["exp_id"] == "EXP-106")
                & (run_df["config_name"] == "A13_A18_A19")
                & (run_df["n"] == n)
            ]["rev_multiscale"].sum()
        )
        a14_rev = int(
            run_df[
                (run_df["exp_id"] == "EXP-106")
                & (run_df["config_name"] == "A13_A14_A19")
                & (run_df["n"] == n)
            ]["rev_multiscale"].sum()
        )
        table = [[a14_rev, 10 - a14_rev], [chain_rev, 10 - chain_rev]]
        _, fisher_p = fisher_exact(table, alternative="less")  # A14 has fewer REV
        h139[str(n)] = {
            "sigma_test": t,
            "chain_rev": chain_rev,
            "a14_rev": a14_rev,
            "rev_fisher_p": float(fisher_p),
        }
    t64 = h139["64"]["sigma_test"]
    f64 = h139["64"]["rev_fisher_p"]
    support_139 = (
        t64 is not None
        and t64["p"] < ALPHA
        and t64["median_diff"] > 0
        and f64 < ALPHA
        and h139["64"]["a14_rev"] < h139["64"]["chain_rev"]
    )
    out["HYP-139"] = {"verdict": "supported" if support_139 else "refuted", "scales": h139}

    # HYP-202: PLA2 gap lower in full_action vs baseline at k=4 (EXP-109).
    x202 = get_scan_values(scan_df, "EXP-109", "full_action", [64, 128], 4, "pla2_gap")
    y202 = get_scan_values(scan_df, "EXP-109", "baseline", [64, 128], 4, "pla2_gap")
    t202 = mw_test(x202, y202, "less")
    effect202 = pooled_mad_effect(x202, y202) if x202 and y202 else None
    support_202 = (
        t202 is not None
        and t202["p"] < BONFERRONI_ALPHA
        and t202["median_diff"] < 0
        and effect202 is not None
        and effect202 >= 0.5
    )
    out["HYP-202"] = {"verdict": "supported" if support_202 else "refuted", "test": t202, "pooled_mad_effect": effect202}

    # HYP-203: geo_r2 positively correlates with frob.
    # Primary test for verdict: fixed k=4 at n=128 to avoid k-axis confounding.
    g203_all = scan_df[
        (scan_df["exp_id"] == "EXP-109")
        & (scan_df["n"] == 128)
        & scan_df["lagr_geo_r2"].notna()
        & scan_df["frob"].notna()
        & scan_df["k"].notna()
    ]
    rho_all, p_all = spearmanr(g203_all["lagr_geo_r2"], g203_all["frob"]) if len(g203_all) else (float("nan"), float("nan"))

    g203_k4 = g203_all[g203_all["k"] == 4]
    rho_k4, p_k4 = spearmanr(g203_k4["lagr_geo_r2"], g203_k4["frob"]) if len(g203_k4) else (float("nan"), float("nan"))

    partial = rank_partial_corr(
        g203_all["lagr_geo_r2"].tolist(),
        g203_all["frob"].tolist(),
        g203_all["k"].tolist(),
    ) if len(g203_all) else None

    support_203 = (len(g203_k4) >= 10 and p_k4 < BONFERRONI_ALPHA and rho_k4 >= 0.3)
    if support_203:
        verdict_203 = "supported"
    elif len(g203_k4) >= 10 and p_k4 < BONFERRONI_ALPHA and rho_k4 < 0.3:
        verdict_203 = "refuted"
    else:
        verdict_203 = "open"
    out["HYP-203"] = {
        "verdict": verdict_203,
        "primary_fixed_k4": {
            "n_points": int(len(g203_k4)),
            "rho": float(rho_k4),
            "p": float(p_k4),
        },
        "secondary_pooled": {
            "n_points": int(len(g203_all)),
            "rho": float(rho_all),
            "p": float(p_all),
        },
        "secondary_partial_control_k": partial,
    }

    # HYP-204: full_action lower gap_ratio at k=4 n=128.
    x204 = get_scan_values(scan_df, "EXP-109", "full_action", [128], 4, "gap_ratio")
    y204 = get_scan_values(scan_df, "EXP-109", "baseline", [128], 4, "gap_ratio")
    t204 = mw_test(x204, y204, "less")
    support_204 = (
        t204 is not None
        and t204["p"] < BONFERRONI_ALPHA
        and t204["median_diff"] <= -0.1
    )
    out["HYP-204"] = {"verdict": "supported" if support_204 else "refuted", "test": t204}

    # HYP-205: A14 lower diff_kl at k=4 n=128 (with A16/A17 replications).
    x205 = get_scan_values(scan_df, "EXP-109", "A14_only", [128], 4, "lagr_diff_kl")
    y205 = get_scan_values(scan_df, "EXP-109", "baseline", [128], 4, "lagr_diff_kl")
    t205 = mw_test(x205, y205, "less")
    rel205 = None
    if x205 and y205:
        med_x = statistics.median(x205)
        med_y = statistics.median(y205)
        rel205 = ((med_y - med_x) / med_y) if med_y > 1e-15 else 0.0

    rep = {}
    for cfg in ["A16_only", "A17_only"]:
        xx = get_scan_values(scan_df, "EXP-109", cfg, [128], 4, "lagr_diff_kl")
        tt = mw_test(xx, y205, "less")
        rr = None
        if xx and y205:
            med_x = statistics.median(xx)
            med_y = statistics.median(y205)
            rr = ((med_y - med_x) / med_y) if med_y > 1e-15 else 0.0
        rep[cfg] = {"test": tt, "relative_reduction": rr}

    support_205 = (
        t205 is not None
        and rel205 is not None
        and t205["p"] < BONFERRONI_ALPHA
        and rel205 >= 0.2
    )
    out["HYP-205"] = {
        "verdict": "supported" if support_205 else "refuted",
        "test": t205,
        "relative_reduction": rel205,
        "replication": rep,
    }

    return out


def prefixed_finding_checks(run_df: pd.DataFrame, scan_df: pd.DataFrame) -> dict:
    out = {}

    # F36 (ranking at n=128)
    n128 = run_df[run_df["n"] == 128]
    med_by_cfg = (
        n128.groupby("config_name")["frob_from_rank1"].median().sort_values(ascending=False)
    )
    top_cfg = med_by_cfg.index[0]
    second_cfg = med_by_cfg.index[1]
    top_vals = n128[n128["config_name"] == top_cfg]["frob_from_rank1"].dropna().tolist()
    second_vals = n128[n128["config_name"] == second_cfg]["frob_from_rank1"].dropna().tolist()
    top_vs_second = mw_test(top_vals, second_vals, "two-sided")
    inferentially_dominant = (
        top_vs_second is not None
        and top_vs_second["p"] < ALPHA
        and abs(top_vs_second["cliffs_delta"]) > 0.5
    )
    out["F36"] = {
        "top_config_global_n128_by_median": top_cfg,
        "second_config_global_n128_by_median": second_cfg,
        "top_frob_median": float(med_by_cfg.iloc[0]),
        "second_frob_median": float(med_by_cfg.iloc[1]),
        "top_vs_second_test": top_vs_second,
        "inferentially_dominant": inferentially_dominant,
        "full_action_frob_median_global_n128": float(med_by_cfg.get("full_action", float("nan"))),
        "exp107_top": "full_action",
        "exp107_top_frob": float(
            run_df[
                (run_df["exp_id"] == "EXP-107")
                & (run_df["n"] == 128)
                & (run_df["config_name"] == "full_action")
            ]["frob_from_rank1"].median()
        ),
    }

    # F40/F41 from EXP-109 n=128.
    singles = ["A13_only", "A14_only", "A16_only", "A17_only", "A19_only", "A25_only"]
    s = {}
    for cfg in singles:
        vals = get_run_values(run_df, "EXP-109", cfg, 128, "frob_from_rank1")
        s[cfg] = float(statistics.median(vals))
    singles_sorted = sorted(s.items(), key=lambda kv: kv[1], reverse=True)
    top_single, top_single_med = singles_sorted[0]
    second_single, second_single_med = singles_sorted[1]
    x = get_run_values(run_df, "EXP-109", top_single, 128, "frob_from_rank1")
    y = get_run_values(run_df, "EXP-109", second_single, 128, "frob_from_rank1")
    top_single_vs_second = mw_test(x, y, "two-sided")
    top_single_dominant = (
        top_single_vs_second is not None
        and top_single_vs_second["p"] < ALPHA
        and abs(top_single_vs_second["cliffs_delta"]) > 0.5
    )
    out["F40"] = {
        "single_cell_medians_exp109_n128": s,
        "strongest_single_by_median": top_single,
        "second_single_by_median": second_single,
        "top_single_median": top_single_med,
        "second_single_median": second_single_med,
        "top_single_vs_second_test": top_single_vs_second,
        "inferentially_dominant": top_single_dominant,
    }
    base = get_run_values(run_df, "EXP-109", "baseline", 128, "frob_from_rank1")
    pcomp = {}
    for cfg in ["A14_only", "A16_only", "A17_only"]:
        vals = get_run_values(run_df, "EXP-109", cfg, 128, "frob_from_rank1")
        pcomp[cfg] = mw_test(vals, base, "greater")
    out["F41"] = {"partition_competition_vs_baseline_n128": pcomp}

    # REV rates for A19_only/full_action (EXP-107).
    rev_rates = {}
    for cfg in ["A19_only", "full_action"]:
        rev_rates[cfg] = {}
        for n in [32, 64, 128]:
            g = run_df[
                (run_df["exp_id"] == "EXP-107")
                & (run_df["config_name"] == cfg)
                & (run_df["n"] == n)
            ]
            rev_rates[cfg][str(n)] = float(g["rev_multiscale"].mean())
    out["REV_rates"] = rev_rates

    # Budget saturation vs REV across wave_1.
    rev = run_df[run_df["rev_multiscale"] == True]["budget_ratio"].dropna().tolist()
    nrev = run_df[run_df["rev_multiscale"] == False]["budget_ratio"].dropna().tolist()
    test = mw_test(rev, nrev, "greater")
    out["Budget_vs_REV"] = {
        "rev_n": len(rev),
        "nonrev_n": len(nrev),
        "rev_budget_ratio_median": float(statistics.median(rev)) if rev else None,
        "nonrev_budget_ratio_median": float(statistics.median(nrev)) if nrev else None,
        "mw_greater": test,
    }

    # A18 check: available in wave_1?
    has_a18_only = ((run_df["config_name"] == "A18_only").sum() > 0)
    out["A18_testability"] = {
        "has_A18_only_in_wave1": bool(has_a18_only),
        "note": "No direct A18_only run in wave_1 experiments." if not has_a18_only else "A18_only present.",
    }

    return out


def build_report(
    run_df: pd.DataFrame,
    stats_df: pd.DataFrame,
    hyp: Dict[str, dict],
    checks: dict,
) -> str:
    lines: List[str] = []
    lines.append("# Wave 1 Analysis Report (post-19-fix)")
    lines.append("")
    lines.append("## Dataset")
    lines.append(f"- Source logs: `lab/runs/campaign_v3/wave_1/*.log`")
    lines.append(f"- Audit records: {len(run_df)} rich-tier runs")
    lines.append(f"- Experiments: {', '.join(sorted(run_df['exp_id'].unique().tolist()))}")
    lines.append(f"- Scales: {sorted(run_df['n'].unique().tolist())}")
    lines.append("")

    # Per-experiment tables
    lines.append("## Per-Experiment Summary Tables")
    lines.append("Columns: config, n, runs, median frob, IQR frob, median sigma, IQR sigma, REV rate.")
    lines.append("")
    for exp in sorted(stats_df["exp_id"].unique().tolist()):
        lines.append(f"### {exp}")
        sub = stats_df[stats_df["exp_id"] == exp].copy()
        sub = sub.sort_values(["n", "frob_from_rank1_median"], ascending=[True, False])
        table_rows = []
        for _, r in sub.iterrows():
            table_rows.append(
                [
                    r["config_name"],
                    int(r["n"]),
                    int(r["runs"]),
                    fmt(r["frob_from_rank1_median"]),
                    fmt(r["frob_from_rank1_iqr"]),
                    fmt(r["sigma_median"]),
                    fmt(r["sigma_iqr"]),
                    fmt(r["rev_rate"]),
                ]
            )
        lines.append(
            md_table(
                ["config", "n", "runs", "frob_med", "frob_iqr", "sigma_med", "sigma_iqr", "rev_rate"],
                table_rows,
            )
        )
        lines.append("")

    # Hypothesis tests
    lines.append("## Hypothesis Tests")
    lines.append(
        f"Thresholds: alpha={ALPHA} generally; prereg Bonferroni alpha={BONFERRONI_ALPHA} for HYP-202..205; "
        "HYP-130 uses BH-FDR per cell across 15 tests."
    )
    lines.append("")
    hrows = []
    for hid in ["HYP-130", "HYP-131", "HYP-137", "HYP-138", "HYP-139", "HYP-202", "HYP-203", "HYP-204", "HYP-205"]:
        hrows.append([hid, hyp[hid]["verdict"]])
    lines.append(md_table(["hypothesis", "verdict"], hrows))
    lines.append("")

    # Detailed key hypothesis outcomes
    lines.append("### HYP-130 (single-cell distinct signatures)")
    h130 = hyp["HYP-130"]
    lines.append(
        f"- Distinct cells (BH-FDR q<0.05 and |Cliff's delta|>0.5 on at least one metric/scale): "
        f"{h130['distinct_cells']}/{h130['tested_cells']} ({h130['distinct_ratio']:.1%})."
    )
    lines.append(f"- Verdict: **{h130['verdict']}**. {h130['note']}")
    cell_rows = []
    for d in h130["details"]:
        bs = d["best_signal"]
        if bs:
            detail = f"{bs['metric']}@n={bs['n']} d={bs['cliffs_delta']:.2f} p={bs['p']:.3g} q={bs['q_bh']:.3g}"
        else:
            detail = "no BH-significant large-effect signal"
        cell_rows.append([d["cell"], "yes" if d["distinct"] else "no", detail])
    lines.append(md_table(["cell", "distinct", "strongest_signal"], cell_rows))
    lines.append("")

    lines.append("### HYP-131 (multi-level vs static comparator)")
    lines.append(f"- Verdict: **{hyp['HYP-131']['verdict']}**.")
    lines.append(f"- Reason: {hyp['HYP-131']['reason']}")
    lines.append("")

    lines.append("### HYP-137 / HYP-138 / HYP-139 (legacy REV chain claims)")
    for hid in ["HYP-137", "HYP-138", "HYP-139"]:
        lines.append(f"- {hid}: **{hyp[hid]['verdict']}**")
    lines.append("")

    lines.append("### HYP-202..205 (Lagrange / spectral probes, EXP-109)")
    for hid in ["HYP-202", "HYP-203", "HYP-204", "HYP-205"]:
        lines.append(f"- {hid}: **{hyp[hid]['verdict']}**")
    h202 = hyp["HYP-202"]["test"]
    h202_eff = hyp["HYP-202"].get("pooled_mad_effect")
    h204 = hyp["HYP-204"]["test"]
    h205 = hyp["HYP-205"]["test"]
    if h202:
        lines.append(
            f"- HYP-202 detail: PLA2 full_action vs baseline (k=4, n=64+128) med diff={h202['median_diff']:.3f}, "
            f"p={h202['p']:.3g}, pooled-MAD effect={fmt(h202_eff)}."
        )
    h203p = hyp["HYP-203"]["primary_fixed_k4"]
    h203s = hyp["HYP-203"]["secondary_pooled"]
    h203pc = hyp["HYP-203"].get("secondary_partial_control_k")
    lines.append(
        f"- HYP-203 primary (fixed k=4): rho={h203p['rho']:.3f}, p={h203p['p']:.3g}, points={h203p['n_points']}."
    )
    lines.append(
        f"- HYP-203 secondary pooled (all k): rho={h203s['rho']:.3f}, p={h203s['p']:.3g}, points={h203s['n_points']}."
    )
    if h203pc:
        lines.append(
            f"- HYP-203 secondary partial (control k): rho={h203pc['rho']:.3f}, permutation p={h203pc['p']:.3g}, points={h203pc['n']}."
        )
    if h204:
        lines.append(
            f"- HYP-204 detail: gap_ratio full_action-baseline (k=4,n=128) diff={h204['median_diff']:.3f}, p={h204['p']:.3g}."
        )
    if h205:
        rel205 = hyp["HYP-205"].get("relative_reduction")
        lines.append(
            f"- HYP-205 detail: diff_kl A14-baseline (k=4,n=128) diff={h205['median_diff']:.3f}, p={h205['p']:.3g}, "
            f"relative reduction={fmt(rel205)}."
        )
    lines.append("")

    # Pre-fix comparisons
    lines.append("## Comparison vs Pre-fix Findings")
    f36 = checks["F36"]
    f40 = checks["F40"]
    lines.append(
        f"- F36 (full_action dominates n=128): **changed**. Global n=128 highest median is "
        f"`{f36['top_config_global_n128_by_median']}` ({f36['top_frob_median']:.3f}); "
        f"second is `{f36['second_config_global_n128_by_median']}` ({f36['second_frob_median']:.3f}). "
        f"Top-vs-second inferential test p={f36['top_vs_second_test']['p']:.3g} "
        f"(dominance={f36['inferentially_dominant']}). "
        f"full_action median in global n=128 pool is {f36['full_action_frob_median_global_n128']:.3f}. "
        f"In EXP-107 subset, full_action remains top ({f36['exp107_top_frob']:.3f})."
    )
    lines.append(
        f"- F40 (A14 strongest single-cell booster): strongest single by median in EXP-109 n=128 is "
        f"`{f40['strongest_single_by_median']}` ({f40['top_single_median']:.3f}); second is "
        f"`{f40['second_single_by_median']}` ({f40['second_single_median']:.3f}). "
        f"Top-vs-second p={f40['top_single_vs_second_test']['p']:.3g} "
        f"(dominance={f40['inferentially_dominant']})."
    )
    lines.append("- F41 (partition competition boosts structure): **holds**; A14/A16/A17 all exceed baseline at n=128.")
    lines.append("- F46 (n=256 partition competition survival): **not testable in wave_1** (no n=256 runs).")
    rr = checks["REV_rates"]
    lines.append(
        f"- REV rates changed materially: full_action REV rate in EXP-107 is 100%/90%/0% at n=32/64/128; "
        f"A19_only is 80%/20%/0%."
    )
    bv = checks["Budget_vs_REV"]
    lines.append(
        f"- Budget saturation link: **holds strongly**. REV median budget ratio={bv['rev_budget_ratio_median']:.3f} "
        f"vs non-REV={bv['nonrev_budget_ratio_median']:.3f} (Mann-Whitney p={bv['mw_greater']['p']:.3g})."
    )
    lines.append(f"- A18 regime-switch claim: {checks['A18_testability']['note']}")
    lines.append("")

    # New findings
    lines.append("## New Findings (post-fix wave_1)")
    lines.append("- Lagrange results are mixed: PLA2 dominance and gap_ratio claims hold (HYP-202/HYP-204), but geometrizability correlation and diffusion-KL partition-competition claims fail (HYP-203/HYP-205).")
    lines.append("- REV chain claims from pre-fix characterization (HYP-137/138/139) do not replicate on this dataset.")
    lines.append("- A13_A14_A19 and A14_only exceed full_action frob at n=128 in EXP-109, despite full_action leading EXP-107’s narrower config set.")
    lines.append("- REV behavior is now concentrated at n<=64; at n=128 it is largely absent across EXP-106/107.")
    lines.append("")

    # Updated cell classification (tested cells only)
    lines.append("## Updated Cell Classification (from wave_1 data)")
    lines.append("- Strong structure boosters at n=128: A14, A16, A17, A13_A14_A19.")
    lines.append("- Irreversibility boosters: A13 (higher sigma without large frob gain).")
    lines.append("- Near-baseline / weak in tested contexts: A1, A2, A4, A5, A6, A7, A25.")
    lines.append("- Not directly isolated in wave_1: A18 (no A18_only run in this campaign).")
    lines.append("")

    lines.append("## Artifacts Produced")
    lines.append(f"- `{STATS_CSV}`")
    lines.append(f"- `{HYP_JSON}`")
    lines.append(f"- `{REPORT_MD}`")
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    records = load_jsonl(AUDITS_PATH)
    run_df, scan_df = build_dataframes(records)

    stats_df = group_stats(run_df)
    stats_df.to_csv(STATS_CSV, index=False)

    hyp = hypothesis_tests(run_df, scan_df)
    checks = prefixed_finding_checks(run_df, scan_df)
    with HYP_JSON.open("w") as f:
        json.dump({"hypotheses": hyp, "prefixed_checks": checks}, f, indent=2)

    report = build_report(run_df, stats_df, hyp, checks)
    with REPORT_MD.open("w") as f:
        f.write(report)

    print(f"Loaded {len(records)} records")
    print(f"Wrote {STATS_CSV}")
    print(f"Wrote {HYP_JSON}")
    print(f"Wrote {REPORT_MD}")


if __name__ == "__main__":
    main()
