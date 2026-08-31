# Generation report — instructor key

Generated **2026-08-31**, anchored to as-of date **2026-08-31**, seed `20260831`, mode **dirty (trainee)**.

Regenerate on the morning a cohort starts. Elapsed-time cohorts, ED length of stay and the stale breach flags are all relative to the as-of date and drift daily.

> **Every figure below is post-cleaning** — trimmed keys, blank keys dropped, duplicate keys dropped. They are what a correct pipeline outputs, not what the raw CSVs contain. Raw row counts are given separately and labelled *raw*.

## What a correct pipeline outputs

| Dataset | Raw rows | After cleaning |
| --- | ---: | ---: |
| `patients.csv` | 2448 | **2397** |
| `rtt_pathways.csv` | 3060 | **2998** |
| `clinics.csv` | 21 | 21 |
| `ed_attendance_events.csv` | 1245 | **400** (one row per attendance) |
| `hospital_beds.csv` | 109 | **106** |
| `wards.csv` | 6 | 6 |

Blanking a primary key destroys that row's identifier, so the cleaned counts sit slightly below the generated volumes. That is correct and expected — mark against this column.

## Project 1 — RTT

- 48 duplicate patient rows and 60 duplicate pathway rows injected; 3 null primary keys per file
- `clinics.csv` — 7 specialties x 3 sites = 21 clinic rows

### Waiting-time cohorts

All pathways, active and paused. The stretch-goal waterfall may be built on either population — say which, and be consistent.

| Cohort | Pathways |
| --- | ---: |
| <6w | 445 |
| 6-12w | 547 |
| 12-18w | 578 |
| 18-52w | 1178 |
| 52w+ | 250 |

Breach rate against the 92% standard: **52.4% within 18 weeks**.

### Backlog clearance by specialty

`active pathways / summed weekly capacity across the specialty's three sites`, counting only `clock_status == 'ACTIVE'`.

| Specialty | Active | Weekly capacity | Clearance (weeks) |
| --- | ---: | ---: | ---: |
| ENT (Ear, Nose & Throat) | 369 | 13 | 28.4 |
| Cardiology | 328 | 15 | 21.9 |
| Trauma & Orthopaedics | 375 | 18 | 20.8 |
| Gynaecology | 350 | 22 | 15.9 |
| General Surgery | 387 | 28 | 13.8 |
| General Internal Medicine | 358 | 34 | 10.5 |
| Ophthalmology | 338 | 44 | 7.7 |

**3 of 7 specialties exceed the 20-week highlight threshold**: ENT (Ear, Nose & Throat), Cardiology, Trauma & Orthopaedics.

- `getEligibleExpeditedSlots` search space: each pathway has **2 alternative sites** in its specialty before capacity filtering — a real object-set filter, not a single-row lookup.
- Priority mix: P1 84, P2 400, P3 917, P4 1597
- `is_breached` disagrees with a live calculation on **58 cleaned rows** as of the as-of date, and drifts further every day the file sits unregenerated.
- `target_breach_date` is **not shipped** — trainees derive it from `referral_date` + 18 weeks.

## Project 2 — ED flow

- `ed_attendance_events.csv` — **1245 event rows** for **400 attendances** (3.11 per attendance)
- Rows are **shuffled**; `arrival_timestamp` is constant within an attendance. A `group_by('attendance_id').last()` without a prior `sort('event_timestamp')` retains the wrong state.
- `event_timestamp` is strictly increasing within every attendance (**0 ordering faults** — must be 0, or the documented resolution is not reproducible).

### Resolved dispositions after a correct CDC collapse

| Disposition | Attendances |
| --- | ---: |
| Discharged | 245 |
| Admitted | 71 |
| Awaiting Bed | 65 |
| Under Assessment | 12 |
| Registered | 7 |

The Day 4 worklist (`disposition == 'Awaiting Bed'`) holds **65 patients**.

### The 4-hour standard

- **84 attendances are in flight** (Registered, Under Assessment or Awaiting Bed).
- Length of stay across them: median **230 minutes**, max **594**. Against the 240-minute standard, **40 of 84 have breached** — so the metric cards and countdown alerts discriminate rather than reading red for everyone.
- `is_breached` on the newest event disagrees with a live calculation for **41 of those 84**. Most of that is honest temporal drift, not injected error: the flag was true when written and the patient has been waiting ever since.

### Bed estate

- **106 beds after cleaning** — Available 22, Cleaning 7, Maintenance 6, Occupied 71.
- Isolation: **5 available of 12**.
- Occupied beds with no admitted attendance behind them: **0** (must be 0 — occupancy is generated from the ED feed, so the two cannot disagree).
- Available beds by ward: Acute Medical Unit (AMU) 4, Trauma & Orthopaedic Ward 4, Surgical Ward (Male) 4, Surgical Ward (Female) 4, Coronary Care Unit 1, Emergency Assessment Unit 5.

### Ward pressure index

| Ward | Occupied | Total | Pressure |
| --- | ---: | ---: | ---: |
| Coronary Care Unit | 11 | 12 | 91.7% (red) |
| Acute Medical Unit (AMU) | 20 | 24 | 83.3% (amber) |
| Trauma & Orthopaedic Ward | 14 | 20 | 70.0% (green) |
| Surgical Ward (Male) | 11 | 18 | 61.1% (green) |
| Emergency Assessment Unit | 8 | 16 | 50.0% (green) |
| Surgical Ward (Female) | 7 | 18 | 38.9% (green) |

The denominator is the ward's `total_beds` property, not the count of linked bed objects — those differ where a bed row lost its primary key. Both briefs say so explicitly.

### Compatible-bed counts across patients awaiting a bed

All four matching rules applied (availability, specialty, isolation, single-sex), against **deduplicated** beds.

| Compatible beds | Patients |
| ---: | ---: |
| 0 | 3 |
| 1 | 18 |
| 4 | 39 |
| 5 | 5 |

The counts cluster because availability is a ward-level property: every Acute Medicine patient sees the same AMU stock. The spread comes from the single-sex split in General Surgery, the tight Cardiology ward, and the isolation rule.

A tail of **3 patients with zero matches** is intentional — one ward has no available side room, so sepsis and fever patients in that specialty have nowhere compatible to go. The empty state is worth showing. It should be a minority, not everyone.

## Deliberate defects

| Ref | Defect | Where |
| --- | --- | --- |
| D1 | Duplicate primary keys | patients, rtt_pathways, hospital_beds |
| D2 | Null primary keys | patients, rtt_pathways, hospital_beds |
| D3 | Mixed date formats (ISO + `DD/MM/YYYY`) | all date and timestamp columns |
| D4 | Whitespace on join keys | patient_id, clinic_id, attendance_id |
| D5 | Categorical casing drift (`Male`/`male`/`M`) | gender, patient_sex |
| D6 | Blank optional categoricals | risk_category |
| D7 | Stale precomputed `is_breached` | rtt_pathways, ed_attendance_events |
| D8 | Shuffled CDC event stream | ed_attendance_events |
| D9 | Status casing drift (`Available`/`AVAILABLE`) | hospital_beds.status |

Duplicate rows are byte-identical copies, so which one survives does not change any downstream figure. Day 5 question 6 is phrased accordingly.

Run with `--clean` to regenerate the same data with every defect suppressed, for building or checking a reference solution.
