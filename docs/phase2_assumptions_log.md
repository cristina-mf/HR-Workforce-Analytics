# Phase 2: Synthetic Enrichment - Assumptions Log

Generated 2026-11-22 by `02_enrich_synthetic_hris.py` (random seed 42).

## Parameters

| Parameter | Value |
|---|---|
| As-of date | 2025-12-31 |
| Window | 2024-01-01 to 2025-12-31 (24 months) |
| Voluntary share of terminations | 80% (target) |
| Locations | 4 US sites (New York, Chicago, Austin, Atlanta) |
| First-line span of control | about 1 first-line manager per 12 people in each org unit (leavers included), roughly 8-9 active direct reports |
| Employee types | Full-time 90%, Part-time 7%, Contractor 3% |
| Planted errors | 6 (see answer key, kept separate) |

## Derivation rules

- **Termination date:** employees flagged Attrition = Yes get a date drawn uniformly across the window, then moved to the previous weekday if it fell on a weekend.
- **Hire date:** reference date (termination date for leavers, as-of date for stayers) minus `YearsAtCompany` years plus 0-364 random days, then moved to the next weekday (tenure changes by at most 2 days).
- **Birth date:** reference date minus `Age` years plus 0-364 random days, so a leaver's age matches the seed at the time they left. For 38 employees the seed implies a hire before age 18 (the seed says everyone is over 18), so birth date was moved earlier; their age at the reference date can be up to 1.8 years above the seed Age.
- **Weekdays:** hire, termination, promotion, transfer and manager-start dates all fall on weekdays.
- **Promotion:** created when `YearsSinceLastPromotion` < `YearsAtCompany` and level > 1. Prior income = current / (1 + `PercentSalaryHike`).
- **Transfer:** created when `YearsInCurrentRole` < `YearsAtCompany`. The prior role is synthetic (another non-leadership role in the same department).
- **Hierarchy:** one hierarchy per department x location org unit, built from seniority (role priority Manager > Research Director > Manufacturing Director, then job level, then tenure). Only active employees can be managers, and a report is matched to a manager at the same or higher job level where one exists. A leaver is assigned a manager who had been hired before the leaver's termination date. The most senior unit head in each department has no manager.
- **Manager start date:** from `YearsWithCurrManager`; stays NULL where the seed value is missing (decision D-04).
- **Termination type/reason:** seeded random. Retirement only at age 55+. Work-life-balance resignations only where OverTime = Yes or WorkLifeBalance <= 2. Contractors who leave involuntarily are 'End of contract'.
- **Employee type:** drawn at random; managers are always Full-time. Headcount rules for Contractors are a Phase 6 KPI definition.

## Validation (before errors were planted)

| Check | Result |
|---|---|
| employees | 1470 |
| terminations | 237 |
| hires in window | 169 |
| opening headcount | 1301 |
| closing headcount | 1233 |
| headcount reconciles | True |
| annualized turnover | 0.091 |
| voluntary share | 0.789 |
| term after hire | True |
| all managers exist | True |
| span median | 8.0 |
| span p90 | 11.0 |
| span max | 13 |
| dept heads | 3 |
| job history rows | 3499 |
| promotions | 851 |
| transfers | 1178 |
| latest history matches employee | True |
| hired under 18 | 0 |
| hires on weekend | 0 |
| terminations on weekend | 0 |
| reports with lower level manager | 3 |
| people managers | 159 |

## Known limitations

- **Survivorship:** only people active at some point in the window exist, so there is no pre-2024 history of departed employees.
- **Turnover level:** with 24 months of exits, annualized turnover is about 9.1%, lower than the 16% attrition flag in the seed, which is a share of all employees, not an annual rate.
- **Only the current manager is stored.** Manager history is not modeled.
- **Job titles are not people management.** Many 'Manufacturing Director' employees have no direct reports, because the hierarchy needs about 160 managers and the seed has about 300 leadership titles. Compute span of control only for employees with at least one report.
- **Prior roles are synthetic.** Only the current role is real seed data.
- **Month-end reconciliation passes by construction** at this stage. The real test happens in Snowflake in Phase 5, where the planted errors should break it.
