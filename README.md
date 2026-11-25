# HR Workforce Analytics: Warehouse and KPI Dashboard

A portfolio project that builds a trusted source of truth for headcount and turnover, from raw HRIS-style exports to a Tableau dashboard, using SQL, Snowflake, and a documented data model.

> **Status:** Phases 0-2 complete (brief, data profiling, synthetic enrichment). Phases 3-9 (Snowflake load, modeling, data quality, KPIs, Tableau, documentation) are in progress.

## The business problem

HR, Finance, and Recruiting each report a different headcount for the same month-end, and turnover swings by several points depending on who calculates it. Leaders stop trusting HR data. This project builds one reconciled data layer with documented KPI definitions, so every number can be traced back to a source record.

Full brief: [`docs/00_project_brief.pdf`](docs/00_project_brief.pdf)

## Roadmap

| Phase | What | Status |
|---|---|---|
| 0 | Scope and business brief | Done |
| 1 | Data profiling of the source dataset | Done |
| 2 | Synthetic enrichment (dates, managers, history, planted errors) | Done |
| 3 | Load to Snowflake (RAW and STAGING layers) | Next |
| 4 | Dimensional model (star schema, SCD Type 2) | Planned |
| 5 | Data quality checks and headcount reconciliation | Planned |
| 6 | KPI layer (SQL views) | Planned |
| 7 | Tableau dashboard | Planned |
| 8 | KPI dictionary, SOP, and RACI | Planned |
| 9 | Stakeholder readout | Planned |

## Architecture

```
Kaggle HR snapshot -> Python enrichment -> HRIS-style CSV feeds -> Snowflake RAW -> STAGING -> MART -> Tableau
   (Phase 1-2)           (Phase 2)              (data/synthetic)            (Phase 3)        (Phase 4-6)  (Phase 7)
```

## What has been built so far

- **Data profiling (Phase 1):** 1,480 source rows profiled across 38 columns. Found 10 duplicate rows (7 exact copies, 3 with conflicting values), 57 missing manager-tenure values, 1 label typo, and 8 constant, redundant, or noise columns. Every finding and decision is logged in the [profiling report](docs/data_profiling_report.xlsx).
- **Synthetic enrichment (Phase 2):** The source file is a single snapshot with no dates. A reproducible script rebuilds a 24-month history (hire and termination dates, job history, a manager hierarchy, 4 locations, 12 org units) for 1,470 employees and writes six HRIS-style feeds. Six data errors are planted deliberately, with a separate answer key, so the Phase 5 checks have something real to catch. Parameters, rules, and limitations: [assumptions log](docs/phase2_assumptions_log.md).

## Repository structure

```
data/
  raw/          Source file from Kaggle (not committed, see below)
  processed/    De-duplicated seed (generated, not committed)
  synthetic/    HRIS-style feeds produced in Phase 2
  answer_key/   Locations of the planted errors (test key for Phase 5)
docs/           Brief, profiling report, assumptions log
src/            Python scripts, numbered by phase
```

## How to reproduce

Requires Python 3.9+. Tested with pandas 2.2 and 3.0.

```bash
pip install -r requirements.txt

# 1. Download the source file (see Data source) and save it as data/raw/HR_Analytics_Kaggle.csv

# 2. Phase 1: profile the data and write the de-duplicated seed
python src/01_profile_hr_data.py --export-clean

# 3. Phase 2: build the synthetic HRIS feeds (seed 42; same seed gives identical files)
python src/02_enrich_synthetic_hris.py
```

Run all commands from the repository root.

## Data source

Source dataset: HR_Analytics, link: https://www.kaggle.com/datasets/anshika2301/hr-analytics-dataset. The raw file is not redistributed here; download it from the link above. All employee-level history (dates, managers, locations, terminations) is **synthetic**. No real employee data is used.

## Privacy by design

Sensitive attributes (age, gender, marital status, education) are restricted to representation reporting, with a minimum group size and no individual-level drill-down. Decisions are logged in the Decisions sheet of the profiling report.

## Tech stack

Python (pandas, numpy, openpyxl) - Snowflake (SQL, ELT) - Tableau - Excel

## Author

Cristina Montenegro - [LinkedIn](https://www.linkedin.com/in/cristinamf)
