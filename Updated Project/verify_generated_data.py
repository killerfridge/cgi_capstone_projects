#!/usr/bin/env python3
"""Assert the generated data addresses every finding from the brief review."""
import csv, datetime as dt, sys, collections
from pathlib import Path

OUT = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
AS_OF = dt.date(2026, 8, 31)
fails = []

def read(p):
    with (OUT / p).open(encoding="utf-8") as fh:
        return list(csv.DictReader(fh))

def check(label, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{'  — ' + detail if detail else ''}")
    if not ok:
        fails.append(label)

def pdate(s):
    s = s.strip()
    for f in ("%Y-%m-%d", "%d/%m/%Y"):
        try: return dt.datetime.strptime(s, f).date()
        except ValueError: pass
    return None

def pts(s):
    s = s.strip()
    for f in ("%Y-%m-%dT%H:%M", "%d/%m/%Y %H:%M"):
        try: return dt.datetime.strptime(s, f)
        except ValueError: pass
    return None

def clean(rows, pk):
    """Day 1 in nine lines: trim the key, drop blanks, drop duplicates.

    Anything asserted about what a TRAINEE will see has to run on this, not on
    the raw rows. Counting raw rows previously let a duplicated Available bed
    into the compatible-bed distribution and inflated the per-specialty active
    counts past the 20-week clearance threshold.
    """
    seen, kept = set(), []
    for r in rows:
        key = r[pk].strip()
        if not key or key in seen:
            continue
        seen.add(key)
        kept.append({**r, pk: key})
    return kept

AS_OF_NOW = dt.datetime.combine(AS_OF, dt.time(20, 0))
ED_TARGET_MINUTES = 240

pat_raw = read("project_1_rtt/patients.csv")
pw_raw  = read("project_1_rtt/rtt_pathways.csv")
bd_raw  = read("project_2_ed_flow/hospital_beds.csv")

pat = pat_raw
pw  = pw_raw
cl  = read("project_1_rtt/clinics.csv")
ev  = read("project_2_ed_flow/ed_attendance_events.csv")
bd  = bd_raw
wd  = {w["ward_id"]: w for w in read("project_2_ed_flow/wards.csv")}

# Cleaned views — what a correct pipeline actually holds.
pat_c = clean(pat_raw, "patient_id")
pw_c  = clean(pw_raw, "pathway_id")
bd_c  = clean(bd_raw, "bed_id")

print("\nDay 1 has real work to do")
for name, rows, pk in (("patients", pat, "patient_id"),
                       ("rtt_pathways", pw, "pathway_id"),
                       ("hospital_beds", bd, "bed_id")):
    ids = [r[pk].strip() for r in rows]
    nulls = sum(1 for i in ids if not i)
    dups  = len(ids) - len(set(ids)) - (1 if nulls > 1 else 0)
    check(f"{name}: duplicate PKs present", dups > 0, f"{dups} dupes")
    check(f"{name}: null PKs present", nulls > 0, f"{nulls} nulls")

alt = sum(1 for r in pw if "/" in r["referral_date"])
check("mixed date formats present", alt > 0, f"{alt}/{len(pw)} rows DD/MM/YYYY")
ws = sum(1 for r in pw if r["patient_id"] != r["patient_id"].strip())
check("whitespace on join keys", ws > 0, f"{ws} padded patient_id refs")
gv = collections.Counter(r["gender"] for r in pat)
check("gender casing drift", len(gv) > 2, f"{len(gv)} distinct values")
blanks = sum(1 for r in pat if not r["risk_category"].strip())
check("blank optional categoricals", blanks > 0, f"{blanks} blank risk_category")
check("target_breach_date NOT shipped", "target_breach_date" not in pw[0])

print("\nReferential integrity survives the dirt")
pids = {r["patient_id"].strip() for r in pat if r["patient_id"].strip()}
cids = {r["clinic_id"].strip() for r in cl}
orph_p = sum(1 for r in pw if r["pathway_id"].strip() and r["patient_id"].strip() not in pids)
orph_c = sum(1 for r in pw if r["pathway_id"].strip() and r["clinic_id"].strip() not in cids)
check("no orphan patient refs (after trimming)", orph_p == 0, f"{orph_p} orphans")
check("no orphan clinic refs (after trimming)", orph_c == 0, f"{orph_c} orphans")

print("\nProject 1 — findings from the review")
clean_pw = [r for r in pw_c if pdate(r["referral_date"])]
weeks = {r["pathway_id"]: (AS_OF - pdate(r["referral_date"])).days / 7 for r in clean_pw}
c52 = sum(1 for w in weeks.values() if w >= 52)
check("52w+ cohort is populated", c52 > 0, f"{c52} pathways")

cap = collections.Counter()
for c in cl:
    cap[c["specialty_code"]] += int(c["weekly_capacity"])
act = collections.Counter(r["specialty_code"].strip() for r in clean_pw
                          if r["clock_status"] == "ACTIVE")
clear = {s: act[s] / cap[s] for s in act}
over20 = [s for s, v in clear.items() if v > 20]
check("three specialties exceed 20-week clearance", len(over20) == 3,
      f"{len(over20)} of {len(clear)}; range {min(clear.values()):.1f}-{max(clear.values()):.1f}w")
# Nothing may sit within half a week of the threshold: a knife-edge specialty
# flips between the key and a trainee's chart on a rounding difference.
edge = [s_ for s_, v in clear.items() if 19.5 <= v <= 20.5]
check("no specialty sits on the 20-week knife edge", not edge, f"{len(edge)} borderline")

p1 = sum(1 for r in clean_pw if r["priority_band"] == "P1")
check("P1 rows exist", p1 > 0, f"{p1} pathways")

sites = collections.Counter(c["specialty_code"] for c in cl)
check("multiple clinic sites per specialty", min(sites.values()) >= 3,
      f"{min(sites.values())}-{max(sites.values())} sites each")

stale = sum(1 for r in clean_pw
            if (r["is_breached"] == "True") != (weeks[r["pathway_id"]] >= 18))
check("is_breached is stale on some rows", stale > 0, f"{stale} rows already wrong")

print("\nProject 2 — findings from the review")
check("patient_sex column exists", "patient_sex" in ev[0])
per = collections.Counter(e["attendance_id"].strip() for e in ev)
multi = sum(1 for v in per.values() if v > 1)
check("CDC stream has multiple events per attendance", multi > 0,
      f"{multi}/{len(per)} attendances, {len(ev)/len(per):.2f} events each")

# The naive pipeline: sort on arrival_timestamp (constant per attendance) and
# take .last(). It must disagree with the correct answer, or the lesson is dead.
correct, naive = {}, {}
for e in sorted(ev, key=lambda r: (pts(r["arrival_timestamp"]) or dt.datetime.min)):
    naive[e["attendance_id"].strip()] = e["disposition"]
for e in sorted(ev, key=lambda r: (pts(r["event_timestamp"]) or dt.datetime.min)):
    correct[e["attendance_id"].strip()] = e["disposition"]
wrong = sum(1 for k in correct if naive.get(k) != correct[k])
check("naive sort-on-arrival gives a wrong answer", wrong > 0,
      f"{wrong}/{len(correct)} attendances resolve incorrectly")

avail = [b for b in bd_c if b["status"].strip().lower() == "available"]
iso_av = [b for b in avail if b["is_isolation_capable"] == "True"]
check("available beds are sufficient", len(avail) >= 20, f"{len(avail)} available")
check("isolation beds are available", len(iso_av) > 0,
      f"{len(iso_av)} of {sum(1 for b in bd if b['is_isolation_capable']=='True')}")

specs = {e["required_specialty"] for e in ev}
ward_specs = {w["specialty_type"] for w in wd.values()}
check("every ward specialty is reachable", ward_specs <= specs,
      f"unreachable: {ward_specs - specs or 'none'}")
check("every required specialty has a ward", specs <= ward_specs,
      f"unmatched: {specs - ward_specs or 'none'}")

norm = {"m": "Male", "male": "Male", "f": "Female", "female": "Female"}
awaiting = [e for k, e in
            {e["attendance_id"].strip(): e for e in
             sorted(ev, key=lambda r: (pts(r["event_timestamp"]) or dt.datetime.min))}.items()
            if e["disposition"] == "Awaiting Bed"]
dist = collections.Counter()
for a in awaiting:
    sex = norm.get(a["patient_sex"].strip().lower(), "")
    n = 0
    for b in avail:
        w = wd.get(b["ward_id"])
        if not w or w["specialty_type"] != a["required_specialty"]: continue
        c = a["chief_complaint"]
        if ("Sepsis" in c or "Fever" in c) and b["is_isolation_capable"] != "True": continue
        if w["gender_policy"] in ("Male", "Female") and w["gender_policy"] != sex: continue
        n += 1
    dist[n] += 1
zero = dist[0]
check("most awaiting patients have a match", (len(awaiting) - zero) / len(awaiting) > 0.7,
      f"{len(awaiting)-zero}/{len(awaiting)} matchable")
check("a minority have none (empty state is reachable)", 0 < zero < len(awaiting) * 0.3,
      f"{zero} with zero compatible beds")
check("single-sex wards are exercised", any(
    w["gender_policy"] in ("Male", "Female") for w in
    (wd[b["ward_id"]] for b in avail if b["ward_id"] in wd)))

print("\nThe marking key is reproducible from the data")
# The failure this section exists to catch: event_timestamp ordering that
# contradicts the clinical state sequence. It made the brief's own prescribed
# resolution disagree with the generator's intent for 15 of 400 attendances,
# and produced patients whose current state was `Registered` after later
# events had already been written.
by_att = {}
for e in ev:
    aid = e["attendance_id"].strip()
    ts = pts(e["event_timestamp"])
    if aid and ts:
        by_att.setdefault(aid, []).append((ts, e))
for rows in by_att.values():
    rows.sort(key=lambda t: t[0])

non_mono = sum(1 for rows in by_att.values()
               if any(rows[i][0] >= rows[i + 1][0] for i in range(len(rows) - 1)))
check("event_timestamp strictly increases within every attendance", non_mono == 0,
      f"{non_mono} attendances out of order or tied")

STATE_ORDER = {"Registered": 0, "Under Assessment": 1, "Awaiting Bed": 2,
               "Admitted": 3, "Discharged": 3}
disagree = sum(1 for rows in by_att.values()
               if [STATE_ORDER[e["disposition"]] for _, e in rows]
               != sorted(STATE_ORDER[e["disposition"]] for _, e in rows))
check("timestamp order agrees with clinical state order", disagree == 0,
      f"{disagree} attendances contradict the state sequence")

latest_state = {aid: rows[-1][1]["disposition"] for aid, rows in by_att.items()}
resolved = collections.Counter(latest_state.values())
# A patient cannot currently be `Registered` if later events exist for them.
stuck = sum(1 for aid, rows in by_att.items()
            if rows[-1][1]["disposition"] == "Registered" and len(rows) > 1)
check("no attendance resolves to Registered with later events", stuck == 0,
      f"{stuck} attendances")

print("\nThe 4-hour standard has signal")
in_flight = [rows[-1][1] for rows in by_att.values()
             if rows[-1][1]["disposition"] in
             ("Registered", "Under Assessment", "Awaiting Bed")]
los = [ (AS_OF_NOW - pts(a["arrival_timestamp"])).total_seconds() / 60
        for a in in_flight if pts(a["arrival_timestamp"]) ]
breached = sum(1 for v in los if v >= ED_TARGET_MINUTES)
check("in-flight attendances exist", len(los) > 20, f"{len(los)} in flight")
check("length of stay is clinically plausible", max(los) <= 12 * 60,
      f"median {sorted(los)[len(los)//2]:.0f}m, max {max(los):.0f}m")
# The metric cards have to separate patients, not colour everything red.
check("breaches are a genuine subset, not everyone",
      0.15 < breached / len(los) < 0.85,
      f"{breached}/{len(los)} breached ({100*breached/len(los):.0f}%)")

print("\nExisting occupancy obeys the rules trainees must implement")
bed_by_id = {b["bed_id"]: b for b in bd_c}
latest = {e["attendance_id"].strip(): e for e in
          sorted(ev, key=lambda r: (pts(r["event_timestamp"]) or dt.datetime.min))}
admitted = [e for e in latest.values() if e["disposition"] == "Admitted"]
bad_spec = bad_sex = no_bed = not_occ = 0
for a in admitted:
    bid = a["allocated_bed_id"].strip()
    if not bid:
        no_bed += 1; continue
    b = bed_by_id.get(bid)
    if not b:
        no_bed += 1; continue
    if b["status"].strip().lower() != "occupied":
        not_occ += 1
    w = wd.get(b["ward_id"])
    if w and w["specialty_type"] != a["required_specialty"]:
        bad_spec += 1
    sex = norm.get(a["patient_sex"].strip().lower(), "")
    if w and w["gender_policy"] in ("Male", "Female") and w["gender_policy"] != sex:
        bad_sex += 1
check("every admitted patient holds a bed", no_bed == 0, f"{no_bed} without one")
check("allocated beds are marked Occupied", not_occ == 0, f"{not_occ} mismatched")
check("allocations respect ward specialty", bad_spec == 0, f"{bad_spec} violations")
check("allocations respect single-sex policy", bad_sex == 0, f"{bad_sex} violations")
alloc = [a["allocated_bed_id"].strip() for a in admitted if a["allocated_bed_id"].strip()]
check("no bed holds two patients", len(alloc) == len(set(alloc)),
      f"{len(alloc)-len(set(alloc))} double-booked")

# The ghost-occupant check. The seed data previously shipped 68 Occupied beds
# against 51 admitted attendances, which contradicted the brief's own claim of
# internal consistency and undercut the trap it was meant to set up.
occupied_ids = {b["bed_id"] for b in bd_c if b["status"].strip().lower() == "occupied"}
held_ids = {a["allocated_bed_id"].strip() for a in admitted if a["allocated_bed_id"].strip()}
check("every occupied bed has an admitted patient behind it",
      not (occupied_ids - held_ids),
      f"{len(occupied_ids - held_ids)} occupied beds with nobody in them")
check("occupied bed count equals admitted attendance count",
      len(occupied_ids) == len(admitted),
      f"{len(occupied_ids)} occupied vs {len(admitted)} admitted")

print("\nEvery pressure band on the heatmap has a member")
bands = collections.Counter()
for w in wd.values():
    occ = sum(1 for b in bd_c if b["ward_id"].strip() == w["ward_id"]
              and b["status"].strip().lower() == "occupied")
    pct = occ / int(w["total_beds"]) * 100
    bands["red" if pct >= 90 else "amber" if pct >= 75 else "green"] += 1
check("at least one ward is in the red band", bands["red"] >= 1,
      f"green {bands['green']}, amber {bands['amber']}, red {bands['red']}")

print(f"\nresolved dispositions: {dict(resolved.most_common())}")
print(f"compatible-bed distribution: {dict(sorted(dist.items()))}")
print(f"cleaned row counts: {len(pat_c)} patients, {len(pw_c)} pathways, "
      f"{len(by_att)} attendances, {len(bd_c)} beds")
print(f"\n{'ALL CHECKS PASSED' if not fails else str(len(fails)) + ' FAILED: ' + ', '.join(fails)}")
sys.exit(1 if fails else 0)
