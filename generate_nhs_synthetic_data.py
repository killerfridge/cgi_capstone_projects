#!/usr/bin/env python3
"""
NHS synthetic data generator for the Palantir Foundry training capstones
========================================================================

Produces the CSV inputs for all three trainee projects:

    project_1_rtt/            patients.csv, clinics.csv, rtt_pathways.csv
    project_2_ed_flow/        wards.csv, hospital_beds.csv, ed_attendance_events.csv
    project_3_virtual_ward/   inpatient_admissions.csv, virtual_ward_beds.csv,
                              telemetry_readings.csv

Design rules
------------
1.  The data is DIRTY BY DEFAULT.  Day 1 of every brief is a cleaning day; if the
    data is spotless, Day 1 is a no-op and the trainees learn nothing.  Every
    defect is deliberate, catalogued (D1-D10 below) and reported in
    GENERATION_REPORT.md, which is the instructor key.

2.  Defects NEVER break referential integrity.  Null and blank primary keys are
    only injected on rows that nothing else points at.  A bed that an attendance
    references is never given a null key; an admission that has telemetry is
    never given a null key.

3.  Every headline figure in the report is computed on CLEANED rows -- the same
    cleaning the briefs ask for -- so the numbers in the briefs match what a
    correct pipeline actually produces.  Raw counts are labelled separately.

4.  --clean suppresses every defect.  Use it to build the reference solution and
    to prove that a figure comes from the data rather than from the dirt.

Deliberate defects
------------------
    D1  Byte-identical duplicate rows (all three projects).
    D2  Null / blank primary keys on unreferenced rows.
    D3  Whitespace-padded and mixed-case foreign and primary keys.
    D4  Inconsistent categorical casing ("ACTIVE"/"active"/"Active").
    D5  Null categoricals that must be defaulted, not dropped.
    D6  Stale precomputed flags (is_breached, news2_cached) -- the temporal
        staleness trap.  These are WRONG on purpose and must be recomputed.
    D7  Telemetry sensor dropouts: sentinel values (0, -1) and nulls.
    D8  Telemetry physiologically impossible values (HR 300, SpO2 130).
    D9  Mixed timestamp formats in the ED event stream (ISO 'T' vs space).
    D10 Out-of-order CDC event rows -- the file is shuffled, so the terminal
        state only emerges if you sort on event_timestamp.

Usage
-----
    python generate_nhs_synthetic_data.py                 # dirty, as of today
    python generate_nhs_synthetic_data.py --clean         # reference build
    python generate_nhs_synthetic_data.py --as-of 2026-09-03 --seed 20260903
"""

from __future__ import annotations

import argparse
import csv
import random
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

# ==============================================================================
# NHS synthetic data compliance
#   1. Synthetic NHS numbers: 10 digits, valid Modulus 11 check digit, '999'
#      test prefix (NHS Digital reserved test range).
#   2. Caldicott: outward-only postcode districts, never full postcodes.
#   3. Standard codes: NHS Treatment Function Codes, Manchester Triage.
#   4. Real standards: 18-week RTT, 4-hour ED access, NEWS2 (RCP 2017).
# ==============================================================================

# ------------------------------------------------------------------ volumes --
N_PATIENTS = 900
N_PATHWAYS = 3000
BREACHED_SHARE = 0.32          # share of pathways referred more than 126 days ago
# The ED feed is a rolling operational window, not a day's history.  Keep it
# short: if it stretches too far back, every in-flight patient is hours past the
# 4-hour standard and the standard stops discriminating.  Target: in-flight
# median length of stay a little under 240 minutes, roughly half breached.
ED_WINDOW_HOURS = 18
N_ATTENDANCES = 420
N_ADMISSIONS = 220
TELEMETRY_WINDOW_HOURS = 48
TELEMETRY_INTERVAL_MINUTES = 30

# ------------------------------------------------------------- reference data --

POSTCODE_DISTRICTS = [
    "SW1A", "SE1", "RH7", "RH10", "CR0", "CR8",
    "TN8", "TN16", "E1", "NW1", "BR1", "KT1",
]

SITES = [
    {"site_code": "RJ611", "site_name": "St Aldate's General Hospital"},
    {"site_code": "RJ612", "site_name": "Northgate Community Hospital"},
]

TRUSTS = [
    {"trust_code": "RJ6", "trust_name": "South Downs Acute NHS Trust"},
    {"trust_code": "RTK", "trust_name": "Weald Valley NHS Foundation Trust"},
]

# Treatment Function Codes.  weekly_capacity is per CLINIC (i.e. per site), and
# the three site capacities per specialty are tuned so that backlog clearance
# spans roughly 8-30 weeks with exactly three specialties over 20 weeks.
SPECIALTIES = [
    {"specialty_id": "TFC-100", "specialty_name": "General Surgery",
     "site_capacity": [8, 6, 3], "lead": "CONS-1042"},
    {"specialty_id": "TFC-110", "specialty_name": "Trauma and Orthopaedics",
     "site_capacity": [17, 13, 10], "lead": "CONS-2190"},
    {"specialty_id": "TFC-120", "specialty_name": "ENT",
     "site_capacity": [6, 5, 3], "lead": "CONS-0881"},
    {"specialty_id": "TFC-130", "specialty_name": "Ophthalmology",
     "site_capacity": [30, 25, 17], "lead": "CONS-4412"},
    {"specialty_id": "TFC-300", "specialty_name": "General Internal Medicine",
     "site_capacity": [22, 17, 12], "lead": "CONS-3098"},
    {"specialty_id": "TFC-320", "specialty_name": "Cardiology",
     "site_capacity": [7, 5, 3], "lead": "CONS-1554"},
    {"specialty_id": "TFC-502", "specialty_name": "Gynaecology",
     "site_capacity": [14, 11, 8], "lead": "CONS-6721"},
]

CLINIC_SITES = [
    ("A", "Main Theatres"),
    ("B", "Day Surgery Unit"),
    ("C", "Community Diagnostic Hub"),
]

# Wards.  Note RJ612 has no Cardiology ward: a Cardiology decision-to-admit at
# Northgate has NO compatible bed anywhere on its own site.  That empty state is
# deliberate -- the trainee must render it rather than crash on it.
WARDS = [
    {"ward_id": "WARD-AMU-1",    "ward_name": "Acute Medical Unit",
     "site_code": "RJ611", "gender_policy": "Mixed",
     "specialty_type": "Acute Medicine", "total_beds": 24, "available": 3},
    {"ward_id": "WARD-TRAUMA-1", "ward_name": "Trauma and Orthopaedic Ward",
     "site_code": "RJ611", "gender_policy": "Mixed",
     "specialty_type": "Trauma and Orthopaedics", "total_beds": 20, "available": 5},
    {"ward_id": "WARD-SURG-M",   "ward_name": "Surgical Ward (Male)",
     "site_code": "RJ611", "gender_policy": "Male",
     "specialty_type": "General Surgery", "total_beds": 18, "available": 4},
    {"ward_id": "WARD-SURG-F",   "ward_name": "Surgical Ward (Female)",
     "site_code": "RJ611", "gender_policy": "Female",
     "specialty_type": "General Surgery", "total_beds": 18, "available": 2},
    {"ward_id": "WARD-CARD-1",   "ward_name": "Coronary Care Unit",
     "site_code": "RJ611", "gender_policy": "Mixed",
     "specialty_type": "Cardiology", "total_beds": 12, "available": 1},
    {"ward_id": "WARD-AMU-2",    "ward_name": "Northgate Assessment Unit",
     "site_code": "RJ612", "gender_policy": "Mixed",
     "specialty_type": "Acute Medicine", "total_beds": 20, "available": 4},
    {"ward_id": "WARD-TRAUMA-2", "ward_name": "Northgate Orthopaedic Ward",
     "site_code": "RJ612", "gender_policy": "Mixed",
     "specialty_type": "Trauma and Orthopaedics", "total_beds": 16, "available": 3},
    {"ward_id": "WARD-SURG-2",   "ward_name": "Northgate Surgical Ward",
     "site_code": "RJ612", "gender_policy": "Mixed",
     "specialty_type": "General Surgery", "total_beds": 16, "available": 2},
]

# Wards whose available beds are deliberately NOT isolation-capable, so that a
# Sepsis/Fever presentation routed there finds a genuinely empty candidate set.
NO_ISOLATION_AVAILABLE_IN = {"WARD-AMU-2"}

# (chief_complaint, required_specialty, likely to need admission)
COMPLAINTS = [
    ("Chest Pain",                  "Cardiology",               True),
    ("Shortness of Breath",         "Acute Medicine",           True),
    ("Suspected Fracture / Fall",   "Trauma and Orthopaedics",  True),
    ("Severe Abdominal Pain",       "General Surgery",          True),
    ("Fever / Sepsis Alert",        "Acute Medicine",           True),
    ("Minor Head Injury",           "Trauma and Orthopaedics",  False),
    ("Laceration / Minor Wound",    "General Surgery",          False),
    ("Sprained Ankle",              "Trauma and Orthopaedics",  False),
]

TRIAGE = [
    ("Immediate (Red)", 0.05),
    ("Very Urgent (Orange)", 0.20),
    ("Urgent (Yellow)", 0.40),
    ("Standard (Green)", 0.25),
    ("Non-Urgent (Blue)", 0.10),
]

CARE_PATHWAYS = ["Frailty", "Respiratory", "Cardiac", "Post-Surgical"]
MOBILITY = ["Independent", "Assisted", "Immobile"]
SOCIAL_CARE = ["Package Confirmed", "Pending", "Unassigned"]

# Virtual ward capacity per care pathway.  Post-Surgical is deliberately given
# zero available beds: recommendVirtualWardBed must return an empty set for a
# ready Post-Surgical patient without erroring.
VW_BEDS_PER_PATHWAY = {
    "Frailty": (14, 5),        # (total, available)
    "Respiratory": (12, 4),
    "Cardiac": (10, 3),
    "Post-Surgical": (8, 0),
}


# ------------------------------------------------------------------ helpers --

def nhs_number(rng: random.Random) -> str:
    """10-digit synthetic NHS number, Modulus 11 check digit, '999' test prefix."""
    while True:
        first_9 = "999" + f"{rng.randint(0, 999999):06d}"
        total = sum(int(d) * (10 - i) for i, d in enumerate(first_9))
        check = 11 - (total % 11)
        if check == 11:
            check = 0
        elif check == 10:
            continue          # check digit 10 is invalid under the NHS standard
        return f"{first_9}{check}"


def weighted(rng: random.Random, pairs):
    return rng.choices([p[0] for p in pairs], weights=[p[1] for p in pairs])[0]


def iso_minutes(dt: datetime) -> str:
    return dt.isoformat(timespec="minutes")


def write_csv(path: Path, fieldnames, rows) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


class Dirt:
    """Applies the deliberate defects, or does nothing in --clean mode.

    Deliberately holds its OWN random stream.  If defect decisions were drawn
    from the main generator's stream, --clean would not produce the same
    hospital with the dirt removed -- it would produce a different hospital,
    because every suppressed decision shifts every subsequent draw.  With a
    separate stream, dirty and clean builds of the same seed describe exactly
    the same patients, beds and events.
    """

    def __init__(self, seed: int, enabled: bool):
        self.rng = random.Random(seed ^ 0x5EED)
        self.enabled = enabled
        self.log = Counter()

    def _hit(self, rate: float) -> bool:
        return self.enabled and self.rng.random() < rate

    def pad_key(self, value: str, rate: float, tag: str) -> str:
        """D3: whitespace-padded key."""
        if self._hit(rate):
            self.log[tag] += 1
            return f"  {value} "
        return value

    def recase(self, value: str, rate: float, tag: str) -> str:
        """D4: inconsistent categorical casing."""
        if self._hit(rate):
            self.log[tag] += 1
            return self.rng.choice([value.upper(), value.lower(), value.title()])
        return value

    def blank(self, value, rate: float, tag: str):
        """D5: null categorical that must be defaulted."""
        if self._hit(rate):
            self.log[tag] += 1
            return ""
        return value

    def duplicate(self, rows: list, rate: float, tag: str) -> list:
        """D1: byte-identical duplicate rows, inserted at random positions."""
        if not self.enabled:
            return rows
        out = list(rows)
        n = max(1, int(len(rows) * rate))
        for _ in range(n):
            src = self.rng.choice(rows)
            out.insert(self.rng.randrange(len(out) + 1), dict(src))
            self.log[tag] += 1
        return out

    def orphan_rows(self, template_fn, count: int, tag: str) -> list:
        """D2: rows with a null/blank primary key.  Nothing references these."""
        if not self.enabled:
            return []
        rows = []
        for i in range(count):
            row = template_fn(i)
            rows.append(row)
            self.log[tag] += 1
        return rows


# =============================================================================
# Project 1 -- Elective care RTT and waiting-list validation
# =============================================================================

def build_project_1(out_dir: Path, rng: random.Random, dirt: Dirt, as_of: date):
    # -- patients -------------------------------------------------------------
    patients = []
    used_nhs_numbers = set()
    for _ in range(N_PATIENTS):
        dob = as_of - timedelta(days=rng.randint(18 * 365, 92 * 365))
        # The NHS number is the primary key, so collisions must be excluded at
        # source: a duplicate here would be an accidental defect, and the whole
        # point of D1-D10 is that every defect is deliberate.
        while True:
            candidate = nhs_number(rng)
            if candidate not in used_nhs_numbers:
                used_nhs_numbers.add(candidate)
                break
        patients.append({
            "patient_id": candidate,
            "postcode_district": rng.choice(POSTCODE_DISTRICTS),
            "dob": dob.isoformat(),
            "sex": weighted(rng, [("Male", 0.49), ("Female", 0.51)]),
            "risk_category": weighted(rng, [("Low", 0.60), ("Medium", 0.30), ("High", 0.10)]),
            # Safeguarding pathways: the row-level filter target for the
            # restricted view exercise.
            "sensitive_care_flag": rng.random() < 0.04,
        })

    # -- clinics (three sites per specialty) ----------------------------------
    clinics = []
    for spec in SPECIALTIES:
        for idx, (suffix, site_label) in enumerate(CLINIC_SITES):
            clinics.append({
                "clinic_id": f"CLIN-{spec['specialty_id'].split('-')[1]}-{suffix}",
                "specialty_id": spec["specialty_id"],
                "specialty_name": spec["specialty_name"],
                "clinic_name": f"{spec['specialty_name']} - {site_label}",
                "weekly_capacity": spec["site_capacity"][idx],
                "lead_consultant_code": spec["lead"],
            })

    clinics_by_specialty = defaultdict(list)
    for c in clinics:
        clinics_by_specialty[c["specialty_id"]].append(c)

    # -- pathways -------------------------------------------------------------
    pathways = []
    for i in range(N_PATHWAYS):
        patient = rng.choice(patients)
        spec = rng.choice(SPECIALTIES)
        clinic = rng.choice(clinics_by_specialty[spec["specialty_id"]])

        # Waiting time is drawn in two explicit bands so the headline breach
        # rate is a tuning knob rather than an emergent surprise.  Roughly a
        # third of the list sits past the 18-week (126-day) standard, which is
        # the order of magnitude published NHS RTT statistics show.
        if rng.random() < BREACHED_SHARE:
            days_ago = int(rng.triangular(126, 400, 150))
        else:
            days_ago = int(rng.triangular(7, 125, 55))
        referral_date = as_of - timedelta(days=days_ago)

        priority = weighted(rng, [("P1", 0.03), ("P2", 0.15), ("P3", 0.34), ("P4", 0.48)])
        clock_status = "ACTIVE" if rng.random() > 0.15 else "PAUSED"

        # D6: is_breached is computed at a STALE snapshot date, deliberately.
        # A pathway that has crossed 18 weeks since the snapshot still reads
        # False.  Trainees must recompute; the brief says so.
        stale_days = rng.randint(20, 70)   # drawn unconditionally: see Dirt
        snapshot = as_of - timedelta(days=stale_days if dirt.enabled else 0)
        stale_breached = snapshot > (referral_date + timedelta(days=126))

        row = {
            "pathway_id": f"RTT-2026-{i + 1:05d}",
            "patient_id": patient["patient_id"],
            "clinic_id": clinic["clinic_id"],
            "specialty_id": spec["specialty_id"],
            "referral_date": referral_date.isoformat(),
            "clock_status": clock_status,
            "rtt_status_code": rng.choice(["10", "20"]),
            "priority_band": priority,
            "is_breached": stale_breached,
            "validated_flag": rng.random() < 0.38,
        }
        pathways.append(row)

    # -- defects --------------------------------------------------------------
    for row in pathways:
        row["patient_id"] = dirt.pad_key(row["patient_id"], 0.02, "D3 padded patient_id (pathways)")
        row["clock_status"] = dirt.recase(row["clock_status"], 0.06, "D4 clock_status casing")
        row["priority_band"] = dirt.blank(row["priority_band"], 0.03, "D5 null priority_band")

    for p in patients:
        p["risk_category"] = dirt.blank(p["risk_category"], 0.04, "D5 null risk_category")
        p["sex"] = dirt.blank(p["sex"], 0.02, "D5 null sex (patients)")
        p["postcode_district"] = dirt.recase(p["postcode_district"], 0.05,
                                             "D4 postcode_district casing")

    # D2: unreferenced pathway rows with a blank primary key.
    def blank_pathway(i):
        spec = dirt.rng.choice(SPECIALTIES)
        return {
            "pathway_id": "" if i % 2 == 0 else "   ",
            "patient_id": dirt.rng.choice(patients)["patient_id"],
            "clinic_id": dirt.rng.choice(clinics_by_specialty[spec["specialty_id"]])["clinic_id"],
            "specialty_id": spec["specialty_id"],
            "referral_date": (as_of - timedelta(days=dirt.rng.randint(7, 300))).isoformat(),
            "clock_status": "ACTIVE",
            "rtt_status_code": "10",
            "priority_band": "P4",
            "is_breached": False,
            "validated_flag": False,
        }

    pathways += dirt.orphan_rows(blank_pathway, 12, "D2 blank pathway_id")
    pathways = dirt.duplicate(pathways, 0.015, "D1 duplicate pathway rows")
    patients_out = dirt.duplicate(patients, 0.01, "D1 duplicate patient rows")

    # Shuffle on the dirt stream: these lists differ in LENGTH between clean
    # and dirty mode, and shuffle consumes draws in proportion to length, so
    # using the main stream here would desynchronise the two builds.
    dirt.rng.shuffle(pathways)

    write_csv(out_dir / "patients.csv",
              ["patient_id", "postcode_district", "dob", "sex",
               "risk_category", "sensitive_care_flag"], patients_out)
    write_csv(out_dir / "clinics.csv",
              ["clinic_id", "specialty_id", "specialty_name", "clinic_name",
               "weekly_capacity", "lead_consultant_code"], clinics)
    write_csv(out_dir / "rtt_pathways.csv",
              ["pathway_id", "patient_id", "clinic_id", "specialty_id",
               "referral_date", "clock_status", "rtt_status_code",
               "priority_band", "is_breached", "validated_flag"], pathways)

    return {"patients": patients, "clinics": clinics, "pathways": pathways}


# =============================================================================
# Project 2 -- ED flow and inpatient bed allocation
# =============================================================================

# The CDC event stream.  Each attendance emits 2-4 rows.  The terminal clinical
# state is whichever row has the greatest event_timestamp -- NOT the greatest
# arrival_timestamp, which is constant within an attendance.  Chains are built
# forward from arrival and TRUNCATED at now, never clamped backwards, so the
# terminal state emerges from the truncation by construction.
EVENT_CHAIN = [
    ("Registered", "Awaiting Triage"),
    ("Triaged", "Under Assessment"),
    ("Decision To Admit", "Awaiting Bed"),
    ("Bed Allocated", "Admitted"),
]
DISCHARGE_CHAIN = [
    ("Registered", "Awaiting Triage"),
    ("Triaged", "Under Assessment"),
    ("Discharged", "Discharged"),
]


def build_project_2(out_dir: Path, rng: random.Random, dirt: Dirt, now: datetime):
    wards_by_site = defaultdict(list)
    for w in WARDS:
        wards_by_site[w["site_code"]].append(w)

    # -- beds -----------------------------------------------------------------
    beds = []
    beds_by_ward = defaultdict(list)
    for ward in WARDS:
        n_available = ward["available"]
        # Bed 01 and 02 of each ward are the side rooms (isolation capable).
        statuses = []
        for b in range(1, ward["total_beds"] + 1):
            statuses.append("Occupied")
        # Choose which beds are available.  In wards listed in
        # NO_ISOLATION_AVAILABLE_IN, never free a side room.
        candidates = list(range(1, ward["total_beds"] + 1))
        if ward["ward_id"] in NO_ISOLATION_AVAILABLE_IN:
            # Deliberately starve this ward of free side rooms so that a
            # Sepsis/Fever presentation routed here finds nothing.
            candidates = [b for b in candidates if b > 2]
            free = rng.sample(candidates, n_available)
        else:
            # Guarantee one free side room, otherwise random sampling leaves
            # most wards with no isolation-capable bed available and the
            # infection-control filter never has a success path.
            side_room = rng.choice([1, 2])
            free = [side_room] + rng.sample(
                [b for b in candidates if b != side_room], n_available - 1)
        # A small number of beds are out of service.  Never taken from a ward
        # whose pressure band matters (CARD, AMU-1).
        oos = []
        if ward["ward_id"] not in {"WARD-CARD-1", "WARD-AMU-1"}:
            oos_pool = [b for b in range(1, ward["total_beds"] + 1) if b not in free]
            oos = rng.sample(oos_pool, min(2, len(oos_pool)))

        for b in range(1, ward["total_beds"] + 1):
            if b in free:
                status = "Available"
            elif b in oos:
                status = rng.choice(["Cleaning", "Maintenance"])
            else:
                status = "Occupied"
            row = {
                "bed_id": f"BED-{ward['ward_id'].replace('WARD-', '')}-{b:02d}",
                "ward_id": ward["ward_id"],
                "bed_number": f"{b:02d}",
                "status": status,
                "is_isolation_capable": b <= 2,
            }
            beds.append(row)
            beds_by_ward[ward["ward_id"]].append(row)

    occupied = [b for b in beds if b["status"] == "Occupied"]
    rng.shuffle(occupied)

    # -- attendances ----------------------------------------------------------
    # Bed allocations are drawn FROM the Occupied set, never invented: every
    # allocated_bed_id in the ED feed names a bed whose status is Occupied, and
    # the patient's sex and required specialty are derived from the ward holding
    # that bed, so the single-sex and specialty rules are self-consistent.
    #
    # The converse is deliberately NOT true, because it is not true in a
    # hospital: most occupied beds hold patients admitted days ago, well before
    # this 13-hour ED feed begins.  Ward occupancy is therefore set by the
    # `available` figure in WARDS, which is what makes ward pressure a stable,
    # tunable number rather than a by-product of today's arrivals.
    ward_by_id = {w["ward_id"]: w for w in WARDS}

    attendances = []
    admitted_pool = list(occupied)

    for i in range(N_ATTENDANCES):
        attendance_id = f"ED-2026-{i + 1:05d}"
        mins_ago = rng.randint(5, ED_WINDOW_HOURS * 60)
        arrival = now - timedelta(minutes=mins_ago)
        los = int((now - arrival).total_seconds() / 60)

        complaint, specialty, admission_likely = rng.choice(COMPLAINTS)
        triage = weighted(rng, TRIAGE)
        needs_admission = admission_likely and (
            triage in ("Immediate (Red)", "Very Urgent (Orange)", "Urgent (Yellow)")
            or rng.random() < 0.30
        )

        allocated_bed = None
        # Allocate a bed only if the attendance has been here long enough and a
        # matching occupied bed is still unclaimed.
        if needs_admission and los > 180 and admitted_pool:
            for idx, cand in enumerate(admitted_pool):
                ward = ward_by_id[cand["ward_id"]]
                if ward["specialty_type"] == specialty:
                    allocated_bed = admitted_pool.pop(idx)
                    break

        if allocated_bed is not None:
            ward = ward_by_id[allocated_bed["ward_id"]]
            site = ward["site_code"]
            if ward["gender_policy"] == "Male":
                sex = "Male"
            elif ward["gender_policy"] == "Female":
                sex = "Female"
            else:
                sex = weighted(rng, [("Male", 0.49), ("Female", 0.51)])
        else:
            site = rng.choice(SITES)["site_code"]
            sex = weighted(rng, [("Male", 0.49), ("Female", 0.51)])

        attendances.append({
            "attendance_id": attendance_id,
            "patient_pseudo_id": nhs_number(rng),
            "patient_sex": sex,
            "hospital_site_code": site,
            "arrival": arrival,
            "triage_category": triage,
            "chief_complaint": complaint,
            "required_specialty": specialty,
            "needs_admission": needs_admission,
            "allocated_bed_id": allocated_bed["bed_id"] if allocated_bed else None,
            "los": los,
        })

    # -- build the CDC event stream ------------------------------------------
    events = []
    event_seq = 0
    for att in attendances:
        arrival = att["arrival"]
        if att["allocated_bed_id"]:
            chain = EVENT_CHAIN
        elif att["needs_admission"]:
            chain = EVENT_CHAIN[:3]
        elif att["los"] > 200:
            chain = DISCHARGE_CHAIN
        else:
            chain = EVENT_CHAIN[:2]

        cursor = arrival
        dta_ts = None
        discharge_ts = None
        emitted = []
        for step, (event_type, disposition) in enumerate(chain):
            if step > 0:
                cursor = cursor + timedelta(minutes=rng.randint(15, 110))
            if cursor > now:
                break                     # truncate forward, never clamp back
            if event_type == "Decision To Admit":
                dta_ts = cursor
            if event_type == "Discharged":
                discharge_ts = cursor
            emitted.append((cursor, event_type, disposition))

        if not emitted:                   # always emit at least registration
            emitted = [(arrival, "Registered", "Awaiting Triage")]

        for cursor, event_type, disposition in emitted:
            event_seq += 1
            events.append({
                "event_id": f"EVT-{event_seq:07d}",
                "attendance_id": att["attendance_id"],
                "event_timestamp": iso_minutes(cursor),
                "event_type": event_type,
                "arrival_timestamp": iso_minutes(arrival),
                "patient_pseudo_id": att["patient_pseudo_id"],
                "patient_sex": att["patient_sex"],
                "hospital_site_code": att["hospital_site_code"],
                "triage_category": att["triage_category"],
                "chief_complaint": att["chief_complaint"],
                "required_specialty": att["required_specialty"],
                "decision_to_admit_timestamp": iso_minutes(dta_ts) if dta_ts else "",
                "allocated_bed_id": (att["allocated_bed_id"] or "")
                                    if event_type == "Bed Allocated" else "",
                "disposition": disposition,
                "discharge_timestamp": iso_minutes(discharge_ts) if discharge_ts else "",
                # D6: computed by the source system AT THE MOMENT THE EVENT WAS
                # WRITTEN and never revised.  For a patient still in the
                # department it under-reports, because time has passed since.
                # Recomputing against the current clock is the exercise.
                "is_breached": ((now if not dirt.enabled else cursor) - arrival)
                               > timedelta(minutes=240),
            })
        # Reconcile the attendance's terminal state for the report.
        att["final_disposition"] = emitted[-1][2]
        att["final_event_type"] = emitted[-1][1]
        # The chain is truncated at `now`, so an attendance can be handed a bed
        # whose "Bed Allocated" event never fires.  Hand that bed back: an
        # Occupied bed with no admitted patient behind it is ghost occupancy,
        # and it would make the bed board disagree with the attendance list.
        if att["allocated_bed_id"] and att["final_event_type"] != "Bed Allocated":
            att["allocated_bed_id"] = None

    # -- defects --------------------------------------------------------------
    for e in events:
        e["attendance_id"] = dirt.pad_key(e["attendance_id"], 0.015,
                                          "D3 padded attendance_id")
        e["hospital_site_code"] = dirt.recase(e["hospital_site_code"], 0.05,
                                              "D4 hospital_site_code casing")
        e["patient_sex"] = dirt.blank(e["patient_sex"], 0.02, "D5 null patient_sex")
        e["chief_complaint"] = dirt.blank(e["chief_complaint"], 0.02,
                                          "D5 null chief_complaint")
        # D9: mixed timestamp formats.
        if dirt._hit(0.08):
            e["event_timestamp"] = e["event_timestamp"].replace("T", " ")
            dirt.log["D9 space-separated event_timestamp"] += 1

    def blank_event(i):
        return {
            "event_id": "" if i % 2 == 0 else "  ",
            "attendance_id": "",
            "event_timestamp": iso_minutes(now - timedelta(minutes=dirt.rng.randint(1, 600))),
            "event_type": "Registered",
            "arrival_timestamp": iso_minutes(now - timedelta(minutes=dirt.rng.randint(1, 600))),
            "patient_pseudo_id": nhs_number(dirt.rng),
            "patient_sex": "Female",
            "hospital_site_code": "RJ611",
            "triage_category": "Urgent (Yellow)",
            "chief_complaint": "Sprained Ankle",
            "required_specialty": "Trauma and Orthopaedics",
            "decision_to_admit_timestamp": "",
            "allocated_bed_id": "",
            "disposition": "Awaiting Triage",
            "discharge_timestamp": "",
            "is_breached": False,
        }

    events += dirt.orphan_rows(blank_event, 10, "D2 blank event_id/attendance_id")
    events = dirt.duplicate(events, 0.02, "D1 duplicate event rows")

    # D10: the file is shuffled.  Order in the file carries no information.
    # Shuffle on the dirt stream: these lists differ in LENGTH between clean
    # and dirty mode, and shuffle consumes draws in proportion to length, so
    # using the main stream here would desynchronise the two builds.
    dirt.rng.shuffle(events)

    beds_out = dirt.duplicate(beds, 0.01, "D1 duplicate bed rows")
    for b in beds_out:
        b["status"] = dirt.recase(b["status"], 0.04, "D4 bed status casing")

    wards_out = [dict(w) for w in WARDS]
    for w in wards_out:
        w.pop("available", None)

    write_csv(out_dir / "wards.csv",
              ["ward_id", "ward_name", "site_code", "gender_policy",
               "specialty_type", "total_beds"], wards_out)
    write_csv(out_dir / "hospital_beds.csv",
              ["bed_id", "ward_id", "bed_number", "status",
               "is_isolation_capable"], beds_out)
    write_csv(out_dir / "ed_attendance_events.csv",
              ["event_id", "attendance_id", "event_timestamp", "event_type",
               "arrival_timestamp", "patient_pseudo_id", "patient_sex",
               "hospital_site_code", "triage_category", "chief_complaint",
               "required_specialty", "decision_to_admit_timestamp",
               "allocated_bed_id", "disposition", "discharge_timestamp",
               "is_breached"], events)

    return {"attendances": attendances, "beds": beds, "events": events}


# =============================================================================
# Project 3 -- Inpatient discharge orchestration and virtual ward telemetry
# =============================================================================

def news2_respiration(v: int) -> int:
    if v <= 8 or v >= 25:
        return 3
    if v >= 21:
        return 2
    if v <= 11:
        return 1
    return 0


def news2_spo2(v: int) -> int:
    if v <= 91:
        return 3
    if v <= 93:
        return 2
    if v <= 95:
        return 1
    return 0


def news2_systolic(v: int) -> int:
    if v <= 90 or v >= 220:
        return 3
    if v <= 100:
        return 2
    if v <= 110:
        return 1
    return 0


def news2_heart_rate(v: int) -> int:
    if v <= 40 or v >= 131:
        return 3
    if v >= 111:
        return 2
    if (41 <= v <= 50) or (91 <= v <= 110):
        return 1
    return 0


def news2_total(rr: int, spo2: int, sbp: int, hr: int) -> int:
    return (news2_respiration(rr) + news2_spo2(spo2)
            + news2_systolic(sbp) + news2_heart_rate(hr))


# Vital-sign draws that land on a chosen per-parameter NEWS2 sub-score.
VITAL_BANDS = {
    "respiration_rate": {0: (12, 20), 1: (9, 11), 2: (21, 24), 3: (25, 29)},
    "spo2":             {0: (96, 100), 1: (94, 95), 2: (92, 93), 3: (86, 91)},
    "systolic_bp":      {0: (111, 160), 1: (101, 110), 2: (91, 100), 3: (78, 90)},
    "heart_rate":       {0: (51, 90), 1: (91, 110), 2: (111, 130), 3: (131, 148)},
}


def draw_vital(rng: random.Random, name: str, band: int) -> int:
    lo, hi = VITAL_BANDS[name][band]
    return rng.randint(lo, hi)


def bands_for_target(rng: random.Random, target: int) -> dict:
    """Pick per-parameter NEWS2 sub-scores summing to `target`.

    Below 5, no single parameter is allowed to score 3, so that
    'NEWS2 >= 5 OR any single parameter == 3' has two distinguishable
    triggers in the data rather than one.
    """
    names = list(VITAL_BANDS)
    while True:
        remaining = target
        bands = {}
        rng.shuffle(names)
        for idx, name in enumerate(names):
            if idx == len(names) - 1:
                bands[name] = remaining
            else:
                cap = min(3, remaining)
                bands[name] = rng.randint(0, cap)
                remaining -= bands[name]
        if any(b < 0 or b > 3 for b in bands.values()):
            continue
        if sum(bands.values()) != target:
            continue
        if target < 5 and 3 in bands.values() and rng.random() < 0.8:
            continue                    # keep single-parameter-3 cases rare-ish
        return bands


def build_project_3(out_dir: Path, rng: random.Random, dirt: Dirt, now: datetime):
    # -- virtual ward beds ----------------------------------------------------
    vw_beds = []
    for pathway, (total, available) in VW_BEDS_PER_PATHWAY.items():
        free = set(rng.sample(range(1, total + 1), available))
        for n in range(1, total + 1):
            vw_beds.append({
                "virtual_bed_id": f"VWB-{pathway[:4].upper()}-{n:02d}",
                "care_pathway": pathway,
                "bed_status": "Available" if n in free else "Occupied",
                "capacity_team": f"{pathway} Virtual Ward Team",
                "trust_code": TRUSTS[0]["trust_code"] if n % 3 else TRUSTS[1]["trust_code"],
            })

    # -- inpatient admissions -------------------------------------------------
    acute_wards = [w["ward_id"] for w in WARDS]
    admissions = []
    for i in range(N_ADMISSIONS):
        admission_id = f"IPA-2026-{i + 1:05d}"
        admitted_at = now - timedelta(hours=rng.randint(24, 21 * 24))
        pathway = weighted(rng, [("Frailty", 0.34), ("Respiratory", 0.26),
                                 ("Cardiac", 0.20), ("Post-Surgical", 0.20)])

        medically_fit = rng.random() < 0.42
        fit_at = None
        if medically_fit:
            fit_at = now - timedelta(hours=rng.randint(2, 96))

        # Bias the fit cohort towards discharge-capable mobility/social states so
        # a workable number of admissions clear the readiness threshold.
        if medically_fit:
            mobility = weighted(rng, [("Independent", 0.50), ("Assisted", 0.34),
                                      ("Immobile", 0.16)])
            social = weighted(rng, [("Package Confirmed", 0.48), ("Pending", 0.37),
                                    ("Unassigned", 0.15)])
            target_news2 = weighted(rng, [(0, 0.26), (1, 0.22), (2, 0.18),
                                          (3, 0.13), (4, 0.10), (5, 0.06),
                                          (6, 0.03), (7, 0.02)])
        else:
            mobility = weighted(rng, [("Independent", 0.22), ("Assisted", 0.40),
                                      ("Immobile", 0.38)])
            social = weighted(rng, [("Package Confirmed", 0.20), ("Pending", 0.50),
                                    ("Unassigned", 0.30)])
            target_news2 = weighted(rng, [(0, 0.08), (1, 0.12), (2, 0.16),
                                          (3, 0.18), (4, 0.16), (5, 0.13),
                                          (6, 0.09), (7, 0.05), (8, 0.03)])

        # Roughly a fifth of admissions are already stepped down into the
        # virtual ward; those carry a virtual_bed_id.
        virtual_bed_id = ""
        location = "Acute Ward"
        occupied_vw = [b for b in vw_beds
                       if b["bed_status"] == "Occupied" and b["care_pathway"] == pathway]
        if occupied_vw and rng.random() < 0.30:
            chosen = rng.choice(occupied_vw)
            occupied_vw.remove(chosen)
            vw_beds = [b for b in vw_beds if b is not chosen] + [chosen]
            virtual_bed_id = chosen["virtual_bed_id"]
            location = "Virtual Ward"

        admissions.append({
            "admission_id": admission_id,
            "patient_id": nhs_number(rng),
            "patient_sex": weighted(rng, [("Male", 0.49), ("Female", 0.51)]),
            "trust_code": weighted(rng, [(TRUSTS[0]["trust_code"], 0.72),
                                         (TRUSTS[1]["trust_code"], 0.28)]),
            "ward_id": rng.choice(acute_wards) if location == "Acute Ward" else "",
            "location_type": location,
            "virtual_bed_id": virtual_bed_id,
            "admitted_at": iso_minutes(admitted_at),
            "care_pathway": pathway,
            "mobility_status": mobility,
            "social_care_status": social,
            "medically_fit_flag": medically_fit,
            "medically_fit_at": iso_minutes(fit_at) if fit_at else "",
            "_target_news2": target_news2,
        })

    # -- telemetry ------------------------------------------------------------
    # Every admission gets a 48-hour trace at 30-minute cadence.  The LAST VALID
    # reading is constructed to hit the admission's target NEWS2 exactly, so the
    # expected escalation counts in the report are exact for a correct pipeline.
    readings = []
    reading_seq = 0
    n_ticks = (TELEMETRY_WINDOW_HOURS * 60) // TELEMETRY_INTERVAL_MINUTES

    for adm in admissions:
        target = adm["_target_news2"]
        final_bands = bands_for_target(rng, target)
        # Recorded for the report: does the escalation rule fire on the
        # "any single parameter scores 3" limb rather than on the total?
        adm["_single_param_3"] = target < 5 and 3 in final_bands.values()
        # Walk backwards in time from the current state so the trace tells a
        # plausible clinical story rather than random noise.
        drift = rng.choice([-1, 0, 0, 1])
        trace = []
        for tick in range(n_ticks):
            recorded_at = now - timedelta(minutes=TELEMETRY_INTERVAL_MINUTES * tick)
            if tick == 0:
                bands = final_bands
            else:
                bands = {}
                for name, b in final_bands.items():
                    shift = 0
                    if rng.random() < 0.35:
                        shift = rng.choice([-1, 1])
                    if drift:
                        shift += drift if rng.random() < min(0.5, tick / n_ticks) else 0
                    bands[name] = max(0, min(3, b + shift))
            trace.append((recorded_at, {
                name: draw_vital(rng, name, band) for name, band in bands.items()
            }))

        for idx, (recorded_at, vitals) in enumerate(trace):
            reading_seq += 1
            row = {
                "reading_id": f"TEL-{reading_seq:08d}",
                "admission_id": adm["admission_id"],
                "virtual_bed_id": adm["virtual_bed_id"],
                "device_id": f"DEV-{adm['admission_id'][-5:]}",
                "recorded_at": iso_minutes(recorded_at),
                "heart_rate": vitals["heart_rate"],
                "spo2": vitals["spo2"],
                "systolic_bp": vitals["systolic_bp"],
                "respiration_rate": vitals["respiration_rate"],
                "reading_source": "Continuous Monitor" if adm["location_type"] == "Virtual Ward"
                                  else "Ward Observation",
            }
            # idx 0 is the reading that defines the current clinical state.  It
            # is NEVER corrupted -- the trap is that corrupted readings sit
            # around it, not that the answer is unknowable.
            if idx > 0:
                # D7: sensor dropout -- sentinel and null values.
                if dirt._hit(0.035):
                    field = dirt.rng.choice(["spo2", "heart_rate", "respiration_rate"])
                    row[field] = dirt.rng.choice([0, -1, ""])
                    dirt.log["D7 telemetry sensor dropout"] += 1
                # D8: physiologically impossible values.
                elif dirt._hit(0.015):
                    field, value = dirt.rng.choice([
                        ("heart_rate", dirt.rng.randint(280, 340)),
                        ("spo2", dirt.rng.randint(115, 140)),
                        ("systolic_bp", dirt.rng.randint(320, 400)),
                        ("respiration_rate", dirt.rng.randint(90, 130)),
                    ])
                    row[field] = value
                    dirt.log["D8 impossible telemetry value"] += 1
            readings.append(row)

    # -- defects on the admission and bed tables ------------------------------
    for adm in admissions:
        adm["mobility_status"] = dirt.recase(adm["mobility_status"], 0.06,
                                             "D4 mobility_status casing")
        adm["care_pathway"] = dirt.recase(adm["care_pathway"], 0.05,
                                          "D4 care_pathway casing")
        adm["social_care_status"] = dirt.blank(adm["social_care_status"], 0.04,
                                               "D5 null social_care_status")
        adm["patient_id"] = dirt.pad_key(adm["patient_id"], 0.02,
                                         "D3 padded patient_id (admissions)")
        # D6: a cached NEWS2 written by an upstream system at admission time and
        # never refreshed.  Stale on purpose; recomputing it is the exercise.
        drift_choice = rng.choice([-3, -2, -2, -1, 0, 1, 2])
        age_hours = rng.randint(6, 72)   # both drawn unconditionally: see Dirt
        if dirt.enabled:
            adm["news2_cached"] = max(0, adm["_target_news2"] + drift_choice)
            adm["news2_cached_at"] = iso_minutes(now - timedelta(hours=age_hours))
        else:
            adm["news2_cached"] = adm["_target_news2"]
            adm["news2_cached_at"] = iso_minutes(now)

    def blank_admission(i):
        return {
            "admission_id": "" if i % 2 == 0 else "   ",
            "patient_id": nhs_number(dirt.rng),
            "patient_sex": "Male",
            "trust_code": TRUSTS[0]["trust_code"],
            "ward_id": dirt.rng.choice(acute_wards),
            "location_type": "Acute Ward",
            "virtual_bed_id": "",
            "admitted_at": iso_minutes(now - timedelta(hours=dirt.rng.randint(24, 400))),
            "care_pathway": "Frailty",
            "mobility_status": "Assisted",
            "social_care_status": "Pending",
            "medically_fit_flag": False,
            "medically_fit_at": "",
            "news2_cached": 2,
            "news2_cached_at": iso_minutes(now - timedelta(hours=12)),
        }

    admissions_out = list(admissions) + dirt.orphan_rows(
        blank_admission, 8, "D2 blank admission_id")
    admissions_out = dirt.duplicate(admissions_out, 0.015,
                                    "D1 duplicate admission rows")
    readings_out = dirt.duplicate(readings, 0.008, "D1 duplicate telemetry rows")
    # Shuffle on the dirt stream: these lists differ in LENGTH between clean
    # and dirty mode, and shuffle consumes draws in proportion to length, so
    # using the main stream here would desynchronise the two builds.
    dirt.rng.shuffle(readings_out)

    write_csv(out_dir / "virtual_ward_beds.csv",
              ["virtual_bed_id", "care_pathway", "bed_status",
               "capacity_team", "trust_code"], vw_beds)
    write_csv(out_dir / "inpatient_admissions.csv",
              ["admission_id", "patient_id", "patient_sex", "trust_code",
               "ward_id", "location_type", "virtual_bed_id", "admitted_at",
               "care_pathway", "mobility_status", "social_care_status",
               "medically_fit_flag", "medically_fit_at",
               "news2_cached", "news2_cached_at"], admissions_out)
    write_csv(out_dir / "telemetry_readings.csv",
              ["reading_id", "admission_id", "virtual_bed_id", "device_id",
               "recorded_at", "heart_rate", "spo2", "systolic_bp",
               "respiration_rate", "reading_source"], readings_out)

    return {"admissions": admissions, "admissions_raw": admissions_out,
            "vw_beds": vw_beds, "readings": readings_out}


# =============================================================================
# Analysis -- every figure computed on CLEANED rows
# =============================================================================

def clean_key(value) -> str:
    return str(value).strip()


def analyse(p1, p2, p3, as_of: date, now: datetime) -> dict:
    report = {}

    # ---------------------------------------------------------------- P1 -----
    seen = set()
    pathways = []
    for row in p1["pathways"]:
        pk = clean_key(row["pathway_id"])
        if not pk or pk in seen:
            continue
        seen.add(pk)
        pathways.append(row)

    active = [p for p in pathways if clean_key(p["clock_status"]).upper() == "ACTIVE"]
    weeks = [(as_of - date.fromisoformat(p["referral_date"])).days / 7 for p in active]
    breached = [w for w in weeks if w >= 18]

    cohorts = Counter()
    for w in weeks:
        if w < 6:
            cohorts["<6w"] += 1
        elif w < 12:
            cohorts["6-12w"] += 1
        elif w < 18:
            cohorts["12-18w"] += 1
        else:
            cohorts[">18w"] += 1

    stale_wrong = sum(
        1 for p in active
        if (str(p["is_breached"]).strip().lower() == "true")
        != ((as_of - date.fromisoformat(p["referral_date"])).days >= 126)
    )

    cap_by_spec = defaultdict(int)
    for c in p1["clinics"]:
        cap_by_spec[c["specialty_id"]] += int(c["weekly_capacity"])
    backlog = Counter(p["specialty_id"] for p in active)
    clearance = {
        sid: round(backlog[sid] / cap_by_spec[sid], 1)
        for sid in sorted(backlog)
    }

    report["project_1"] = {
        "raw_pathway_rows": len(p1["pathways"]),
        "clean_pathways": len(pathways),
        "active_pathways": len(active),
        "breached_over_18w": len(breached),
        "breach_rate_pct": round(100 * len(breached) / len(active), 1),
        "cohorts": dict(cohorts),
        "stale_is_breached_wrong": stale_wrong,
        "clearance_weeks_by_specialty": clearance,
        "specialties_over_20_weeks": sorted(
            s for s, v in clearance.items() if v > 20),
        "raw_patient_rows": len(p1["patients"]),
        "unvalidated_active": sum(
            1 for p in active if str(p["validated_flag"]).strip().lower() != "true"),
    }

    # ---------------------------------------------------------------- P2 -----
    seen = set()
    events = []
    for e in p2["events"]:
        eid = clean_key(e["event_id"])
        aid = clean_key(e["attendance_id"])
        if not eid or not aid or eid in seen:
            continue
        seen.add(eid)
        events.append(e)

    def parse_ts(value: str) -> datetime:
        return datetime.fromisoformat(clean_key(value).replace(" ", "T"))

    latest = {}
    for e in events:
        aid = clean_key(e["attendance_id"])
        ts = parse_ts(e["event_timestamp"])
        if aid not in latest or ts > latest[aid][0]:
            latest[aid] = (ts, e)

    dispositions = Counter(e["disposition"] for _, e in latest.values())

    # What a trainee who sorts on arrival_timestamp instead would get.
    wrong = {}
    for e in events:
        aid = clean_key(e["attendance_id"])
        ts = parse_ts(e["arrival_timestamp"])
        if aid not in wrong or ts >= wrong[aid][0]:
            wrong[aid] = (ts, e)
    wrong_count = sum(
        1 for aid in latest
        if latest[aid][1]["disposition"] != wrong[aid][1]["disposition"])

    in_flight = []
    for aid, (ts, e) in latest.items():
        if e["disposition"] in ("Discharged", "Admitted"):
            continue
        los = int((now - parse_ts(e["arrival_timestamp"])).total_seconds() / 60)
        in_flight.append(los)
    in_flight.sort()

    beds_clean = {}
    for b in p2["beds"]:
        beds_clean[clean_key(b["bed_id"])] = b
    ward_counts = defaultdict(Counter)
    for b in beds_clean.values():
        ward_counts[b["ward_id"]][clean_key(b["status"]).title()] += 1
    pressure = {
        w["ward_id"]: round(100 * ward_counts[w["ward_id"]]["Occupied"] / w["total_beds"], 1)
        for w in WARDS
    }

    report["project_2"] = {
        "raw_event_rows": len(p2["events"]),
        "clean_event_rows": len(events),
        "distinct_attendances": len(latest),
        "dispositions": dict(dispositions),
        "attendances_wrong_if_sorted_on_arrival": wrong_count,
        "pct_wrong_if_sorted_on_arrival": round(100 * wrong_count / len(latest), 1),
        "in_flight_median_los_minutes": (
            in_flight[len(in_flight) // 2] if in_flight else None),
        "in_flight_over_4_hours": sum(1 for l in in_flight if l > 240),
        "in_flight_total": len(in_flight),
        "available_beds": sum(
            1 for b in beds_clean.values() if clean_key(b["status"]).title() == "Available"),
        "ward_pressure_pct": pressure,
        "awaiting_bed": dispositions.get("Awaiting Bed", 0),
    }

    # ---------------------------------------------------------------- P3 -----
    seen = set()
    admissions = []
    for a in p3["admissions_raw"]:
        aid = clean_key(a["admission_id"])
        if not aid or aid in seen or "_target_news2" not in a:
            continue
        seen.add(aid)
        admissions.append(a)

    def norm(value: str, default: str) -> str:
        v = clean_key(value)
        return v.title() if v else default

    eligible = 0
    escalation = 0
    single_param_3 = 0
    readiness_values = []
    fit = 0
    eligible_by_pathway = Counter()
    unplaceable = Counter()

    for a in admissions:
        news2 = a["_target_news2"]
        if a["_single_param_3"]:
            single_param_3 += 1
        mobility = norm(a["mobility_status"], "Independent")
        social = clean_key(a["social_care_status"]) or "Pending"
        social = social.title()
        readiness = 100 - news2 * 10
        readiness -= {"Independent": 0, "Assisted": 15, "Immobile": 35}[mobility]
        readiness -= 0 if social == "Package Confirmed" else 25
        readiness = max(0, readiness)
        readiness_values.append(readiness)
        if str(a["medically_fit_flag"]).strip().lower() == "true":
            fit += 1
            if readiness >= 70 and news2 < 3:
                eligible += 1
                eligible_by_pathway[norm(a["care_pathway"], "Frailty")] += 1
        if news2 >= 5 or a["_single_param_3"]:
            escalation += 1

    stale_cached_wrong = sum(
        1 for a in admissions if int(a.get("news2_cached", 0)) != a["_target_news2"])

    vw_available = {
        pathway: sum(1 for b in p3["vw_beds"]
                     if b["care_pathway"] == pathway and b["bed_status"] == "Available")
        for pathway in VW_BEDS_PER_PATHWAY
    }
    for pathway, n in eligible_by_pathway.items():
        if vw_available.get(pathway, 0) == 0:
            unplaceable[pathway] = n

    report["project_3"] = {
        "raw_admission_rows": len(p3["admissions_raw"]),
        "clean_admissions": len(admissions),
        "medically_fit": fit,
        "step_down_eligible": eligible,
        "eligible_by_care_pathway": dict(eligible_by_pathway),
        "virtual_beds_available_by_pathway": vw_available,
        "eligible_but_no_bed_available": dict(unplaceable),
        "news2_escalation_total": escalation,
        "news2_below_5_with_a_single_3": single_param_3,
        "stale_news2_cached_wrong": stale_cached_wrong,
        "telemetry_rows": len(p3["readings"]),
        "median_readiness": sorted(readiness_values)[len(readiness_values) // 2],
    }

    return report


# =============================================================================
# Report
# =============================================================================

def write_report(path: Path, report: dict, dirt: Dirt, args, as_of: date, now: datetime):
    lines = []
    a = lines.append
    a("# Generation report (instructor key)")
    a("")
    a(f"- Generated: {datetime.now().isoformat(timespec='seconds')}")
    a(f"- As-of date: {as_of.isoformat()}  (clock anchor {iso_minutes(now)})")
    a(f"- Seed: {args.seed}")
    a(f"- Mode: {'CLEAN (no defects)' if args.clean else 'DIRTY (defects injected)'}")
    a("")
    a("Every figure below is computed on **cleaned** rows -- trimmed keys, blank")
    a("keys dropped, duplicates removed -- i.e. what a correct Day 1 pipeline")
    a("produces. Raw row counts are labelled `raw_`.")
    a("")

    titles = {
        "project_1": "Project 1 -- Elective care RTT",
        "project_2": "Project 2 -- ED flow and bed allocation",
        "project_3": "Project 3 -- Discharge and virtual ward",
    }
    for key, title in titles.items():
        a(f"## {title}")
        a("")
        for k, v in report[key].items():
            if isinstance(v, dict):
                a(f"- **{k}**:")
                for kk, vv in v.items():
                    a(f"    - {kk}: {vv}")
            else:
                a(f"- **{k}**: {v}")
        a("")

    a("## Deliberate defects injected")
    a("")
    if not dirt.log:
        a("None -- clean mode.")
    else:
        for k in sorted(dirt.log):
            a(f"- {k}: {dirt.log[k]}")
    a("")
    a("## Marking notes")
    a("")
    a("1. **Project 2 CDC sort key.** Sorting on `arrival_timestamp` rather than")
    a("   `event_timestamp` yields a green pipeline with the correct row count and")
    a(f"   {report['project_2']['pct_wrong_if_sorted_on_arrival']}% of dispositions")
    a("   wrong. It is invisible from the UI. Check the sort key directly.")
    a("2. **Stale flags.** `is_breached` (Project 1 and 2) and `news2_cached`")
    a("   (Project 3) are stale on purpose. A trainee who carries them through")
    a("   rather than recomputing gets a plausible-looking wrong answer.")
    a("3. **Empty states are real.** Cardiology at RJ612 has no compatible ward;")
    a("   WARD-AMU-2 has no free isolation room; the Post-Surgical virtual ward")
    a("   has zero available beds. All three must render, not crash.")
    a("4. **Telemetry cleaning is exclusion, not smoothing.** The most recent")
    a("   reading per admission is always valid. Dropouts (0, -1, null) and")
    a("   impossible values must be dropped before taking the latest reading.")
    a("   Smoothing vitals with a rolling median would mask real deterioration.")

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# =============================================================================

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="./synthetic_nhs_data", type=Path)
    ap.add_argument("--as-of", default=None,
                    help="ISO date the data is anchored to (default: today)")
    ap.add_argument("--seed", default=20260903, type=int)
    ap.add_argument("--clean", action="store_true",
                    help="suppress every deliberate defect (reference build)")
    args = ap.parse_args()

    as_of = date.fromisoformat(args.as_of) if args.as_of else date.today()
    now = datetime.combine(as_of, datetime.min.time()) + timedelta(hours=14, minutes=30)

    rng = random.Random(args.seed)
    dirt = Dirt(args.seed, enabled=not args.clean)

    base = Path(args.out)
    p1 = build_project_1(base / "project_1_rtt", rng, dirt, as_of)
    p2 = build_project_2(base / "project_2_ed_flow", rng, dirt, now)
    p3 = build_project_3(base / "project_3_virtual_ward", rng, dirt, now)

    report = analyse(p1, p2, p3, as_of, now)
    write_report(base / "GENERATION_REPORT.md", report, dirt, args, as_of, now)

    print(f"Written to {base.resolve()}")
    print(f"  Project 1: {report['project_1']['clean_pathways']} clean pathways, "
          f"{report['project_1']['breach_rate_pct']}% breached")
    print(f"  Project 2: {report['project_2']['distinct_attendances']} attendances, "
          f"{report['project_2']['pct_wrong_if_sorted_on_arrival']}% wrong on bad sort key")
    print(f"  Project 3: {report['project_3']['clean_admissions']} admissions, "
          f"{report['project_3']['step_down_eligible']} step-down eligible, "
          f"{report['project_3']['telemetry_rows']} telemetry rows")
    print(f"  Defects injected: {sum(dirt.log.values())}")


if __name__ == "__main__":
    main()
