#!/usr/bin/env python3
"""
02_enrich_synthetic_hris.py
===========================
Phase 2 (Synthetic Enrichment) of the HR Workforce Analytics project.

PURPOSE
    The Kaggle file is a single snapshot (one row per employee, no dates). A warehouse
    needs history. This script uses what the snapshot knows (years at company, years in
    role, years since last promotion, attrition flag) to RECONSTRUCT a plausible history,
    and adds the tables a real HRIS would have. Output = raw "source system" CSV feeds
    that Phase 3 loads into Snowflake.

INPUT
    data/processed/hr_seed_deduplicated.csv     (written by 01_profile_hr_data.py --export-clean)

OUTPUT
    data/synthetic/hris_employees.csv           1 row per employee (current state)
    data/synthetic/hris_job_history.csv         hires, promotions, transfers (effective-dated)
    data/synthetic/hris_terminations.csv        1 row per termination
    data/synthetic/hris_locations.csv           4 US sites
    data/synthetic/hris_org_units.csv           department x location
    data/synthetic/hris_employee_extra.csv      "stage only" columns (survey / work attributes)
    data/answer_key/planted_errors.csv          where the deliberate errors are (do not peek)
    docs/phase2_assumptions_log.md              parameters, rules, validation, limitations

USAGE
    python src/02_enrich_synthetic_hris.py
    python src/02_enrich_synthetic_hris.py --seed 7

REPRODUCIBILITY
    Every random choice comes from one seeded generator (default seed 42). Same seed + same
    input = identical output files.

DESIGN NOTES
    * Dates are derived from the seed's tenure fields, not invented from nothing.
    * The planted errors are injected AFTER validation, into the output feeds only.
      The seed file is never touched.
    * Manager history is not modeled: only the current manager is stored (a known limitation).
"""
from __future__ import annotations

import argparse
import logging
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

# --------------------------------------------------------------------------- #
# Parameters (agreed in the Phase 2 plan)
# --------------------------------------------------------------------------- #
SEED = 42
AS_OF = pd.Timestamp("2025-12-31")
WINDOW_START = pd.Timestamp("2024-01-01")
VOLUNTARY_SHARE = 0.80
LEADER_ROLES = {"Manager", "Research Director", "Manufacturing Director"}
FIRST_LINE_RATIO = 12            # about 1 first-line manager per 12 people in an org unit (leavers included)
MIN_HIRE_AGE_DAYS = int(18 * 365.25)   # the seed says everyone is over 18 (Over18 = Y)
LEADER_PRIORITY = {"Manager": 130, "Research Director": 120, "Manufacturing Director": 110}
EMPLOYEE_TYPES = {"Full-time": 0.90, "Part-time": 0.07, "Contractor": 0.03}

# (id, city, state, region, short code, share of employees)
LOCATIONS = [
    ("L01", "New York", "NY", "Northeast", "NYC", 0.35),
    ("L02", "Chicago", "IL", "Midwest", "CHI", 0.25),
    ("L03", "Austin", "TX", "South", "AUS", 0.22),
    ("L04", "Atlanta", "GA", "South", "ATL", 0.18),
]
DEPT_CODES = {"Research & Development": "RD", "Sales": "SL", "Human Resources": "HR"}

log = logging.getLogger("enrich_hris")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def days(n) -> pd.Series:
    return pd.to_timedelta(n, unit="D")


def weekday_series(s: pd.Series, hi=None, prefer: str = "forward") -> pd.Series:
    """Move Saturday/Sunday dates to a weekday. 'forward' = next Monday (falls back to the
    previous Friday if that would pass `hi`); 'back' = previous Friday."""
    dow = s.dt.dayofweek
    weekend = dow >= 5
    fwd = s + days((7 - dow).where(weekend, 0).fillna(0).astype(int))
    bwd = s - days((dow - 4).where(weekend, 0).fillna(0).astype(int))
    out = bwd if prefer == "back" else fwd
    if prefer == "forward" and hi is not None:
        out = out.where(~(out > hi), bwd)
    return out.where(weekend, s)


def weekday_ts(ts: pd.Timestamp, prefer: str = "back") -> pd.Timestamp:
    dow = ts.dayofweek
    if dow < 5:
        return ts
    return ts - pd.Timedelta(days=dow - 4) if prefer == "back" else ts + pd.Timedelta(days=7 - dow)


def weighted_pick(rng, options: dict) -> str:
    keys = list(options)
    w = np.array([options[k] for k in keys], dtype=float)
    return str(rng.choice(keys, p=w / w.sum()))


# --------------------------------------------------------------------------- #
# Step 1-2: dates, demographics, location
# --------------------------------------------------------------------------- #
def build_core(seed: pd.DataFrame, rng) -> pd.DataFrame:
    df = seed.reset_index(drop=True).copy()
    n = len(df)
    leaver = df["Attrition"].eq("Yes").to_numpy()

    # Termination dates: uniform over the 24-month window (leavers only)
    window_days = (AS_OF - WINDOW_START).days + 1
    term = pd.Series(pd.NaT, index=df.index, dtype="datetime64[ns]")
    term[leaver] = WINDOW_START + days(rng.integers(0, window_days, size=int(leaver.sum())))
    term = weekday_series(term, prefer="back")             # exits happen on weekdays
    df["term_date"] = term
    ref = term.where(leaver, AS_OF)                       # reference date for tenure
    df["ref_date"] = ref

    # Hire date = reference date - tenure (whole years from the seed + random days)
    tenure_days = np.maximum(df["YearsAtCompany"].to_numpy() * 365 + rng.integers(0, 365, size=n), 1)
    hire_raw = ref - days(tenure_days)
    df["hire_date"] = weekday_series(hire_raw, hi=ref - pd.Timedelta(days=1), prefer="forward")

    # Birth date from Age at the REFERENCE date (exit date for leavers, as-of date for stayers)
    age_days = np.round(df["Age"].to_numpy() * 365.25).astype(int) + rng.integers(0, 365, size=n)
    birth = ref - days(age_days)
    too_young = ((df["hire_date"] - birth).dt.days < MIN_HIRE_AGE_DAYS).to_numpy()
    if too_young.any():
        fix = df["hire_date"] - days(MIN_HIRE_AGE_DAYS + rng.integers(0, 365, size=n))
        birth = birth.where(~too_young, fix)
        log.warning("%d employees would have been hired before age 18; birth_date moved earlier "
                    "(their age at the reference date drifts above the seed Age)", int(too_young.sum()))
    df["birth_date"] = birth
    df["_age_fixed"] = too_young

    # Location and org unit
    df["location_id"] = rng.choice([l[0] for l in LOCATIONS], size=n, p=[l[5] for l in LOCATIONS])
    short = {l[0]: l[4] for l in LOCATIONS}
    codes = {d: DEPT_CODES.get(d, d[:2].upper()) for d in df["Department"].unique()}
    df["org_unit_id"] = df["Department"].map(codes) + "-" + df["location_id"].map(short)
    df["_leaver"] = leaver
    return df


# --------------------------------------------------------------------------- #
# Step 3: manager hierarchy
# --------------------------------------------------------------------------- #
def build_hierarchy(df: pd.DataFrame, rng):
    """Returns (manager position array with -1 for none, set of positions that manage someone)."""
    n = len(df)
    leaver = df["_leaver"].to_numpy()
    hire = list(df["hire_date"])
    term = list(df["term_date"])
    lvl = df["JobLevel"].to_numpy()
    score = (df["JobRole"].map(LEADER_PRIORITY).fillna(0).to_numpy() + lvl * 10
             + df["YearsAtCompany"].clip(upper=9).to_numpy() + rng.random(n))
    mgr = np.full(n, -1)
    unit_top = {}
    relaxed = 0

    for unit, idx in df.groupby("org_unit_id").groups.items():
        idx = np.array(list(idx))
        stay = idx[~leaver[idx]]
        stay = stay[np.argsort(-score[stay])]
        k1 = max(1, int(round(len(idx) / FIRST_LINE_RATIO)))
        k2 = int(round(k1 / 5)) if k1 > 5 else 0
        if len(stay) < 1 + k2 + k1:
            k2 = 0
            k1 = max(0, min(k1, len(stay) - 1))
        top, sec, first = stay[0], stay[1:1 + k2], stay[1 + k2:1 + k2 + k1]
        leaders = {int(top), *map(int, sec), *map(int, first)}
        unit_top[unit] = int(top)

        for i in sec:
            mgr[i] = top
        for j, i in enumerate(first):
            mgr[i] = sec[j % len(sec)] if len(sec) else top

        pool = [int(m) for m in first] or [int(top)]
        fallback = pool + [int(m) for m in sec] + [int(top)]
        counts = {m: 0 for m in fallback}
        people = [int(i) for i in idx if int(i) not in leaders]
        rng.shuffle(people)
        for i in people:
            end = term[i] if leaver[i] else AS_OF
            elig = [m for m in pool if hire[m] < end] or [m for m in fallback if hire[m] < end]
            if not elig:
                elig, relaxed = [min(fallback, key=lambda m: hire[m])], relaxed + 1
            senior = [m for m in elig if lvl[m] >= lvl[i]]      # prefer a manager at the same or higher level
            elig = senior or elig
            w = np.array([1 / (1 + counts[m]) for m in elig])
            m = int(rng.choice(elig, p=w / w.sum()))
            mgr[i] = m
            counts[m] += 1

    # Department heads: the most senior unit head per department reports to nobody
    for dept, sub in df.groupby("Department").groups.items():
        units = [u for u in unit_top if df.loc[unit_top[u], "Department"] == dept]
        head = max((unit_top[u] for u in units), key=lambda i: score[i])
        mgr[head] = -1
        for u in units:
            if unit_top[u] != head:
                mgr[unit_top[u]] = head
    if relaxed:
        log.warning("%d employees assigned to a manager hired after they left (no eligible manager)", relaxed)
    return mgr, set(int(m) for m in mgr[mgr >= 0])


# --------------------------------------------------------------------------- #
# Step 4: employees, terminations, job history
# --------------------------------------------------------------------------- #
def build_employees(df: pd.DataFrame, mgr: np.ndarray, managers: set, rng) -> pd.DataFrame:
    n = len(df)
    ids = df["EmpID"].to_numpy()
    emp_type = rng.choice(list(EMPLOYEE_TYPES), size=n, p=list(EMPLOYEE_TYPES.values()))
    for m in managers:
        emp_type[m] = "Full-time"                    # people managers are always full-time
    has_mgr = mgr >= 0
    mgr_hire = pd.Series(df["hire_date"].to_numpy()[np.where(has_mgr, mgr, 0)]).where(has_mgr)

    start = df["ref_date"] - days(df["YearsWithCurrManager"] * 365 + rng.integers(0, 365, size=n))
    latest = pd.concat([start, df["hire_date"], mgr_hire], axis=1).max(axis=1)
    mgr_start = latest.where(start.notna() & pd.Series(has_mgr))   # nulls stay NULL (decision D-04)
    mgr_start = weekday_series(mgr_start, hi=df["ref_date"], prefer="forward")

    emp = pd.DataFrame({
        "employee_id": ids,
        "employee_number": df["EmployeeNumber"],
        "hire_date": df["hire_date"],
        "birth_date": df["birth_date"],
        "gender": df["Gender"],
        "marital_status": df["MaritalStatus"],
        "education": df["Education"],
        "education_field": df["EducationField"],
        "department": df["Department"],
        "job_role": df["JobRole"],
        "job_level": df["JobLevel"],
        "monthly_income": df["MonthlyIncome"],
        "business_travel": df["BusinessTravel"],          # raw label kept (typo fixed later in STAGING)
        "num_companies_worked": df["NumCompaniesWorked"],
        "total_working_years": df["TotalWorkingYears"],
        "percent_salary_hike": df["PercentSalaryHike"],
        "stock_option_level": df["StockOptionLevel"],
        "employee_type": emp_type,
        "employment_status": np.where(df["_leaver"], "Terminated", "Active"),
        "location_id": df["location_id"],
        "org_unit_id": df["org_unit_id"],
        "manager_id": np.where(has_mgr, ids[np.where(has_mgr, mgr, 0)], None),
        "manager_start_date": mgr_start,
    })
    return emp


def build_terminations(df: pd.DataFrame, emp: pd.DataFrame, rng) -> pd.DataFrame:
    rows = []
    for i in np.flatnonzero(df["_leaver"].to_numpy()):
        r = df.iloc[i]
        age_at_term = (r["term_date"] - r["birth_date"]).days / 365.25
        if rng.random() < VOLUNTARY_SHARE:
            ttype = "Voluntary"
            opts = {"Resignation - better opportunity": .40, "Resignation - compensation": .20,
                    "Resignation - career growth": .20, "Relocation / personal": .08}
            if r["OverTime"] == "Yes" or r["WorkLifeBalance"] <= 2:
                opts["Resignation - work-life balance"] = .12
            if age_at_term >= 55:
                opts["Retirement"] = .30
        else:
            ttype = "Involuntary"
            if emp.at[i, "employee_type"] == "Contractor":
                opts = {"End of contract": 1.0}
            else:
                opts = {"Dismissal - performance": .45, "Dismissal - conduct": .20, "Layoff / restructuring": .35}
        rows.append({"employee_id": r["EmpID"], "termination_date": r["term_date"],
                     "termination_type": ttype, "termination_reason": weighted_pick(rng, opts)})
    return pd.DataFrame(rows)


def build_job_history(df: pd.DataFrame, rng) -> pd.DataFrame:
    """Hire + promotion + transfer records, rebuilt backwards from each employee's current state."""
    roles_by_dept = {d: sorted(set(g["JobRole"]) - LEADER_ROLES) for d, g in df.groupby("Department")}
    rows = []
    for _, r in df.iterrows():
        hire, ref = r["hire_date"], r["ref_date"]
        lo = hire + pd.Timedelta(days=30)
        events = []                                          # (date, type, prior_role)

        def clip(d):
            if lo > ref:
                return None
            d = min(max(d, lo), ref)
            w = weekday_ts(d, "forward")
            return w if w <= ref else weekday_ts(d, "back")

        promo = None
        if r["YearsSinceLastPromotion"] < r["YearsAtCompany"] and r["JobLevel"] > 1:
            promo = clip(ref - days(int(r["YearsSinceLastPromotion"]) * 365 + int(rng.integers(0, 365))))
            if promo is not None:
                events.append((promo, "Promotion", None))
        if r["YearsInCurrentRole"] < r["YearsAtCompany"]:
            choices = [x for x in roles_by_dept[r["Department"]] if x != r["JobRole"]]
            d = clip(ref - days(int(r["YearsInCurrentRole"]) * 365 + int(rng.integers(0, 365))))
            if choices and d is not None and (promo is None or abs((d - promo).days) >= 30):
                events.append((d, "Transfer", str(rng.choice(choices))))
        events.sort(key=lambda e: e[0])

        state = {"role": r["JobRole"], "level": int(r["JobLevel"]), "income": int(r["MonthlyIncome"])}
        after = []                                           # state after each event, newest first
        for d, kind, prior in reversed(events):
            after.append((d, kind, dict(state)))
            if kind == "Promotion":
                state["level"] -= 1
                state["income"] = int(round(state["income"] / (1 + r["PercentSalaryHike"] / 100) / 10) * 10)
            else:
                state["role"] = prior
        rows.append((r["EmpID"], hire, "Hire", r["Department"], state))
        for d, kind, s in reversed(after):
            rows.append((r["EmpID"], d, kind, r["Department"], s))

    return pd.DataFrame([{"employee_id": e, "effective_date": d, "change_type": k, "department": dep,
                          "job_role": s["role"], "job_level": s["level"], "monthly_income": s["income"]}
                         for e, d, k, dep, s in rows])


def build_reference_tables(emp: pd.DataFrame):
    loc = pd.DataFrame([{"location_id": l[0], "city": l[1], "state": l[2], "region": l[3]} for l in LOCATIONS])
    short = {l[0]: l[4] for l in LOCATIONS}
    ou = (emp[["org_unit_id", "department", "location_id"]].drop_duplicates().sort_values("org_unit_id"))
    ou["org_unit_name"] = ou["department"] + " - " + ou["location_id"].map(
        {l[0]: l[1] for l in LOCATIONS})
    return loc, ou[["org_unit_id", "org_unit_name", "department", "location_id"]].reset_index(drop=True)


def build_extra(df: pd.DataFrame) -> pd.DataFrame:
    """The 'stage only' columns from Phase 1, as a separate source-system export."""
    return pd.DataFrame({
        "employee_id": df["EmpID"], "overtime": df["OverTime"], "distance_from_home": df["DistanceFromHome"],
        "performance_rating": df["PerformanceRating"], "environment_satisfaction": df["EnvironmentSatisfaction"],
        "job_involvement": df["JobInvolvement"], "job_satisfaction": df["JobSatisfaction"],
        "relationship_satisfaction": df["RelationshipSatisfaction"],
        "training_times_last_year": df["TrainingTimesLastYear"], "work_life_balance": df["WorkLifeBalance"]})


# --------------------------------------------------------------------------- #
# Step 5: validation (before errors are planted)
# --------------------------------------------------------------------------- #
def validate(emp, term, jh) -> dict:
    v = {}
    hire, tdate = emp["hire_date"], emp.merge(term, on="employee_id", how="left")["termination_date"]
    ends = pd.date_range(WINDOW_START - pd.Timedelta(days=1), periods=25, freq=pd.offsets.MonthEnd())
    head = [int((hire <= e).sum() - (tdate <= e).sum()) for e in ends]
    ok = True
    for i in range(1, len(ends)):
        h = int(((hire > ends[i - 1]) & (hire <= ends[i])).sum())
        t = int(((tdate > ends[i - 1]) & (tdate <= ends[i])).sum())
        ok &= head[i] == head[i - 1] + h - t
    n_hires = int(((hire >= WINDOW_START) & (hire <= AS_OF)).sum())
    n_terms = int(len(term))
    active = emp[emp["employment_status"] == "Active"]
    spans = active.groupby("manager_id").size()
    v.update({
        "employees": len(emp), "terminations": n_terms, "hires_in_window": n_hires,
        "opening_headcount": head[0], "closing_headcount": head[-1],
        "headcount_reconciles": bool(ok),
        "annualized_turnover": n_terms / 2 / np.mean(head[1:]),
        "voluntary_share": float((term["termination_type"] == "Voluntary").mean()),
        "term_after_hire": bool((emp.merge(term, on="employee_id")
                                 .eval("termination_date > hire_date")).all()),
        "all_managers_exist": bool(emp["manager_id"].dropna().isin(emp["employee_id"]).all()),
        "span_median": float(spans.median()), "span_p90": float(spans.quantile(.9)), "span_max": int(spans.max()),
        "dept_heads": int(emp["manager_id"].isna().sum()),
        "job_history_rows": len(jh), "promotions": int((jh["change_type"] == "Promotion").sum()),
        "transfers": int((jh["change_type"] == "Transfer").sum()),
        "latest_history_matches_employee": bool(
            jh.sort_values("effective_date").groupby("employee_id").tail(1)
            .merge(emp, on="employee_id", suffixes=("_h", "_e"))
            .eval("job_role_h == job_role_e and job_level_h == job_level_e and monthly_income_h == monthly_income_e").all()),
    })
    age_h = (emp["hire_date"] - emp["birth_date"]).dt.days / 365.25
    v["hired_under_18"] = int((age_h < 18).sum())
    v["hires_on_weekend"] = int((emp["hire_date"].dt.dayofweek >= 5).sum())
    v["terminations_on_weekend"] = int((term["termination_date"].dt.dayofweek >= 5).sum())
    lv = emp.set_index("employee_id")["job_level"]
    rr = emp.dropna(subset=["manager_id"])
    v["reports_with_lower_level_manager"] = int((lv.reindex(rr["manager_id"]).to_numpy() < rr["job_level"].to_numpy()).sum())
    v["people_managers"] = int(emp["manager_id"].nunique())
    for k, val in v.items():
        log.info("validation | %-34s %s", k, round(val, 3) if isinstance(val, float) else val)
    return v


# --------------------------------------------------------------------------- #
# Step 6: planted errors
# --------------------------------------------------------------------------- #
def plant_errors(emp, term, jh, rng):
    emp, term, jh = emp.copy(), term.copy(), jh.copy()
    key = []
    leavers = term["employee_id"].tolist()
    managers = set(emp["manager_id"].dropna())
    active_ics = emp[(emp["employment_status"] == "Active") & (~emp["employee_id"].isin(managers))]["employee_id"].tolist()
    multi = jh.groupby("employee_id").size()
    with_promo = jh[jh["change_type"] == "Promotion"]["employee_id"].tolist()
    recent_hires = emp[(emp["employment_status"] == "Active") & (emp["hire_date"] > AS_OF - pd.Timedelta(days=365))
                       & (~emp["employee_id"].isin(managers))]["employee_id"].tolist()
    used = set()

    def pick(pool):
        c = [x for x in pool if x not in used]
        e = str(rng.choice(c))
        used.add(e)
        return e

    # E1 duplicate employee record
    e = pick(active_ics)
    emp = pd.concat([emp, emp[emp["employee_id"] == e]], ignore_index=True)
    key.append(("E1", "hris_employees", e, "employee_id", "1 row", "2 identical rows",
                "Duplicate employee record", "Uniqueness of employee_id"))
    # E2 termination before hire
    e = pick(leavers)
    h = emp.loc[emp["employee_id"] == e, "hire_date"].iloc[0]
    orig = term.loc[term["employee_id"] == e, "termination_date"].iloc[0]
    new = weekday_ts(h - pd.Timedelta(days=30), "back")
    term.loc[term["employee_id"] == e, "termination_date"] = new
    key.append(("E2", "hris_terminations", e, "termination_date", orig.date().isoformat(), new.date().isoformat(),
                "Termination date 30 days before hire date", "Date logic: termination_date > hire_date"))
    # E3 orphan manager
    e = pick(active_ics)
    orig = emp.loc[emp["employee_id"] == e, "manager_id"].iloc[0]
    emp.loc[emp["employee_id"] == e, "manager_id"] = "RM9999"
    key.append(("E3", "hris_employees", e, "manager_id", str(orig), "RM9999",
                "Manager ID that does not exist", "Referential integrity: manager_id in employees"))
    # E4 overlapping job assignments (two records, same employee, same effective date)
    e = pick(with_promo)
    orig_row = jh[(jh["employee_id"] == e) & (jh["change_type"] == "Promotion")].iloc[0]
    orig_level = int(orig_row["job_level"])
    new_level = orig_level + 1 if orig_level < 5 else orig_level - 1
    dup = orig_row.copy()
    dup["job_level"] = new_level
    jh = pd.concat([jh, dup.to_frame().T], ignore_index=True)
    key.append(("E4", "hris_job_history", e, "job_level", str(orig_level), str(new_level),
                "Two conflicting job records with the same effective date",
                "Overlapping assignments: one record per employee per effective_date"))
    # E5 active employee with a termination record
    e = pick(active_ics)
    d = weekday_ts(WINDOW_START + pd.Timedelta(days=int(rng.integers(0, (AS_OF - WINDOW_START).days))), "back")
    term = pd.concat([term, pd.DataFrame([{"employee_id": e, "termination_date": d,
                                           "termination_type": "Voluntary",
                                           "termination_reason": "Resignation - better opportunity"}])], ignore_index=True)
    key.append(("E5", "hris_terminations", e, "employee_id", "no termination record", d.date().isoformat(),
                "Employee is Active but has a termination record", "Status consistency: employment_status vs terminations"))
    # E6 future-dated hire
    e = pick(recent_hires)
    orig = emp.loc[emp["employee_id"] == e, "hire_date"].iloc[0]
    new = AS_OF + pd.Timedelta(days=75)          # Monday 2026-03-16
    emp.loc[emp["employee_id"] == e, "hire_date"] = new
    key.append(("E6", "hris_employees", e, "hire_date", orig.date().isoformat(), new.date().isoformat(),
                "Hire date after the as-of date (future-dated hire)", "Date logic: hire_date <= as-of date"))
    answer = pd.DataFrame(key, columns=["error_id", "table", "record_key", "field", "original_value",
                                        "injected_value", "description", "expected_dq_check"])
    return emp, term, jh, answer


# --------------------------------------------------------------------------- #
# Documentation
# --------------------------------------------------------------------------- #
def write_assumptions(path: Path, v: dict, seed: int, n_fixed_age: int, max_drift: float):
    L = [
        "# Phase 2: Synthetic Enrichment - Assumptions Log", "",
        f"Generated {date.today().isoformat()} by `02_enrich_synthetic_hris.py` (random seed {seed}).", "",
        "## Parameters", "",
        "| Parameter | Value |", "|---|---|",
        f"| As-of date | {AS_OF.date()} |",
        f"| Window | {WINDOW_START.date()} to {AS_OF.date()} (24 months) |",
        f"| Voluntary share of terminations | {VOLUNTARY_SHARE:.0%} (target) |",
        "| Locations | 4 US sites (New York, Chicago, Austin, Atlanta) |",
        f"| First-line span of control | about 1 first-line manager per {FIRST_LINE_RATIO} people in each org unit (leavers included), roughly 8-9 active direct reports |",
        f"| Employee types | {', '.join(f'{k} {p:.0%}' for k, p in EMPLOYEE_TYPES.items())} |",
        "| Planted errors | 6 (see answer key, kept separate) |", "",
        "## Derivation rules", "",
        "- **Termination date:** employees flagged Attrition = Yes get a date drawn uniformly across the window, then moved to the previous weekday if it fell on a weekend.",
        "- **Hire date:** reference date (termination date for leavers, as-of date for stayers) minus `YearsAtCompany` years plus 0-364 random days, then moved to the next weekday (tenure changes by at most 2 days).",
        "- **Birth date:** reference date minus `Age` years plus 0-364 random days, so a leaver's age matches the seed at the time they left"
        + (f". For {n_fixed_age} employees the seed implies a hire before age 18 (the seed says everyone is over 18), so birth date was moved earlier; their age at the reference date can be up to {max_drift:.1f} years above the seed Age." if n_fixed_age else "."),
        "- **Weekdays:** hire, termination, promotion, transfer and manager-start dates all fall on weekdays.",
        "- **Promotion:** created when `YearsSinceLastPromotion` < `YearsAtCompany` and level > 1. Prior income = current / (1 + `PercentSalaryHike`).",
        "- **Transfer:** created when `YearsInCurrentRole` < `YearsAtCompany`. The prior role is synthetic (another non-leadership role in the same department).",
        "- **Hierarchy:** one hierarchy per department x location org unit, built from seniority (role priority Manager > Research Director > Manufacturing Director, then job level, then tenure). Only active employees can be managers, and a report is matched to a manager at the same or higher job level where one exists. "
        "A leaver is assigned a manager who had been hired before the leaver's termination date. The most senior unit head in each department has no manager.",
        "- **Manager start date:** from `YearsWithCurrManager`; stays NULL where the seed value is missing (decision D-04).",
        "- **Termination type/reason:** seeded random. Retirement only at age 55+. Work-life-balance resignations only where OverTime = Yes or WorkLifeBalance <= 2. Contractors who leave involuntarily are 'End of contract'.",
        "- **Employee type:** drawn at random; managers are always Full-time. Headcount rules for Contractors are a Phase 6 KPI definition.", "",
        "## Validation (before errors were planted)", "",
        "| Check | Result |", "|---|---|",
    ]
    for k, val in v.items():
        L.append(f"| {k.replace('_', ' ')} | {round(val, 3) if isinstance(val, float) else val} |")
    L += ["", "## Known limitations", "",
          "- **Survivorship:** only people active at some point in the window exist, so there is no pre-2024 history of departed employees.",
          f"- **Turnover level:** with 24 months of exits, annualized turnover is about {v['annualized_turnover']:.1%}, lower than the 16% attrition flag in the seed, which is a share of all employees, not an annual rate.",
          "- **Only the current manager is stored.** Manager history is not modeled.",
          "- **Job titles are not people management.** Many 'Manufacturing Director' employees have no direct reports, because the hierarchy needs about 160 managers and the seed has about 300 leadership titles. Compute span of control only for employees with at least one report.",
          "- **Prior roles are synthetic.** Only the current role is real seed data.",
          "- **Month-end reconciliation passes by construction** at this stage. The real test happens in Snowflake in Phase 5, where the planted errors should break it.", ""]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(L), encoding="utf-8")


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def fmt_dates(d: pd.DataFrame) -> pd.DataFrame:
    d = d.copy()
    for c in d.columns:
        if str(d[c].dtype).startswith("datetime"):
            d[c] = d[c].dt.strftime("%Y-%m-%d")
    return d


def main():
    ap = argparse.ArgumentParser(description="Build synthetic HRIS source feeds from the Kaggle seed.")
    ap.add_argument("--input", type=Path, default=Path("data/processed/hr_seed_deduplicated.csv"))
    ap.add_argument("--out-dir", type=Path, default=Path("data/synthetic"))
    ap.add_argument("--key-dir", type=Path, default=Path("data/answer_key"))
    ap.add_argument("--docs-dir", type=Path, default=Path("docs"))
    ap.add_argument("--seed", type=int, default=SEED)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    rng = np.random.default_rng(args.seed)

    seed = pd.read_csv(args.input)
    assert seed["EmpID"].is_unique, "Seed must have unique EmpID. Run 01_profile_hr_data.py --export-clean first."
    log.info("Seed: %d employees", len(seed))

    df = build_core(seed, rng)
    mgr, managers = build_hierarchy(df, rng)
    emp = build_employees(df, mgr, managers, rng)
    term = build_terminations(df, emp, rng)
    jh = build_job_history(df, rng)
    loc, org = build_reference_tables(emp)
    extra = build_extra(df)

    v = validate(emp, term, jh)
    drift = ((df["ref_date"] - df["birth_date"]).dt.days / 365.25 - df["Age"])[df["_age_fixed"]]
    write_assumptions(args.docs_dir / "phase2_assumptions_log.md", v, args.seed,
                      int(df["_age_fixed"].sum()), float(drift.max()) if len(drift) else 0.0)

    emp_e, term_e, jh_e, answer = plant_errors(emp, term, jh, rng)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    args.key_dir.mkdir(parents=True, exist_ok=True)
    for name, t in [("hris_employees", emp_e), ("hris_job_history", jh_e.sort_values(["employee_id", "effective_date"])),
                    ("hris_terminations", term_e), ("hris_locations", loc), ("hris_org_units", org),
                    ("hris_employee_extra", extra)]:
        fmt_dates(t).to_csv(args.out_dir / f"{name}.csv", index=False)
        log.info("wrote %-22s %5d rows", name, len(t))
    answer.to_csv(args.key_dir / "planted_errors.csv", index=False)
    log.info("wrote answer key with %d planted errors", len(answer))


if __name__ == "__main__":
    main()
