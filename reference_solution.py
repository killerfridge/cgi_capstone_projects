"""
RTT capstone - reference solution for Project 1.

Reads the six raw feeds from sample/ and writes the expected outputs to
expected/. Deliberately plain stdlib Python: this is the *specification* of what
each dataset should contain, not a model of how to write a Foundry transform.
Trainees produce the same content with Polars and PySpark inside code repos.

Run after make_sample.py.
"""
import csv
import datetime as dt
import itertools
from pathlib import Path

import sys

# python3 reference_solution.py [src_dir] [out_dir]
SRC = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("sample")
OUT = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("expected")
OUT.mkdir(exist_ok=True)
SNAPSHOT = dt.date(2026, 3, 31)

# Code sets and display wording come from the reference feed, not from literals
# in this file. National code sets change; a code deploy should not be the way
# you respond to that.
_rtt = {r["rtt_status_code"]: r for r in
        csv.DictReader((SRC / "ref_rtt_status.csv").open())}
START_CODES = {c for c, r in _rtt.items() if r["clock_effect"] == "START"}
STOP_CODES = {c for c, r in _rtt.items() if r["clock_effect"] == "STOP"}
STOP_REASON = {"30": "TREATED", "31": "ACTIVE_MONITORING", "32": "ACTIVE_MONITORING",
               "33": "DNA_FIRST", "34": "NO_TREATMENT", "35": "DECLINED", "36": "DIED"}

DEDUP_WINDOW_DAYS = 30       # step 3 - parameterised on purpose

# Not everything the pipeline notices is somebody's job. Anything the pipeline
# resolved on its own is a build log entry; only rows needing a human decision
# become a Validation Task on a validator's queue.
LOG_CODES = {"DUPLICATE_REFERRAL_COLLAPSED", "DUPLICATE_EVENT_DISCARDED",
             "PROVIDER_SUCCESSION_APPLIED"}

exceptions = []


def read(name):
    return list(csv.DictReader((SRC / f"{name}.csv").open()))


def write(name, rows, cols):
    with (OUT / f"{name}.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def raise_exc(reason, severity, source, record_id, referral_id, detail, judgement="N"):
    exceptions.append(dict(
        seq=len(exceptions) + 1, reason_code=reason, severity=severity,
        source_dataset=source, record_id=record_id, referral_id=referral_id,
        detail=detail, requires_judgement=judgement))


def d(s):
    return dt.date.fromisoformat(s)


# =============================================================== step 2: refs

tfc = [dict(treatment_function_code=r["treatment_function_code"],
            treatment_function_name=r["treatment_function_name"],
            is_consultant_led=r["is_consultant_led"] == "Y")
       for r in read("ref_treatment_function")]
write("clean_rtt_status", list(_rtt.values()),
      ["rtt_status_code", "official_description", "clock_effect",
       "patient_facing_description"])

write("clean_treatment_function", tfc,
      ["treatment_function_code", "treatment_function_name", "is_consultant_led"])

prov_raw = read("raw_ods_providers")
# successor mapping: a code closing the day before another opens is a succession
closed = {p["provider_code"]: p["valid_to"] for p in prov_raw if p["valid_to"]}
opened = {p["valid_from"]: p["provider_code"] for p in prov_raw if not p["valid_to"]}
prov = []
for p in prov_raw:
    successor = ""
    if p["valid_to"]:
        nxt = (d(p["valid_to"]) + dt.timedelta(days=1)).isoformat()
        successor = opened.get(nxt, "")
    prov.append(dict(
        provider_code=p["provider_code"], provider_name=p["provider_name"],
        org_type=p["org_type"], valid_from=p["valid_from"],
        valid_to=p["valid_to"] or "", is_current="N" if p["valid_to"] else "Y",
        successor_code=successor))
write("clean_provider", prov,
      ["provider_code", "provider_name", "org_type", "valid_from", "valid_to",
       "is_current", "successor_code"])

prov_by_code = {p["provider_code"]: p for p in prov}


def resolve_provider(code, on_date):
    """Provider valid at on_date, following the succession chain if it has closed."""
    p = prov_by_code.get(code)
    if not p:
        return code, "UNKNOWN"
    if not p["valid_to"] or d(on_date) <= d(p["valid_to"]):
        return code, p["provider_name"]
    if p["successor_code"]:
        return resolve_provider(p["successor_code"], on_date)
    return code, p["provider_name"]


# ========================================================== step 3: referrals

def nhs_valid(num):
    if len(num) != 10 or not num.isdigit():
        return False
    total = sum(int(x) * w for x, w in zip(num[:9], range(10, 1, -1)))
    cd = 11 - (total % 11)
    cd = 0 if cd == 11 else cd
    return cd != 10 and cd == int(num[9])


ref_raw = sorted(read("raw_referrals"), key=lambda r: (r["nhs_number"],
                 r["treatment_function_code"], r["referral_received_date"]))

seen, referrals = {}, []
dedup_map = {}          # deduplicated referral_id -> surviving referral_id

for r in ref_raw:
    key = (r["nhs_number"], r["treatment_function_code"])
    prior = seen.get(key)
    if prior and (d(r["referral_received_date"])
                  - d(prior["referral_received_date"])).days <= DEDUP_WINDOW_DAYS:
        dedup_map[r["referral_id"]] = prior["referral_id"]
        raise_exc("DUPLICATE_REFERRAL_COLLAPSED", "LOW", "raw_referrals", r["referral_id"],
                  prior["referral_id"],
                  f"Same patient and treatment function as {prior['referral_id']}, "
                  f"{(d(r['referral_received_date']) - d(prior['referral_received_date'])).days}"
                  f" days apart. Collapsed, earlier clock start retained.")
        continue

    valid = nhs_valid(r["nhs_number"])
    if not valid:
        raise_exc("INVALID_NHS_NUMBER", "MEDIUM", "raw_referrals", r["referral_id"],
                  r["referral_id"],
                  "NHS number fails the Modulus 11 check. Pathway retained - the "
                  "identifier is wrong, the patient is not.", judgement="Y")

    # resolve as at the referral date - the code was valid when the referral was made
    resolved, _ = resolve_provider(r["provider_code"], r["referral_received_date"])

    seen[key] = r
    referrals.append(dict(
        referral_id=r["referral_id"], nhs_number=r["nhs_number"],
        nhs_number_valid="Y" if valid else "N",
        referral_received_date=r["referral_received_date"],
        referral_source=r["referral_source"],
        treatment_function_code=r["treatment_function_code"],
        provider_code=r["provider_code"], provider_code_resolved=resolved,
        priority=r["priority"]))

referrals.sort(key=lambda r: r["referral_id"])
write("clean_referral", referrals,
      ["referral_id", "nhs_number", "nhs_number_valid", "referral_received_date",
       "referral_source", "treatment_function_code", "provider_code",
       "provider_code_resolved", "priority"])

ref_by_id = {r["referral_id"]: r for r in referrals}


# =================================================== step 4: conform activity

RTT_TEXT = {c: r["patient_facing_description"] for c, r in _rtt.items()}
ATT_TEXT = {"ATTENDED": "Seen in clinic", "DNA": "Did not attend",
            "PATIENT_CANCELLED": "Patient cancelled - clock continues",
            "PROVIDER_CANCELLED": "Hospital cancelled - clock continues"}

stream = []
for e in read("raw_pathway_events"):
    # a collapsed duplicate carries duplicate RTT status events. Reparenting them
    # onto the survivor would open a second clock and re-inflate the waiting list,
    # which is the very thing the deduplication was for. Discard them instead.
    if e["referral_id"] in dedup_map:
        raise_exc("DUPLICATE_EVENT_DISCARDED", "LOW", "raw_pathway_events", e["event_id"],
                  dedup_map[e["referral_id"]],
                  f"Status event on {e['referral_id']}, which collapsed into "
                  f"{dedup_map[e['referral_id']]}. Discarded - reparenting it would "
                  f"open a duplicate clock.")
        continue
    if e["referral_id"] not in ref_by_id:
        raise_exc("REFERRAL_NOT_FOUND", "HIGH", "raw_pathway_events", e["event_id"],
                  e["referral_id"],
                  f"Quotes referral {e['referral_id']}, which is not in raw_referrals.")
        continue
    stream.append(dict(
        source_record_id=e["event_id"], referral_id=e["referral_id"],
        event_date=e["event_date"], event_source="PAS_RTT",
        rtt_status_code=e["rtt_status_code"], attendance_status="",
        admission_method="", appointment_communicated="",
        is_care_activity="N", description=RTT_TEXT.get(e["rtt_status_code"], "Unknown status")))

for o in read("raw_outpatient_attendances"):
    # care activity, unlike a status event, is real and must follow the survivor
    o["referral_id"] = dedup_map.get(o["referral_id"], o["referral_id"])
    if not o["referral_id"]:
        raise_exc("NO_REFERRAL_LINK", "HIGH", "raw_outpatient_attendances",
                  o["appointment_id"], "",
                  "Attendance carries no referral linkage - cannot be attributed to a pathway.")
        continue
    if o["referral_id"] not in ref_by_id:
        raise_exc("REFERRAL_NOT_FOUND", "HIGH", "raw_outpatient_attendances",
                  o["appointment_id"], o["referral_id"],
                  f"Quotes referral {o['referral_id']}, which is not in raw_referrals.")
        continue
    stream.append(dict(
        source_record_id=o["appointment_id"], referral_id=o["referral_id"],
        event_date=o["appointment_date"], event_source="OUTPATIENT",
        rtt_status_code="", attendance_status=o["attendance_status"],
        admission_method="", appointment_communicated=o["appointment_communicated"],
        is_care_activity="Y" if o["attendance_status"] in ("ATTENDED", "DNA") else "N",
        description=ATT_TEXT.get(o["attendance_status"], o["attendance_status"])))

for a in read("raw_inpatient_admissions"):
    a["referral_id"] = dedup_map.get(a["referral_id"], a["referral_id"])
    elective = a["admission_method"].startswith("ELECTIVE")
    if not a["referral_id"]:
        if not elective:
            stream.append(dict(
                source_record_id=a["admission_id"], referral_id="", event_date=a["admission_date"],
                event_source="INPATIENT", rtt_status_code="", attendance_status="",
                admission_method=a["admission_method"], appointment_communicated="",
                is_care_activity="N", description="Emergency admission - not part of the RTT pathway"))
        continue
    stream.append(dict(
        source_record_id=a["admission_id"], referral_id=a["referral_id"],
        event_date=a["admission_date"], event_source="INPATIENT",
        rtt_status_code="", attendance_status="", admission_method=a["admission_method"],
        appointment_communicated="", is_care_activity="Y" if elective else "N",
        description="Admitted for treatment" if elective
                    else "Emergency admission - not part of the RTT pathway"))

stream.sort(key=lambda r: (r["referral_id"], r["event_date"], r["source_record_id"]))
for i, r in enumerate(stream, 1):
    r["pathway_event_id"] = f"PE{i:05d}"

EVENT_COLS = ["pathway_event_id", "referral_id", "event_date", "event_source",
              "rtt_status_code", "attendance_status", "admission_method",
              "appointment_communicated", "is_care_activity", "description",
              "source_record_id"]


# ======================================================= step 5: derive clocks

rtt_only = [r for r in stream if r["event_source"] == "PAS_RTT"]
groups = {}
for ref, grp in itertools.groupby(rtt_only, key=lambda r: r["referral_id"]):
    seq = 0
    for r in grp:
        if r["rtt_status_code"] in START_CODES:
            seq += 1
        groups.setdefault((ref, seq), []).append(r)

pathways = []
for (ref, seq), evs in sorted(groups.items()):
    if seq == 0:
        for e in evs:
            raise_exc("EVENT_BEFORE_CLOCK_START", "MEDIUM", "raw_pathway_events",
                      e["source_record_id"], ref,
                      f"Status {e['rtt_status_code']} dated {e['event_date']}, before any "
                      f"clock start on this referral. Event discarded; pathway retained.")
        continue
    stops = sorted([e for e in evs if e["rtt_status_code"] in STOP_CODES],
                   key=lambda e: e["event_date"])
    pathways.append(dict(
        pathway_id=f"{ref}-{seq}", referral_id=ref, clock_seq=seq,
        clock_start_date=min(e["event_date"] for e in evs),
        clock_stop_date=stops[0]["event_date"] if stops else "",
        stop_code=stops[0]["rtt_status_code"] if stops else ""))

# index the stream by referral once - scanning it per pathway is quadratic and
# becomes minutes rather than seconds at volume tier
stream_by_ref = {}
for e in stream:
    stream_by_ref.setdefault(e["referral_id"], []).append(e)

# attach each conformed event to the clock whose window contains it
for p in pathways:
    hi = p["clock_stop_date"] or "9999-12-31"
    for e in stream_by_ref.get(p["referral_id"], []):
        if p["clock_start_date"] <= e["event_date"] <= hi:
            e.setdefault("pathway_id", p["pathway_id"])
for e in stream:
    e.setdefault("pathway_id", "")
write("pathway_event", stream, EVENT_COLS[:1] + ["pathway_id"] + EVENT_COLS[1:])


# ==================================================== step 6: resolve the DNAs

for p in pathways:
    p["is_nullified"] = "N"
    if p["stop_code"] != "33":
        continue
    hi = p["clock_stop_date"]
    acts = [e for e in stream_by_ref.get(p["referral_id"], [])
            if e["is_care_activity"] == "Y"
            and p["clock_start_date"] <= e["event_date"] <= hi]
    is_first = bool(acts) and min(a["event_date"] for a in acts) == hi
    told = any(a["event_date"] == hi and a["attendance_status"] == "DNA"
               and a["appointment_communicated"] == "TRUE" for a in acts)

    if is_first and told:
        p["is_nullified"] = "Y"
    elif is_first and not told:
        raise_exc("STATUS_CONFLICT", "HIGH", "raw_pathway_events", p["pathway_id"],
                  p["referral_id"],
                  "Status 33 recorded, but the appointment was not demonstrably "
                  "communicated. Cannot nullify - clock continues.", judgement="Y")
        p["clock_stop_date"], p["stop_code"] = "", ""
    else:
        raise_exc("DNA_NOT_FIRST_ACTIVITY", "HIGH", "raw_pathway_events", p["pathway_id"],
                  p["referral_id"],
                  "Status 33 recorded, but an earlier care activity exists on this "
                  "clock. Misrecorded - clock continues.", judgement="Y")
        p["clock_stop_date"], p["stop_code"] = "", ""


# ================================================ step 7: classify and measure

admitted = {(e["referral_id"], e["event_date"]) for e in stream
            if e["event_source"] == "INPATIENT" and e["admission_method"].startswith("ELECTIVE")}


def band(w):
    return ("0-17" if w < 18 else "18-25" if w < 26 else
            "26-51" if w < 52 else "52-64" if w < 65 else "65+")


for p in pathways:
    r = ref_by_id[p["referral_id"]]
    p["nhs_number"] = r["nhs_number"]
    p["treatment_function_code"] = r["treatment_function_code"]
    p["provider_code"] = r["provider_code_resolved"]
    p["stop_reason"] = STOP_REASON.get(p["stop_code"], "")

    if p["is_nullified"] == "Y":
        p.update(pathway_status="NULLIFIED", weeks_waiting="", breach_band="",
                 within_18_weeks="")
        continue

    end = p["clock_stop_date"] or SNAPSHOT.isoformat()
    w = (d(end) - d(p["clock_start_date"])).days // 7
    if not p["clock_stop_date"]:
        status = "INCOMPLETE"
    elif (p["referral_id"], p["clock_stop_date"]) in admitted:
        status = "COMPLETED_ADMITTED"
    else:
        status = "COMPLETED_NON_ADMITTED"
    p.update(pathway_status=status, weeks_waiting=w, breach_band=band(w),
             within_18_weeks="Y" if w < 18 else "N")

pathways.sort(key=lambda p: p["pathway_id"])
write("pathway", pathways,
      ["pathway_id", "referral_id", "clock_seq", "nhs_number", "treatment_function_code",
       "provider_code", "clock_start_date", "clock_stop_date", "stop_code", "stop_reason",
       "pathway_status", "is_nullified", "weeks_waiting", "breach_band", "within_18_weeks"])


# ======================================================== step 8: publish PTL

tfc_name = {t["treatment_function_code"]: t["treatment_function_name"] for t in tfc}
ptl = []
for p in pathways:
    if p["pathway_status"] != "INCOMPLETE":
        continue
    # the PTL is read at the snapshot date, so it must name an organisation that
    # exists at the snapshot date - resolve the code, not only the name
    pcode, pname = resolve_provider(p["provider_code"], SNAPSHOT.isoformat())
    if pcode != p["provider_code"]:
        raise_exc("PROVIDER_SUCCESSION_APPLIED", "MEDIUM", "clean_provider", p["pathway_id"],
                  p["referral_id"],
                  f"Referred to {p['provider_code']}, which closed on "
                  f"{prov_by_code[p['provider_code']]['valid_to']}. Resolved through "
                  f"succession to {pcode} for the snapshot.", judgement="Y")
    ptl.append(dict(
        snapshot_date=SNAPSHOT.isoformat(), pathway_id=p["pathway_id"],
        nhs_number=p["nhs_number"], treatment_function_code=p["treatment_function_code"],
        treatment_function_name=tfc_name.get(p["treatment_function_code"], ""),
        provider_code=pcode, provider_code_as_referred=p["provider_code"],
        provider_name=pname,
        clock_start_date=p["clock_start_date"], weeks_waiting=p["weeks_waiting"],
        breach_band=p["breach_band"], within_18_weeks=p["within_18_weeks"]))

ptl.sort(key=lambda r: -int(r["weeks_waiting"]))
write("ptl_snapshot", ptl,
      ["snapshot_date", "pathway_id", "nhs_number", "treatment_function_code",
       "treatment_function_name", "provider_code", "provider_code_as_referred",
       "provider_name", "clock_start_date", "weeks_waiting", "breach_band",
       "within_18_weeks"])

# ---------------------------------- split: work for a human, versus a build log

pathway_of = {p["referral_id"]: p["pathway_id"] for p in pathways}

tasks, build_log = [], []
for e in exceptions:
    if e["reason_code"] in LOG_CODES:
        build_log.append(dict(
            log_id=f"BL{len(build_log) + 1:04d}",
            action_taken=e["reason_code"], source_dataset=e["source_dataset"],
            record_id=e["record_id"], detail=e["detail"],
            build_date=SNAPSHOT.isoformat()))
    else:
        tasks.append(dict(
            task_id=f"VT{len(tasks) + 1:04d}",
            pathway_id=pathway_of.get(e["referral_id"], ""),
            referral_id=e["referral_id"], source_record_id=e["record_id"],
            raised_reason=e["reason_code"], raised_at=SNAPSHOT.isoformat(),
            severity=e["severity"], detail=e["detail"],
            requires_judgement=e["requires_judgement"],
            # lifecycle - the pipeline only ever creates a task OPEN.
            # Everything below is written by Ruth, through Actions.
            status="OPEN", assigned_to="", outcome="",
            resolved_by="", resolved_at=""))

write("validation_task", tasks,
      ["task_id", "pathway_id", "referral_id", "source_record_id", "raised_reason",
       "raised_at", "severity", "detail", "requires_judgement",
       "status", "assigned_to", "outcome", "resolved_by", "resolved_at"])

write("build_log", build_log,
      ["log_id", "action_taken", "source_dataset", "record_id", "detail", "build_date"])


# ============================================================ reconciliation

within = [r for r in ptl if r["within_18_weeks"] == "Y"]
counts = {}
for p in pathways:
    counts[p["pathway_status"]] = counts.get(p["pathway_status"], 0) + 1

print("Expected outputs written to", OUT.resolve())
print(f"  clean_rtt_status          {len(_rtt):>3} rows")
print(f"  clean_treatment_function  {len(tfc):>3} rows")
print(f"  clean_provider            {len(prov):>3} rows")
print(f"  clean_referral            {len(referrals):>3} rows")
print(f"  pathway_event             {len(stream):>3} rows")
print(f"  pathway                   {len(pathways):>3} rows")
print(f"  ptl_snapshot              {len(ptl):>3} rows")
print(f"  validation_task           {len(tasks):>3} rows")
print(f"  build_log                 {len(build_log):>3} rows")
print("\nPathway status:")
for k in sorted(counts):
    print(f"  {k:<24} {counts[k]:>3}")
print(f"\nPTL at {SNAPSHOT}: {len(ptl)} incomplete, {len(within)} within 18 weeks "
      f"= {100 * len(within) / len(ptl):.1f}% compliance")
