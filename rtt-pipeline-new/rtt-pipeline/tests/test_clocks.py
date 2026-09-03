"""
The tests that matter. Each one is a rule from the national guidance, written
so that breaking the rule fails a named test rather than moving a percentage.

Six rows of input, no build, no Spark. If these take more than a second,
something in rules/ has grown a Foundry import.
"""
import datetime as dt

import polars as pl

from rules import clocks

START = ["10", "11", "12"]
STOP = ["30", "31", "32", "33", "34", "35", "36"]


def events(rows):
    """rows: (source_record_id, referral_id, 'YYYY-MM-DD', status)"""
    return pl.LazyFrame(
        {
            "source_record_id": [r[0] for r in rows],
            "referral_id": [r[1] for r in rows],
            "event_date": [dt.date.fromisoformat(r[2]) for r in rows],
            "rtt_status_code": [r[3] for r in rows],
        }
    )


def pathways_from(rows):
    seq = clocks.assign_clock_seq(events(rows), START)
    return clocks.build_pathways(seq, STOP).collect().sort("pathway_id")


def test_one_referral_can_carry_two_clocks():
    # Referred, treated, referred again, still waiting. This is the single most
    # important test in the repository: a referral is not a pathway.
    p = pathways_from([
        ("E1", "REF1", "2025-01-06", "10"),
        ("E2", "REF1", "2025-02-17", "30"),
        ("E3", "REF1", "2025-09-01", "10"),
        ("E4", "REF1", "2025-11-24", "30"),
    ])
    assert p.height == 2
    assert p["pathway_id"].to_list() == ["REF1-1", "REF1-2"]


def test_weeks_are_per_clock_and_never_summed():
    # 6 weeks then 12 weeks is not an 18-week wait, and it is certainly not the
    # 46 weeks of elapsed time. The gap between clocks is excluded on purpose.
    p = pathways_from([
        ("E1", "REF1", "2025-01-06", "10"),
        ("E2", "REF1", "2025-02-17", "30"),
        ("E3", "REF1", "2025-09-01", "10"),
        ("E4", "REF1", "2025-11-24", "30"),
    ])
    weeks = [
        (stop - start).days // 7
        for start, stop in zip(p["clock_start_date"], p["clock_stop_date"])
    ]
    assert weeks == [6, 12]


def test_only_the_last_clock_can_be_open():
    p = pathways_from([
        ("E1", "REF1", "2025-01-06", "10"),
        ("E2", "REF1", "2025-02-17", "30"),
        ("E3", "REF1", "2025-09-01", "10"),
    ])
    assert p["clock_stop_date"].to_list() == [dt.date(2025, 2, 17), None]
    assert p.filter(pl.col("clock_stop_date").is_null()).height == 1


def test_events_before_any_start_land_in_clock_zero():
    # A stop with no start. Not a pathway, and not something to drop quietly.
    seq = clocks.assign_clock_seq(
        events([
            ("E1", "REF1", "2024-12-01", "30"),
            ("E2", "REF1", "2025-01-06", "10"),
        ]),
        START,
    ).collect()
    assert seq.filter(pl.col("clock_seq") == 0).height == 1
    assert pathways_from([
        ("E1", "REF1", "2024-12-01", "30"),
        ("E2", "REF1", "2025-01-06", "10"),
    ]).height == 1


def test_earliest_stop_wins_when_two_are_recorded():
    # min(), not first(). Reverse the input order and this test is what catches
    # the difference.
    p = pathways_from([
        ("E3", "REF1", "2025-03-01", "34"),
        ("E1", "REF1", "2025-01-06", "10"),
        ("E2", "REF1", "2025-02-17", "30"),
    ])
    assert p["clock_stop_date"].to_list() == [dt.date(2025, 2, 17)]
    assert p["stop_code"].to_list() == ["30"]


# ------------------------------------------------------------------ DNA rules

def care(rows):
    """rows: (pathway_event_id, referral_id, 'YYYY-MM-DD', attendance, communicated)"""
    return pl.LazyFrame(
        {
            "pathway_event_id": [r[0] for r in rows],
            "referral_id": [r[1] for r in rows],
            "event_date": [dt.date.fromisoformat(r[2]) for r in rows],
            "attendance_status": [r[3] for r in rows],
            "appointment_communicated": [r[4] for r in rows],
            "is_care_activity": [r[3] in ("ATTENDED", "DNA") for r in rows],
        }
    )


def verdict_for(status_rows, care_rows):
    p = clocks.build_pathways(clocks.assign_clock_seq(events(status_rows), START), STOP)
    stamped = clocks.attach_events_to_clocks(care(care_rows), p)
    return clocks.resolve_dna_stops(p, stamped).collect()


BASE = [("E1", "REF1", "2025-01-06", "10"), ("E2", "REF1", "2025-02-17", "33")]


def test_communicated_dna_at_first_activity_nullifies():
    out = verdict_for(BASE, [("A1", "REF1", "2025-02-17", "DNA", True)])
    assert out["dna_verdict"].to_list() == ["NULLIFIED"]
    assert out["is_nullified"].to_list() == [True]


def test_dna_that_was_not_communicated_does_not_stop_the_clock():
    out = verdict_for(BASE, [("A1", "REF1", "2025-02-17", "DNA", False)])
    assert out["dna_verdict"].to_list() == ["NOT_COMMUNICATED"]
    assert out["is_nullified"].to_list() == [False]
    # And the clock is still running, which puts the pathway back on the list.
    assert out["clock_stop_date"].to_list() == [None]


def test_dna_after_an_earlier_attendance_does_not_nullify():
    # "First" means first care activity on THIS clock. A patient who was seen in
    # January and missed February has not failed to start treatment.
    out = verdict_for(
        BASE,
        [
            ("A1", "REF1", "2025-01-20", "ATTENDED", True),
            ("A2", "REF1", "2025-02-17", "DNA", True),
        ],
    )
    assert out["dna_verdict"].to_list() == ["NOT_FIRST_ACTIVITY"]
    assert out["clock_stop_date"].to_list() == [None]


def test_a_cancellation_is_not_a_care_activity():
    # The patient cancelled in January, so the February DNA is still the first
    # care activity on the clock and the pathway nullifies.
    out = verdict_for(
        BASE,
        [
            ("A1", "REF1", "2025-01-20", "PATIENT_CANCELLED", True),
            ("A2", "REF1", "2025-02-17", "DNA", True),
        ],
    )
    assert out["dna_verdict"].to_list() == ["NULLIFIED"]
