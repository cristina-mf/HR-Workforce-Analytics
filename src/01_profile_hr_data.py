#!/usr/bin/env python3
"""
01_profile_hr_data.py
=====================
Phase 1 (Data Profiling) of the HR Workforce Analytics project.

PURPOSE
    Read the raw Kaggle HR file and produce a Data Profiling Report (Excel) that
    documents what is in the data, what is wrong with it, and what we decided to do.
    The raw file is NEVER modified. Cleaning of the source happens later, in
    Snowflake STAGING (SQL). The only optional output besides the report is a
    de-duplicated copy of the seed data, which the Phase 2 enrichment step reads.

INPUT
    data/raw/HR_Analytics_Kaggle.csv     (1 row per employee, business key = EmpID)

OUTPUT
    docs/data_profiling_report.xlsx      (always)
    data/processed/hr_seed_deduplicated.csv   (only with --export-clean)

USAGE
    python src/01_profile_hr_data.py
    python src/01_profile_hr_data.py --export-clean
    python src/01_profile_hr_data.py --input path/to/file.csv --report docs/report.xlsx

DUPLICATE-RESOLUTION RULES (decided with the project owner)
    1. Exact duplicate rows ............ drop the copies, keep the first occurrence.
    2. Same EmpID, conflicting values .. keep the record with the non-null value.
       If that rule cannot decide (both values present), fall back to:
         a) keep the record that passes the logic checks, then
         b) keep the first occurrence in the file.
       Fallback b) is flagged in the DQ log as needing human confirmation.

REQUIREMENTS
    Python 3.9+, pandas, numpy, openpyxl
"""
from __future__ import annotations

import argparse
import logging
import re
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
BUSINESS_KEY = "EmpID"          # decision D-01
ALT_KEY = "EmployeeNumber"
NOISE_CORR_THRESHOLD = 0.05     # |correlation with MonthlyIncome| below this = noise
LOW_CARDINALITY_MAX = 12        # columns with <= this many distinct values get a value profile
MIN_GROUP_SIZE = 5              # proposed minimum reporting group size (see Decisions sheet)

# Column triage: column -> (decision, rationale). Decisions are counted in the Summary sheet.
CORE, RESTRICTED, STAGE, DROP = "Keep - core", "Keep - restricted", "Stage only", "Drop"
TRIAGE = {
    "EmpID": (CORE, "Business key for dim_employee (decision D-01)."),
    "EmployeeNumber": (CORE, "Alternate ID kept for traceability to the source; unique after de-duplication."),
    "Department": (CORE, "Org dimension; needed for headcount and turnover cuts."),
    "JobRole": (CORE, "Job dimension; maps to a single department except 'Manager'."),
    "JobLevel": (CORE, "Job dimension; income rises consistently with level."),
    "BusinessTravel": (CORE, "Employment attribute; label typo to fix in STAGING."),
    "Attrition": (CORE, "Seed for the synthetic termination records (Phase 2)."),
    "MonthlyIncome": (CORE, "Compensation measure; used to build bands in SQL."),
    "PercentSalaryHike": (CORE, "Compensation movement input."),
    "StockOptionLevel": (CORE, "Compensation attribute."),
    "NumCompaniesWorked": (CORE, "Prior-experience attribute."),
    "TotalWorkingYears": (CORE, "Career length; upper bound for tenure checks."),
    "YearsAtCompany": (CORE, "Input to derive hire_date in Phase 2."),
    "YearsInCurrentRole": (CORE, "Input to derive job-history start dates in Phase 2."),
    "YearsSinceLastPromotion": (CORE, "Input to derive promotion movements in Phase 2."),
    "YearsWithCurrManager": (CORE, "Input to derive manager assignment in Phase 2; has nulls."),
    "Age": (RESTRICTED, "Demographic. Age bands rebuilt in SQL; reporting uses minimum group sizes."),
    "Gender": (RESTRICTED, "Demographic. Representation reporting only, minimum group sizes."),
    "MaritalStatus": (RESTRICTED, "Demographic. Not needed for core KPIs; restrict access."),
    "Education": (RESTRICTED, "Demographic. Representation reporting only."),
    "EducationField": (RESTRICTED, "Demographic. Representation reporting only."),
    "DistanceFromHome": (STAGE, "Not needed for headcount/turnover KPIs; reserved for a later attrition-drivers project."),
    "EnvironmentSatisfaction": (STAGE, "Survey-style score; outside current KPI scope."),
    "JobInvolvement": (STAGE, "Survey-style score; outside current KPI scope."),
    "JobSatisfaction": (STAGE, "Survey-style score; outside current KPI scope."),
    "OverTime": (STAGE, "Outside current KPI scope; useful for attrition drivers later."),
    "PerformanceRating": (STAGE, "Only values 3 and 4 present; outside current KPI scope."),
    "RelationshipSatisfaction": (STAGE, "Survey-style score; outside current KPI scope."),
    "TrainingTimesLastYear": (STAGE, "Outside current KPI scope."),
    "WorkLifeBalance": (STAGE, "Survey-style score; outside current KPI scope."),
    "AgeGroup": (DROP, "Redundant: fully determined by Age. Rebuild the bands in SQL so the logic is visible."),
    "SalarySlab": (DROP, "Redundant: fully determined by MonthlyIncome. Rebuild in SQL."),
    "EmployeeCount": (DROP, "Constant (always 1). No information."),
    "StandardHours": (DROP, "Constant (always 80). No information."),
    "Over18": (DROP, "Constant (always Y). No information."),
    "DailyRate": (DROP, "Noise: no relationship with MonthlyIncome. Would confuse stakeholders."),
    "HourlyRate": (DROP, "Noise: no relationship with MonthlyIncome."),
    "MonthlyRate": (DROP, "Noise: no relationship with MonthlyIncome."),
}
DECISION_ORDER = [CORE, RESTRICTED, STAGE, DROP]

log = logging.getLogger("profile_hr")

# --------------------------------------------------------------------------- #
# Profiling functions
# --------------------------------------------------------------------------- #
def load_data(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    log.info("Loaded %s: %d rows x %d columns", path.name, *df.shape)
    return df


def friendly_type(s: pd.Series) -> str:
    if pd.api.types.is_integer_dtype(s):
        return "integer"
    if pd.api.types.is_float_dtype(s):
        return "decimal"
    return "text"


def column_inventory(df: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for col in df.columns:
        s = df[col]
        numeric = pd.api.types.is_numeric_dtype(s)
        decision, why = TRIAGE.get(col, ("Review", "Column not in triage table - decide manually."))
        rows.append({
            "Column": col,
            "Type": friendly_type(s),
            "Nulls": int(s.isna().sum()),
            "Distinct": int(s.nunique(dropna=True)),
            "Min": s.min() if numeric else None,
            "Max": s.max() if numeric else None,
            "Example": s.dropna().iloc[0] if s.notna().any() else None,
            "Decision": decision,
            "Rationale": why,
        })
    return pd.DataFrame(rows)


def logic_violations(d: pd.DataFrame) -> dict:
    """Row-level logic checks. Returns {check name: boolean Series (True = violation)}."""
    return {
        "YearsInCurrentRole > YearsAtCompany": d["YearsInCurrentRole"] > d["YearsAtCompany"],
        "YearsSinceLastPromotion > YearsAtCompany": d["YearsSinceLastPromotion"] > d["YearsAtCompany"],
        "YearsWithCurrManager > YearsAtCompany": d["YearsWithCurrManager"] > d["YearsAtCompany"],
        "YearsAtCompany > TotalWorkingYears": d["YearsAtCompany"] > d["TotalWorkingYears"],
        "TotalWorkingYears > Age - 15 (started working before 15)": d["TotalWorkingYears"] > (d["Age"] - 15),
    }


def resolve_duplicates(df: pd.DataFrame):
    """Apply the duplicate rules. Returns (deduplicated_df, per-row resolution log)."""
    data_cols = list(df.columns)
    work = df.copy()
    work["_row"] = np.arange(len(work)) + 2  # CSV line number (header is line 1)
    work["_nulls"] = work[data_cols].isna().sum(axis=1)
    work["_logic_fail"] = (work["YearsWithCurrManager"] > work["YearsAtCompany"]).astype(int)

    keep_idx, records = [], []
    for key, grp in work.groupby(BUSINESS_KEY, sort=False):
        if len(grp) == 1:
            keep_idx.append(grp.index[0])
            continue
        distinct = grp.drop_duplicates(subset=data_cols, keep="first")
        if len(distinct) == 1:
            winner, gtype, rule = grp.index[0], "Exact copy", "Exact copy: keep first occurrence"
            diff_cols = []
        else:
            diff_cols = [c for c in data_cols if distinct[c].nunique(dropna=False) > 1]
            ranked = distinct.sort_values(["_nulls", "_logic_fail", "_row"])
            winner, gtype = ranked.index[0], "Conflicting"
            if distinct["_nulls"].nunique() > 1:
                rule = "Kept the record with the non-null value"
            elif distinct["_logic_fail"].nunique() > 1:
                rule = "Fallback: kept the record that passes logic checks"
            else:
                rule = "Fallback: tie-break, kept first occurrence (CONFIRM)"
        keep_idx.append(winner)
        for idx, r in grp.iterrows():
            records.append({
                "Source row": int(r["_row"]),
                BUSINESS_KEY: key,
                "Group type": gtype,
                "Action": "Keep" if idx == winner else "Drop",
                "Rule applied": rule,
                "Differing field(s)": ", ".join(diff_cols),
                "Value(s)": "; ".join(f"{c}={r[c]}" for c in diff_cols),
            })
    clean = df.loc[sorted(keep_idx)].reset_index(drop=True)
    res = pd.DataFrame(records)
    return clean, res


def find_label_variants(df: pd.DataFrame) -> list:
    """Text columns where labels differ only by case/punctuation (e.g. Travel_Rarely vs TravelRarely)."""
    out = []
    for col in df.columns:
        if pd.api.types.is_numeric_dtype(df[col]):
            continue
        counts = df[col].value_counts()
        norm = {v: re.sub(r"[^a-z0-9]", "", str(v).lower()) for v in counts.index}
        for key in set(norm.values()):
            variants = [v for v in counts.index if norm[v] == key]
            if len(variants) > 1:
                minority = [v for v in variants if v != variants[0]]  # value_counts is sorted desc
                out.append((col, variants, int(counts[minority].sum())))
    return out


def value_profile(df: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for col in df.columns:
        if col == BUSINESS_KEY or df[col].nunique() > LOW_CARDINALITY_MAX:
            continue
        for val, n in df[col].value_counts(dropna=False).sort_index().items():
            rows.append({"Column": col, "Value": "(null)" if pd.isna(val) else str(val), "Rows": int(n)})
    return pd.DataFrame(rows)


def numeric_profile(df: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for col in df.select_dtypes("number").columns:
        s = df[col].dropna()
        q1, med, q3 = s.quantile([0.25, 0.5, 0.75])
        iqr = q3 - q1
        outliers = int(((s < q1 - 1.5 * iqr) | (s > q3 + 1.5 * iqr)).sum())
        rows.append({"Column": col, "Count": int(s.count()), "Mean": round(float(s.mean()), 2),
                     "Std dev": round(float(s.std()), 2), "Min": float(s.min()), "Q1": float(q1),
                     "Median": float(med), "Q3": float(q3), "Max": float(s.max()),
                     "IQR outliers": outliers})
    return pd.DataFrame(rows)


def build_dq_log(raw: pd.DataFrame, clean: pd.DataFrame, dup_log: pd.DataFrame) -> pd.DataFrame:
    """One row per check. Status: Resolved / Planned / Open / Pass / Info."""
    R = []  # (ID, Area, Issue, Check performed, Affected, Unit, Severity, Handling, Status)

    # --- Duplicates
    exact_drop = int(((dup_log["Group type"] == "Exact copy") & (dup_log["Action"] == "Drop")).sum()) if len(dup_log) else 0
    conf_drop = int(((dup_log["Group type"] == "Conflicting") & (dup_log["Action"] == "Drop")).sum()) if len(dup_log) else 0
    tie = int(dup_log[(dup_log["Rule applied"].str.contains("CONFIRM")) & (dup_log["Action"] == "Keep")].shape[0]) if len(dup_log) else 0
    R.append(("DQ-01", "Rows", "Exact duplicate rows (same EmpID, identical values)",
              f"Group by {BUSINESS_KEY}; compare all columns", exact_drop, "rows", "High",
              "Drop copies, keep first occurrence (decision D-02).", "Resolved"))
    R.append(("DQ-02", "Rows", "Duplicate EmpID with conflicting values",
              f"Group by {BUSINESS_KEY}; find differing columns", conf_drop, "rows", "High",
              "Keep the non-null value (D-03). Rule not decisive when both values are present: "
              "fallback to logic check, then first occurrence.", "Resolved"))
    R.append(("DQ-03", "Rows", "Conflicts decided by tie-break (first occurrence), not by evidence",
              "Count conflict groups where neither null nor logic check separated the records", tie, "rows", "Medium",
              "Accepted first-occurrence tie-break (decision D-03, confirmed by project owner). Both values were "
              "non-null and passed logic checks, so no evidence favored either. Impact low: the field only feeds the "
              "synthetic manager assignment (Phase 2), not any KPI.", "Resolved" if tie else "Pass"))

    # --- Nulls
    for col in raw.columns[raw.isna().any()]:
        n = int(raw[col].isna().sum())
        R.append((f"DQ-{len(R)+1:02d}", "Column", f"Missing values in {col}",
                  "Null count per column", n, "rows", "Medium",
                  "Left as NULL, not imputed (decision D-04, confirmed by project owner). Imputing from "
                  "YearsInCurrentRole matches exactly in only 45% of complete records (67% within 1 year), which would add "
                  "false precision. No in-scope KPI uses this field; Phase 2 assigns managers independently of it.",
                  "Resolved"))

    # --- Label variants
    for col, variants, n in find_label_variants(raw):
        R.append((f"DQ-{len(R)+1:02d}", "Column", f"Inconsistent labels in {col}: {', '.join(map(str, variants))}",
                  "Normalize labels (case/punctuation) and compare", n, "rows", "Low",
                  "Standardize in STAGING (SQL). Raw data is not overwritten (D-05).", "Planned"))

    # --- Logic checks (raw vs cleaned)
    raw_v, clean_v = logic_violations(raw), logic_violations(clean)
    for name, mask in raw_v.items():
        n_raw, n_clean = int(mask.sum()), int(clean_v[name].sum())
        if n_raw == 0:
            status, handling = "Pass", "No violations."
        elif n_clean == 0:
            status, handling = "Resolved", "Violating record was the duplicate dropped by rule D-03."
        else:
            status, handling = "Open", "Cap at tenure or flag; decide in Phase 2."
        R.append((f"DQ-{len(R)+1:02d}", "Rows", f"Logic: {name}", "Row-level comparison", n_raw, "rows",
                  "Medium" if n_raw else "None", handling, status))

    # --- Income monotonic by job level
    med = raw.groupby("JobLevel")["MonthlyIncome"].median().sort_index()
    out_of_order = int((med.diff().dropna() <= 0).sum())
    R.append((f"DQ-{len(R)+1:02d}", "Column", "Median MonthlyIncome should rise with JobLevel",
              "Median income per level, check increasing", out_of_order, "levels",
              "Medium" if out_of_order else "None", "No action." if not out_of_order else "Investigate.",
              "Pass" if not out_of_order else "Open"))

    # --- Constant / redundant / noise columns
    const = [c for c in raw.columns if raw[c].nunique(dropna=False) == 1]
    R.append((f"DQ-{len(R)+1:02d}", "Column", f"Constant columns: {', '.join(const)}",
              "Distinct values = 1", len(const), "columns", "Low",
              "Drop from MART; kept in RAW for traceability (D-07).", "Planned"))
    age_ok = raw.groupby("Age")["AgeGroup"].nunique().max() == 1
    s = raw.groupby("SalarySlab")["MonthlyIncome"].agg(["min", "max"]).sort_values("min")
    slab_ok = bool((s["max"].values[:-1] < s["min"].values[1:]).all())
    red = [c for c, ok in (("AgeGroup", age_ok), ("SalarySlab", slab_ok)) if ok]
    R.append((f"DQ-{len(R)+1:02d}", "Column", f"Redundant derived columns: {', '.join(red)}",
              "Each Age maps to one AgeGroup; salary slabs do not overlap", len(red), "columns", "Low",
              "Drop; rebuild bands in SQL (D-07).", "Planned"))
    corr = raw[["DailyRate", "HourlyRate", "MonthlyRate", "MonthlyIncome"]].corr()["MonthlyIncome"].drop("MonthlyIncome")
    noise = [c for c, v in corr.items() if abs(v) < NOISE_CORR_THRESHOLD]
    R.append((f"DQ-{len(R)+1:02d}", "Column", f"Noise columns (|corr| with MonthlyIncome < {NOISE_CORR_THRESHOLD}): {', '.join(noise)}",
              "Pearson correlation: " + ", ".join(f"{k}={v:.3f}" for k, v in corr.items()),
              len(noise), "columns", "Low", "Drop; they do not behave like pay rates (D-07).", "Planned"))

    # --- Keys
    R.append((f"DQ-{len(R)+1:02d}", "Column", f"{BUSINESS_KEY} is unique and non-null after cleaning",
              "Count nulls and duplicates in the cleaned data",
              int(clean[BUSINESS_KEY].isna().sum() + clean[BUSINESS_KEY].duplicated().sum()), "rows", "High",
              "No action." , "Pass" if not (clean[BUSINESS_KEY].isna().any() or clean[BUSINESS_KEY].duplicated().any()) else "Open"))
    R.append((f"DQ-{len(R)+1:02d}", "Column", f"{BUSINESS_KEY} and {ALT_KEY} are two different identifiers",
              "Compare numeric part of EmpID with EmployeeNumber", int((clean[BUSINESS_KEY].str.extract(r"(\d+)")[0].astype(int) != clean[ALT_KEY]).sum()),
              "rows", "Low", f"Use {BUSINESS_KEY} as key (D-01); keep {ALT_KEY} as an alternate ID.", "Info"))

    return pd.DataFrame(R, columns=["ID", "Area", "Issue", "Check performed", "Affected", "Unit",
                                    "Severity", "Handling", "Status"])


DECISIONS = [
    ("D-01", "Business key", f"Use {BUSINESS_KEY}.", "Project owner"),
    ("D-02", "Exact duplicate rows", "Drop the copies, keep the first occurrence.", "Project owner"),
    ("D-03", "Duplicate EmpID with conflicting values",
     "Keep the non-null value. Where both values are present the rule cannot decide, so the fallback is: "
     "record that passes logic checks, then first occurrence. The first-occurrence tie-break (RM1466, RM1467) "
     "was confirmed by the project owner.", "Project owner"),
    ("D-04", "Missing YearsWithCurrManager",
     "Leave as NULL and flag in the DQ log; do not impute. No in-scope KPI uses this field.", "Project owner"),
    ("D-05", "Label typos / standardization", "Fix in STAGING (SQL), never in RAW. The raw file is read-only.", "Analyst default"),
    ("D-06", "Sensitive attributes (age, gender, marital status, education)",
     f"Representation reporting only, with a minimum group size of {MIN_GROUP_SIZE} (proposed) and no drill to individuals.", "Analyst default"),
    ("D-07", "Constant, redundant and noise columns", "Excluded from the MART; retained in RAW for traceability.", "Analyst default"),
]

# --------------------------------------------------------------------------- #
# Excel writing
# --------------------------------------------------------------------------- #
FONT = "Arial"
HEAD_FILL = PatternFill("solid", fgColor="1F3864")
SECTION_FILL = PatternFill("solid", fgColor="D9E2F3")
THIN = Side(style="thin", color="BFBFBF")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)


def py(v):
    """Convert numpy/pandas scalars to plain Python so openpyxl can write them."""
    if v is None or (not isinstance(v, str) and pd.isna(v)):
        return None
    if isinstance(v, np.integer):
        return int(v)
    if isinstance(v, np.floating):
        return float(v)
    return v


def write_table(ws, df, start_row, wrap_cols=(), widths=None):
    """Write a DataFrame as a formatted table. Returns (first_data_row, last_data_row)."""
    for j, col in enumerate(df.columns, 1):
        c = ws.cell(row=start_row, column=j, value=col)
        c.font, c.fill, c.border = Font(name=FONT, bold=True, color="FFFFFF", size=10), HEAD_FILL, BORDER
        c.alignment = Alignment(wrap_text=True, vertical="center")
    for i, row in enumerate(df.itertuples(index=False), start_row + 1):
        for j, v in enumerate(row, 1):
            c = ws.cell(row=i, column=j, value=py(v))
            c.font, c.border = Font(name=FONT, size=10), BORDER
            c.alignment = Alignment(wrap_text=df.columns[j - 1] in wrap_cols, vertical="top")
    last = start_row + max(len(df), 1)
    ws.freeze_panes = ws.cell(row=start_row + 1, column=1)
    ws.auto_filter.ref = f"A{start_row}:{get_column_letter(len(df.columns))}{last}"
    for j, w in enumerate(widths or [], 1):
        ws.column_dimensions[get_column_letter(j)].width = w
    return start_row + 1, last


def title(ws, text, note=None):
    ws["A1"] = text
    ws["A1"].font = Font(name=FONT, bold=True, size=14, color="1F3864")
    if note:
        ws["A2"] = note
        ws["A2"].font = Font(name=FONT, italic=True, size=9, color="595959")


def build_workbook(raw, clean, dup_log, inventory, dq, vals, nums, source_name):
    wb = Workbook()
    ws_sum = wb.active
    ws_sum.title = "Summary"
    ws_inv = wb.create_sheet("Column_Inventory")
    ws_dq = wb.create_sheet("DQ_Log")
    ws_dup = wb.create_sheet("Duplicates")
    ws_val = wb.create_sheet("Value_Profile")
    ws_num = wb.create_sheet("Numeric_Profile")
    ws_dec = wb.create_sheet("Decisions")

    RAW_ROWS = "Summary!$B$6"

    # Column inventory (null % is a formula against the raw row count on Summary)
    title(ws_inv, "Column inventory and triage",
          "Counts and ranges computed by the script from the raw CSV. Null % and the Summary roll-ups are Excel formulas.")
    inv = inventory.copy()
    inv.insert(3, "Null %", None)
    first, last_inv = write_table(ws_inv, inv, 4, wrap_cols=("Rationale",),
                                  widths=[26, 10, 8, 9, 10, 10, 10, 24, 18, 70])
    for r in range(first, last_inv + 1):
        ws_inv.cell(row=r, column=4, value=f"=C{r}/{RAW_ROWS}").number_format = "0.0%"
        ws_inv.cell(row=r, column=4).font = Font(name=FONT, size=10)
        ws_inv.cell(row=r, column=4).border = BORDER

    # DQ log
    title(ws_dq, "Data quality log",
          "Status: Resolved = handled by this script | Planned = to be done in Snowflake STAGING | Open = decision needed | Pass | Info")
    f, last_dq = write_table(ws_dq, dq, 4, wrap_cols=("Issue", "Check performed", "Handling"),
                             widths=[9, 9, 52, 44, 10, 9, 10, 62, 11])

    # Duplicates
    title(ws_dup, "Duplicate EmpID resolution", "Every row that shares an EmpID with another row, and what happened to it.")
    if len(dup_log):
        f, last_dup = write_table(ws_dup, dup_log, 4, wrap_cols=("Rule applied", "Value(s)"),
                                  widths=[11, 10, 13, 9, 48, 24, 28])
    else:
        ws_dup["A4"], last_dup = "No duplicate keys found.", 5
    # Value profile
    title(ws_val, "Value profile (low-cardinality columns)", "% of rows is a formula against the raw row count.")
    v = vals.copy()
    v["% of rows"] = None
    f, last_val = write_table(ws_val, v, 4, widths=[28, 30, 9, 11])
    for r in range(f, last_val + 1):
        ws_val.cell(row=r, column=4, value=f"=C{r}/{RAW_ROWS}").number_format = "0.0%"
        ws_val.cell(row=r, column=4).font = Font(name=FONT, size=10)
        ws_val.cell(row=r, column=4).border = BORDER
    # Numeric profile
    title(ws_num, "Numeric profile", "Computed by the script from the raw CSV (nulls excluded). Outliers = outside 1.5 x IQR.")
    write_table(ws_num, nums, 4, widths=[28, 8, 10, 10, 10, 10, 10, 10, 10, 13])
    # Decisions
    title(ws_dec, "Decisions and assumptions", f"Logged {date.today().isoformat()}")
    dec = pd.DataFrame(DECISIONS, columns=["ID", "Topic", "Decision", "Decided by"])
    write_table(ws_dec, dec, 4, wrap_cols=("Topic", "Decision"), widths=[8, 40, 90, 28])

    # ---------------- Summary ----------------
    title(ws_sum, "Data Profiling Report - HR Workforce Analytics (Phase 1)",
          f"Source: {source_name} | Generated {date.today().isoformat()} by 01_profile_hr_data.py")
    ws_sum.column_dimensions["A"].width = 46
    ws_sum.column_dimensions["B"].width = 18
    ws_sum.column_dimensions["C"].width = 70

    def section(row, text):
        for c in "ABC":
            ws_sum[f"{c}{row}"].fill = SECTION_FILL
        ws_sum[f"A{row}"] = text
        ws_sum[f"A{row}"].font = Font(name=FONT, bold=True, size=11)

    def line(row, label, value, note="", fmt=None, is_input=False):
        ws_sum[f"A{row}"], ws_sum[f"B{row}"], ws_sum[f"C{row}"] = label, value, note
        ws_sum[f"A{row}"].font = Font(name=FONT, size=10)
        ws_sum[f"B{row}"].font = Font(name=FONT, size=10, color="0000FF" if is_input else "000000")
        ws_sum[f"C{row}"].font = Font(name=FONT, size=9, italic=True, color="595959")
        ws_sum[f"B{row}"].alignment = Alignment(horizontal="right")
        if fmt:
            ws_sum[f"B{row}"].number_format = fmt

    attr = (clean["Attrition"] == "Yes").mean()
    dup_rng = f"Duplicates!$D$5:$D${max(last_dup, 5)}"
    section(4, "1. Dataset overview")
    line(5, "Source file", source_name, "", None, True)
    line(6, "Rows in raw file", len(raw), "Input: counted by the script (blue = value written by script).", "#,##0", True)
    line(7, "Columns in raw file", f"=COUNTA(Column_Inventory!$A$5:$A${last_inv})", "", "#,##0")
    line(8, "Duplicate rows dropped", f'=COUNTIF({dup_rng},"Drop")', "See Duplicates sheet.", "#,##0")
    line(9, "Rows after de-duplication", "=B6-B8", "Equals the number of unique employees (EmpID).", "#,##0")
    line(10, "Attrition rate after de-duplication", float(attr), "Input: computed by the script.", "0.0%", True)

    section(12, "2. Column triage")
    r = 13
    for d in DECISION_ORDER:
        line(r, d, f'=COUNTIF(Column_Inventory!$I$5:$I${last_inv},"{d}")', "", "#,##0")
        r += 1
    line(r, "Total columns triaged", f"=SUM(B13:B{r-1})", "", "#,##0")
    line(r + 1, "Reconciliation: triaged = raw columns", f'=IF(B{r}=B7,"OK","CHECK")', "")
    tri_end = r + 1

    s = tri_end + 2
    section(s, "3. Data quality log")
    rng = f"DQ_Log!$I$5:$I${last_dq}"
    line(s + 1, "Checks performed", f"=COUNTA(DQ_Log!$A$5:$A${last_dq})", "", "#,##0")
    for k, status in enumerate(["Pass", "Resolved", "Planned", "Open", "Info"], 2):
        line(s + k, f"  {status}", f'=COUNTIF({rng},"{status}")', "", "#,##0")
    line(s + 7, "Reconciliation: statuses = checks performed", f'=IF(SUM(B{s+2}:B{s+6})=B{s+1},"OK","CHECK")', "")
    s2 = s + 9
    section(s2, "4. Read this first")
    notes = [
        "The raw file has no dates: it is a single snapshot. Hire/termination dates and history are synthesized in Phase 2.",
        "Rule D-03 (keep the non-null value) could not decide the 3 conflicting EmpIDs because no value was null. "
        "A documented fallback was applied and confirmed; see Duplicates and DQ_Log.",
        ("No open items remain: decisions D-03 and D-04 were confirmed by the project owner (see Decisions)."
         if not (dq["Status"] == "Open").any()
         else "Open items need a decision before Phase 2 starts (see DQ_Log, Status = Open, and Decisions)."),
        f"Sensitive attributes (age, gender, marital status, education) are restricted: min group size {MIN_GROUP_SIZE}, no individual drill-down.",
    ]
    for i, t in enumerate(notes, s2 + 1):
        ws_sum[f"A{i}"] = "- " + t
        ws_sum[f"A{i}"].font = Font(name=FONT, size=10)
        ws_sum[f"A{i}"].alignment = Alignment(wrap_text=True, vertical="top")
        ws_sum.merge_cells(f"A{i}:C{i}")
        ws_sum.row_dimensions[i].height = 30
    return wb


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description="Profile the raw HR dataset and write the Data Profiling Report.")
    ap.add_argument("--input", type=Path, default=Path("data/raw/HR_Analytics_Kaggle.csv"))
    ap.add_argument("--report", type=Path, default=Path("docs/data_profiling_report.xlsx"))
    ap.add_argument("--clean-out", type=Path, default=Path("data/processed/hr_seed_deduplicated.csv"))
    ap.add_argument("--export-clean", action="store_true", help="also write the de-duplicated seed CSV")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    raw = load_data(args.input)
    clean, dup_log = resolve_duplicates(raw)
    log.info("De-duplicated: %d -> %d rows", len(raw), len(clean))

    wb = build_workbook(raw, clean, dup_log, column_inventory(raw), build_dq_log(raw, clean, dup_log),
                        value_profile(raw), numeric_profile(raw), args.input.name)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    wb.save(args.report)
    log.info("Report written: %s", args.report)

    if args.export_clean:
        args.clean_out.parent.mkdir(parents=True, exist_ok=True)
        clean.to_csv(args.clean_out, index=False)
        log.info("De-duplicated seed written: %s (%d rows)", args.clean_out, len(clean))


if __name__ == "__main__":
    main()
