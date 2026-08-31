# Trainee Project Brief 1: Elective Care Referral-to-Treatment (RTT) & Waiting List Validation

**Target Role**: Transitioning Data Engineer (Databricks → Palantir Foundry)
**Domain**: NHS Elective Care & Referral-to-Treatment (RTT) Pathways
**Duration**: 5 Working Days (Day 5 is review and presentation)

---

## 1. Project Overview & Operational Definitions

### A. Business Context

In the NHS, the statutory **Referral-to-Treatment (RTT)** standard requires that at least 92% of patients on non-urgent consultant-led pathways start treatment within **18 weeks** of referral. Because of elective backlogs, clinical validation teams continuously review waiting lists, prioritise patients at risk of breaching the 18-week standard, reallocate clinical slots, and record validation outcomes.

You are building the tool that team uses.

### B. What is "Validating an RTT Pathway"?

**Pathway Validation** is a formal administrative and clinical check performed on unverified pathways (`validatedFlag == false`). The validator:

1. Reviews the patient's elapsed waiting time and background clinical risk.
2. Confirms or modifies their **Clinical Priority Band** (Royal College of Surgeons standard: P1–P4).
3. Optionally records a validation audit note.
4. Executes the **Validate RTT Pathway** action, which sets `validatedFlag = true` and updates the priority, removing the patient from the unvalidated triage queue.

### C. The Breach Risk Scoring System (`calculateBreachRiskScore`)

A deterministic integer from **0 to 100**, computed at runtime from three weighted components:

```
BreachRiskScore = min(100, DurationScore + PriorityScore + PatientRiskScore)
```

**1. Duration Score (max 50 points)**

```
elapsedWeeks  = (now - referralDate) / (7 * 24 * 3600 * 1000)
DurationScore = min(50, round((elapsedWeeks / 18.0) * 50))
```

If `elapsedWeeks >= 18.0`, DurationScore is locked at 50.

If `referralDate` cannot be read at all, there is no anchor and therefore no
duration component: score the other two and return that. Do not throw, and do
not substitute today's date — a pathway with no referral date is a data problem
to surface, not one to paper over with a zero-week wait.

**2. Clinical Priority Score (max 30 points)**

| Band | Meaning | Points |
| --- | --- | ---: |
| `P1` | Urgent, < 72 hours | 30 |
| `P2` | Urgent surgery, < 4 weeks | 25 |
| `P3` | Priority surgery, < 12 weeks | 18 |
| `P4` | Routine surgery, > 12 weeks (default) | 10 |

**3. Patient Background Risk Score (max 20 points)**

| Risk Category | Points |
| --- | ---: |
| `High` | 20 |
| `Medium` | 12 |
| `Low` or unspecified | 5 |

Note that `riskCategory` is blank on some patients and the band is missing on none — but do not assume either. An unreadable or absent value scores 5, it does not throw.

### D. The Data You Are Given

Three CSVs in `/Datasource/`. **This is a raw operational extract, not a curated dataset.** It contains the defects you would expect from a source system that has been running for years: duplicate rows, missing keys, dates written in more than one format, identifiers with stray whitespace, and categorical values whose casing has drifted over time. Finding and fixing these is Day 1's actual work — not a formality.

| File | Grain | Approx. rows |
| --- | --- | ---: |
| `patients.csv` | one row per patient | ~2,450 |
| `rtt_pathways.csv` | one row per pathway | ~3,060 |
| `clinics.csv` | one row per clinic site | 21 |

Those are *raw* row counts. Fewer rows survive Day 1 — see the self-check at the
end of Day 1 for the numbers you should land on.

A patient may be on more than one pathway. Each specialty runs **three clinic sites**, each with its own weekly capacity — this matters for the slot recommender in Section 3.C.

**Two definitions to fix before you build anything.** An **active pathway** is
one with `clock_status == 'ACTIVE'`; a paused pathway is still on the waiting
list but is excluded from the clearance calculation in Section 4. Say which
population each figure you present is built on, and stay consistent — the same
chart built on all pathways and on active-only differs by about 17%.
`rtt_status_code` is the national RTT activity code (`10` first activity, `20`
subsequent). Nothing in this project uses it; it ships because the source
extract carries it. You are not expected to model it.

**Two columns deserve suspicion.** `rtt_pathways.csv` ships an `is_breached` boolean produced by the source system at extract time. It is a stored snapshot of a moving fact. Part of your Day 5 write-up is to say what you did about it and why. There is no `target_breach_date` column; you derive that.

---

## 2. 5-Day Milestone Plan

```
Day 1: Project setup & Polars data pipeline    (Datasource -> Transform -> Ontology)
Day 2: Ontology modelling, primary keys & link types
Day 3: TypeScript Functions on Objects          (risk scoring & slot recommender)
Day 4: Action Types & Workshop validation console
Day 5: Data health, lineage in Monocle, polish & presentation
```

### Day 1 — Pipeline Engineering with Polars (Code Repositories)

Set up a three-tier project structure: `/Datasource/` → `/Transform/` → `/Ontology/`.

**Polars in Foundry does not work the way you may expect.** A standard `@transform` gives you a Spark DataFrame. Polars runs on the *lightweight* transforms path instead:

```python
from transforms.api import transform_polars, Input, Output
import polars as pl

@transform_polars(
    Output("/.../Ontology/clean_rtt_pathways"),
    source=Input("/.../Datasource/rtt_pathways"),
)
def compute(ctx, source):
    return source.filter(pl.col("pathway_id").is_not_null())
```

Two things will stop you if you skip them:

* `polars` must be declared as a **run dependency in `meta.yml`**. It is not there by default.
* `@transform_polars` is a thin wrapper around `@lightweight`, and **Spark profiles cannot be used** on this path. That is fine at this data volume, and understanding *why* the split exists is worth ten minutes of your time.

Your transforms must:

* Normalise identifiers before they are used as join keys.
* Parse date columns properly. More than one format is present; a naive cast will silently produce nulls.
* Standardise categorical values whose casing has drifted.
* Enforce primary key non-nullness and uniqueness with `.unique(subset=[...])`. Decide and document *which* duplicate you keep and why.
* Derive `target_breach_date` as `referral_date + 18 weeks`.

Output `/Ontology/clean_patients`, `/Ontology/clean_rtt_pathways`, `/Ontology/clean_clinics`.

**Self-check.** A correct pipeline outputs **2,397 patients**, **2,998
pathways** and **21 clinics**. Those are below the raw row counts by more than
the duplicates alone: blanking a primary key destroys that row's identifier, so
the patient or pathway it described is gone for good. If you land on 2,400 and
3,000 you have kept rows you should have dropped; if you land well below, check
you are not dropping on a column that is legitimately blank.

### Day 2 — Ontology Architecture (Ontology Manager)

Ingest the clean backing datasets and define three object types and two link types (see Section 3.B). Publish them.

**A deliberate investigation.** You need `weeksWaiting` — elapsed weeks since referral — available to the application. Spend no more than thirty minutes establishing whether an Ontology **derived property** can express it, then write down what you found. The docs describe derived properties as aggregations over *linked* objects, and Workshop derived properties as linked aggregations plus column maths over properties on the same object; neither documents access to the current time. If you conclude it cannot be done that way, say so and implement it as a function-backed column instead (Day 3). Reaching the right answer matters less than showing how you established it.

### Day 3 — TypeScript Functions on Objects

Scaffold a TypeScript Functions repository. Functions live in a class exported from `src/index.ts`:

```typescript
import { Function, Integer } from "@foundry/functions-api";

export class RttFunctions {
    @Function()
    public example(a: Integer): Integer {
        return a + 1;
    }
}
```

Implement:

* `calculateBreachRiskScore(pathway: RttPathway): Integer` — exactly the specification in Section 1.C.
* `getEligibleExpeditedSlots(pathway: RttPathway): ObjectSet<Clinic>` — see Section 3.C.

Write unit tests covering: a pathway with no referral date, a pathway already past 18 weeks, a patient whose `riskCategory` is blank, a pathway whose linked patient cannot be resolved, and a pathway already booked at its specialty's highest-capacity site.

### Day 4 — Action Types & Workshop Console

Create the `Validate RTT Pathway` action type, then build a **single** Workshop console:

* Filter bar: specialty, priority band, validation status.
* Pathway table with relative duration formatting and a red badge past 18 weeks.
  Check what Workshop's own relative-date formatter gives you before building
  around a specific string — if it will not render fractional weeks, a
  function-backed column is the fallback, and either is acceptable.
* Detail panel showing the live risk score, the recommended alternative clinic sites, and the action button.

### Day 5 — Data Health, Lineage & Demo

* Configure build health checks and data expectations.
* Verify end-to-end writeback from Workshop into the Ontology.
* Prepare a 10-minute technical walkthrough: pipeline lineage in Monocle, then the live application.

---

## 3. Technical Requirements & Deliverables

### A. Data Pipelines (Code Repositories / Polars)

* **Inputs**: `patients.csv`, `rtt_pathways.csv`, `clinics.csv` from `/Datasource/`.
* **Outputs**: `/Ontology/clean_patients`, `/Ontology/clean_rtt_pathways`, `/Ontology/clean_clinics`.
* **Rule**: use **Polars** (`import polars as pl`) via `@transform_polars`, not Pandas or PySpark. Primary keys must be non-null and unique on output.

### B. Ontology Schema

| Object Type | Primary Key | Properties |
| --- | --- | --- |
| **RTT Pathway** | `pathway_id` | `referralDate`, `targetBreachDate`, `clockStatus`, `clockStopDate`, `priorityBand`, `validatedFlag` |
| **Patient** | `patient_id` | `postcodeDistrict`, `dob`, `gender`, `riskCategory` |
| **Clinic** | `clinic_id` | `specialtyCode`, `specialtyName`, `siteName`, `weeklyCapacity`, `leadConsultantCode` |

**Link Types**

* `Patient` 1 → N `RTT Pathway` (`pathways` / `patient`)
* `Clinic` 1 → N `RTT Pathway` (`pathways` / `clinic`)

Do not create a separate object type for specialty. It is a property of a clinic, nothing acts on it, and it has no lifecycle of its own.

### C. TypeScript Functions (`src/index.ts`)

**`calculateBreachRiskScore(pathway: RttPathway): Integer`**

Exact implementation of the 0–100 algorithm in Section 1.C.

**`getEligibleExpeditedSlots(pathway: RttPathway): ObjectSet<Clinic>`**

Return the clinic sites this patient could realistically be moved to in order to be seen sooner. A site qualifies when it:

* runs the same `specialtyCode` as the pathway's current clinic, **and**
* is not the site the patient is already booked at, **and**
* has a **higher** `weeklyCapacity` than the site the patient is currently booked at.

Order the result by descending `weeklyCapacity`.

Note what the third rule requires: you cannot evaluate it without first traversing the link to the pathway's current clinic and reading its capacity. A patient already at their specialty's highest-throughput site correctly returns an empty set — roughly a third of pathways do. Your interface has to say "no faster site available" rather than render an empty table.

### D. Action Types & Workshop

**Action `Validate RTT Pathway`**

| | |
| --- | --- |
| Parameters | `target_pathway` (RTT Pathway), `new_priority_band` (choice: P1/P2/P3/P4), `validation_notes` (string, optional) |
| Rules | set `target_pathway.validatedFlag = true`; set `target_pathway.priorityBand = new_priority_band` |

The Workshop console must be two-way reactive: selecting a row updates the detail panel and re-evaluates the scoring function, and submitting the action refreshes the table without a manual reload.

---

## 4. Stretch Goals: Executive Dashboarding & Capacity Modelling

**Optional.** Attempt these only once the console in Section 3.D works end to end. A complete, solid core build scores higher than a broken one with a half-finished dashboard attached.

### Stretch Goal 1: 18-Week Backlog Waterfall & Cohort Distribution

Add an **Executive Overview** tab for Service Line Managers:

* **Waiting list waterfall**: pathways segmented into standard NHS cohorts (`<6w`, `6–12w`, `12–18w`, `18–52w`, `52w+`), grouped by specialty. All five cohorts are populated in this dataset.
* **Breach rate gauge**: percentage of active pathways within 18 weeks, with conditional colouring against the 92% target. Expect the real figure to be far below target — this is a backlog dataset.

### Stretch Goal 2: Capacity vs. Demand Clearance Forecaster

For each specialty, compute:

```
ClearanceWeeks = total active waiting patients / total weekly capacity across that specialty's sites
```

Display a ranked bar chart. Highlight specialties above **20 weeks**; three of
the seven currently exceed it, and the range runs from roughly 8 to 28 weeks.
Compute this on active pathways only, per the definition in Section 1.D, and sum
capacity across all three of a specialty's sites.

---

## 5. Key Architectural Watch-Outs

**1. The stale temporal state trap.**
Store only the invariant anchor (`referral_date`). Derive elapsed weeks at read time — in TypeScript via `Timestamp.now()` or `Date.now()`, or in Workshop's relative date formatting. The `is_breached` column in the source extract is exactly what this warning is about: a boolean that was true when it was written and is not checked again. Some rows already disagree with a live calculation. Find them, decide what to do, and be ready to explain the decision.

**2. The derived-value trap, second form.**
Editing a property through an Action does **not** recompute anything a pipeline derived from it. If you were to store `weeksWaiting` as a column and a validator corrected `referralDate`, the stored value would sit beside it, stale and silent. This is why the elapsed-time calculation belongs at read time.

**3. OSv2 primary key strictness.**
Object Storage v2 fails indexing if any row has a null or duplicate primary key. The source data contains both. `.unique(subset=['pk_column'])` after filtering nulls, before output.

**4. TypeScript null-safety.**
Link traversal returns `undefined` when the link cannot be resolved. Use `pathway.patient.get()?.riskCategory` and `??` defaults rather than assuming the patient is there.

**5. Paused clocks.**
Roughly one pathway in six has `clockStatus == 'PAUSED'` and a `clockStopDate`. The Section 1.C specification deliberately ignores this and counts elapsed time as though every clock were running. That is a simplification, and a real RTT calculation would not make it. You are not required to fix it — you *are* expected to have noticed it by Day 5.

---

## 6. Acceptance Checklist

**Core — all required**

- [ ] Pipeline runs on `@transform_polars` with `polars` declared in `meta.yml`.
- [ ] Duplicate and null primary keys are removed; the choice of surviving duplicate is documented.
- [ ] Dates in both source formats parse correctly; no silent null coercion.
- [ ] Identifiers are normalised before being used as join keys; no orphaned references.
- [ ] `target_breach_date` is derived in the pipeline; elapsed waiting time is not stored.
- [ ] All three object types and both link types are published in Ontology Manager.
- [ ] `calculateBreachRiskScore` matches the specification and passes unit tests including the four edge cases in Day 3.
- [ ] `getEligibleExpeditedSlots` returns correctly filtered and ordered clinic sites.
- [ ] `Validate RTT Pathway` updates priority and validation status; the table refreshes without reload.
- [ ] Monocle shows clean end-to-end lineage.
- [ ] The `is_breached` question is answered in the Day 5 walkthrough.

**Stretch — optional, not required to pass**

- [ ] Executive Overview tab with backlog waterfall and breach rate gauge.
- [ ] Clearance forecaster with the 20-week threshold highlighted.
