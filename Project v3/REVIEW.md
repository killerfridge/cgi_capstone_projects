# Review — Foundry training capstones v3

Reviewed 3 September 2026 against the two v3 `.docx` files and
`generate_nhs_synthetic_data.py` in `Project v3/`.

Every Foundry claim below was checked against palantir.com/docs and every Polars
claim was executed against Polars 1.44. Where the docs render as navigation only
and I could not confirm something, I say so rather than guessing.

**Summary.** The three-project structure is good and Project 3 is a genuinely
better capstone than either of the other two — NEWS2 is a real algorithm with
real boundaries, and step-down eligibility is a decision a human actually makes.
But the required set has grown to roughly seven to eight days of work for four
build days, four pieces of reference code do not run or do not compile, and the
security architecture rests on a capability restricted views do not have. There
was also no data for Project 3 at all, and the Project 1 and 2 generators had
drifted out of step with what the briefs describe.

---

## 1. Blockers

### B1 — Restricted views cannot do column masking

Brief 1 §3 asks for a restricted view implementing "column-level masking
(masking `dob` to birth year only, hashing `patient_id` for non-clinical
reviewers)". The instructor guide repeats it as
`dob is truncated to date_trunc('year', dob)`.

The docs are explicit that a restricted view limits **rows**, not columns:
policies "compare user attributes, columns, and values" to decide row
visibility. They add a caveat that matters even more for this course:

> The restricted view policy filters what a user can read. It does not extend to
> functions, actions, AIP Logic, OSDK responses, writeback operations, or
> exports.

So a restricted view is not a general security boundary around the Ontology —
it does not cover the Functions the trainees spend Day 3 writing. Two
consequences:

- **Do the masking in the Transform tier.** Emit `birth_year` and a hashed
  `patient_ref`; do not carry `dob` or the raw NHS number into the Ontology
  backing dataset at all. That is a Polars exercise the trainees can actually
  do, and it is the honest answer: if a property must not be seen, it should not
  be indexed.
- **Keep exactly one restricted view per project, row-filter only.** Project 1
  filters `sensitive_care_flag = FALSE`; Project 2 filters on site; Project 3
  filters on trust. The generated data now supports all three.

The rubric line "Object Type backed by raw dataset instead of Restricted View
(-10)" should keep its deduction but gain the caveat, or you will teach a
security model the platform does not implement.

Source: [Restricted views](https://www.palantir.com/docs/foundry/security/restricted-views),
[Configuring restricted-view-backed object types](https://www.palantir.com/docs/foundry/object-permissioning/configuring-rv-access-controls).

### B2 — `findCompatibleBeds` will not compile

The reference solution filters a linked object inside a predicate:

```typescript
.filter(b => b.ward.has(w => w.specialtyType.exactMatch(requiredSpecialty)))
```

There is no such API. The docs state that link traversal on an object set is
done with generated `searchAround<LinkName>()` methods, capped at three per
query, and that `.get()` / `.all()` return object instances (a `ReadOnlyArray`
for a 1-to-many link) rather than object sets. A filter predicate operates on
one object type's own properties only.

The correct shape starts from the wards and traverses to the beds:

```typescript
@Function()
public findCompatibleBeds(attendance: EdAttendance): ObjectSet<HospitalBed> {
    const specialty = attendance.requiredSpecialty ?? "";
    const site      = attendance.hospitalSiteCode ?? "";
    const sex       = attendance.patientSex ?? "Not Specified";
    const complaint = attendance.chiefComplaint ?? "";

    const wards = Objects.search().ward()
        .filter(w => w.specialtyType.exactMatch(specialty))
        .filter(w => w.siteCode.exactMatch(site))
        .filter(w => Filters.or(
            w.genderPolicy.exactMatch("Mixed"),
            w.genderPolicy.exactMatch(sex),
        ));

    let beds = wards.searchAroundBeds()
        .filter(b => b.status.exactMatch("Available"));

    if (complaint.includes("Sepsis") || complaint.includes("Fever")) {
        beds = beds.filter(b => b.isIsolationCapable.isTrue());
    }
    return beds;
}
```

Note the gender predicate moved onto the ward set, where it belongs — the
patient's sex is a plain string in scope, so `exactMatch(sex)` covers the
Male-ward and Female-ward cases in one clause.

This is the single likeliest place for a light-TypeScript trainee to lose a
day. Day 3 needs one worked `searchAround` example before they start.

Source: [Object sets](https://www.palantir.com/docs/foundry/functions/api-object-sets),
[Objects and links](https://www.palantir.com/docs/foundry/functions/api-objects-links).

### B3 — All three Polars rolling-window snippets raise `TypeError`

Executed against Polars 1.44:

```
Expr.rolling_mean() got an unexpected keyword argument 'by'
Expr.rolling_median() got an unexpected keyword argument 'by'
```

The `by=` parameter was removed; time-based rolling is now `rolling_mean_by`,
`rolling_median_by`, and so on, taking the index column as the first argument.

There is a second, worse problem in the Project 1 and Project 2 snippets:

```python
pl.col("pathway_id").count().rolling_mean(window_size="7d", by="referral_date")
```

`count()` collapses the column to a single scalar. There is nothing left to roll
a window over. Even with the keyword fixed this does not express "7-day rolling
referral velocity". The metric needs a grain first:

```python
daily = (
    df.group_by(["specialty_id", "referral_date"]).len()
      .sort("referral_date")
      .with_columns(
          pl.col("len")
            .rolling_mean_by("referral_date", window_size="7d")
            .over("specialty_id")
            .alias("rolling_7d_referral_velocity")
      )
)
```

Same correction for the Project 2 hourly inflow and the Project 3 rolling
median.

While you are in that code: the reference transforms use `@transform` with
`in_patients.as_arrow()` and `out.write_table(...)`. `@transform_polars` is
documented as a thin wrapper over `@lightweight` and is the idiomatic entry
point for a Polars transform; it requires `polars` declared as a run dependency
in `meta.yml`, and Spark profiles cannot be used with it. Worth pinning in the
brief so nobody spends Day 1 on the transform decorator.

### B4 — The CDC deduplication key is wrong in both documents

Brief 2 Day 1 and the instructor solution both prescribe:

```python
.sort("arrival_timestamp").group_by("attendance_id").last()
```

`arrival_timestamp` is constant within an attendance, so `.last()` returns an
arbitrary row of the group — whichever the shuffle happened to put last. The
correct key is `event_timestamp`.

Measured on the regenerated data: **62.9% of dispositions come out wrong**. The
pipeline is green, the row count is exactly right, and nothing about the bed
board looks off. This is the highest-value marking point in the whole programme
and it is currently the answer the documents hand out.

Decide which you want:

- Leave the wrong snippet in the **brief** as a deliberate trap, and put the
  right answer in the **instructor guide** with the 62.9% figure next to it. My
  recommendation, but only if the brief also says somewhere that the sample code
  is illustrative and not to be trusted — otherwise it punishes the trainees who
  read carefully, which is the wrong lesson.
- Or make the brief pose it as a question ("what is the correct sort key for
  this stream, and how would you prove it?") and remove the snippet entirely.

Do not leave it as-is in both places.

### B5 — Project 3 had no generator, and Projects 1 and 2 had drifted

`generate_nhs_synthetic_data.py` in `Project v3/` was the original version. It
produced no Project 3 data at all, and for Projects 1 and 2 it produced
something different from what the briefs describe:

| Brief / instructor guide expects | Old generator produced |
| --- | --- |
| An ED **event stream** with `event_timestamp` | One row per attendance |
| `patient_gender` on the attendance | Absent — the single-sex rule was unimplementable |
| `hospital_site_code` for the site restricted view | Absent |
| `clinics.csv` with a `clinic_id` primary key | `specialty_clinics.csv`, one row per specialty |
| `sensitive_care_flag` for the row filter | Absent |
| Dirty data (Day 1 is a cleaning day) | Spotless — every cleaning task was a no-op |
| `target_breach_date` derived by the trainee | Precomputed in the CSV |

Replaced. See §4.

---

## 2. Scope — the required set does not fit in four build days

Day 5 is the review, so there are four build days. My estimate of the current
required set is seven to eight. Worse, several items are not blocked on trainee
skill but on tenant permissions they will not have.

| Requirement | Est. | Verdict |
| --- | --- | --- |
| Create and apply security markings | 0.5d | **Cut.** Creating a marking is a Control Panel operation, not something a trainee does inside a project. Pre-create one marking per project and have them apply it. |
| Restricted views with column masking | 1.0d | **Cut to row-filter only** (see B1); move masking into the Day 1 transform, where it costs ~20 minutes. |
| Three RBAC groups per project | 0.5d | **Cut to one**, pre-created by the instructor, membership pre-set. The lesson is "an action is permissioned", not "I can administer groups". |
| Time series properties (P3 Day 2) | 0.5–1.0d | **Cut from required, keep as a specified stretch.** Now verified — see §3a. It is documented and has a guided setup assistant, but the sync requires a long `seriesId`/`timestamp`/`value` schema and the telemetry is wide, so it needs an unpivot plus four properties plus series-ID matching. Model `TelemetryReading` as an ordinary object type for the required build. |
| Three TypeScript functions per project | 1.5d | **Cut to two.** Make the third optional. |
| Quiver "waterfall run charts" | 0.5d | **Reduce to one chart**, and confirm the chart type exists before naming it in a brief. |
| Monocle lineage verification | 0.25d | **Keep, rename.** The application is called **Data Lineage** in current docs; there is no Monocle page. A trainee searching the platform for "Monocle" finds nothing. |
| Workshop console | 1.5d | Keep — this is the point of the course. |
| Polars pipeline + cleaning | 1.0d | Keep. |
| Ontology modelling + links | 0.75d | Keep. |
| Action type + validation | 0.75d | Keep. |

That trims to roughly 4.5 days of required work, which is about right once you
allow for the Day 4 Workshop choke-point the guide itself flags.

Two more scope notes:

- **"Concurrency checks (preventing double-allocation)"** — submission criteria
  are documented as the conditions determining whether an action can be
  submitted. The docs say nothing about concurrency or race conditions. Call it
  a **validation guard** throughout. As written, a trainee could reasonably
  build something to defeat a race and then be marked against a criterion the
  platform does not claim to meet.
- **"Configure `weeksWaiting` as an Ontology Derived Property"** (P1 Day 2) —
  derived properties are documented as aggregations over *linked* objects
  (count, sum, average, min, max, cardinality, collect), max three hops; the
  Workshop variant adds column maths on one object type. Neither documents
  access to the current time, so an elapsed-time-since-now derived property is
  very likely not expressible. The docs are silent rather than explicit, so do
  not assert it is impossible — reframe the task as a **timeboxed
  investigation** ("establish whether this can be a derived property; if not,
  back it with a function and say why"). The exercise then tests how they
  establish a platform limit, which is a better thing to test.

---

## 3a. Time series properties — resolved

This was the one item I could not confirm on the first pass, because the time
series pages return navigation only through the documentation fetcher. Read in a
browser, they render fine. Here is what they actually say.

**It is documented and there is a guided setup assistant.** From a dataset
preview with a timestamp column, *Analyze data → Set up time series* launches an
assistant that walks through creating or selecting the object type and adding the
properties. So my original reason for cutting it — "I cannot confirm this is
available" — was wrong, and I have replaced it.

**But the sync schema is the problem.** A time series sync requires exactly
three columns:

| Column | Type |
| --- | --- |
| `seriesId` | string |
| `timestamp` | timestamp, or long with the unit declared |
| `value` | double, integer, float or string |

That is a **long** shape, one row per series per instant. `telemetry_readings.csv`
is **wide** — four value columns per reading. So a trainee cannot point a sync at
it. They would have to:

1. Unpivot to long, minting a composite series identifier per parameter
   (`IPA-2026-00001|spo2` and so on).
2. Create the sync — Time Series Catalog or Pipeline Builder — map the three
   columns, then save and build. Creating a sync builds a projection over the
   dataset.
3. Ensure the series ID on the sync matches the series ID on the object type
   backing dataset. The docs call this out explicitly, which is usually a sign
   it is the step people get wrong.
4. Add four time series properties, one per physiological parameter.

**Verdict unchanged, reasoning replaced.** Keep it out of the required set, for
two better reasons than the one I gave first time:

- Day 2 already carries the object model, the links and the restricted view. The
  unpivot plus sync plus four properties plus series-ID matching is not a
  twenty-minute detour.
- NEWS2 needs all four parameters from **one instant**, as a row. A time series
  property is built for reading a series over a window, which is the opposite
  access pattern. The `latest_vitals` transform is not a workaround for the
  absence of time series properties — it is the right shape for the scoring
  question, and would still be the right shape if the properties existed.

Where time series properties genuinely earn their place here is the Day 5 Quiver
chart — 48 hours of SpO2 with a threshold line. That is a window read, and it is
exactly what they are for. So the brief now carries this as a **specified
optional stretch** with the unpivot spelled out, rather than a vague "scoped
out". The unpivot itself is a good Polars exercise; if a group finishes early it
is the best thing they could do with the time.

Source: [Set up a time series](https://www.palantir.com/docs/foundry/time-series/time-series-setup),
[Time series syncs](https://www.palantir.com/docs/foundry/time-series/time-series-syncs).

---

## 3. Correctness and internal consistency

**C1. `getEligibleExpeditedSlots` returns an empty set for every pathway.**
The filter is `weeklyCapacity >= 40`. No clinic in the regenerated data has a
weekly capacity above 30, and in the old data only Ophthalmology qualified — so
the two most-backlogged specialties, ENT and Cardiology, were permanently empty.
Change the rule to *capacity strictly greater than the pathway's current
clinic*. That forces a link dereference (read the current clinic, then filter),
and on the new data it yields a genuine 0 / 1 / 2 spread across 836 / 818 / 895 pathways.

**C2. The reference solution violates the guide's own rubric.**
`calculateWardPressure` does `ward.beds.all()` then `.filter(...).length` —
correct code, since `.all()` returns a `ReadOnlyArray`. But rubric dimension 4
deducts 5 points for "over-fetching objects to JS array instead of ObjectSet".
Either soften the rubric line to "over-fetching **large** object sets", or
rewrite the reference to aggregate on the object set. A ward has at most 24
beds, so materialising is fine here — say so, or a marker will penalise the
model answer.

**C3. Rolling-median smoothing of vitals is clinically wrong and makes the
expected answer ambiguous.** A six-hour rolling median is exactly the operation
that hides an acute deterioration, which is the one thing the NEWS2 escalation
rule exists to catch. It also means two trainees who both "clean correctly" get
different NEWS2 scores depending on window handling. Change the Day 1 task to
**exclusion**: drop sensor dropouts (`0`, `-1`, null) and physiologically
impossible values, then score the latest surviving reading. Keep the rolling
median as an optional smoothed *chart* series on Day 5, which is what it is
actually good for. The generator now guarantees that the most recent reading for
every admission is valid, so the correct answer is well defined.

**C4. `calculateDischargeReadiness(admission, latestNews2)` takes the score as a
parameter and the brief never says where the caller gets it.** Specify it:
latest valid `TelemetryReading` linked to the admission, ordered by
`recordedAt` descending, take 1.

**C5. Brief 2's bed rules have no site constraint.** Rules 1–4 would happily
offer a Northgate bed manager a bed at St Aldate's. The restricted view would
mask it in the UI, but the Function is a separate read path (see B1), so the
rule belongs in the Function. Added as rule 0 in the rewritten brief.

**C6. The patient's sex is read from two different objects.** The brief's rule 4
reads `patient.gender`; the instructor solution reads
`attendance.patientGender`. The event stream now carries `patient_sex` on the
attendance, so pin it there — one less link hop on the hot path. Also note the
column is named for sex, not gender, because the NHS single-sex accommodation
standard is written in terms of sex; the briefs use `patient_sex` throughout.

**C7. Stale flags need naming in the briefs.** `is_breached` (Projects 1 and 2)
and `news2_cached` (Project 3) are deliberately stale. On the regenerated data
`is_breached` disagrees with the clock on 154 of 2,549 active pathways, and
`news2_cached` disagrees on 174 of 220 admissions. If the briefs do not say
"recompute these", a trainee who trusts them produces a plausible, wrong,
green-pipeline answer — the same failure mode as B4.

**C8. Object type naming.** "Specialty Clinic" was fine when there was one row
per specialty. There are now three clinics per specialty, so the object type is
`Clinic` with a `clinic_id` primary key and a link to a `Specialty`. Keeping
"Specialty Clinic" as a name for something keyed by clinic invites the 1:1
CSV-mirror anti-pattern the guide warns about elsewhere.

**C9. Minor.** `Timestamp.now()` exists in the Functions API — the reference
solutions drop to `Date.now()` unnecessarily. Not wrong, but the brief teaches
the platform type elsewhere. Also, the Universal Acceptance Checklist has no
rows specific to Project 3.

---

## 4. New data generator

`generate_nhs_synthetic_data.py` rewritten; `verify_generated_data.py` added.

```
python generate_nhs_synthetic_data.py --as-of 2026-09-03 --seed 20260903
python generate_nhs_synthetic_data.py --as-of 2026-09-03 --seed 20260903 --clean \
    --out ./synthetic_nhs_data_clean
python verify_generated_data.py --data ./synthetic_nhs_data --as-of 2026-09-03
```

53 assertions pass in dirty mode, 51 in clean mode (the two extra dirty-mode
assertions check that the staleness traps still have bite).

**Design rules it holds to.**

1. *Dirty by default.* Ten catalogued defect classes, D1–D10, all listed in
   `GENERATION_REPORT.md`. Without them Day 1 is a no-op and the trainees learn
   nothing from a cleaning exercise.
2. *Defects never break referential integrity.* Blank primary keys are injected
   only on rows nothing points at.
3. *Every figure in the report is computed on cleaned rows* — the same cleaning
   the briefs ask for — so a number quoted in a brief is a number a correct
   pipeline actually produces. Raw counts are labelled `raw_`.
4. *`--clean` produces the same hospital with the dirt removed*, not a different
   hospital. The defect injector holds its own random stream; drawing defect
   decisions from the main stream would shift every subsequent value. This
   caught three real bugs during the rewrite.

**What Project 3 now gets** (`project_3_virtual_ward/`):

- `inpatient_admissions.csv` — 220 admissions, of which 90 are medically fit,
  with `mobility_status`, `social_care_status`, `care_pathway`, `trust_code`
  for the trust-level row filter, and the deliberately stale `news2_cached`.
- `virtual_ward_beds.csv` — 44 beds across four care pathways. **Post-Surgical
  has zero available beds on purpose**: nine eligible patients have nowhere to
  go, so `recommendVirtualWardBed` has a genuine empty state to render rather
  than crash on.
- `telemetry_readings.csv` — 21,288 rows, 48 hours at 30-minute cadence, with
  sensor dropouts and impossible values scattered through. The most recent
  reading for every admission is always valid, so "current NEWS2" is
  well-defined no matter how the trainee cleans.

Headline figures a brief may quote (as of 2026-09-03, seed 20260903): 27
step-down eligible of 90 medically fit; NEWS2 spans 0–8; the escalation rule
fires on 59 of 220 admissions, five of which trip only the "any single parameter
scores 3" limb rather than the total.

**Projects 1 and 2** keep their shape but now match the briefs: a CDC event
stream with `event_timestamp` and `patient_sex`, `clinics.csv` with three sites
per specialty, `sensitive_care_flag`, and no precomputed `target_breach_date`.
31.3% of active pathways are past 18 weeks; exactly three specialties exceed the
20-week clearance threshold the stretch goal highlights; ward pressure spans
65.0% to 91.7% so the colour bands all appear; 45.3% of the in-flight ED cohort
is past four hours, against a median in-flight stay of 189 minutes.

**Two invariants worth knowing before you retune anything.**

- Ward occupancy is set by the `available` figure in `WARDS`, not derived from
  the ED feed. Every bed the ED allocates is Occupied, but the converse is
  false, because in a real hospital most occupied beds hold patients admitted
  days before this thirteen-hour feed opens. I tried the stricter invariant
  first; it forces ward pressure down to whatever today's arrivals happen to
  produce and the red band disappears.
- Clinic capacities are tuned against **cleaned** active pathway counts.
  Deduplication and null-key removal cost about 2% of rows, which is enough to
  flip a specialty across the 20-week line. `verify_generated_data.py` rejects
  any specialty sitting in the 19.5–20.5 band for exactly that reason.

---

## 5. What I have not verified

- ~~Time series setup.~~ **Resolved — see §3a.** Read in a browser rather than
  through the documentation fetcher. The recommendation to cut it from the
  required set stands, but on evidence now rather than on uncertainty, and the
  brief carries a properly specified optional stretch instead of a hand-wave.
- **Quiver waterfall run charts.** I did not confirm that chart type by name.
  The rewritten documents no longer name a chart type, so nothing now depends on
  it — but if you want a specific visualisation named in the brief, check it
  first.
- **Time series permissions.** I read the setup and sync pages but not the
  permissions page, so I do not know whether creating a sync needs a right your
  trainees lack. It only matters if a group attempts the stretch.
- **Whether your training tenant grants trainees marking creation, group
  administration, or restricted view creation.** The scope cuts in §2 assume it
  does not. If it does, several of them can come back.
