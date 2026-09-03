# Generation report (instructor key)

- Generated: 2026-09-03T11:42:47
- As-of date: 2026-09-03  (clock anchor 2026-09-03T14:30)
- Seed: 20260903
- Mode: DIRTY (defects injected)

Every figure below is computed on **cleaned** rows -- trimmed keys, blank
keys dropped, duplicates removed -- i.e. what a correct Day 1 pipeline
produces. Raw row counts are labelled `raw_`.

## Project 1 -- Elective care RTT

- **raw_pathway_rows**: 3057
- **clean_pathways**: 3000
- **active_pathways**: 2549
- **breached_over_18w**: 797
- **breach_rate_pct**: 31.3
- **cohorts**:
    - 6-12w: 1052
    - 12-18w: 360
    - >18w: 797
    - <6w: 340
- **stale_is_breached_wrong**: 154
- **clearance_weeks_by_specialty**:
    - TFC-100: 20.9
    - TFC-110: 9.3
    - TFC-120: 26.5
    - TFC-130: 4.7
    - TFC-300: 7.1
    - TFC-320: 23.7
    - TFC-502: 12.1
- **specialties_over_20_weeks**: ['TFC-100', 'TFC-120', 'TFC-320']
- **raw_patient_rows**: 900
- **unvalidated_active**: 1566

## Project 2 -- ED flow and bed allocation

- **raw_event_rows**: 1317
- **clean_event_rows**: 1282
- **distinct_attendances**: 420
- **dispositions**:
    - Admitted: 105
    - Under Assessment: 35
    - Discharged: 187
    - Awaiting Bed: 69
    - Awaiting Triage: 24
- **attendances_wrong_if_sorted_on_arrival**: 264
- **pct_wrong_if_sorted_on_arrival**: 62.9
- **in_flight_median_los_minutes**: 189
- **in_flight_over_4_hours**: 58
- **in_flight_total**: 128
- **available_beds**: 24
- **ward_pressure_pct**:
    - WARD-AMU-1: 87.5
    - WARD-TRAUMA-1: 65.0
    - WARD-SURG-M: 66.7
    - WARD-SURG-F: 77.8
    - WARD-CARD-1: 91.7
    - WARD-AMU-2: 70.0
    - WARD-TRAUMA-2: 68.8
    - WARD-SURG-2: 75.0
- **awaiting_bed**: 69

## Project 3 -- Discharge and virtual ward

- **raw_admission_rows**: 231
- **clean_admissions**: 220
- **medically_fit**: 90
- **step_down_eligible**: 27
- **eligible_by_care_pathway**:
    - Post-Surgical: 9
    - Frailty: 6
    - Respiratory: 8
    - Cardiac: 4
- **virtual_beds_available_by_pathway**:
    - Frailty: 5
    - Respiratory: 4
    - Cardiac: 3
    - Post-Surgical: 0
- **eligible_but_no_bed_available**:
    - Post-Surgical: 9
- **news2_escalation_total**: 59
- **news2_below_5_with_a_single_3**: 5
- **stale_news2_cached_wrong**: 174
- **telemetry_rows**: 21288
- **median_readiness**: 40

## Deliberate defects injected

- D1 duplicate admission rows: 3
- D1 duplicate bed rows: 1
- D1 duplicate event rows: 25
- D1 duplicate pathway rows: 45
- D1 duplicate patient rows: 9
- D1 duplicate telemetry rows: 168
- D2 blank admission_id: 8
- D2 blank event_id/attendance_id: 10
- D2 blank pathway_id: 12
- D3 padded attendance_id: 18
- D3 padded patient_id (pathways): 65
- D4 bed status casing: 5
- D4 care_pathway casing: 9
- D4 clock_status casing: 181
- D4 hospital_site_code casing: 58
- D4 mobility_status casing: 19
- D4 postcode_district casing: 48
- D5 null chief_complaint: 27
- D5 null patient_sex: 27
- D5 null priority_band: 87
- D5 null risk_category: 39
- D5 null sex (patients): 20
- D5 null social_care_status: 7
- D7 telemetry sensor dropout: 714
- D8 impossible telemetry value: 313
- D9 space-separated event_timestamp: 102

## Marking notes

1. **Project 2 CDC sort key.** Sorting on `arrival_timestamp` rather than
   `event_timestamp` yields a green pipeline with the correct row count and
   62.9% of dispositions
   wrong. It is invisible from the UI. Check the sort key directly.
2. **Stale flags.** `is_breached` (Project 1 and 2) and `news2_cached`
   (Project 3) are stale on purpose. A trainee who carries them through
   rather than recomputing gets a plausible-looking wrong answer.
3. **Empty states are real.** Cardiology at RJ612 has no compatible ward;
   WARD-AMU-2 has no free isolation room; the Post-Surgical virtual ward
   has zero available beds. All three must render, not crash.
4. **Telemetry cleaning is exclusion, not smoothing.** The most recent
   reading per admission is always valid. Dropouts (0, -1, null) and
   impossible values must be dropped before taking the latest reading.
   Smoothing vitals with a rolling median would mask real deterioration.
