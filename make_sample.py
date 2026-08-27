"""
RTT capstone - representative sample of the six raw input feeds.

Every row here is hand-designed to demonstrate one specific RTT case or one
planted defect. This is the shape sample, not the volume generator: the full
generator will reproduce these patterns at scale with a fixed seed.

Snapshot date: 2026-03-31
"""
import csv
import datetime as dt
from pathlib import Path

from rtt_status_codes import RTT_STATUS, HEADER as RTT_HEADER

OUT = Path("sample")
OUT.mkdir(exist_ok=True)

SNAPSHOT = dt.date(2026, 3, 31)


# ---------------------------------------------------------------- NHS numbers

def check_digit(first9: str):
    """Modulus 11 check digit. Returns None if the number is unusable (rem==1)."""
    total = sum(int(d) * w for d, w in zip(first9, range(10, 1, -1)))
    rem = total % 11
    cd = 11 - rem
    if cd == 11:
        cd = 0
    if cd == 10:
        return None
    return cd


def nhs(seq: int) -> str:
    """Valid NHS number in the 999-prefixed synthetic test range."""
    n = seq
    while True:
        first9 = f"999{n:06d}"
        cd = check_digit(first9)
        if cd is not None:
            return first9 + str(cd)
        n += 1


def is_valid(num: str) -> bool:
    if len(num) != 10 or not num.isdigit():
        return False
    return check_digit(num[:9]) == int(num[9])


# 21 synthetic patients
P = {i: nhs(100000 + i * 7919) for i in range(1, 22)}

# P12 carries a deliberately invalid NHS number (check digit off by one)
bad = P[12]
P[12] = bad[:9] + str((int(bad[9]) + 1) % 10)
assert not is_valid(P[12])


def d(s):
    return dt.date.fromisoformat(s)


# ------------------------------------------------------------ ref / providers

treatment_functions = [
    ("100", "General Surgery", "Y"),
    ("101", "Urology", "Y"),
    ("110", "Trauma and Orthopaedics", "Y"),
    ("120", "ENT", "Y"),
    ("130", "Ophthalmology", "Y"),
    ("300", "General Medicine", "Y"),
    ("320", "Cardiology", "Y"),
    ("330", "Dermatology", "Y"),
    ("400", "Neurology", "Y"),
    ("502", "Gynaecology", "Y"),
]

# RZB is retired mid-period and succeeded by RZD - the reference-data drift case
providers = [
    ("RZA", "Northmoor Acute NHS Foundation Trust", "TRUST", "2013-04-01", ""),
    ("RZB", "Eastvale Community Hospitals NHS Trust", "TRUST", "2015-04-01", "2025-09-30"),
    ("RZC", "Kingsmere Treatment Centre", "INDEPENDENT", "2019-07-01", ""),
    ("RZD", "Eastvale and Northmoor NHS Foundation Trust", "TRUST", "2025-10-01", ""),
]


# ----------------------------------------------------------------- referrals

# referral_id, patient, received, source, tfc, provider, priority, referrer
referrals = [
    ("REF0001", 1,  "2025-06-02", "GP",         "110", "RZA", "ROUTINE",        "G81234"),
    ("REF0002", 2,  "2025-08-25", "GP",         "120", "RZA", "ROUTINE",        "G81234"),
    ("REF0003", 3,  "2026-01-12", "GP",         "130", "RZA", "ROUTINE",        "G84455"),
    ("REF0004", 4,  "2025-07-07", "GP",         "100", "RZA", "ROUTINE",        "G81234"),
    ("REF0006", 5,  "2024-11-18", "GP",         "110", "RZA", "ROUTINE",        "G84455"),
    ("REF0007", 6,  "2025-01-06", "GP",         "330", "RZA", "ROUTINE",        "G81234"),
    ("REF0008", 7,  "2025-02-10", "CONSULTANT", "320", "RZA", "URGENT",         "RZA"),
    ("REF0009", 8,  "2025-05-19", "GP",         "502", "RZA", "ROUTINE",        "G84455"),
    ("REF0010", 9,  "2025-06-16", "GP",         "110", "RZA", "ROUTINE",        "G81234"),
    ("REF0011", 10, "2025-09-08", "GP",         "120", "RZA", "ROUTINE",        "G87001"),
    ("REF0012", 10, "2025-09-10", "GP",         "120", "RZA", "ROUTINE",        "G87001"),
    ("REF0013", 12, "2025-10-06", "GP",         "130", "RZC", "ROUTINE",        "G84455"),
    ("REF0014", 13, "2025-12-01", "GP",         "100", "RZA", "TWO_WEEK_WAIT",  "G81234"),
    ("REF0015", 14, "2025-08-20", "GP",         "400", "RZB", "ROUTINE",        "G87001"),
    ("REF0016", 15, "2025-04-07", "A&E",        "320", "RZA", "URGENT",         "RZA"),
    ("REF0017", 16, "2025-07-28", "GP",         "101", "RZA", "ROUTINE",        "G84455"),
    # ordinary recent waiters, still inside 18 weeks - the sample needs a
    # non-zero compliance figure or the percentage cannot be tested
    ("REF0018", 18, "2026-01-26", "GP",         "110", "RZA", "ROUTINE",        "G81234"),
    ("REF0019", 19, "2026-02-09", "GP",         "130", "RZD", "ROUTINE",        "G87001"),
    ("REF0020", 20, "2025-12-15", "GP",         "502", "RZA", "URGENT",         "G84455"),
    ("REF0021", 21, "2026-01-05", "SELF",       "320", "RZA", "ROUTINE",        ""),
]


# ------------------------------------------------------------ pathway events

# referral_id, date, rtt_status_code, recorded_by, source_system
events = [
    # P01 clean pathway, treated at 11 weeks
    ("REF0001", "2025-06-02", "10", "SYSTEM",   "PAS"),
    ("REF0001", "2025-07-14", "20", "C.OKONKWO", "PAS"),
    ("REF0001", "2025-08-19", "30", "C.OKONKWO", "PAS"),

    # P02 still waiting at snapshot
    ("REF0002", "2025-08-25", "10", "SYSTEM",   "PAS"),
    ("REF0002", "2025-11-03", "20", "A.HUSSAIN", "PAS"),

    # P03 DNA at first care activity, nullified
    ("REF0003", "2026-01-12", "10", "SYSTEM",   "PAS"),
    ("REF0003", "2026-02-16", "33", "SYSTEM",   "PAS"),

    # P04 first-activity DNA, then rebooks -> SECOND clock on the same referral
    ("REF0004", "2025-07-07", "10", "SYSTEM",   "PAS"),
    ("REF0004", "2025-08-11", "33", "SYSTEM",   "PAS"),
    ("REF0004", "2025-09-01", "10", "SYSTEM",   "PAS"),
    ("REF0004", "2025-10-06", "20", "J.MERCER", "PAS"),
    ("REF0004", "2025-11-25", "30", "J.MERCER", "PAS"),

    # P05 subsequent DNA - clock keeps running, now 71 weeks
    ("REF0006", "2024-11-18", "10", "SYSTEM",   "PAS"),
    ("REF0006", "2025-01-20", "20", "C.OKONKWO", "PAS"),
    ("REF0006", "2025-04-14", "20", "SYSTEM",   "PAS"),

    # P06 active monitoring, then a decision to treat starts a new clock
    ("REF0007", "2025-01-06", "10", "SYSTEM",   "PAS"),
    ("REF0007", "2025-02-17", "32", "L.BRIGHT", "PAS"),
    ("REF0007", "2025-09-15", "11", "L.BRIGHT", "PAS"),
    ("REF0007", "2025-10-20", "20", "L.BRIGHT", "PAS"),
    ("REF0007", "2025-12-08", "30", "L.BRIGHT", "PAS"),

    # P07 THE TRAP - the second clock opens with a DNA
    ("REF0008", "2025-02-10", "10", "SYSTEM",   "PAS"),
    ("REF0008", "2025-03-24", "20", "S.PATEL",  "PAS"),
    ("REF0008", "2025-05-06", "31", "S.PATEL",  "PAS"),
    ("REF0008", "2025-11-10", "11", "S.PATEL",  "PAS"),
    ("REF0008", "2025-12-15", "33", "SYSTEM",   "PAS"),

    # P08 cancellations either side - none of them touch the clock
    ("REF0009", "2025-05-19", "10", "SYSTEM",   "PAS"),
    ("REF0009", "2025-07-21", "20", "M.ADEYEMI", "PAS"),

    # P09 an emergency admission mid-pathway that must not stop the clock
    ("REF0010", "2025-06-16", "10", "SYSTEM",   "PAS"),
    ("REF0010", "2025-08-04", "20", "C.OKONKWO", "PAS"),
    ("REF0010", "2025-10-13", "98", "SYSTEM",   "SUS"),

    # P10 duplicate referral - two clocks for one clinical intention
    ("REF0011", "2025-09-08", "10", "SYSTEM",   "PAS"),
    ("REF0011", "2025-12-01", "20", "A.HUSSAIN", "PAS"),
    ("REF0012", "2025-09-10", "10", "SYSTEM",   "PAS"),

    # P12 invalid NHS number on the referral
    ("REF0013", "2025-10-06", "10", "SYSTEM",   "PAS"),

    # P13 treatment recorded BEFORE the clock started
    ("REF0014", "2025-11-14", "30", "J.MERCER", "PAS"),
    ("REF0014", "2025-12-01", "10", "SYSTEM",   "PAS"),

    # P14 referred against a provider code that was retired mid-pathway
    ("REF0015", "2025-08-20", "10", "SYSTEM",   "PAS"),
    ("REF0015", "2025-10-27", "20", "R.FIELDING", "PAS"),

    # P15 died before treatment
    ("REF0016", "2025-04-07", "10", "SYSTEM",   "PAS"),
    ("REF0016", "2025-05-19", "20", "S.PATEL",  "PAS"),
    ("REF0016", "2025-08-30", "36", "SYSTEM",   "PAS"),

    # P16 code 33 recorded, but the appointment was never communicated
    ("REF0017", "2025-07-28", "10", "SYSTEM",   "PAS"),
    ("REF0017", "2025-09-01", "33", "SYSTEM",   "PAS"),

    # ordinary recent waiters, still within 18 weeks
    ("REF0018", "2026-01-26", "10", "SYSTEM",   "PAS"),
    ("REF0018", "2026-03-02", "20", "C.OKONKWO", "PAS"),
    ("REF0019", "2026-02-09", "10", "SYSTEM",   "PAS"),
    ("REF0020", "2025-12-15", "10", "SYSTEM",   "PAS"),
    ("REF0020", "2026-02-02", "20", "M.ADEYEMI", "PAS"),
    ("REF0021", "2026-01-05", "10", "SYSTEM",   "PAS"),

    # orphan - references a referral that does not exist
    ("REF9999", "2025-11-04", "20", "SYSTEM",   "PAS"),
]


# ------------------------------------------------------- outpatient activity

# appt_id, patient, referral_id, date, status, communicated, tfc, outcome
outpatient = [
    ("OP00001", 1,  "REF0001", "2025-07-14", "ATTENDED",           "TRUE",  "110", "LIST_FOR_PROCEDURE"),
    ("OP00002", 2,  "REF0002", "2025-11-03", "ATTENDED",           "TRUE",  "120", "FOLLOW_UP_REQUIRED"),
    ("OP00003", 3,  "REF0003", "2026-02-16", "DNA",                "TRUE",  "130", ""),
    ("OP00004", 4,  "REF0004", "2025-08-11", "DNA",                "TRUE",  "100", ""),
    ("OP00005", 4,  "REF0004", "2025-10-06", "ATTENDED",           "TRUE",  "100", "LIST_FOR_PROCEDURE"),
    ("OP00006", 5,  "REF0006", "2025-01-20", "ATTENDED",           "TRUE",  "110", "FOLLOW_UP_REQUIRED"),
    ("OP00007", 5,  "REF0006", "2025-04-14", "DNA",                "TRUE",  "110", ""),
    ("OP00008", 6,  "REF0007", "2025-02-17", "ATTENDED",           "TRUE",  "330", "ACTIVE_MONITORING"),
    ("OP00009", 6,  "REF0007", "2025-10-20", "ATTENDED",           "TRUE",  "330", "LIST_FOR_PROCEDURE"),
    ("OP00010", 7,  "REF0008", "2025-03-24", "ATTENDED",           "TRUE",  "320", "ACTIVE_MONITORING"),
    ("OP00011", 7,  "REF0008", "2025-12-15", "DNA",                "TRUE",  "320", ""),
    ("OP00012", 8,  "REF0009", "2025-06-30", "PROVIDER_CANCELLED", "TRUE",  "502", ""),
    ("OP00013", 8,  "REF0009", "2025-07-21", "ATTENDED",           "TRUE",  "502", "FOLLOW_UP_REQUIRED"),
    ("OP00014", 8,  "REF0009", "2025-09-08", "PATIENT_CANCELLED",  "TRUE",  "502", ""),
    ("OP00015", 8,  "REF0009", "2025-11-17", "PATIENT_CANCELLED",  "TRUE",  "502", ""),
    ("OP00016", 9,  "REF0010", "2025-08-04", "ATTENDED",           "TRUE",  "110", "LIST_FOR_PROCEDURE"),
    ("OP00017", 10, "REF0011", "2025-12-01", "ATTENDED",           "TRUE",  "120", "FOLLOW_UP_REQUIRED"),
    ("OP00018", 13, "REF0015", "2025-10-27", "ATTENDED",           "TRUE",  "400", "FOLLOW_UP_REQUIRED"),
    ("OP00019", 15, "REF0016", "2025-05-19", "ATTENDED",           "TRUE",  "320", "FOLLOW_UP_REQUIRED"),
    ("OP00020", 16, "REF0017", "2025-09-01", "DNA",                "FALSE", "101", ""),
    # referral linkage missing entirely
    ("OP00021", 17, "",        "2025-11-04", "ATTENDED",           "",      "300", "FOLLOW_UP_REQUIRED"),
    # points at a referral that is not in raw_referrals
    ("OP00022", 11, "REF9999", "2025-11-04", "ATTENDED",           "TRUE",  "300", "FOLLOW_UP_REQUIRED"),
    ("OP00023", 18, "REF0018", "2026-03-02", "ATTENDED",           "TRUE",  "110", "LIST_FOR_PROCEDURE"),
    ("OP00024", 20, "REF0020", "2026-02-02", "ATTENDED",           "TRUE",  "502", "FOLLOW_UP_REQUIRED"),
]


# -------------------------------------------------------- inpatient activity

# adm_id, patient, referral_id, admitted, discharged, method, procedure, tfc
inpatient = [
    ("IP00001", 1,  "REF0001", "2025-08-19", "2025-08-20", "ELECTIVE_WL",     "W3711", "110"),
    ("IP00002", 4,  "REF0004", "2025-11-25", "2025-11-25", "ELECTIVE_BOOKED", "H2011", "100"),
    ("IP00003", 6,  "REF0007", "2025-12-08", "2025-12-08", "ELECTIVE_WL",     "S0621", "330"),
    # emergency, unrelated to the elective pathway - must not stop the clock
    ("IP00004", 9,  "",        "2025-10-13", "2025-10-18", "EMERGENCY",       "E8541", "300"),
    # treatment dated before the clock start
    ("IP00005", 13, "REF0014", "2025-11-14", "2025-11-15", "ELECTIVE_WL",     "H0201", "100"),
]


# ------------------------------------------------------------------- writers

def write(name, header, rows):
    path = OUT / f"{name}.csv"
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)
    return path


write("ref_rtt_status", RTT_HEADER, RTT_STATUS)

write("ref_treatment_function",
      ["treatment_function_code", "treatment_function_name", "is_consultant_led"],
      treatment_functions)

write("raw_ods_providers",
      ["provider_code", "provider_name", "org_type", "valid_from", "valid_to"],
      providers)

write("raw_referrals",
      ["referral_id", "nhs_number", "referral_received_date", "referral_source",
       "treatment_function_code", "provider_code", "priority", "referring_org_code"],
      [(r[0], P[r[1]], r[2], r[3], r[4], r[5], r[6], r[7]) for r in referrals])

write("raw_pathway_events",
      ["event_id", "referral_id", "event_date", "rtt_status_code", "recorded_by", "source_system"],
      [(f"EV{i:05d}", e[0], e[1], e[2], e[3], e[4]) for i, e in enumerate(events, 1)])

write("raw_outpatient_attendances",
      ["appointment_id", "nhs_number", "referral_id", "appointment_date",
       "attendance_status", "appointment_communicated", "treatment_function_code", "outcome_code"],
      [(o[0], P[o[1]], o[2], o[3], o[4], o[5], o[6], o[7]) for o in outpatient])

write("raw_inpatient_admissions",
      ["admission_id", "nhs_number", "referral_id", "admission_date", "discharge_date",
       "admission_method", "primary_procedure_code", "treatment_function_code"],
      [(a[0], P[a[1]], a[2], a[3], a[4], a[5], a[6], a[7]) for a in inpatient])


# -------------------------------------------------------------- verification

print("NHS numbers (999-prefixed synthetic test range):")
for k in sorted(P):
    flag = "VALID" if is_valid(P[k]) else "*** INVALID (planted) ***"
    print(f"  P{k:02d}  {P[k]}  {flag}")

# ---------------------------------------------- derive the expected answer key
# Derived, not hard-coded: this applies the same rules the trainees must apply,
# so the key can never drift out of step with the data above.

import itertools

START_CODES = {"10", "11", "12"}
STOP_CODES = {"30", "31", "32", "33", "34", "35", "36"}
STOP_REASON = {"30": "TREATED", "31": "ACTIVE_MONITORING", "32": "ACTIVE_MONITORING",
               "33": "DNA_FIRST", "34": "NO_TREATMENT", "35": "DECLINED", "36": "DIED"}
DEDUPED = {"REF0012"}          # collapses into REF0011

ev_rows = sorted(
    [dict(event_id=f"EV{i:05d}", referral_id=e[0], event_date=e[1], code=e[2])
     for i, e in enumerate(events, 1)],
    key=lambda r: (r["referral_id"], r["event_date"], r["event_id"]),
)
ref_ids = {r[0] for r in referrals}

# split each referral's events into clocks
groups = {}
for ref, grp in itertools.groupby(ev_rows, key=lambda r: r["referral_id"]):
    seq = 0
    for r in grp:
        if r["code"] in START_CODES:
            seq += 1
        groups.setdefault((ref, seq), []).append(r)

pathways, orphan_events = [], []
for (ref, seq), evs in sorted(groups.items()):
    if seq == 0:
        orphan_events += [(ref, e["event_id"]) for e in evs]
        continue
    if ref in DEDUPED or ref not in ref_ids:
        continue
    stops = [e for e in evs if e["code"] in STOP_CODES]
    stops.sort(key=lambda e: e["event_date"])
    pathways.append(dict(
        ref=ref, seq=seq,
        start=min(e["event_date"] for e in evs),
        stop=stops[0]["event_date"] if stops else None,
        code=stops[0]["code"] if stops else None,
    ))

# verify every code-33 claim against the attendance feed
op_by_ref = {}
for o in outpatient:
    op_by_ref.setdefault(o[2], []).append(o)
ip_by_ref = {}
for a in inpatient:
    ip_by_ref.setdefault(a[2], []).append(a)

for p in pathways:
    p["nullified"] = False
    if p["code"] != "33":
        continue
    lo, hi = p["start"], p["stop"]
    acts = [o[3] for o in op_by_ref.get(p["ref"], []) if lo <= o[3] <= hi]
    acts += [a[3] for a in ip_by_ref.get(p["ref"], []) if lo <= a[3] <= hi]
    is_first = bool(acts) and min(acts) == hi
    told = any(o[3] == hi and o[4] == "DNA" and o[5] == "TRUE"
               for o in op_by_ref.get(p["ref"], []))
    if is_first and told:
        p["nullified"] = True
    else:
        p["stop"], p["code"] = None, None      # cannot nullify -> clock runs on


def weeks(a, b):
    return (d(b) - d(a)).days // 7


def band(w):
    return ("0-17" if w < 18 else "18-25" if w < 26 else
            "26-51" if w < 52 else "52-64" if w < 65 else "65+")


print("\nExpected clock outcomes (what a correct pipeline must produce):")
print(f"  {'pathway':<16}{'start':<12}{'stop':<12}{'wks':>4}  {'band':<7} outcome")
n_inc = n_cmp = n_nul = 0
for p in sorted(pathways, key=lambda x: (x["ref"], x["seq"])):
    name = f"{p['ref']} c{p['seq']}"
    if p["nullified"]:
        n_nul += 1
        print(f"  {name:<16}{p['start']:<12}{p['stop']:<12}{'-':>4}  {'n/a':<7} NULLIFIED / excluded")
        continue
    end = p["stop"] or SNAPSHOT.isoformat()
    w = weeks(p["start"], end)
    if p["stop"]:
        n_cmp += 1
        outcome = "completed / " + STOP_REASON[p["code"]]
    else:
        n_inc += 1
        outcome = "incomplete"
    print(f"  {name:<16}{p['start']:<12}{(p['stop'] or '-'):<12}{w:>4}  {band(w):<7} {outcome}")

live = [p for p in pathways if not p["nullified"] and not p["stop"]]
within = [p for p in live if weeks(p["start"], SNAPSHOT.isoformat()) < 18]

print(f"\nEvents that belong to no clock (arrived before any clock start):")
for ref, eid in orphan_events:
    print(f"  {eid}  {ref}  -> validation_task")

print(f"\n{len(pathways)} pathways: {n_cmp} completed, {n_nul} nullified, {n_inc} incomplete")
print(f"PTL at {SNAPSHOT}: {len(live)} incomplete, {len(within)} within 18 weeks "
      f"= {100 * len(within) / len(live):.1f}% compliance")
print(f"\nFiles written to {OUT.resolve()}")
