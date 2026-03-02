# Campaign v3 — Data & Artifact Guide

## Overview

Campaign v3 is the first clean dataset produced after 19 bug fixes to the dynamics engine.
All prior campaign data was deleted. This is the only valid dataset for the current binary.

Binary git SHA: `d93b01a` (post-19-fix, step count = n × 2000).

## Directory Structure

```
lab/runs/campaign_v3/
├── wave_1/                    # n=32, 64, 128 — COMPLETE (1470 jobs)
│   ├── manifest.json          # Job definitions
│   ├── EXP-*.log              # Raw experiment logs (1470 files)
│   ├── audits.jsonl           # Parsed KEY_AUDIT_JSON records from all logs
│   ├── report.md              # Auto-generated per-config statistics
│   ├── analysis_report.md     # Full analysis with hypothesis tests & findings
│   ├── stats_by_exp_config_scale.csv  # Tabular summary stats
│   └── hypothesis_tests.json  # Detailed test results (p-values, effect sizes)
│
├── wave_2/                    # n=256 — RUNNING (~640 jobs, ~2 days)
│   ├── manifest.json          # 490 original + 150 selective EXP-112 ablations
│   └── wave_2/*.log           # Logs written here on completion
│
└── wave_3/                    # EXP-112 ablations, n=32, 64, 128 — COMPLETE (2070 jobs)
    ├── manifest.json          # Job definitions
    └── wave_3/*.log           # Raw experiment logs (2070 files)
```

## Experiments

| Experiment | What it tests | Configs | Scales |
|-----------|---------------|---------|--------|
| EXP-F1 | Empty baseline (PicaConfig::none(), no PICA) | 1 | 32,64,128,256 |
| EXP-100 | Single-cell toggles (A1-A13 individually) | 13 | 32,64,128,256 |
| EXP-101 | Multi-level ladder dynamics | 7 | 32,64,128,256 |
| EXP-106 | Phase transition combos (boost params, 3-cell combos) | 13 | 32,64,128,256 |
| EXP-107 | Scale dependence (main characterization sweep) | 13 | 32,64,128,256 |
| EXP-109 | Lagrange baseline survey | 13 (internal) | 32,64,128,256 |
| EXP-110 | Lagrange scale dependence | 3 (internal) | 32,64,128,256 |
| EXP-112 | Systematic PICA ablation survey (69 configs, 9 groups) | 69 | 32,64,128 (+15 at 256) |

All experiments use 10 seeds (0-9). Scales 32-128 complete; 256 running in wave_2.

## EXP-112 Ablation Groups (69 configs)

| Group | Count | Purpose |
|-------|-------|---------|
| 0: Controls | 3 | empty, baseline, full_action |
| 1: Single-cell retests | 6 | A14, A16, A17, A18, A20, A22 (cells missing from EXP-100) |
| 2: Row ablations | 6 | All cells in one actor-row (P1-P6) |
| 3: Column ablations | 6 | All cells sharing one informant |
| 4: Leave-one-out | 22 | full_action minus one cell |
| 5: Synergy pairs | 8 | Untested 2-cell combinations |
| 6: Row-pair interactions | 4 | Two rows enabled together |
| 7: Diagnostic removal | 8 | full_action minus entire row/column |
| 8: Generating sets | 6 | Candidate minimal subsets for high-frob regime |

Design notes: P5-consuming cells (A5, A11, A20) dropped from configs without P5 producers
to avoid fallback impurity. A22 added as co-producer where needed.

## Log Format

Each `.log` file contains:
- `KEY_100_CFG ...` — config label and PICA hash
- `KEY_100_TAU ...` — tau source (spectral or pat:N)
- `KEY_100_MACRO ...` — headline macro kernel metrics
- `KEY_100_DIAG ...` — B1-B12 diagnostic cells
- `KEY_AUDIT_JSON {...}` — rich-tier audit record (machine-parseable JSON)

The KEY_AUDIT_JSON record is the primary data source. Schema version 3. Key fields:
- `frob_from_rank1`: Frobenius distance from rank-1 (higher = more structure)
- `macro_gap`: spectral gap of macro kernel
- `sigma`: path-reversal asymmetry (0 = reversible)
- `sigma_u`, `sigma_ratio`: uniform-start sigma and micro→macro transfer ratio
- `tau`, `active_tau`: observation timescale (spectral fallback vs PICA-produced)
- `budget`: P6 budget at end of run
- `multi_scale_scan`: array of {k, frob, gap, sigma_pi, sigma_u} across k=2,4,8,16,...
- `pica_config`: full PICA configuration (enabled matrix, lens_selector, all params)

## Analysis Pipeline

```bash
# 1. Collect audit records from logs
python3 analysis/collect_audits.py --input-dir lab/runs/campaign_v3/wave_1 --output audits.jsonl

# 2. Generate statistics report
python3 analysis/report_stage.py --input audits.jsonl --output report.md --stage "Wave 1"

# 3. Merge multiple audit files
python3 analysis/collect_audits.py --merge wave_1/audits.jsonl wave_3/audits.jsonl --output merged.jsonl
```

## Ledger Files

All ledger files are append-only JSONL. Schema: `lab/ledger/SCHEMA.md`.

| File | Contents | ID range |
|------|----------|----------|
| `lab/ledger/hypotheses.jsonl` | Hypothesis claims and status updates | HYP-100..210+ |
| `lab/ledger/experiments.jsonl` | Experiment definitions | EXP-073..112+ |
| `lab/ledger/closures.jsonl` | Verified findings | CLO-001..105+ |
| `lab/ledger/results.jsonl` | Run-level result summaries | RES-* |

## Key Findings from Wave 1 Analysis

Full analysis: `wave_1/analysis_report.md`

1. **A14_only (1.305) and A13_A14_A19 (1.333) outperform full_action (1.159) at n=128.**
   Activating all 25 cells generates structural interference; a 3-cell combo wins.

2. **REV (reversibility) is now an n≤64 phenomenon only.** 0% REV at n=128 for all configs
   (was 50% pre-fix for full_action). The 19 bug fixes eliminated the n=128 REV regime.

3. **Partition competition (A14, A16, A17) confirmed as primary structure mechanism.**
   Alternative lenses independently boost frob via partition selection at all scales.

4. **EXP-F1 (no PICA) gives frob=0.206 at n=128** — even baseline PICA (A10+A15) provides
   substantial structure (0.988), and the best configs reach 1.3+.

5. **HYP-137/138/139 refuted** (legacy REV chain claims don't hold post-fix).
   HYP-202/204 supported (PLA2 dominance, gap_ratio). HYP-203/205 refuted.

## PICA Architecture Reference

Full documentation: `lab/pica_cell_characterization.md` (pre-fix, 46 findings).
Source code: `crates/dynamics/src/pica/` (12 modules).
Master plan: `lab/reengineering_notebook.md`.

Cell naming: A{N} where N=1-25. Matrix position: `enabled[actor][informant]`.
Actor = which primitive is modulated (P1-P6, rows 0-5).
Informant = which primitive provides the signal (P1-P6, columns 0-5).

Presets: `PicaConfig::none()` (all off), `::baseline()` (A10+A15),
`::full_action()` (all 25 except A16), `::full_all()` (all 25).

## Codebase

- `crates/dynamics/` — dynamics engine + PICA (the simulation)
- `crates/runner/src/main.rs` — experiment definitions (run_exp_100..112)
- `crates/six_primitives_core/` — substrate, primitives, helpers
- `analysis/` — Python analysis scripts
- `theory/` — primitive specifications
