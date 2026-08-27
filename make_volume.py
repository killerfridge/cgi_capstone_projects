"""
RTT capstone - volume generator.

Produces the same synthetic world as the seed cohort, at scale, in two tiers:

    dev   ~12,000 pathways   - iterate against this on a branch, builds in seconds
    full  ~250,000 pathways  - one acute Trust over three years, ~2M rows

Every case archetype and every defect class from the seed cohort appears here at
a plausible rate rather than as a single planted instance. Seeded throughout, so
two runs produce byte-identical files and the marking numbers stay stable.

    python3 make_volume.py dev
    python3 make_volume.py full
    python3 make_volume.py both

Late-arriving records for the incremental extension task are written separately
to <tier>_late/ and are NOT part of the required build.
"""
import csv
import datetime as dt
import random
import sys
from pathlib import Path

from rtt_status_codes import RTT_STATUS, HEADER as RTT_HEADER

SNAPSHOT = dt.date(2026, 3, 31)
HISTORY_START = dt.date(2023, 4, 1)

TIERS = {
    "dev": dict(pathways=12_000, seed=20260331),
    "full": dict(pathways=250_000, seed=20260331),
}

# ------------------------------------------------------------------ reference

TREATMENT_FUNCTIONS = [
    ("100", "General Surgery", 11), ("101", "Urology", 8),
    ("110", "Trauma and Orthopaedics", 19), ("120", "ENT", 14),
    ("130", "Ophthalmology", 16), ("300", "General Medicine", 4),
    ("320", "Cardiology", 7), ("330", "Dermatology", 9),
    ("400", "Neurology", 4), ("502", "Gynaecology", 8),
]
# Specialties under real pressure wait longer AND a larger share of them breach.
# The multiplier scales the WAITING_SHORT weight: below 1 means proportionally
# fewer patients seen inside 18 weeks. Without this the compliance rate comes out
# flat across every specialty and the day-5 Contour analysis has nothing to find.
LONG_TAIL = {"110", "120", "130"}
PRESSURE = {"110": 0.45, "120": 0.55, "130": 0.60}
PRESSURE_DEFAULT = 1.7

PROVIDERS = [
    ("RZA", "Northmoor Acute NHS Foundation Trust", "TRUST", "2013-04-01", ""),
    ("RZB", "Eastvale Community Hospitals NHS Trust", "TRUST", "2015-04-01", "2025-09-30"),
    ("RZC", "Kingsmere Treatment Centre", "INDEPENDENT", "2019-07-01", ""),
    ("RZD", "Eastvale and Northmoor NHS Foundation Trust", "TRUST", "2025-10-01", ""),
]

GP_PRACTICES = [f"G8{n:04d}" for n in range(1, 61)]
CLINICIANS = ["C.OKONKWO", "A.HUSSAIN", "J.MERCER", "L.BRIGHT", "S.PATEL",
              "M.ADEYEMI", "R.FIELDING", "T.NAKAMURA", "D.OSEI", "K.WHITLOCK"]

# archetype, weight - tuned so the incomplete list lands near 60% within 18 weeks
ARCHETYPES = [
    ("TREATED_ON_TIME",        26),
    ("TREATED_LATE",           10),
    ("WAITING_SHORT",          36),
    ("WAITING_MEDIUM",          9),
    ("WAITING_LONG",            5),
    ("WAITING_VERY_LONG",       1.2),
    ("DNA_FIRST_NULLIFIED",     6),
    ("DNA_REBOOK_TWO_CLOCKS",   4),
    ("SUBSEQUENT_DNA_WAITING",  4),
    ("AM_THEN_TREATED",         4),
    ("AM_THEN_DNA_TRAP",        2),
    ("CANCELLATIONS_WAITING",   3),
    ("EMERGENCY_MIDPATHWAY",    1),
    ("DECLINED",                1),
    ("NO_TREATMENT",            1),
]

# defect injection rates, applied on top of the archetypes
DEFECT_RATES = dict(
    duplicate_referral=0.004,
    invalid_nhs_number=0.003,
    event_before_start=0.002,
    orphan_activity=0.012,
    status_conflict=0.005,
    retired_provider=0.010,
)


# ------------------------------------------------------------- NHS numbers

def check_digit(first9):
    total = sum(int(d) * w for d, w in zip(first9, range(10, 1, -1)))
    cd = 11 - (total % 11)
    if cd == 11:
        cd = 0
    return None if cd == 10 else cd


def nhs_number(n):
    while True:
        first9 = f"999{n % 1_000_000:06d}"
        cd = check_digit(first9)
        if cd is not None:
            return first9 + str(cd)
        n += 1


def break_nhs(num):
    return num[:9] + str((int(num[9]) + 1) % 10)


# ------------------------------------------------------------------ helpers

def weighted(rng, pairs):
    total = sum(w for _, w in pairs)
    r = rng.uniform(0, total)
    upto = 0
    for item, w in pairs:
        upto += w
        if r <= upto:
            return item
    return pairs[-1][0]


def iso(d):
    return d.isoformat()


class Builder:
    """Accumulates rows for the six raw feeds."""

    def __init__(self, rng):
        self.rng = rng
        self.referrals, self.events, self.op, self.ip = [], [], [], []
        self.late_events, self.late_op = [], []
        self._ref = 0
        self._ev = 0
        self._op = 0
        self._ip = 0

    def next_referral_id(self):
        self._ref += 1
        return f"REF{self._ref:07d}"

    def add_referral(self, rid, nhs, received, tfc, provider, priority, source, gp):
        self.referrals.append((rid, nhs, iso(received), source, tfc, provider, priority, gp))

    def add_event(self, rid, date, code, by, late=False):
        self._ev += 1
        row = (f"EV{self._ev:08d}", rid, iso(date), code, by, "PAS")
        (self.late_events if late else self.events).append(row)

    def add_op(self, nhs, rid, date, status, communicated, tfc, outcome, late=False):
        self._op += 1
        row = (f"OP{self._op:08d}", nhs, rid, iso(date), status, communicated, tfc, outcome)
        (self.late_op if late else self.op).append(row)

    def add_ip(self, nhs, rid, admitted, method, tfc):
        self._ip += 1
        self.ip.append((f"IP{self._ip:08d}", nhs, rid, iso(admitted),
                        iso(admitted + dt.timedelta(days=self.rng.choice([0, 0, 1, 2]))),
                        method, self.rng.choice(["W3711", "H2011", "S0621", "C7121", "M4531"]), tfc))


# --------------------------------------------------------------- archetypes

def wait_days(rng, tfc, kind):
    """Weeks-waiting profile, with the long tail concentrated in a few specialties."""
    stretch = 1.5 if tfc in LONG_TAIL else 1.0
    if kind == "short":
        return rng.randint(7, 17 * 7)
    if kind == "medium":
        return rng.randint(18 * 7, int(25 * 7 * stretch))
    if kind == "long":
        return rng.randint(26 * 7, int(51 * 7 * stretch))
    if kind == "very_long":
        return rng.randint(52 * 7, int(66 * 7 * stretch))
    return rng.randint(7, 17 * 7)


def pick_archetype(rng, tfc):
    """Archetype mix varies by specialty - pressured ones see fewer patients in time."""
    m = PRESSURE.get(tfc, PRESSURE_DEFAULT)
    return weighted(rng, [(a, w * m if a == "WAITING_SHORT" else w)
                          for a, w in ARCHETYPES])


def build_pathway(b, rng, patient_nhs, tfc, provider, defects):
    """Emit one clinical story. Returns nothing; appends to the builder."""
    rid = b.next_referral_id()
    arch = pick_archetype(rng, tfc)
    priority = weighted(rng, [("ROUTINE", 84), ("URGENT", 12), ("TWO_WEEK_WAIT", 4)])
    source = weighted(rng, [("GP", 88), ("CONSULTANT", 8), ("SELF", 2), ("A&E", 2)])
    gp = rng.choice(GP_PRACTICES)
    clin = rng.choice(CLINICIANS)
    nhs = break_nhs(patient_nhs) if defects.get("invalid_nhs_number") else patient_nhs

    # ---- pick a clock start such that the story lands sensibly before snapshot
    if arch in ("TREATED_ON_TIME", "TREATED_LATE", "AM_THEN_TREATED",
                "AM_THEN_DNA_TRAP", "DNA_REBOOK_TWO_CLOCKS", "DECLINED",
                "NO_TREATMENT", "DNA_FIRST_NULLIFIED"):
        span = (SNAPSHOT - HISTORY_START).days - 120
        start = HISTORY_START + dt.timedelta(days=rng.randint(0, max(1, span)))
    else:
        kind = {"WAITING_SHORT": "short", "WAITING_MEDIUM": "medium",
                "WAITING_LONG": "long", "WAITING_VERY_LONG": "very_long",
                "SUBSEQUENT_DNA_WAITING": "long", "CANCELLATIONS_WAITING": "medium",
                "EMERGENCY_MIDPATHWAY": "medium"}.get(arch, "short")
        start = SNAPSHOT - dt.timedelta(days=wait_days(rng, tfc, kind))

    b.add_referral(rid, nhs, start, tfc, provider, priority, source, gp)
    b.add_event(rid, start, "10", "SYSTEM")

    def first_appt(offset_lo=28, offset_hi=90):
        return start + dt.timedelta(days=rng.randint(offset_lo, offset_hi))

    # ---------------------------------------------------------- the stories
    if arch in ("TREATED_ON_TIME", "TREATED_LATE"):
        seen = first_appt()
        treat_wk = rng.randint(9, 17) if arch == "TREATED_ON_TIME" else rng.randint(19, 46)
        treated = start + dt.timedelta(days=treat_wk * 7 + rng.randint(0, 6))
        if treated > SNAPSHOT:
            treated = SNAPSHOT - dt.timedelta(days=rng.randint(1, 30))
        if seen >= treated:
            seen = treated - dt.timedelta(days=rng.randint(7, 21))
        b.add_event(rid, seen, "20", clin)
        b.add_op(nhs, rid, seen, "ATTENDED", "TRUE", tfc, "LIST_FOR_PROCEDURE")
        b.add_event(rid, treated, "30", clin)
        b.add_ip(nhs, rid, treated, rng.choice(["ELECTIVE_WL", "ELECTIVE_BOOKED"]), tfc)

    elif arch.startswith("WAITING"):
        seen = first_appt()
        if seen < SNAPSHOT:
            b.add_event(rid, seen, "20", clin)
            b.add_op(nhs, rid, seen, "ATTENDED", "TRUE", tfc, "FOLLOW_UP_REQUIRED")

    elif arch == "DNA_FIRST_NULLIFIED":
        dna = first_appt()
        b.add_event(rid, dna, "33", "SYSTEM")
        told = "FALSE" if defects.get("status_conflict") else "TRUE"
        b.add_op(nhs, rid, dna, "DNA", told, tfc, "")

    elif arch == "DNA_REBOOK_TWO_CLOCKS":
        dna = first_appt()
        b.add_event(rid, dna, "33", "SYSTEM")
        b.add_op(nhs, rid, dna, "DNA", "TRUE", tfc, "")
        rebook = dna + dt.timedelta(days=rng.randint(14, 60))
        if rebook < SNAPSHOT:
            b.add_event(rid, rebook, "10", "SYSTEM")
            seen2 = rebook + dt.timedelta(days=rng.randint(21, 70))
            if seen2 < SNAPSHOT:
                b.add_event(rid, seen2, "20", clin)
                b.add_op(nhs, rid, seen2, "ATTENDED", "TRUE", tfc, "LIST_FOR_PROCEDURE")
                treated = seen2 + dt.timedelta(days=rng.randint(21, 90))
                if treated < SNAPSHOT:
                    b.add_event(rid, treated, "30", clin)
                    b.add_ip(nhs, rid, treated, "ELECTIVE_WL", tfc)

    elif arch == "SUBSEQUENT_DNA_WAITING":
        seen = first_appt()
        b.add_event(rid, seen, "20", clin)
        b.add_op(nhs, rid, seen, "ATTENDED", "TRUE", tfc, "FOLLOW_UP_REQUIRED")
        dna = seen + dt.timedelta(days=rng.randint(40, 140))
        if dna < SNAPSHOT:
            b.add_event(rid, dna, "20", "SYSTEM")
            b.add_op(nhs, rid, dna, "DNA", "TRUE", tfc, "")

    elif arch in ("AM_THEN_TREATED", "AM_THEN_DNA_TRAP"):
        seen = first_appt()
        b.add_event(rid, seen, "20", clin)
        b.add_op(nhs, rid, seen, "ATTENDED", "TRUE", tfc, "ACTIVE_MONITORING")
        am = seen + dt.timedelta(days=rng.randint(0, 40))
        b.add_event(rid, am, rng.choice(["31", "32"]), clin)
        resume = am + dt.timedelta(days=rng.randint(90, 300))
        if resume < SNAPSHOT:
            b.add_event(rid, resume, "11", clin)          # second clock opens
            nxt = resume + dt.timedelta(days=rng.randint(28, 80))
            if nxt < SNAPSHOT:
                if arch == "AM_THEN_DNA_TRAP":
                    # first care activity on the SECOND clock is a DNA
                    b.add_event(rid, nxt, "33", "SYSTEM")
                    b.add_op(nhs, rid, nxt, "DNA", "TRUE", tfc, "")
                else:
                    b.add_event(rid, nxt, "20", clin)
                    b.add_op(nhs, rid, nxt, "ATTENDED", "TRUE", tfc, "LIST_FOR_PROCEDURE")
                    treated = nxt + dt.timedelta(days=rng.randint(21, 84))
                    if treated < SNAPSHOT:
                        b.add_event(rid, treated, "30", clin)
                        b.add_ip(nhs, rid, treated, "ELECTIVE_WL", tfc)

    elif arch == "CANCELLATIONS_WAITING":
        d1 = first_appt(21, 60)
        b.add_op(nhs, rid, d1, "PROVIDER_CANCELLED", "TRUE", tfc, "")
        seen = d1 + dt.timedelta(days=rng.randint(21, 70))
        if seen < SNAPSHOT:
            b.add_event(rid, seen, "20", clin)
            b.add_op(nhs, rid, seen, "ATTENDED", "TRUE", tfc, "FOLLOW_UP_REQUIRED")
        for _ in range(rng.randint(1, 2)):
            c = seen + dt.timedelta(days=rng.randint(30, 160))
            if c < SNAPSHOT:
                b.add_op(nhs, rid, c, "PATIENT_CANCELLED", "TRUE", tfc, "")

    elif arch == "EMERGENCY_MIDPATHWAY":
        seen = first_appt()
        b.add_event(rid, seen, "20", clin)
        b.add_op(nhs, rid, seen, "ATTENDED", "TRUE", tfc, "LIST_FOR_PROCEDURE")
        emg = seen + dt.timedelta(days=rng.randint(20, 150))
        if emg < SNAPSHOT:
            b.add_event(rid, emg, "98", "SYSTEM")
            b.add_ip(nhs, "", emg, "EMERGENCY", "300")

    elif arch in ("DECLINED", "NO_TREATMENT"):
        seen = first_appt()
        b.add_event(rid, seen, "20", clin)
        b.add_op(nhs, rid, seen, "ATTENDED", "TRUE", tfc, "FOLLOW_UP_REQUIRED")
        end = seen + dt.timedelta(days=rng.randint(14, 120))
        if end < SNAPSHOT:
            b.add_event(rid, end, "35" if arch == "DECLINED" else "34", clin)

    # ------------------------------------------------------------- defects
    if defects.get("event_before_start"):
        b.add_event(rid, start - dt.timedelta(days=rng.randint(5, 40)), "30", clin)

    if defects.get("duplicate_referral"):
        dup = b.next_referral_id()
        dup_date = start + dt.timedelta(days=rng.randint(1, 12))
        b.add_referral(dup, nhs, dup_date, tfc, provider, priority, source, gp)
        b.add_event(dup, dup_date, "10", "SYSTEM")

    if defects.get("orphan_activity"):
        d = start + dt.timedelta(days=rng.randint(10, 200))
        if d < SNAPSHOT:
            if rng.random() < 0.5:
                b.add_op(nhs, "", d, "ATTENDED", "", tfc, "FOLLOW_UP_REQUIRED")
            else:
                b.add_op(nhs, f"REF{rng.randint(9_000_000, 9_999_999)}", d,
                         "ATTENDED", "TRUE", tfc, "FOLLOW_UP_REQUIRED")

    return arch


# ------------------------------------------------------------------- write

def write_csv(path, header, rows):
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


def generate(tier):
    cfg = TIERS[tier]
    rng = random.Random(cfg["seed"])
    out = Path(f"volume_{tier}")
    out.mkdir(exist_ok=True)
    late = Path(f"volume_{tier}_late")
    late.mkdir(exist_ok=True)

    b = Builder(rng)
    tfc_pairs = [(c, w) for c, _, w in TREATMENT_FUNCTIONS]
    arch_counts = {}

    n = cfg["pathways"]
    for i in range(n):
        # ~8% of patients hold more than one pathway
        patient = nhs_number(100_000 + int(i * 0.92) * 7919)
        tfc = weighted(rng, tfc_pairs)

        defects = {k: rng.random() < v for k, v in DEFECT_RATES.items()}
        if defects["retired_provider"]:
            provider = "RZB"
        else:
            provider = weighted(rng, [("RZA", 78), ("RZC", 14), ("RZD", 8)])

        arch = build_pathway(b, rng, patient, tfc, provider, defects)
        arch_counts[arch] = arch_counts.get(arch, 0) + 1

    # ---- late-arriving records: a second delivery for the incremental task.
    # Events dated before the snapshot that simply did not arrive in time.
    n_late = max(50, n // 200)
    sampled = rng.sample(b.referrals, min(n_late, len(b.referrals)))
    for r in sampled:
        rid, nhs, received = r[0], r[1], dt.date.fromisoformat(r[2])
        d = received + dt.timedelta(days=rng.randint(30, 300))
        if d < SNAPSHOT:
            b.add_event(rid, d, "20", rng.choice(CLINICIANS), late=True)
            b.add_op(nhs, rid, d, "ATTENDED", "TRUE", "110", "FOLLOW_UP_REQUIRED", late=True)

    write_csv(out / "ref_rtt_status.csv", RTT_HEADER, RTT_STATUS)
    write_csv(out / "ref_treatment_function.csv",
              ["treatment_function_code", "treatment_function_name", "is_consultant_led"],
              [(c, nme, "Y") for c, nme, _ in TREATMENT_FUNCTIONS])
    write_csv(out / "raw_ods_providers.csv",
              ["provider_code", "provider_name", "org_type", "valid_from", "valid_to"],
              PROVIDERS)
    write_csv(out / "raw_referrals.csv",
              ["referral_id", "nhs_number", "referral_received_date", "referral_source",
               "treatment_function_code", "provider_code", "priority", "referring_org_code"],
              b.referrals)
    write_csv(out / "raw_pathway_events.csv",
              ["event_id", "referral_id", "event_date", "rtt_status_code",
               "recorded_by", "source_system"], b.events)
    write_csv(out / "raw_outpatient_attendances.csv",
              ["appointment_id", "nhs_number", "referral_id", "appointment_date",
               "attendance_status", "appointment_communicated",
               "treatment_function_code", "outcome_code"], b.op)
    write_csv(out / "raw_inpatient_admissions.csv",
              ["admission_id", "nhs_number", "referral_id", "admission_date",
               "discharge_date", "admission_method", "primary_procedure_code",
               "treatment_function_code"], b.ip)

    write_csv(late / "raw_pathway_events.csv",
              ["event_id", "referral_id", "event_date", "rtt_status_code",
               "recorded_by", "source_system"], b.late_events)
    write_csv(late / "raw_outpatient_attendances.csv",
              ["appointment_id", "nhs_number", "referral_id", "appointment_date",
               "attendance_status", "appointment_communicated",
               "treatment_function_code", "outcome_code"], b.late_op)

    total = len(b.referrals) + len(b.events) + len(b.op) + len(b.ip)
    size = sum(p.stat().st_size for p in out.glob("*.csv")) / 1e6
    print(f"[{tier}] {len(b.referrals):,} referrals  {len(b.events):,} events  "
          f"{len(b.op):,} attendances  {len(b.ip):,} admissions")
    print(f"[{tier}] {total:,} rows total, {size:.1f} MB in {out}/")
    print(f"[{tier}] late delivery: {len(b.late_events):,} events, "
          f"{len(b.late_op):,} attendances in {late}/")
    return out


if __name__ == "__main__":
    which = sys.argv[1] if len(sys.argv) > 1 else "both"
    for t in (["dev", "full"] if which == "both" else [which]):
        generate(t)
