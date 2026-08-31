#!/usr/bin/env python3
"""
Synthetic NHS data generator for the CGI Foundry capstone projects.

Produces the input CSVs for both trainee projects:

    Project 1  (RTT / elective waiting list)   -> project_1_rtt/
        patients.csv
        rtt_pathways.csv
        clinics.csv

    Project 2  (ED flow / bed allocation)      -> project_2_ed_flow/
        ed_attendance_events.csv
        hospital_beds.csv
        wards.csv

Design intent
-------------
The data is deliberately NOT clean. Day 1 of both briefs asks trainees to
standardise identifiers, parse dates, and enforce primary key integrity; that
work has to actually change something or the lesson is ceremony. Every defect
below is injected on purpose, is deterministic under a fixed seed, and is
listed in the instructor key written alongside the data.

Deliberate defects
    D1  duplicate primary keys                 (drop or resolve)
    D2  null / blank primary keys              (must be filtered before OSv2)
    D3  mixed date formats  ISO + DD/MM/YYYY   (must be parsed, not cast)
    D4  identifier noise: whitespace, spaces   (must be normalised before join)
    D5  categorical casing drift  Male/male/M  (must be standardised)
    D6  blank optional categoricals            (must be defaulted, not dropped)
    D7  stale precomputed `is_breached` flag   (the temporal-staleness trap)
    D8  CDC event stream, shuffled row order   (must sort before group_by/last)
    D9  status casing drift  Available/AVAILABLE (case-insensitive matching)

Derived columns that trainees must compute themselves are NOT shipped:
`target_breach_date` and elapsed-time fields are absent by design. The one
exception is `is_breached`, which IS shipped and IS deliberately stale — see D7.

Everything is anchored to --as-of (default: today) so the dataset can be
regenerated on the morning a cohort starts and the time-based cohorts stay
correctly distributed.

Usage
    python synthetic_nhs_data_generator.py
    python synthetic_nhs_data_generator.py --as-of 2026-09-14 --out ./data
    python synthetic_nhs_data_generator.py --clean     # defect-free, for solution builds
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import random
from pathlib import Path

# --------------------------------------------------------------------------
# Tuning knobs
# --------------------------------------------------------------------------

SEED = 20260831

# -- Project 1 volumes ------------------------------------------------------
N_PATIENTS = 2400
N_PATHWAYS = 3000
SITES_PER_SPECIALTY = 3

# Waiting-time distribution, in weeks. Weighted so every NHS reporting cohort
# (<6, 6-12, 12-18, 18-52, 52+) is populated, including 52+ which the
# executive-dashboard stretch goal needs.
WAIT_BANDS = [
    ((1, 6), 0.14),
    ((6, 12), 0.18),
    ((12, 18), 0.20),
    ((18, 52), 0.40),
    ((52, 78), 0.08),
]

PRIORITY_MIX = [("P1", 0.03), ("P2", 0.14), ("P3", 0.31), ("P4", 0.52)]
RISK_MIX = [("High", 0.11), ("Medium", 0.32), ("Low", 0.57)]
PAUSED_RATE = 0.16
VALIDATED_RATE = 0.42

# -- Project 2 volumes ------------------------------------------------------
N_ATTENDANCES = 400
# A 24-hour window against a 4-hour standard. Was 72, which put the median
# in-flight patient 31 hours into their stay: every breach metric on the Day 4
# board read red and there was nothing left to count down.
ED_WINDOW_HOURS = 24
# Minutes between consecutive state changes. The chain is built forward from
# arrival and truncated at `now` — never clamped backwards — so event_timestamp
# is monotonic within an attendance by construction.
ED_STEP_MINUTES = (20, 75)
# Nobody sits in Awaiting Bed longer than this; older bed-seekers have been
# admitted or discharged by now. Without this a 24-hour window still produces
# patients queueing for a bed all day.
MAX_AWAITING_HOURS = 10
# Share of non-admitted, recent-enough attendances that need an inpatient bed.
# Tuned to land the Day 4 worklist around 60 patients.
NEEDS_BED_RATE = 0.70

# Available beds per ward, set explicitly rather than by a floor-plus-top-up
# heuristic. Two things depend on the exact spread: the compatible-bed counts a
# trainee's findCompatibleBeds must reproduce, and the pressure index of each
# ward. Deliberately uneven — an estate where every ward has the same number
# free produces a compatible-bed distribution with three spikes and no middle.
# WARD-CARD is the red ward: 1 free of 12 puts it at 91.7%, so every band on
# the stretch-goal heatmap has a member. The estate previously topped out at
# 83.3% and the red state was unreachable.
AVAILABLE_BEDS_BY_WARD = {
    "WARD-AMU": 4,      # of 24 -> 83.3% occupancy, the amber band
    "WARD-TRAUMA": 4,   # of 20
    "WARD-SURG-M": 5,   # of 18
    "WARD-SURG-F": 4,   # of 18
    "WARD-CARD": 1,     # of 12 -> 91.7% occupancy, the red band
    "WARD-EAU": 6,      # of 16
}
# Wards whose occupancy is set deliberately. Out-of-service beds are never
# drawn from these, because a Cleaning or Maintenance bed is not Occupied and
# would pull the ward back out of the band it is meant to sit in.
PRESSURE_MANAGED_WARDS = ("WARD-CARD", "WARD-AMU")
PRESSURE_RED_WARD = "WARD-CARD"
# Isolation beds are made available in every ward EXCEPT this one, so the
# infection-control rule has both a success path and a genuine empty state.
NO_ISOLATION_AVAILABLE_IN = "WARD-EAU"
# Beds out of service across the estate, drawn from wards that are not at
# their availability target. Cleaning and Maintenance beds are not Occupied,
# so they lower a ward's pressure index as well as its free stock.
OUT_OF_SERVICE = {"Cleaning": 7, "Maintenance": 6}

# -- Defect rates -----------------------------------------------------------
DUP_PK_RATE = 0.02
NULL_PK_COUNT = 3
ALT_DATE_FORMAT_RATE = 0.12
ID_NOISE_RATE = 0.08
CASE_DRIFT_RATE = 0.10
BLANK_CATEGORICAL_RATE = 0.04
STALE_BREACH_FLAG_RATE = 0.015

# --------------------------------------------------------------------------
# Reference data
# --------------------------------------------------------------------------

# Site capacities are tuned so that backlog clearance (active queue / summed
# weekly capacity) lands across an 8-28 week spread, with THREE specialties
# clearly above the 20-week threshold the executive stretch goal asks you to
# highlight. Retune these if you change N_PATHWAYS.
#
# Tune against the CLEANED active counts, not the raw ones. Deduplicating and
# dropping null-key rows removes ~2% of pathways, which was enough to drop
# Trauma & Orthopaedics from 20.4 to 19.7 weeks — so the key claimed three
# specialties over the line and a correct pipeline produced two. The capacities
# below leave every one of the three a margin of at least a full week.
SPECIALTIES = [
    # code,    name,                        site capacities (per week)
    ("TFC-100", "General Surgery",            (12, 9, 7)),   # ~13.8w
    ("TFC-110", "Trauma & Orthopaedics",      (7, 6, 5)),    # ~20.8w
    ("TFC-120", "ENT (Ear, Nose & Throat)",   (5, 4, 4)),    # ~28.4w
    ("TFC-130", "Ophthalmology",              (18, 14, 12)), # ~7.7w
    ("TFC-300", "General Internal Medicine",  (14, 11, 9)),  # ~10.5w
    ("TFC-320", "Cardiology",                 (6, 5, 4)),    # ~21.9w
    ("TFC-502", "Gynaecology",                (9, 7, 6)),    # ~15.9w
]

SITE_NAMES = ["Royal Infirmary", "St Chad's Community Hospital", "Northgate Treatment Centre"]

POSTCODE_DISTRICTS = [
    "TN16", "SW1A", "RH10", "BR1", "CR0", "DA1", "ME14", "TN4", "SE9", "BN1",
    "CT1", "GU1", "RG1", "SL1", "KT1", "TW1", "UB1", "HA1", "EN1", "IG1",
]

WARDS = [
    # ward_id,      name,                          gender_policy, specialty_type,          beds
    ("WARD-AMU",    "Acute Medical Unit (AMU)",    "Mixed",  "Acute Medicine",        24),
    ("WARD-TRAUMA", "Trauma & Orthopaedic Ward",   "Mixed",  "Trauma & Orthopaedics", 20),
    ("WARD-SURG-M", "Surgical Ward (Male)",        "Male",   "General Surgery",       18),
    ("WARD-SURG-F", "Surgical Ward (Female)",      "Female", "General Surgery",       18),
    ("WARD-CARD",   "Coronary Care Unit",          "Mixed",  "Cardiology",            12),
    ("WARD-EAU",    "Emergency Assessment Unit",   "Mixed",  "Acute Assessment",      16),
]

# Every value here must appear as a ward specialty_type, or those beds are
# unreachable. "Acute Assessment" was previously missing from this pool, which
# stranded all 16 WARD-EAU beds.
ED_SPECIALTIES = [
    ("Acute Medicine", 0.26),
    ("Trauma & Orthopaedics", 0.27),
    ("General Surgery", 0.22),
    ("Cardiology", 0.13),
    ("Acute Assessment", 0.12),
]

# "Fever / Sepsis Alert" satisfies both substring tests in the isolation rule.
COMPLAINTS = [
    ("Chest Pain", ("Acute Medicine", "Cardiology")),
    ("Shortness of Breath", ("Acute Medicine", "Cardiology", "Acute Assessment")),
    ("Fever / Sepsis Alert", ("Acute Medicine", "General Surgery", "Acute Assessment")),
    ("Severe Abdominal Pain", ("General Surgery", "Acute Assessment")),
    ("Suspected Fracture / Fall", ("Trauma & Orthopaedics",)),
    ("Minor Head Injury", ("Trauma & Orthopaedics", "Acute Assessment")),
    ("Sprained Ankle", ("Trauma & Orthopaedics",)),
    ("Laceration / Minor Wound", ("Trauma & Orthopaedics", "General Surgery")),
]

TRIAGE = [
    ("Immediate (Red)", 0.04),
    ("Urgent (Yellow)", 0.28),
    ("Standard (Green)", 0.48),
    ("Non-Urgent (Blue)", 0.20),
]

RTT_TARGET_WEEKS = 18
ED_TARGET_MINUTES = 240

rng = random.Random(SEED)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def pick(weighted):
    """Weighted choice from [(value, weight), ...]."""
    values, weights = zip(*weighted)
    return rng.choices(values, weights=weights, k=1)[0]


def nhs_number(n: int) -> str:
    return f"999{n:07d}"


def fmt_date(d: dt.date, dirty: bool) -> str:
    """ISO by default; DD/MM/YYYY for a minority of rows when dirty. (D3)"""
    if dirty and rng.random() < ALT_DATE_FORMAT_RATE:
        return d.strftime("%d/%m/%Y")
    return d.isoformat()


def fmt_ts(t: dt.datetime, dirty: bool) -> str:
    """ISO minutes by default; space separator for a minority when dirty. (D3)"""
    if dirty and rng.random() < ALT_DATE_FORMAT_RATE:
        return t.strftime("%d/%m/%Y %H:%M")
    return t.strftime("%Y-%m-%dT%H:%M")


def noisy_id(value: str, dirty: bool) -> str:
    """Leading/trailing whitespace on identifiers used as join keys. (D4)"""
    if not dirty or rng.random() >= ID_NOISE_RATE:
        return value
    return rng.choice([f" {value}", f"{value} ", f"  {value}  "])


def drift_case(value: str, dirty: bool, variants=None) -> str:
    """Categorical casing / abbreviation drift. (D5)"""
    if not dirty or rng.random() >= CASE_DRIFT_RATE:
        return value
    if variants and value in variants:
        return rng.choice(variants[value])
    return rng.choice([value.upper(), value.lower()])


def maybe_blank(value: str, dirty: bool) -> str:
    """Blank an optional categorical. (D6)"""
    if dirty and rng.random() < BLANK_CATEGORICAL_RATE:
        return ""
    return value


GENDER_VARIANTS = {"Male": ["male", "MALE", "M"], "Female": ["female", "FEMALE", "F"]}
STATUS_VARIANTS = {
    "Available": ["available", "AVAILABLE"],
    "Occupied": ["occupied", "OCCUPIED"],
}


def write_csv(path: Path, header: list[str], rows: list[list]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        w.writerows(rows)


def inject_duplicates(rows: list[list], dirty: bool, rate: float = DUP_PK_RATE) -> int:
    """Re-insert a sample of rows verbatim, at random positions. (D1)"""
    if not dirty:
        return 0
    n = max(1, int(len(rows) * rate))
    for _ in range(n):
        src = rng.choice(rows)
        rows.insert(rng.randrange(len(rows)), list(src))
    return n


def inject_null_pks(rows: list[list], pk_index: int, dirty: bool,
                    count: int = NULL_PK_COUNT,
                    eligible: list[int] | None = None) -> int:
    """Blank the primary key on a few rows. (D2)

    `eligible` restricts the choice to rows nothing else references, so the
    defect stays a primary-key problem rather than silently becoming a broken
    foreign key somewhere downstream.
    """
    if not dirty:
        return 0
    pool = eligible if eligible is not None else list(range(len(rows)))
    targets = rng.sample(pool, min(count, len(pool)))
    for i in targets:
        rows[i][pk_index] = ""
    return len(targets)


# --------------------------------------------------------------------------
# Project 1 — RTT
# --------------------------------------------------------------------------

def build_clinics(dirty: bool):
    rows = []
    for code, name, capacities in SPECIALTIES:
        for i, cap in enumerate(capacities):
            rows.append([
                f"CLN-{code[-3:]}-{i + 1}",
                code,
                name,
                SITE_NAMES[i],
                cap,
                f"CONS-{rng.randint(1000, 6999)}",
            ])
    header = ["clinic_id", "specialty_code", "specialty_name",
              "site_name", "weekly_capacity", "lead_consultant_code"]
    return header, rows


def build_patients(dirty: bool, as_of: dt.date):
    rows = []
    for i in range(N_PATIENTS):
        pid = nhs_number(rng.randint(100000, 9999999))
        dob = as_of - dt.timedelta(days=rng.randint(18 * 365, 92 * 365))
        gender = pick([("Male", 0.49), ("Female", 0.51)])
        rows.append([
            noisy_id(pid, dirty),
            rng.choice(POSTCODE_DISTRICTS),
            fmt_date(dob, dirty),
            drift_case(gender, dirty, GENDER_VARIANTS),
            maybe_blank(pick(RISK_MIX), dirty),
        ])
    # Force uniqueness of the underlying id set before defects are added, so
    # duplicates in the output are only the ones we inject on purpose.
    seen, deduped = set(), []
    for r in rows:
        key = r[0].strip()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(r)
    rows = deduped

    dups = inject_duplicates(rows, dirty)
    nulls = inject_null_pks(rows, 0, dirty)
    header = ["patient_id", "postcode_district", "dob", "gender", "risk_category"]
    return header, rows, {"duplicate_rows": dups, "null_pk_rows": nulls,
                          "distinct_patients": len(seen), "raw_rows": len(rows)}


def build_pathways(dirty: bool, as_of: dt.date, patient_ids: list[str],
                   clinic_rows: list[list]):
    clinics_by_spec: dict[str, list[str]] = {}
    for r in clinic_rows:
        clinics_by_spec.setdefault(r[1], []).append(r[0])

    rows = []
    stale_flags = 0
    for i in range(N_PATHWAYS):
        code, _name, _caps = rng.choice(SPECIALTIES)
        lo, hi = pick(WAIT_BANDS)
        weeks = rng.uniform(lo, hi)
        referral = as_of - dt.timedelta(days=round(weeks * 7))

        paused = rng.random() < PAUSED_RATE
        clock_status = "PAUSED" if paused else "ACTIVE"
        clock_stop = ""
        if paused:
            # Clock stopped somewhere between referral and now.
            span = max(1, (as_of - referral).days)
            clock_stop = fmt_date(referral + dt.timedelta(days=rng.randint(1, span)), dirty)

        true_breach = weeks >= RTT_TARGET_WEEKS
        # D7: the flag is a point-in-time snapshot from the source system.
        # A slice of rows is shipped already disagreeing with a live calculation,
        # and the rest drift as soon as the file sits unregenerated.
        flag = true_breach
        if dirty and rng.random() < STALE_BREACH_FLAG_RATE:
            flag = not true_breach
            stale_flags += 1

        rows.append([
            f"RTT-2026-{i + 1:05d}",
            noisy_id(rng.choice(patient_ids), dirty),
            noisy_id(rng.choice(clinics_by_spec[code]), dirty),
            code,
            fmt_date(referral, dirty),
            clock_status,
            clock_stop,
            rng.choice(["10", "20"]),
            pick(PRIORITY_MIX),
            str(flag),
            str(rng.random() < VALIDATED_RATE),
        ])

    dups = inject_duplicates(rows, dirty)
    nulls = inject_null_pks(rows, 0, dirty)
    header = ["pathway_id", "patient_id", "clinic_id", "specialty_code",
              "referral_date", "clock_status", "clock_stop_date",
              "rtt_status_code", "priority_band", "is_breached", "validated_flag"]
    return header, rows, {"duplicate_rows": dups, "null_pk_rows": nulls,
                          "stale_breach_flags": stale_flags, "raw_rows": len(rows)}


# --------------------------------------------------------------------------
# Project 2 — ED flow
# --------------------------------------------------------------------------

def build_wards():
    header = ["ward_id", "ward_name", "gender_policy", "specialty_type", "total_beds"]
    return header, [list(w) for w in WARDS]


def build_beds(dirty: bool):
    """Bed estate with a deliberate, explicitly-set availability profile.

    Availability is not random. `AVAILABLE_BEDS_BY_WARD` fixes how many beds
    each ward has free, because two things a trainee is marked on read straight
    off it: the compatible-bed counts findCompatibleBeds must reproduce, and
    each ward's pressure index. One isolation room is left free in every ward
    except NO_ISOLATION_AVAILABLE_IN, so the infection-control branch has both
    a success path and a genuine empty state.

    Everything not free and not out of service is Occupied — and build_ed_events
    then generates exactly one admitted attendance per occupied bed, so the
    estate and the ED feed cannot disagree.
    """
    beds = []
    for ward_id, _n, _g, _s, total in WARDS:
        for i in range(total):
            beds.append({
                "bed_id": f"BED-{ward_id.split('-', 1)[1]}-{i + 1:02d}",
                "ward_id": ward_id,
                "bed_number": f"{i + 1:02d}",
                "isolation": i < 2,          # two side rooms per ward
                "status": "Occupied",
            })

    by_ward: dict[str, list[dict]] = {}
    for b in beds:
        by_ward.setdefault(b["ward_id"], []).append(b)

    # 1. Free each ward's quota. The first free bed is an isolation room
    #    wherever isolation is meant to be reachable, so sepsis and fever
    #    patients have somewhere to go in every specialty but one.
    for ward_id, ward_beds in by_ward.items():
        quota = AVAILABLE_BEDS_BY_WARD[ward_id]
        picked: list[dict] = []
        if ward_id != NO_ISOLATION_AVAILABLE_IN and quota:
            picked.append(rng.choice([b for b in ward_beds if b["isolation"]]))
        pool = [b for b in ward_beds if b not in picked and not b["isolation"]]
        picked += rng.sample(pool, quota - len(picked))
        for b in picked:
            b["status"] = "Available"

    # 2. A realistic tail of out-of-service beds, never drawn from a
    #    pressure-managed ward.
    pool = [b for b in beds
            if b["status"] == "Occupied"
            and b["ward_id"] not in PRESSURE_MANAGED_WARDS]
    for status, n in OUT_OF_SERVICE.items():
        chosen = rng.sample(pool, n)
        for b in chosen:
            b["status"] = status
        pool = [b for b in pool if b["status"] == "Occupied"]

    rows = [[
        b["bed_id"], b["ward_id"], b["bed_number"],
        drift_case(b["status"], dirty, STATUS_VARIANTS),   # D9
        str(b["isolation"]),
    ] for b in beds]

    dups = inject_duplicates(rows, dirty, rate=0.015)
    # Never blank or duplicate an occupied bed's id — those are referenced by
    # an attendance. Available beds are fair game: a trainee who fails to
    # dedupe will over-count the free stock, which is the point.
    free_rows = [i for i, r in enumerate(rows) if r[3].strip().lower() != "occupied"]
    nulls = inject_null_pks(rows, 0, dirty, count=2, eligible=free_rows)
    header = ["bed_id", "ward_id", "bed_number", "status", "is_isolation_capable"]
    stats = {
        "total_beds": len(beds),
        "raw_rows": len(rows),
        "available": sum(1 for b in beds if b["status"] == "Available"),
        "occupied": sum(1 for b in beds if b["status"] == "Occupied"),
        "isolation_total": sum(1 for b in beds if b["isolation"]),
        "isolation_available": sum(1 for b in beds
                                   if b["isolation"] and b["status"] == "Available"),
        "duplicate_rows": dups,
        "null_pk_rows": nulls,
    }
    return header, rows, stats, beds


def build_ed_events(dirty: bool, as_of: dt.date, beds: list[dict]):
    """A change-data-capture event stream, one row per state change.

    This is the point of Project 2's Day 1. `arrival_timestamp` is constant
    within an attendance, so sorting on it and taking .last() picks an arbitrary
    row — the rows are shuffled before writing specifically so that a pipeline
    which forgets to sort on `event_timestamp` produces visibly wrong state.

    Two invariants this function guarantees, because the marking key depends on
    both:

    1.  `event_timestamp` is strictly increasing within an attendance. Each
        chain is built forward from arrival and TRUNCATED at `now`; it is never
        clamped backwards. An earlier version reset the cursor into the last
        twenty minutes whenever it overshot, which pushed later events in front
        of earlier ones for 15 of 400 attendances and made the documented
        resolution (`sort('event_timestamp').last()`) disagree with the
        generator's own intent.

    2.  The number of attendances resolving to `Admitted` equals the number of
        Occupied beds exactly. Admissions are drawn FROM the bed estate rather
        than sampled independently, and the patient's specialty and sex are
        derived from the ward that holds their bed. Occupancy is therefore
        internally consistent with the four matching rules by construction, and
        there are no ghost occupants for a trainee to trip over on Day 4.

    The terminal state of an attendance is whatever its truncated chain reached,
    so `terminal_states` below is what a correct pipeline actually outputs — not
    an intent the data cannot express.
    """
    now = dt.datetime.combine(as_of, dt.time(20, 0))
    ward_by_id = {w[0]: w for w in WARDS}

    # ------------------------------------------------------------------
    # 1. Arrival times and per-attendance step durations.
    # ------------------------------------------------------------------
    plans = []
    for _ in range(N_ATTENDANCES):
        plans.append({
            "arrival": now - dt.timedelta(minutes=rng.randint(10, ED_WINDOW_HOURS * 60)),
            "steps": [rng.randint(*ED_STEP_MINUTES) for _ in range(3)],
        })

    def stamp(plan, step_index: int) -> dt.datetime:
        """Timestamp of the nth event: arrival, then one step per transition."""
        t = plan["arrival"]
        for s in plan["steps"][:step_index]:
            t += dt.timedelta(minutes=s)
        return t

    # ------------------------------------------------------------------
    # 2. Admissions are drawn from the occupied beds, not sampled freely.
    #    Only attendances whose full four-event chain completes before `now`
    #    can be admitted — a patient who arrived twenty minutes ago has not
    #    been registered, assessed, accepted and bedded down yet.
    # ------------------------------------------------------------------
    occupied = [b for b in beds if b["status"] == "Occupied"]
    rng.shuffle(occupied)

    eligible = [i for i, plan in enumerate(plans) if stamp(plan, 3) <= now]
    if len(eligible) < len(occupied):
        raise RuntimeError(
            f"only {len(eligible)} attendances can complete an admission chain "
            f"but {len(occupied)} beds are occupied — widen ED_WINDOW_HOURS or "
            f"lower the occupancy target"
        )
    for i, bed in zip(rng.sample(eligible, len(occupied)), occupied):
        plans[i]["bed"] = bed

    # ------------------------------------------------------------------
    # 3. Build each attendance.
    # ------------------------------------------------------------------
    events = []
    stale_flags = 0
    terminal_counts: dict[str, int] = {}
    beds_linked = 0

    for i, plan in enumerate(plans):
        att_id = f"ED-2026-{i + 1:05d}"
        arrival = plan["arrival"]
        age_minutes = (now - arrival).total_seconds() / 60
        bed = plan.get("bed")

        if bed is not None:
            # Specialty and sex come from the ward holding the bed, so this
            # admission satisfies the specialty and single-sex rules by
            # construction rather than by luck.
            _wid, _name, policy, ward_specialty, _total = ward_by_id[bed["ward_id"]]
            specialty = ward_specialty
            sex = policy if policy in ("Male", "Female") else pick(
                [("Male", 0.5), ("Female", 0.5)])
            chain = ["Registered", "Under Assessment", "Awaiting Bed", "Admitted"]
        else:
            specialty = pick(ED_SPECIALTIES)
            sex = pick([("Male", 0.5), ("Female", 0.5)])
            needs_bed = (age_minutes <= MAX_AWAITING_HOURS * 60
                         and rng.random() < NEEDS_BED_RATE)
            chain = ["Registered", "Under Assessment",
                     "Awaiting Bed" if needs_bed else "Discharged"]

        complaint = rng.choice([c for c, specs in COMPLAINTS if specialty in specs])
        triage = pick(TRIAGE)
        pseudo = nhs_number(rng.randint(100000, 9999999))

        # Truncate at `now`: the patient is in whatever state they have
        # actually reached. Step 0 is arrival itself, so there is always at
        # least one event.
        emitted = [(step, state, stamp(plan, step))
                   for step, state in enumerate(chain)
                   if stamp(plan, step) <= now]

        dta_ts = ""
        discharge_ts = ""
        allocated = ""
        rows_for_attendance = []

        for step, state, cursor in emitted:
            if state == "Awaiting Bed":
                dta_ts = fmt_ts(cursor, dirty)
            if state in ("Discharged", "Admitted"):
                discharge_ts = fmt_ts(cursor, dirty)
            if state == "Admitted":
                allocated = bed["bed_id"]
                beds_linked += 1

            # Point-in-time: each event records the breach position as at that
            # event, not as at now. For an in-flight attendance the newest row
            # is therefore already behind, and falls further behind every hour.
            los = (cursor - arrival).total_seconds() / 60
            rows_for_attendance.append([
                "",                                   # event_id, filled below
                noisy_id(att_id, dirty),
                pseudo,
                drift_case(sex, dirty, GENDER_VARIANTS),
                fmt_ts(cursor, dirty),
                fmt_ts(arrival, dirty),
                triage,
                complaint,
                specialty,
                str("Awaiting Bed" in chain),
                dta_ts,
                allocated,
                state,
                discharge_ts,
                str(los >= ED_TARGET_MINUTES),
            ])

        # D7 again, in the ED feed: flip the flag on the newest row for a
        # slice of attendances.
        if dirty and rng.random() < STALE_BREACH_FLAG_RATE:
            last = rows_for_attendance[-1]
            last[-1] = str(last[-1] != "True")
            stale_flags += 1

        terminal = emitted[-1][1]
        terminal_counts[terminal] = terminal_counts.get(terminal, 0) + 1

        for row in rows_for_attendance:
            row[0] = f"EVT-{len(events) + 1:06d}"
            events.append(row)

    # D8: shuffle so file order carries no information. A pipeline that groups
    # without sorting on event_timestamp will silently retain the wrong state.
    rng.shuffle(events)

    header = ["event_id", "attendance_id", "patient_pseudo_id", "patient_sex",
              "event_timestamp", "arrival_timestamp", "triage_category",
              "chief_complaint", "required_specialty", "admission_required",
              "decision_to_admit_timestamp", "allocated_bed_id", "disposition",
              "discharge_timestamp", "is_breached"]
    stats = {
        "attendances": N_ATTENDANCES,
        "event_rows": len(events),
        "events_per_attendance": round(len(events) / N_ATTENDANCES, 2),
        "terminal_states": dict(sorted(terminal_counts.items(),
                                       key=lambda kv: -kv[1])),
        "stale_breach_flags": stale_flags,
        "beds_linked": beds_linked,
    }
    return header, events, stats


# --------------------------------------------------------------------------
# Instructor key
# --------------------------------------------------------------------------

def write_key(out: Path, as_of: dt.date, dirty: bool, stats: dict) -> None:
    """The instructor key.

    Every figure here is computed from the written files AFTER Day 1 cleaning
    (see `analyse`). These are the numbers a correct submission produces. Raw
    row counts are reported separately and labelled as such, so nobody marks a
    trainee against a number that includes the defects they were asked to
    remove.
    """
    p1, p2, a = stats["p1"], stats["p2"], stats["analysis"]
    lines = [
        "# Generation report — instructor key",
        "",
        f"Generated **{dt.date.today().isoformat()}**, anchored to as-of date "
        f"**{as_of.isoformat()}**, seed `{SEED}`, mode "
        f"**{'dirty (trainee)' if dirty else 'clean (solution)'}**.",
        "",
        "Regenerate on the morning a cohort starts. Elapsed-time cohorts, ED length "
        "of stay and the stale breach flags are all relative to the as-of date and "
        "drift daily.",
        "",
        "> **Every figure below is post-cleaning** — trimmed keys, blank keys "
        "dropped, duplicate keys dropped. They are what a correct pipeline "
        "outputs, not what the raw CSVs contain. Raw row counts are given "
        "separately and labelled *raw*.",
        "",
        "## What a correct pipeline outputs",
        "",
        "| Dataset | Raw rows | After cleaning |",
        "| --- | ---: | ---: |",
        f"| `patients.csv` | {p1['patients']['raw_rows']} | **{a['clean_patients']}** |",
        f"| `rtt_pathways.csv` | {p1['pathways']['raw_rows']} | **{a['clean_pathways']}** |",
        f"| `clinics.csv` | {len(SPECIALTIES) * SITES_PER_SPECIALTY} | "
        f"{len(SPECIALTIES) * SITES_PER_SPECIALTY} |",
        f"| `ed_attendance_events.csv` | {p2['events']['event_rows']} | "
        f"**{a['resolved_attendances']}** (one row per attendance) |",
        f"| `hospital_beds.csv` | {p2['beds']['raw_rows']} | **{a['clean_beds']}** |",
        f"| `wards.csv` | {len(WARDS)} | {len(WARDS)} |",
        "",
        "Blanking a primary key destroys that row's identifier, so the cleaned "
        "counts sit slightly below the generated volumes. That is correct and "
        "expected — mark against this column.",
        "",
        "## Project 1 — RTT",
        "",
        f"- {p1['patients']['duplicate_rows']} duplicate patient rows and "
        f"{p1['pathways']['duplicate_rows']} duplicate pathway rows injected; "
        f"{p1['patients']['null_pk_rows']} null primary keys per file",
        f"- `clinics.csv` — {len(SPECIALTIES)} specialties x {SITES_PER_SPECIALTY} sites "
        f"= {len(SPECIALTIES) * SITES_PER_SPECIALTY} clinic rows",
        "",
        "### Waiting-time cohorts",
        "",
        "All pathways, active and paused. The stretch-goal waterfall may be built "
        "on either population — say which, and be consistent.",
        "",
        "| Cohort | Pathways |",
        "| --- | ---: |",
    ]
    for band, n in a["cohorts"].items():
        lines.append(f"| {band} | {n} |")
    lines += [
        "",
        f"Breach rate against the 92% standard: **{a['within_target_pct']}% within 18 weeks**.",
        "",
        "### Backlog clearance by specialty",
        "",
        "`active pathways / summed weekly capacity across the specialty's three "
        "sites`, counting only `clock_status == 'ACTIVE'`.",
        "",
        "| Specialty | Active | Weekly capacity | Clearance (weeks) |",
        "| --- | ---: | ---: | ---: |",
    ]
    for row in a["clearance"]:
        lines.append(f"| {row[0]} | {row[1]} | {row[2]} | {row[3]} |")
    over = [r[0] for r in a["clearance"] if r[3] > 20]
    lines += [
        "",
        f"**{len(over)} of {len(a['clearance'])} specialties exceed the 20-week "
        f"highlight threshold**: {', '.join(over) if over else 'none'}.",
        "",
        f"- `getEligibleExpeditedSlots` search space: each pathway has "
        f"**{SITES_PER_SPECIALTY - 1} alternative sites** in its specialty before "
        f"capacity filtering — a real object-set filter, not a single-row lookup.",
        "- Priority mix: " + ", ".join(f"{k} {v}" for k, v in a["priority"].items()),
        f"- `is_breached` disagrees with a live calculation on "
        f"**{a['stale_breach_rows']} cleaned rows** as of the as-of date, and "
        f"drifts further every day the file sits unregenerated.",
        "- `target_breach_date` is **not shipped** — trainees derive it from "
        "`referral_date` + 18 weeks.",
        "",
        "## Project 2 — ED flow",
        "",
        f"- `ed_attendance_events.csv` — **{p2['events']['event_rows']} event rows** for "
        f"**{a['resolved_attendances']} attendances** "
        f"({p2['events']['events_per_attendance']} per attendance)",
        "- Rows are **shuffled**; `arrival_timestamp` is constant within an attendance. "
        "A `group_by('attendance_id').last()` without a prior "
        "`sort('event_timestamp')` retains the wrong state.",
        f"- `event_timestamp` is strictly increasing within every attendance "
        f"(**{a['ordering_faults']} ordering faults** — must be 0, or the "
        f"documented resolution is not reproducible).",
        "",
        "### Resolved dispositions after a correct CDC collapse",
        "",
        "| Disposition | Attendances |",
        "| --- | ---: |",
    ]
    for k, v in a["terminal"].items():
        lines.append(f"| {k} | {v} |")
    lines += [
        "",
        f"The Day 4 worklist (`disposition == 'Awaiting Bed'`) holds "
        f"**{a['awaiting_bed']} patients**.",
        "",
        "### The 4-hour standard",
        "",
        f"- **{a['in_flight']} attendances are in flight** (Registered, Under "
        f"Assessment or Awaiting Bed).",
        f"- Length of stay across them: median **{a['los_median']} minutes**, "
        f"max **{a['los_max']}**. Against the {ED_TARGET_MINUTES}-minute standard, "
        f"**{a['ed_breached']} of {a['in_flight']} have breached** — so the metric "
        f"cards and countdown alerts discriminate rather than reading red for "
        f"everyone.",
        f"- `is_breached` on the newest event disagrees with a live calculation for "
        f"**{a['ed_stale_flags']} of those {a['in_flight']}**. Most of that is honest "
        f"temporal drift, not injected error: the flag was true when written and "
        f"the patient has been waiting ever since.",
        "",
        "### Bed estate",
        "",
        f"- **{a['clean_beds']} beds after cleaning** — "
        + ", ".join(f"{k} {v}" for k, v in sorted(a["bed_status"].items()))
        + ".",
        f"- Isolation: **{a['isolation_available']} available of "
        f"{a['isolation_total']}**.",
        f"- Occupied beds with no admitted attendance behind them: "
        f"**{a['ghost_beds']}** (must be 0 — occupancy is generated from the ED "
        f"feed, so the two cannot disagree).",
        "- Available beds by ward: "
        + ", ".join(f"{k} {v}" for k, v in a["available_by_ward"].items()) + ".",
        "",
        "### Ward pressure index",
        "",
        "| Ward | Occupied | Total | Pressure |",
        "| --- | ---: | ---: | ---: |",
    ]
    for row in a["pressure"]:
        band = "red" if row[3] >= 90 else "amber" if row[3] >= 75 else "green"
        lines.append(f"| {row[0]} | {row[1]} | {row[2]} | {row[3]}% ({band}) |")
    lines += [
        "",
        "The denominator is the ward's `total_beds` property, not the count of "
        "linked bed objects — those differ where a bed row lost its primary key. "
        "Both briefs say so explicitly.",
        "",
        "### Compatible-bed counts across patients awaiting a bed",
        "",
        "All four matching rules applied (availability, specialty, isolation, "
        "single-sex), against **deduplicated** beds.",
        "",
        "| Compatible beds | Patients |",
        "| ---: | ---: |",
    ]
    for k in sorted(p2["compat"]):
        lines.append(f"| {k} | {p2['compat'][k]} |")
    lines += [
        "",
        "The counts cluster because availability is a ward-level property: every "
        "Acute Medicine patient sees the same AMU stock. The spread comes from the "
        "single-sex split in General Surgery, the tight Cardiology ward, and the "
        "isolation rule.",
        "",
        f"A tail of **{p2['compat'].get(0, 0)} patients with zero matches** is "
        "intentional — one ward has no available side room, so sepsis and fever "
        "patients in that specialty have nowhere compatible to go. The empty state "
        "is worth showing. It should be a minority, not everyone.",
        "",
        "## Deliberate defects",
        "",
        "| Ref | Defect | Where |",
        "| --- | --- | --- |",
        "| D1 | Duplicate primary keys | patients, rtt_pathways, hospital_beds |",
        "| D2 | Null primary keys | patients, rtt_pathways, hospital_beds |",
        "| D3 | Mixed date formats (ISO + `DD/MM/YYYY`) | all date and timestamp columns |",
        "| D4 | Whitespace on join keys | patient_id, clinic_id, attendance_id |",
        "| D5 | Categorical casing drift (`Male`/`male`/`M`) | gender, patient_sex |",
        "| D6 | Blank optional categoricals | risk_category |",
        "| D7 | Stale precomputed `is_breached` | rtt_pathways, ed_attendance_events |",
        "| D8 | Shuffled CDC event stream | ed_attendance_events |",
        "| D9 | Status casing drift (`Available`/`AVAILABLE`) | hospital_beds.status |",
        "",
        "Duplicate rows are byte-identical copies, so which one survives does not "
        "change any downstream figure. Day 5 question 6 is phrased accordingly.",
        "",
        "Run with `--clean` to regenerate the same data with every defect suppressed, "
        "for building or checking a reference solution.",
        "",
    ]
    (out / "GENERATION_REPORT.md").write_text("\n".join(lines), encoding="utf-8")


# --------------------------------------------------------------------------
# Verification — recomputed from the written files
# --------------------------------------------------------------------------

def analyse(out: Path, as_of: dt.date) -> dict:
    """Recompute every published figure from the written files, AS CLEANED.

    This function deliberately applies the same Day 1 cleaning the briefs ask
    for — strip the key, drop blank keys, drop duplicate keys — before counting
    anything. The instructor key must state what a CORRECT pipeline produces,
    not what the raw extract contains.

    Counting the raw rows instead was wrong in six places at once: it reported
    2,400 patients and 3,000 pathways where a correct pipeline yields fewer
    (blanking a key destroys that row's id), it double-counted a duplicated
    Available bed into the compatible-bed distribution, and it inflated the
    per-specialty active counts enough to push a third specialty over the
    20-week clearance threshold that a correct pipeline leaves under it.
    """
    def read(p):
        with (out / p).open(encoding="utf-8") as fh:
            return list(csv.DictReader(fh))

    def clean(rows: list[dict], pk: str) -> list[dict]:
        """Day 1, in nine lines: trim, drop blank keys, drop duplicate keys."""
        seen, kept = set(), []
        for r in rows:
            key = r[pk].strip()
            if not key or key in seen:
                continue
            seen.add(key)
            kept.append({**r, pk: key})
        return kept

    def parse_date(s):
        s = s.strip()
        for f in ("%Y-%m-%d", "%d/%m/%Y"):
            try:
                return dt.datetime.strptime(s, f).date()
            except ValueError:
                pass
        return None

    def parse_ts(s):
        s = s.strip()
        for f in ("%Y-%m-%dT%H:%M", "%d/%m/%Y %H:%M"):
            try:
                return dt.datetime.strptime(s, f)
            except ValueError:
                pass
        return None

    # ---------------- Project 1 ----------------
    patients = clean(read("project_1_rtt/patients.csv"), "patient_id")
    pathways = clean(read("project_1_rtt/rtt_pathways.csv"), "pathway_id")
    clinics = read("project_1_rtt/clinics.csv")

    cohorts = {"<6w": 0, "6-12w": 0, "12-18w": 0, "18-52w": 0, "52w+": 0}
    active_by_spec: dict[str, int] = {}
    priority: dict[str, int] = {}
    within = total = stale_breach = 0
    for r in pathways:
        d = parse_date(r["referral_date"])
        if d is None:
            continue
        w = (as_of - d).days / 7
        key = ("<6w" if w < 6 else "6-12w" if w < 12 else "12-18w" if w < 18
               else "18-52w" if w < 52 else "52w+")
        cohorts[key] += 1
        priority[r["priority_band"]] = priority.get(r["priority_band"], 0) + 1
        total += 1
        if w < 18:
            within += 1
        if (r["is_breached"] == "True") != (w >= RTT_TARGET_WEEKS):
            stale_breach += 1
        if r["clock_status"] == "ACTIVE":
            code = r["specialty_code"].strip()
            active_by_spec[code] = active_by_spec.get(code, 0) + 1

    cap_by_spec: dict[str, int] = {}
    name_by_spec: dict[str, str] = {}
    for c in clinics:
        code = c["specialty_code"]
        cap_by_spec[code] = cap_by_spec.get(code, 0) + int(c["weekly_capacity"])
        name_by_spec[code] = c["specialty_name"]

    clearance = sorted(
        [[name_by_spec[c], n, cap_by_spec[c], round(n / cap_by_spec[c], 1)]
         for c, n in active_by_spec.items()],
        key=lambda r: -r[3],
    )

    # ---------------- Project 2 ----------------
    events = read("project_2_ed_flow/ed_attendance_events.csv")
    beds = clean(read("project_2_ed_flow/hospital_beds.csv"), "bed_id")
    ward_rows = read("project_2_ed_flow/wards.csv")
    wards = {w["ward_id"]: w for w in ward_rows}
    now = dt.datetime.combine(as_of, dt.time(20, 0))

    # The documented resolution: sort on event_timestamp, keep the last row.
    latest: dict[str, dict] = {}
    ordering_faults = 0
    seen_ts: dict[str, dt.datetime] = {}
    by_attendance: dict[str, list] = {}
    for e in events:
        aid = e["attendance_id"].strip()
        ts = parse_ts(e["event_timestamp"])
        if not aid or ts is None:
            continue
        by_attendance.setdefault(aid, []).append((ts, e))
    for aid, rows in by_attendance.items():
        rows.sort(key=lambda t: t[0])
        # Timestamps must be strictly increasing, or "the last event" is not
        # well defined and the marking key cannot be reproduced.
        if any(rows[i][0] >= rows[i + 1][0] for i in range(len(rows) - 1)):
            ordering_faults += 1
        latest[aid] = rows[-1][1]

    status_of = lambda b: b["status"].strip().lower()
    avail = [b for b in beds if status_of(b) == "available"]
    bed_status: dict[str, int] = {}
    for b in beds:
        k = status_of(b).capitalize()
        bed_status[k] = bed_status.get(k, 0) + 1

    occupied_ids = {b["bed_id"] for b in beds if status_of(b) == "occupied"}
    admitted = [a for a in latest.values() if a["disposition"] == "Admitted"]
    held_ids = {a["allocated_bed_id"].strip() for a in admitted
                if a["allocated_bed_id"].strip()}
    ghost_beds = len(occupied_ids - held_ids)

    pressure = []
    for w in ward_rows:
        total_beds = int(w["total_beds"])
        occ = sum(1 for b in beds
                  if b["ward_id"].strip() == w["ward_id"] and status_of(b) == "occupied")
        pressure.append([w["ward_name"], occ, total_beds,
                         round(occ / total_beds * 100, 1)])
    pressure.sort(key=lambda r: -r[3])

    norm_sex = {"m": "Male", "male": "Male", "f": "Female", "female": "Female"}
    compat: dict[int, int] = {}
    for a in latest.values():
        if a["disposition"] != "Awaiting Bed":
            continue
        sex = norm_sex.get(a["patient_sex"].strip().lower(), "")
        n = 0
        for b in avail:
            w = wards.get(b["ward_id"].strip())
            if not w or w["specialty_type"] != a["required_specialty"]:
                continue
            c = a["chief_complaint"]
            if ("Sepsis" in c or "Fever" in c) and b["is_isolation_capable"] != "True":
                continue
            if w["gender_policy"] in ("Male", "Female") and w["gender_policy"] != sex:
                continue
            n += 1
        compat[n] = compat.get(n, 0) + 1

    terminal: dict[str, int] = {}
    for a in latest.values():
        terminal[a["disposition"]] = terminal.get(a["disposition"], 0) + 1

    in_flight = [a for a in latest.values()
                 if a["disposition"] in ("Registered", "Under Assessment", "Awaiting Bed")]
    los = sorted((now - parse_ts(a["arrival_timestamp"])).total_seconds() / 60
                 for a in in_flight if parse_ts(a["arrival_timestamp"]))
    ed_stale = sum(1 for a in in_flight
                   if parse_ts(a["arrival_timestamp"])
                   and (a["is_breached"] == "True")
                   != (((now - parse_ts(a["arrival_timestamp"])).total_seconds() / 60)
                       >= ED_TARGET_MINUTES))

    return {
        "cohorts": cohorts,
        "clearance": clearance,
        "priority": dict(sorted(priority.items())),
        "within_target_pct": round(100 * within / total, 1) if total else 0,
        "stale_breach_rows": stale_breach,
        "clean_patients": len(patients),
        "clean_pathways": len(pathways),
        "compat": compat,
        "resolved_attendances": len(latest),
        "terminal": dict(sorted(terminal.items(), key=lambda kv: -kv[1])),
        "clean_beds": len(beds),
        "bed_status": bed_status,
        "isolation_total": sum(1 for b in beds if b["is_isolation_capable"] == "True"),
        "isolation_available": sum(1 for b in avail if b["is_isolation_capable"] == "True"),
        "available_by_ward": {w["ward_name"]: sum(
            1 for b in avail if b["ward_id"].strip() == w["ward_id"]) for w in ward_rows},
        "ghost_beds": ghost_beds,
        "ordering_faults": ordering_faults,
        "pressure": pressure,
        "in_flight": len(in_flight),
        "los_median": round(los[len(los) // 2]) if los else 0,
        "los_max": round(los[-1]) if los else 0,
        "ed_breached": sum(1 for v in los if v >= ED_TARGET_MINUTES),
        "ed_stale_flags": ed_stale,
        "awaiting_bed": terminal.get("Awaiting Bed", 0),
    }


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=".", help="output directory (default: cwd)")
    ap.add_argument("--as-of", default=None,
                    help="anchor date YYYY-MM-DD (default: today)")
    ap.add_argument("--clean", action="store_true",
                    help="suppress all deliberate defects (reference-solution mode)")
    ap.add_argument("--seed", type=int, default=SEED)
    args = ap.parse_args()

    global rng
    rng = random.Random(args.seed)

    as_of = dt.date.fromisoformat(args.as_of) if args.as_of else dt.date.today()
    dirty = not args.clean
    out = Path(args.out)
    p1 = out / "project_1_rtt"
    p2 = out / "project_2_ed_flow"

    ch, clinic_rows = build_clinics(dirty)
    write_csv(p1 / "clinics.csv", ch, clinic_rows)

    ph, patient_rows, patient_stats = build_patients(dirty, as_of)
    write_csv(p1 / "patients.csv", ph, patient_rows)
    patient_ids = [r[0].strip() for r in patient_rows if r[0].strip()]

    rh, pathway_rows, pathway_stats = build_pathways(dirty, as_of, patient_ids, clinic_rows)
    write_csv(p1 / "rtt_pathways.csv", rh, pathway_rows)

    wh, ward_rows = build_wards()
    write_csv(p2 / "wards.csv", wh, ward_rows)

    bh, bed_rows, bed_stats, beds = build_beds(dirty)
    write_csv(p2 / "hospital_beds.csv", bh, bed_rows)

    eh, event_rows, event_stats = build_ed_events(dirty, as_of, beds)
    write_csv(p2 / "ed_attendance_events.csv", eh, event_rows)

    a = analyse(out, as_of)
    stats = {
        "p1": {"patients": patient_stats, "pathways": pathway_stats,
               "cohorts": a["cohorts"], "clearance": a["clearance"],
               "priority": a["priority"], "within_target_pct": a["within_target_pct"]},
        "p2": {"beds": bed_stats, "events": event_stats, "compat": a["compat"]},
        "analysis": a,
    }
    write_key(out, as_of, dirty, stats)

    print(f"as-of {as_of}  seed {args.seed}  mode {'dirty' if dirty else 'clean'}")
    print(f"  project_1_rtt/  {len(patient_rows)} patients, {len(pathway_rows)} pathways, "
          f"{len(clinic_rows)} clinics")
    print(f"  project_2_ed_flow/  {len(event_rows)} events "
          f"({event_stats['attendances']} attendances), {len(bed_rows)} beds")
    print(f"  clean rows: {a['clean_patients']} patients, "
          f"{a['clean_pathways']} pathways, {a['resolved_attendances']} attendances, "
          f"{a['clean_beds']} beds")
    print(f"  cohorts {a['cohorts']}")
    print(f"  clearance weeks {a['clearance'][0][3]} (max) .. {a['clearance'][-1][3]} (min)")
    print(f"  compatible-bed counts {dict(sorted(a['compat'].items()))}")
    print(f"  ED in flight {a['in_flight']} (LOS median {a['los_median']}m, "
          f"{a['ed_breached']} breached), awaiting bed {a['awaiting_bed']}")
    print(f"  ghost beds {a['ghost_beds']}, ordering faults {a['ordering_faults']}")
    print(f"  GENERATION_REPORT.md written")


if __name__ == "__main__":
    main()
