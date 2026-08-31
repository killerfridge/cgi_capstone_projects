# Trainee Project Brief 2: Emergency Department (ED) Patient Flow & Inpatient Bed Allocation

**Target Role**: Transitioning Data Engineer (Databricks → Palantir Foundry)
**Domain**: NHS Acute Hospital Operations, A&E 4-Hour Standard & Bed Management
**Duration**: 5 Working Days (Day 5 is review and presentation)

---

## 1. Project Overview & Operational Definitions

### A. Business Context

NHS Emergency Departments operate under the national **4-hour access standard**: 95% of patients attending A&E should be admitted, transferred or discharged within four hours. When an emergency patient needs acute admission, a "Decision to Admit" (DTA) is recorded, and the bed management team allocates a ward bed based on specialty, clinical urgency, single-sex policy and infection isolation needs.

You are building the bed board that team uses.

### B. Bed Compatibility Matching Rules (`findCompatibleBeds`)

`findCompatibleBeds(attendance: EdAttendance)` returns only beds satisfying **all four** clinical rules:

1. **Bed availability** — `bed.status` is `"Available"`. The source system does
   not write the status consistently (`Available`, `AVAILABLE`, `available`),
   but that is a Day 1 problem: normalise the casing in the pipeline and match
   the canonical value here. Lower-casing inside the function means pulling
   every bed into memory to do it, which works and scales badly.
2. **Specialty alignment** — the linked ward's `specialtyType` matches the patient's `requiredSpecialty`.
3. **Infection isolation** — if the patient's `chiefComplaint` contains `"Sepsis"` or `"Fever"`, only beds with `isIsolationCapable == true` qualify.
4. **Single-sex policy** — if the linked ward's `genderPolicy` is `"Male"`, only
   male patients; if `"Female"`, only female patients; if `"Mixed"`, anyone.
   `patient_sex` arrives as `Male`/`male`/`M` and is normalised in the pipeline
   for the same reason as the status column. Note that `Mixed` admits everyone:
   a filter that only matches the patient's own sex silently excludes four of
   the six wards.

Rules 3 and 4 both narrow the result, and they interact. One ward has no available side room at all, so a subset of patients will correctly return **zero** beds. An empty result is a valid answer that your interface has to handle, not a bug to engineer around.

### C. Ward Pressure Index (`calculateWardPressure`)

A percentage from 0.0 to 100.0 reflecting real-time occupancy:

```
WardPressureIndex = round((count of Occupied beds in ward / ward.totalBeds) * 100.0, 1)
```

| Range | Meaning |
| --- | --- |
| < 75% | Normal capacity (green) |
| 75–89% | Busy, approaching capacity (amber) |
| >= 90% | Critical over-capacity (red) |

The denominator is the ward's own `totalBeds` property — **not** the number of
bed objects linked to it. Those two numbers differ, because a few bed rows lose
their primary key in the source extract and are dropped on Day 1. `totalBeds`
is the ward's establishment; the linked beds are what the extract happens to
carry. One ward currently sits in each of the red and amber bands.

### D. What is "Allocating a Hospital Bed"?

The **Allocate Hospital Bed** action mutates two objects in a single execution:

**Submission criteria (validation guard)**

* `bed.status == 'Available'`
* `attendance.disposition != 'Admitted'`

**Execution rules**

* `bed.status` becomes `"Occupied"`
* `attendance.disposition` becomes `"Admitted"`
* `attendance.allocatedBed` is linked to `bed`

Submission criteria are documented as the conditions that determine whether an action can be submitted. Treat them as a validation guard — they stop *you* allocating a bed that is already taken. Whether they resolve a genuine simultaneous double-submission is a separate question the documentation does not answer, and one worth raising rather than asserting on Day 5.

### E. The Data You Are Given

Three CSVs in `/Datasource/`.

| File | Grain | Approx. rows |
| --- | --- | ---: |
| `ed_attendance_events.csv` | **one row per state change** | ~1,245 |
| `hospital_beds.csv` | one row per bed | ~109 |
| `wards.csv` | one row per ward | 6 |

Those are *raw* row counts. Fewer rows survive Day 1 — see the self-check at the
end of Day 1 for the numbers you should land on.

**Read that grain again.** `ed_attendance_events.csv` is a change-data-capture stream from the hospital's ADT system, not a table of attendances. Each attendance emits one to four rows as the patient moves through
`Registered` → `Under Assessment` → `Awaiting Bed` → `Admitted` or
`Discharged`. A patient who walked in twenty minutes ago has only been
registered, so their attendance is a single row; that is a current state, not a
truncated record. There are 400 real attendances behind those rows.

The rows arrive in no useful order, and `arrival_timestamp` is **constant within an attendance** — it is when the patient walked in, not when the row was written. The column that orders events is `event_timestamp`. Collapsing this stream to current state is Day 1's central task, and getting the sort key wrong produces a pipeline that runs green and reports the wrong state for most of your patients.

Like Project 1, this is a raw extract: expect duplicate rows, missing keys, more than one date format, whitespace on identifiers and casing drift on categorical values. The `is_breached` column is a point-in-time snapshot written at each event and is stale by the time you read it.

---

## 2. 5-Day Milestone Plan

```
Day 1: Pipeline setup & Polars CDC event resolution
Day 2: Ontology modelling, primary keys & bed-patient relationships
Day 3: TypeScript Functions on Objects (bed matching & ward pressure)
Day 4: Action Types with submission criteria & Workshop bed board
Day 5: Validation testing, lineage in Monocle, polish & presentation
```

### Day 1 — Pipeline Engineering with Polars (Code Repositories)

Set up a three-tier structure: `/Datasource/` → `/Transform/` → `/Ontology/`.

**Polars in Foundry runs on the lightweight transforms path**, not the standard Spark one:

```python
from transforms.api import transform_polars, Input, Output
import polars as pl

@transform_polars(
    Output("/.../Ontology/clean_ed_attendances"),
    source=Input("/.../Datasource/ed_attendance_events"),
)
def compute(ctx, source):
    return (
        source
        .sort("event_timestamp")
        .group_by("attendance_id")
        .last()
    )
```

* `polars` must be declared as a **run dependency in `meta.yml`**.
* `@transform_polars` wraps `@lightweight`; **Spark profiles are unavailable** on this path.

The snippet above is the shape of the answer, not the answer. Before it will work you still need to normalise the identifiers you are grouping on, parse `event_timestamp` from more than one format, and satisfy yourself that the row you kept is the row you meant to keep. Prove it: pick three attendances, trace their events by hand, and check your output agrees.

Output `/Ontology/clean_ed_attendances` (one row per attendance), `/Ontology/clean_hospital_beds`, `/Ontology/clean_wards`.

**Self-check.** A correct pipeline outputs exactly **400 attendances**, **106
beds** and **6 wards**. If you get more than 400 attendances you have not
trimmed `attendance_id` before grouping — padded and unpadded copies of the same
id count as different patients, and the raw file contains 480 distinct spellings
of 400 identifiers.

### Day 2 — Ontology Layer & Relationships (Ontology Manager)

Define three object types and two link types (Section 3.B), then publish.

### Day 3 — TypeScript Functions on Objects

Scaffold a TypeScript Functions repository. Functions live in a class exported from `src/index.ts`:

```typescript
import { Function, Double } from "@foundry/functions-api";

export class EdFunctions {
    @Function()
    public example(a: Double): Double {
        return a * 2;
    }
}
```

Implement `findCompatibleBeds` and `calculateWardPressure` (Section 3.C). `getLiveLosMinutes` is listed there as optional — build it only if the first two are finished and tested.

### Day 4 — Action Types & Workshop Bed Board

Create `Allocate Hospital Bed` with the submission criteria from Section 1.D, then build a **single** Workshop console:

* Metric cards: active A&E count, patients waiting over 3.5 hours, total vacant
  inpatient beds. Roughly half the in-flight patients are past four hours and
  half are not, so these cards should separate patients rather than colour
  everything red — if yours reads all-or-nothing, check you are deriving
  length of stay from `arrivalTimestamp` at read time.
* Worklist table: patients with `disposition == 'Awaiting Bed'`, sorted by elapsed length of stay.
* Bed picker panel driven by `findCompatibleBeds`, with the allocate action. It must render an honest empty state.

### Day 5 — Validation, Testing & Demo

* Attempt to allocate a bed whose status is `Cleaning` or `Occupied` and confirm the action is refused.
* Allocate a bed successfully and confirm both the bed and the attendance update, and that the ward pressure figure moves.
* Configure build health checks and dataset monitoring.
* Prepare a 10-minute walkthrough: lineage in Monocle, then the live allocation workflow.

---

## 3. Technical Requirements & Deliverables

### A. Data Pipelines (Code Repositories / Polars)

* **Inputs**: `ed_attendance_events.csv`, `hospital_beds.csv`, `wards.csv` from `/Datasource/`.
* **Outputs**: `/Ontology/clean_ed_attendances`, `/Ontology/clean_hospital_beds`, `/Ontology/clean_wards`.
* **Rule**: use **Polars** via `@transform_polars`. Sort on `event_timestamp` before grouping. One row per `attendance_id` on output.

### B. Ontology Schema

| Object Type | Primary Key | Properties |
| --- | --- | --- |
| **ED Attendance** | `attendance_id` | `patientSex`, `arrivalTimestamp`, `triageCategory`, `chiefComplaint`, `requiredSpecialty`, `admissionRequired`, `decisionToAdmitTimestamp`, `disposition`, `allocatedBedId`, `dischargeTimestamp` |
| **Hospital Bed** | `bed_id` | `bedNumber`, `status` (`Available`/`Occupied`/`Cleaning`/`Maintenance`), `isIsolationCapable` |
| **Ward** | `ward_id` | `wardName`, `genderPolicy`, `specialtyType`, `totalBeds` |

**Link Types**

* `Ward` 1 → N `Hospital Bed` (`beds` / `ward`)
* `Hospital Bed` 1 → 1 `ED Attendance` (`currentPatient` / `allocatedBed`)

The 1:1 link is backed by `allocatedBedId` on the attendance — keep that column
through the pipeline or you have nothing to build the link from.

`disposition` takes five values in this data: `Registered`, `Under Assessment`, `Awaiting Bed`, `Admitted`, `Discharged`.

### C. TypeScript Functions (`src/index.ts`)

**`findCompatibleBeds(attendance: EdAttendance): ObjectSet<HospitalBed>`** — all four rules from Section 1.B.

**`calculateWardPressure(ward: Ward): Double`** — the formula in Section 1.C.

**`getLiveLosMinutes(attendance: EdAttendance): Integer`** *(optional)* — minutes elapsed since arrival, evaluated against the current time.

Write unit tests covering: a sepsis patient in the ward with no available side room, a female patient whose specialty has both a male and a female ward, a ward with zero occupied beds, and an attendance whose required specialty matches no ward.

### D. Action Types & Workshop

**Action `Allocate Hospital Bed`** — enforces the submission criteria and performs both state updates plus the link in one execution.

The console must show live bed board metrics, breach countdown alerts, and an allocation panel that reacts to selection without a manual reload.

---

## 4. Stretch Goals: Operational Command Centre

**Optional.** Attempt these only once the bed board in Section 3.D works end to end.

### Stretch Goal 1: Trust-Wide Ward Occupancy & Pressure Heatmap

An **Executive Site Overview** tab:

* **Ward capacity heatmap** — every ward as a card showing name, specialty,
  single-sex policy, a live occupancy bar broken down by status, and a red
  badge at or above 90% occupancy. One ward is currently red and one amber, so
  all three bands should appear; a heatmap that is uniformly green has almost
  certainly counted `Cleaning` or `Maintenance` beds as occupied, or divided by
  the linked bed count instead of `totalBeds`.
* **Isolation capacity tracker** — available side rooms against total isolation beds. This number is deliberately tight; it should look uncomfortable, because it is.

### Stretch Goal 2: A&E Flow Run Chart

* **Hourly inflow vs. outflow** — attendances grouped by arrival hour against discharge and admission hour, to expose surge periods.
* **DTA-to-bed delay** — average delay between Decision to Admit and actual bed allocation, by specialty.

---

## 5. Key Architectural Watch-Outs

**1. Event ordering (CDC).**
`arrival_timestamp` is constant within an attendance; sorting on it and taking `.last()` returns whichever row happened to be last in the file, which is arbitrary because the rows are shuffled. Sort on `event_timestamp`, which is strictly increasing within every attendance. If you get this wrong the pipeline still runs, the row counts still look right, and most of your patients are in the wrong state — which is precisely why it is worth getting right.

**2. The stale snapshot trap.**
`is_breached` was computed when each event was written and never revisited. Length of stay must be derived at read time from `arrivalTimestamp`, not read from a stored flag.

**3. The "ghost occupant" link sync trap.**
The allocation action must update the bed, the attendance, *and* the link in the same execution. Updating only one leaves a bed marked occupied with nobody in it, or a patient admitted to nowhere. The existing occupancy in the source data is internally consistent: every occupied bed has exactly one admitted patient behind it and every admitted patient holds exactly one occupied bed, with every allocation respecting both specialty and single-sex policy. Any ghost occupant on your board was made by your pipeline or your action, not inherited. Keep it that way.

**4. Validation guard, not a concurrency proof.**
See Section 1.D. Submission criteria stop an invalid submission. Do not claim more than that in your presentation.

**5. Rule interaction.**
Isolation and single-sex constraints compound. A male sepsis patient needing General Surgery is filtered by three separate rules before availability is even considered. Test the intersections, not just the individual rules.

---

## 6. Acceptance Checklist

**Core — all required**

- [ ] Pipeline runs on `@transform_polars` with `polars` declared in `meta.yml`.
- [ ] The event stream resolves to exactly one row per `attendance_id`, sorted on `event_timestamp`.
- [ ] Resolution is verified by hand against at least three traced attendances.
- [ ] Duplicate and null primary keys are removed; identifiers normalised before joining.
- [ ] Length of stay is derived at read time; no stored breach flag is trusted.
- [ ] All three object types and both link types are published in Ontology Manager.
- [ ] `findCompatibleBeds` implements all four rules and passes the unit tests in Section 3.C.
- [ ] `calculateWardPressure` matches the specification.
- [ ] `Allocate Hospital Bed` refuses an unavailable bed and updates bed, attendance and link together.
- [ ] The bed picker renders a correct empty state for patients with no compatible bed.
- [ ] Monocle shows clean end-to-end lineage.

**Stretch — optional, not required to pass**

- [ ] Site Overview tab with ward occupancy heatmap and isolation tracker.
- [ ] Hourly flow run chart and DTA-to-bed delay metric.
