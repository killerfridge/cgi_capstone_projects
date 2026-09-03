"""Render the cleaned-output companion page from expected/."""
import csv
import html
from pathlib import Path

SRC = Path("expected")

NOTES = {
    "clean_referral": {
        "REF0013": "NHS number failed Modulus 11 &mdash; flagged, <b>not dropped</b>",
        "REF0015": "Referred to RZB, still valid on the referral date",
    },
    "pathway": {
        "REF0004-1": "Nullified by a first-activity DNA",
        "REF0004-2": "<b>Second clock on the same referral</b> &mdash; the grain trap",
        "REF0007-2": "Second clock, opened by a decision to treat",
        "REF0008-2": "<b>The trap.</b> First activity on clock 2 was a DNA &rarr; nullified",
        "REF0013-1": "Survives with an invalid identifier",
        "REF0014-1": "Survives; only the impossible event was discarded",
        "REF0017-1": "Status 33 could not nullify &mdash; still running at 35 weeks",
    },
    "ptl_snapshot": {
        "REF0013-1": "The invalid NHS number is still a person waiting",
        "REF0014-1": "17 weeks &mdash; inside 18, and easily lost by a naive filter",
        "REF0015-1": "Provider resolved RZB &rarr; RZD for the snapshot",
        "REF0017-1": "The 35-week waiter a trusting pipeline deletes",
    },
    "validation_task": {},
    "build_log": {},
    "clean_provider": {
        "RZB": "Closed &mdash; succeeded by RZD",
        "RZD": "The successor",
    },
}

COLS = {
    "clean_provider": ["provider_code", "provider_name", "org_type", "valid_from",
                       "valid_to", "is_current", "successor_code"],
    "clean_referral": ["referral_id", "nhs_number", "nhs_number_valid",
                       "referral_received_date", "treatment_function_code",
                       "provider_code", "priority"],
    "pathway": ["pathway_id", "referral_id", "clock_seq", "clock_start_date",
                "clock_stop_date", "stop_reason", "pathway_status",
                "breach_18wk_date", "breach_52wk_date"],
    "ptl_snapshot": ["pathway_id", "treatment_function_name", "provider_code",
                     "clock_start_date", "breach_18wk_date", "weeks_waiting",
                     "breach_band", "within_18_weeks"],
    "validation_task": ["task_id", "pathway_id", "raised_reason", "severity",
                        "requires_judgement", "status", "assigned_to", "outcome", "detail"],
    "build_log": ["log_id", "action_taken", "record_id", "detail"],
}

CAPTIONS = {
    "clean_provider": "Type 2 dimension with the succession made explicit. <code>successor_code</code> is derived, not given &mdash; a code closing the day before another opens is a succession.",
    "clean_referral": "Nineteen rows from twenty. REF0012 is gone, collapsed into REF0011. Nothing else was dropped.",
    "pathway": "One row per <em>clock</em>, not per referral. Twenty-two rows from nineteen referrals, because three referrals carry two clocks each. Note what is <em>not</em> here: no week count, no breach band. Those are functions of a date, and this table is not measured as at any date &mdash; it carries the breach dates instead, which do not move as the calendar advances.",
    "ptl_snapshot": "The thirteen people still waiting on 31 March 2026, longest first. This is the one place the elapsed measures are computed, and they are frozen here: &ldquo;as at 31 March&rdquo; is a statutory return, and a published figure that moves when you rebuild is not reproducible. This is what the Ontology sits on in project 2.",
    "validation_task": "Six rows &mdash; work for a human, created OPEN by the pipeline. The lifecycle columns to the right are empty until Ruth acts, and every one of them is written by an Action.",
    "build_log": "Three rows &mdash; what the pipeline did to itself. Nobody acts on these, so they never reach a validator's queue.",
}

ORDER = ["clean_provider", "clean_referral", "pathway", "ptl_snapshot",
         "validation_task", "build_log"]


def table(name):
    rows = list(csv.DictReader((SRC / f"{name}.csv").open()))
    cols = COLS[name]
    notes = NOTES[name]
    has_notes = bool(notes)
    key = cols[0]

    out = ['<div class="table-scroll tall"><table><thead><tr>']
    for c in cols:
        out.append(f"<th>{html.escape(c)}</th>")
    if has_notes:
        out.append('<th class="note-col">Why this row matters</th>')
    out.append("</tr></thead><tbody>")

    for r in rows:
        note = notes.get(r[key], "")
        flag = ' data-flag="1"' if note else ""
        out.append(f"<tr{flag}>")
        for c in cols:
            v = r.get(c, "")
            if c == "detail":
                cell = f'<span class="detail">{html.escape(v)}</span>'
            elif v == "":
                cell = '<span class="null">&mdash;</span>'
            elif c == "requires_judgement":
                # not a good/bad value - a routing flag. Neutral chip, or nothing.
                cell = '<span class="yn yn-j">judgement</span>' if v == "Y" else '<span class="null">&mdash;</span>'
            elif v in ("Y", "N"):
                cell = f'<span class="yn yn-{v.lower()}">{v}</span>'
            else:
                cell = html.escape(v)
            out.append(f"<td>{cell}</td>")
        if has_notes:
            out.append(f'<td class="note-col">{note}</td>')
        out.append("</tr>")
    out.append("</tbody></table></div>")
    return "".join(out), len(rows)


sections = []
for name in ORDER:
    body, n = table(name)
    sections.append(f"""
    <section>
      <div class="row">
        <div class="rail"><strong>Output</strong>{n} rows</div>
        <div>
          <h2><code class="ds">{name}</code></h2>
          <p class="lede">{CAPTIONS[name]}</p>
          {body}
        </div>
      </div>
    </section>""")


# ---- worked traces through pathway_event -----------------------------------

EV_COLS = ["pathway_event_id", "pathway_id", "event_date", "event_source",
           "rtt_status_code", "is_care_activity", "appointment_communicated", "description"]

def trace(ref):
    rows = [r for r in csv.DictReader((SRC / "pathway_event.csv").open())
            if r["referral_id"] == ref]
    out = ['<div class="table-scroll"><table class="trace"><thead><tr>']
    for c in EV_COLS:
        out.append(f"<th>{html.escape(c)}</th>")
    out.append("</tr></thead><tbody>")
    prev = None
    for r in rows:
        sep = ' class="grp"' if prev and r["pathway_id"] != prev else ""
        prev = r["pathway_id"]
        out.append(f"<tr{sep}>")
        for c in EV_COLS:
            v = r.get(c, "")
            if v == "":
                cell = '<span class="null">&mdash;</span>'
            elif v in ("Y", "N", "TRUE", "FALSE"):
                cell = f'<span class="yn yn-{"y" if v in ("Y","TRUE") else "n"}">{v}</span>'
            else:
                cell = html.escape(v)
            out.append(f"<td>{cell}</td>")
        out.append("</tr>")
    out.append("</tbody></table></div>")
    return "".join(out)


Path("clean_sections.html").write_text("".join(sections))
Path("trace_0004.html").write_text(trace("REF0004"))
Path("trace_0008.html").write_text(trace("REF0008"))
print("ok")


# Assemble the finished page from the template. Keeping this here means one
# command regenerates everything from expected/ - no manual splice step.
_t = Path("clean_template.html").read_text()
_t = _t.replace("{{SECTIONS}}", Path("clean_sections.html").read_text())
_t = _t.replace("{{TRACE0004}}", Path("trace_0004.html").read_text())
_t = _t.replace("{{TRACE0008}}", Path("trace_0008.html").read_text())
Path("rtt-clean-layer.html").write_text(_t)
print("wrote rtt-clean-layer.html")
