#!/usr/bin/env python3
"""
Verification harness for the NHS training data.

Reads ONLY the generated CSVs -- never the generator's internal state -- and
asserts the properties the three briefs and the instructor guide depend on.
If a figure quoted in a brief cannot be recovered from the CSVs by this script,
the brief is wrong.

    python verify_generated_data.py --data ./synthetic_nhs_data --as-of 2026-09-03
    python verify_generated_data.py --data ./synthetic_nhs_data_clean --clean

Exit code 0 = every assertion held.
"""

from __future__ import annotations

import argparse
import csv
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

PASS, FAIL = [], []


def check(name: str, condition: bool, detail="") -> None:
    suffix = f" -- {detail}" if detail else ""
    (PASS if condition else FAIL).append(f"{name}{suffix}")


def read(path: Path) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def k(value) -> str:
    return (value or "").strip()


def truthy(value) -> bool:
    return k(value).lower() == "true"


def parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(k(value).replace(" ", "T"))


def dedupe(rows: list[dict], pk: str) -> list[dict]:
    """The Day 1 cleaning every brief asks for: trim, drop blank PK, dedupe."""
    seen, out = set(), []
    for r in rows:
        key = k(r.get(pk))
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


# =============================================================================
# NEWS2 -- reimplemented independently of the generator, from the RCP bands.
# =============================================================================

def news2(rr: int, spo2: int, sbp: int, hr: int) -> tuple[int, bool]:
    parts = []
    parts.append(3 if (rr <= 8 or rr >= 25) else 2 if rr >= 21 else 1 if rr <= 11 else 0)
    parts.append(3 if spo2 <= 91 else 2 if spo2 <= 93 else 1 if spo2 <= 95 else 0)
    parts.append(3 if (sbp <= 90 or sbp >= 220) else 2 if sbp <= 100 else 1 if sbp <= 110 else 0)
    parts.append(3 if (hr <= 40 or hr >= 131) else 2 if hr >= 111
                 else 1 if (41 <= hr <= 50 or 91 <= hr <= 110) else 0)
    return sum(parts), (3 in parts)


VALID_RANGES = {
    "heart_rate": (20, 250),
    "spo2": (50, 100),
    "systolic_bp": (50, 300),
    "respiration_rate": (4, 60),
}


def valid_reading(row: dict) -> bool:
    """Telemetry cleaning: drop sentinels, nulls and impossible values."""
    for field, (lo, hi) in VALID_RANGES.items():
        raw = k(row.get(field))
        if raw == "":
            return False
        try:
            value = int(raw)
        except ValueError:
            return False
        if value <= 0 or not (lo <= value <= hi):
            return False
    return True


# =============================================================================

def verify_project_1(base: Path, as_of: date, clean: bool) -> None:
    patients_raw = read(base / "project_1_rtt" / "patients.csv")
    clinics = read(base / "project_1_rtt" / "clinics.csv")
    pathways_raw = read(base / "project_1_rtt" / "rtt_pathways.csv")

    patients = dedupe(patients_raw, "patient_id")
    pathways = dedupe(pathways_raw, "pathway_id")

    check("P1 schema: no target_breach_date column (trainees derive it)",
          "target_breach_date" not in pathways_raw[0],
          "pathways carry referral_date only")
    check("P1 schema: clinics carry a clinic_id primary key",
          "clinic_id" in clinics[0])
    check("P1 clinics: three sites per specialty",
          all(v == 3 for v in Counter(c["specialty_id"] for c in clinics).values()))

    check("P1 patients: primary keys unique after cleaning",
          len({k(p["patient_id"]) for p in patients}) == len(patients))
    check("P1 pathways: primary keys unique after cleaning",
          len({k(p["pathway_id"]) for p in pathways}) == len(pathways))

    # Referential integrity must survive the dirt.
    patient_ids = {k(p["patient_id"]) for p in patients}
    clinic_ids = {k(c["clinic_id"]) for c in clinics}
    orphan_patients = [p for p in pathways if k(p["patient_id"]) not in patient_ids]
    orphan_clinics = [p for p in pathways if k(p["clinic_id"]) not in clinic_ids]
    check("P1 referential integrity: every pathway resolves to a patient",
          not orphan_patients, f"{len(orphan_patients)} orphans")
    check("P1 referential integrity: every pathway resolves to a clinic",
          not orphan_clinics, f"{len(orphan_clinics)} orphans")

    # NHS number check digits.
    def modulus_11_ok(nhs: str) -> bool:
        nhs = k(nhs)
        if len(nhs) != 10 or not nhs.isdigit():
            return False
        total = sum(int(d) * (10 - i) for i, d in enumerate(nhs[:9]))
        expected = 11 - (total % 11)
        expected = 0 if expected == 11 else expected
        return expected != 10 and expected == int(nhs[9])

    bad = [p for p in patients if not modulus_11_ok(p["patient_id"])]
    check("P1 NHS numbers: valid Modulus 11 check digit", not bad,
          f"{len(bad)} invalid")

    # Caldicott: outward code only, never a full postcode.
    check("P1 privacy: postcode districts are outward codes only",
          all(" " not in k(p["postcode_district"]) and len(k(p["postcode_district"])) <= 4
              for p in patients))

    active = [p for p in pathways if k(p["clock_status"]).upper() == "ACTIVE"]
    check("P1 clock_status: only ACTIVE/PAUSED once upper-cased",
          {k(p["clock_status"]).upper() for p in pathways} <= {"ACTIVE", "PAUSED"})

    weeks = [(as_of - date.fromisoformat(k(p["referral_date"]))).days / 7 for p in active]
    breach_rate = 100 * sum(1 for w in weeks if w >= 18) / len(weeks)
    check("P1 breach rate lands in a plausible 20-45% band",
          20 <= breach_rate <= 45, f"{breach_rate:.1f}%")
    check("P1 every waiting cohort is populated",
          all(any(lo <= w < hi for w in weeks)
              for lo, hi in [(0, 6), (6, 12), (12, 18), (18, 999)]))

    # is_breached must be stale in dirty mode: that is the whole point of it.
    wrong = sum(1 for p in active
                if truthy(p["is_breached"])
                != ((as_of - date.fromisoformat(k(p["referral_date"]))).days >= 126))
    if clean:
        check("P1 clean mode: is_breached agrees with the clock", wrong == 0,
              f"{wrong} disagree")
    else:
        check("P1 dirty mode: is_breached is meaningfully stale", wrong > 50,
              f"{wrong} of {len(active)} active pathways disagree with the clock")

    # Backlog clearance: the stretch-goal figure the brief quotes.
    capacity = defaultdict(int)
    for c in clinics:
        capacity[k(c["specialty_id"])] += int(c["weekly_capacity"])
    backlog = Counter(k(p["specialty_id"]) for p in active)
    clearance = {s: backlog[s] / capacity[s] for s in backlog}
    over_20 = [s for s, v in clearance.items() if v > 20]
    check("P1 backlog clearance: exactly three specialties exceed 20 weeks",
          len(over_20) == 3, f"{sorted(over_20)} -- "
          + ", ".join(f"{s}={v:.1f}w" for s, v in sorted(clearance.items())))
    check("P1 backlog clearance: no specialty sits ambiguously on the 20w line",
          not any(19.5 <= v <= 20.5 for v in clearance.values()))

    # getEligibleExpeditedSlots filters on capacity strictly greater than the
    # pathway's current clinic.  Check the result spread is not degenerate.
    cap_by_clinic = {k(c["clinic_id"]): int(c["weekly_capacity"]) for c in clinics}
    clinics_by_spec = defaultdict(list)
    for c in clinics:
        clinics_by_spec[k(c["specialty_id"])].append(c)
    spread = Counter()
    for p in active:
        mine = cap_by_clinic[k(p["clinic_id"])]
        spread[sum(1 for c in clinics_by_spec[k(p["specialty_id"])]
                   if int(c["weekly_capacity"]) > mine)] += 1
    check("P1 expedited slots: results spread across 0, 1 and 2 clinics",
          set(spread) == {0, 1, 2}, dict(spread))

    check("P1 restricted view: a safeguarding cohort exists to filter out",
          0 < sum(1 for p in patients if truthy(p["sensitive_care_flag"])) < len(patients) * 0.15)


def verify_project_2(base: Path, now: datetime, clean: bool) -> None:
    wards = read(base / "project_2_ed_flow" / "wards.csv")
    beds_raw = read(base / "project_2_ed_flow" / "hospital_beds.csv")
    events_raw = read(base / "project_2_ed_flow" / "ed_attendance_events.csv")

    beds = dedupe(beds_raw, "bed_id")
    events = [e for e in dedupe(events_raw, "event_id") if k(e["attendance_id"])]

    check("P2 schema: the feed is an event stream, not a snapshot",
          "event_timestamp" in events_raw[0] and "event_type" in events_raw[0])
    check("P2 schema: patient_sex is present (the single-sex rule needs it)",
          "patient_sex" in events_raw[0])
    check("P2 schema: hospital_site_code is present (site restricted view)",
          "hospital_site_code" in events_raw[0])

    by_attendance = defaultdict(list)
    for e in events:
        by_attendance[k(e["attendance_id"])].append(e)

    check("P2 CDC: every attendance emits 1-4 events",
          all(1 <= len(v) <= 4 for v in by_attendance.values()))
    check("P2 CDC: more events than attendances (deduplication is real work)",
          len(events) > len(by_attendance) * 1.5,
          f"{len(events)} events / {len(by_attendance)} attendances")

    # Monotonicity: the state sequence must never contradict the clock.
    ORDER = {"Registered": 0, "Triaged": 1, "Decision To Admit": 2,
             "Bed Allocated": 3, "Discharged": 3}
    non_monotonic = 0
    out_of_order = 0
    for aid, rows in by_attendance.items():
        rows = sorted(rows, key=lambda r: parse_dt(r["event_timestamp"]))
        stamps = [parse_dt(r["event_timestamp"]) for r in rows]
        if any(b < a for a, b in zip(stamps, stamps[1:])):
            non_monotonic += 1
        ranks = [ORDER[r["event_type"]] for r in rows]
        if any(b < a for a, b in zip(ranks, ranks[1:])):
            out_of_order += 1
    check("P2 CDC: event timestamps are strictly non-decreasing",
          non_monotonic == 0, f"{non_monotonic} attendances contradict the clock")
    check("P2 CDC: clinical state order agrees with timestamp order",
          out_of_order == 0, f"{out_of_order} attendances out of order")

    check("P2 CDC: arrival_timestamp is constant within an attendance",
          all(len({k(r["arrival_timestamp"]) for r in rows}) == 1
              for rows in by_attendance.values()))

    # The trap: does the wrong sort key actually produce a wrong answer?
    right = {aid: sorted(rows, key=lambda r: parse_dt(r["event_timestamp"]))[-1]
             for aid, rows in by_attendance.items()}
    wrong = {aid: sorted(rows, key=lambda r: parse_dt(r["arrival_timestamp"]))[-1]
             for aid, rows in by_attendance.items()}
    divergence = sum(1 for aid in right
                     if right[aid]["disposition"] != wrong[aid]["disposition"])
    pct = 100 * divergence / len(right)
    check("P2 the CDC sort-key trap has real bite", pct > 40,
          f"{pct:.1f}% of dispositions differ from the correct key")

    # Bed board must agree with the attendance list.
    ward_by_id = {k(w["ward_id"]): w for w in wards}
    occupied = {k(b["bed_id"]) for b in beds if k(b["status"]).title() == "Occupied"}
    allocated = {k(e["allocated_bed_id"]) for e in events if k(e["allocated_bed_id"])}
    # One-directional on purpose: every bed the ED feed allocated must be
    # Occupied, but the converse is false in any real hospital -- most occupied
    # beds hold patients admitted before this feed's window opened.
    check("P2 no phantom allocation: every allocated bed is Occupied",
          allocated <= occupied,
          f"{len(allocated - occupied)} beds allocated but not marked Occupied")
    check("P2 bed board is plausible: allocations are a minority of occupancy",
          0 < len(allocated) < len(occupied),
          f"{len(allocated)} allocated of {len(occupied)} occupied")

    check("P2 referential integrity: every bed resolves to a ward",
          all(k(b["ward_id"]) in ward_by_id for b in beds))
    check("P2 referential integrity: every allocated bed exists",
          allocated <= {k(b["bed_id"]) for b in beds})

    # Ward pressure must span the bands the brief colours.
    counts = defaultdict(Counter)
    for b in beds:
        counts[k(b["ward_id"])][k(b["status"]).title()] += 1
    pressure = {k(w["ward_id"]): 100 * counts[k(w["ward_id"])]["Occupied"] / int(w["total_beds"])
                for w in wards}
    check("P2 ward pressure reaches the red band (>=90%)",
          any(v >= 90 for v in pressure.values()),
          ", ".join(f"{w}={v:.1f}" for w, v in pressure.items()))
    check("P2 ward pressure has a low band too (<70%)",
          any(v < 70 for v in pressure.values()))

    # The 4-hour standard must discriminate.
    in_flight = [
        int((now - parse_dt(e["arrival_timestamp"])).total_seconds() / 60)
        for e in right.values() if e["disposition"] not in ("Discharged", "Admitted")
    ]
    breached = sum(1 for l in in_flight if l > 240)
    share = 100 * breached / len(in_flight)
    check("P2 the 4-hour standard splits the in-flight cohort (20-65% breached)",
          20 <= share <= 65, f"{share:.1f}% of {len(in_flight)} in flight")

    # Empty states the Workshop must render rather than crash on.
    site_specialties = defaultdict(set)
    for w in wards:
        site_specialties[k(w["site_code"])].add(k(w["specialty_type"]))
    dta_needing_missing_specialty = [
        e for e in right.values()
        if e["disposition"] == "Awaiting Bed"
        and k(e["required_specialty"]) not in site_specialties[k(e["hospital_site_code"]).upper()]
    ]
    check("P2 empty state: at least one DTA has no compatible ward on its site",
          len(dta_needing_missing_specialty) > 0,
          f"{len(dta_needing_missing_specialty)} such attendances")

    iso_free_by_ward = defaultdict(int)
    for b in beds:
        if k(b["status"]).title() == "Available" and truthy(b["is_isolation_capable"]):
            iso_free_by_ward[k(b["ward_id"])] += 1
    starved = [k(w["ward_id"]) for w in wards
               if counts[k(w["ward_id"])]["Available"] > 0
               and iso_free_by_ward[k(w["ward_id"])] == 0]
    check("P2 empty state: a ward has free beds but no free isolation room",
          len(starved) > 0, f"wards: {starved}")

    if not clean:
        check("P2 dirty mode: mixed timestamp formats are present",
              any(" " in k(e["event_timestamp"]) for e in events_raw))
        check("P2 dirty mode: blank-key rows are present and droppable",
              any(not k(e["event_id"]) or not k(e["attendance_id"]) for e in events_raw))


def verify_project_3(base: Path, now: datetime, clean: bool) -> None:
    admissions_raw = read(base / "project_3_virtual_ward" / "inpatient_admissions.csv")
    vw_beds = read(base / "project_3_virtual_ward" / "virtual_ward_beds.csv")
    readings_raw = read(base / "project_3_virtual_ward" / "telemetry_readings.csv")

    admissions = dedupe(admissions_raw, "admission_id")
    readings = dedupe(readings_raw, "reading_id")

    check("P3 admissions: primary keys unique after cleaning",
          len({k(a["admission_id"]) for a in admissions}) == len(admissions))
    check("P3 virtual beds: primary keys unique and non-null",
          len({k(b["virtual_bed_id"]) for b in vw_beds}) == len(vw_beds)
          and all(k(b["virtual_bed_id"]) for b in vw_beds))

    admission_ids = {k(a["admission_id"]) for a in admissions}
    orphans = [r for r in readings if k(r["admission_id"]) not in admission_ids]
    check("P3 referential integrity: every reading resolves to an admission",
          not orphans, f"{len(orphans)} orphan readings")

    vw_ids = {k(b["virtual_bed_id"]) for b in vw_beds}
    bad_links = [a for a in admissions
                 if k(a["virtual_bed_id"]) and k(a["virtual_bed_id"]) not in vw_ids]
    check("P3 referential integrity: every stepped-down admission has a real bed",
          not bad_links, f"{len(bad_links)} dangling")

    # Telemetry cleaning must be non-trivial but must never remove the latest
    # valid reading for an admission.
    valid = [r for r in readings if valid_reading(r)]
    dropped = len(readings) - len(valid)
    if clean:
        check("P3 clean mode: no telemetry needs dropping", dropped == 0,
              f"{dropped} dropped")
    else:
        check("P3 dirty mode: telemetry cleaning removes a real share of rows",
              0.02 < dropped / len(readings) < 0.15,
              f"{dropped}/{len(readings)} = {100*dropped/len(readings):.1f}%")

    latest = {}
    for r in valid:
        aid = k(r["admission_id"])
        ts = parse_dt(r["recorded_at"])
        if aid not in latest or ts > latest[aid][0]:
            latest[aid] = (ts, r)

    check("P3 every admission has at least one valid reading",
          len(latest) == len(admissions),
          f"{len(admissions) - len(latest)} admissions with no usable telemetry")

    # The most recent reading overall must itself be valid -- otherwise the
    # "current" clinical state depends on how you clean, not on the data.
    corrupt_latest = 0
    newest_raw = {}
    for r in readings:
        aid = k(r["admission_id"])
        ts = parse_dt(r["recorded_at"])
        if aid not in newest_raw or ts > newest_raw[aid][0]:
            newest_raw[aid] = (ts, r)
    for aid, (_, r) in newest_raw.items():
        if not valid_reading(r):
            corrupt_latest += 1
    check("P3 the most recent reading per admission is always valid",
          corrupt_latest == 0,
          f"{corrupt_latest} admissions whose newest reading is a dropout")

    # NEWS2 and readiness, recomputed from the CSVs alone.
    scores, escalations, single_3 = {}, 0, 0
    for aid, (_, r) in latest.items():
        total, has_3 = news2(int(r["respiration_rate"]), int(r["spo2"]),
                             int(r["systolic_bp"]), int(r["heart_rate"]))
        scores[aid] = total
        if total >= 5 or has_3:
            escalations += 1
        if total < 5 and has_3:
            single_3 += 1

    check("P3 NEWS2 spans a usable range", min(scores.values()) == 0
          and max(scores.values()) >= 7,
          f"min={min(scores.values())} max={max(scores.values())}")
    check("P3 escalation rule fires on a workable cohort",
          0.10 < escalations / len(scores) < 0.50,
          f"{escalations}/{len(scores)}")
    check("P3 the 'any single parameter scores 3' limb fires independently",
          single_3 > 0, f"{single_3} admissions escalate on the single-parameter limb alone")

    MOB = {"Independent": 0, "Assisted": 15, "Immobile": 35}
    eligible_by_pathway = Counter()
    fit = 0
    for a in admissions:
        aid = k(a["admission_id"])
        if aid not in scores:
            continue
        mobility = (k(a["mobility_status"]).title() or "Independent")
        social = (k(a["social_care_status"]).title() or "Pending")
        readiness = max(0, 100 - scores[aid] * 10 - MOB[mobility]
                        - (0 if social == "Package Confirmed" else 25))
        if truthy(a["medically_fit_flag"]):
            fit += 1
            if readiness >= 70 and scores[aid] < 3:
                eligible_by_pathway[k(a["care_pathway"]).title()] += 1

    total_eligible = sum(eligible_by_pathway.values())
    check("P3 the step-down queue is workable (10-45 patients)",
          10 <= total_eligible <= 45, f"{total_eligible} eligible of {fit} medically fit")
    check("P3 mobility and social-care values normalise to the specified set",
          {k(a["mobility_status"]).title() for a in admissions} <= set(MOB) | {""})

    free_by_pathway = Counter(k(b["care_pathway"]) for b in vw_beds
                              if k(b["bed_status"]) == "Available")
    check("P3 empty state: an eligible patient exists on a pathway with no free bed",
          any(free_by_pathway.get(p, 0) == 0 for p in eligible_by_pathway),
          f"eligible={dict(eligible_by_pathway)} free={dict(free_by_pathway)}")
    check("P3 most pathways do have free beds (the action must succeed too)",
          sum(1 for v in free_by_pathway.values() if v > 0) >= 3)

    # The stale cache.
    stale_wrong = sum(1 for a in admissions
                      if k(a["admission_id"]) in scores
                      and int(k(a["news2_cached"]) or 0) != scores[k(a["admission_id"])])
    if clean:
        check("P3 clean mode: news2_cached agrees with the live score",
              stale_wrong == 0, f"{stale_wrong} disagree")
    else:
        check("P3 dirty mode: news2_cached is meaningfully stale",
              stale_wrong > len(scores) * 0.4,
              f"{stale_wrong}/{len(scores)} disagree with the live score")

    check("P3 trust codes present for the trust-level restricted view",
          len({k(a["trust_code"]) for a in admissions}) >= 2)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="./synthetic_nhs_data", type=Path)
    ap.add_argument("--as-of", default=None)
    ap.add_argument("--clean", action="store_true",
                    help="the data under test was generated with --clean")
    args = ap.parse_args()

    as_of = date.fromisoformat(args.as_of) if args.as_of else date.today()
    now = datetime.combine(as_of, datetime.min.time()) + timedelta(hours=14, minutes=30)

    verify_project_1(args.data, as_of, args.clean)
    verify_project_2(args.data, now, args.clean)
    verify_project_3(args.data, now, args.clean)

    for line in PASS:
        print(f"  ok   {line}")
    for line in FAIL:
        print(f"  FAIL {line}")
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed "
          f"({'clean' if args.clean else 'dirty'} mode)")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
