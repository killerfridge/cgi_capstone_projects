"""
Measurement rules, and the split between what is frozen and what is live.

The first group tests the invariants that go on the Pathway object. The second
tests the as-at measures that are frozen into the snapshot. If a trainee has
merged those two groups back together, this file will not compile against their
code - which is the point.
"""
import datetime as dt

import polars as pl

from rules import measures

SNAPSHOT = dt.date(2026, 3, 31)


def frame(rows):
    """rows: (pathway_id, start, stop|None, is_nullified)"""
    return pl.LazyFrame(
        {
            "pathway_id": [r[0] for r in rows],
            "clock_start_date": [dt.date.fromisoformat(r[1]) for r in rows],
            "clock_stop_date": [dt.date.fromisoformat(r[2]) if r[2] else None for r in rows],
            "is_nullified": [r[3] for r in rows],
        },
        schema_overrides={"clock_stop_date": pl.Date},
    )


def invariants(rows):
    return measures.add_invariants(frame(rows)).collect()


def snapshot(rows, as_at=SNAPSHOT):
    return measures.measure_as_at(frame(rows), as_at).collect()


# ------------------------------------------------ invariants: the live answer

def test_breach_dates_do_not_move_with_the_calendar():
    # The whole reason they exist. 18 weeks is 126 days, 52 weeks is 364.
    out = invariants([("P1", "2026-01-07", None, False)])
    assert out["breach_18wk_date"].to_list() == [dt.date(2026, 5, 13)]
    assert out["breach_52wk_date"].to_list() == [dt.date(2027, 1, 6)]


def test_breaching_is_a_date_comparison_not_arithmetic():
    # A clock started 2025-11-25 breaches 18 weeks on 2026-03-31. Asked on the
    # 30th it is within; asked on the 31st it is not. Same stored row, no
    # rebuild, no read-time subtraction.
    rows = [("P1", "2025-11-25", None, False)]
    out = invariants(rows)
    assert out.lazy().select(
        measures.breaching_as_at(dt.date(2026, 3, 30))
    ).collect().to_series().to_list() == [False]
    assert out.lazy().select(
        measures.breaching_as_at(dt.date(2026, 3, 31))
    ).collect().to_series().to_list() == [True]


def test_a_stopped_clock_is_never_breaching():
    out = invariants([("P1", "2024-01-01", "2024-02-01", False)])
    assert out.lazy().select(
        measures.breaching_as_at(SNAPSHOT)
    ).collect().to_series().to_list() == [False]


def test_a_nullified_pathway_has_no_breach_dates():
    out = invariants([("P1", "2025-01-06", "2025-02-17", True)])
    assert out["breach_18wk_date"].to_list() == [None]
    assert out["breach_52wk_date"].to_list() == [None]


# ------------------------------------------ as at a date: the frozen snapshot

def test_open_clock_is_measured_to_the_snapshot_date():
    out = snapshot([("P1", "2026-01-07", None, False)])
    assert out["weeks_waiting"].to_list() == [11]


def test_completed_weeks_round_down():
    # 17 weeks and 6 days is a 17-week wait. Round up and you manufacture a
    # breach that never happened.
    out = snapshot([("P1", "2025-12-01", "2026-03-30", False)])
    assert out["weeks_waiting"].to_list() == [17]
    assert out["within_18_weeks"].to_list() == [True]


def test_eighteen_weeks_exactly_is_a_breach():
    # The standard is "within 18 weeks", so 18 completed weeks is outside it.
    # Off-by-one here moves the national figure and nothing else fails.
    out = snapshot([("P1", "2025-11-25", "2026-03-31", False)])
    assert out["weeks_waiting"].to_list() == [18]
    assert out["within_18_weeks"].to_list() == [False]


def test_the_snapshot_is_as_at_its_date_not_as_at_today():
    # Same row, two reporting dates. A statutory return asked again next year
    # must give the answer it gave when it was published.
    rows = [("P1", "2026-01-07", None, False)]
    assert snapshot(rows, dt.date(2026, 3, 31))["weeks_waiting"].to_list() == [11]
    assert snapshot(rows, dt.date(2026, 6, 30))["weeks_waiting"].to_list() == [24]


def test_nullified_pathway_has_no_waiting_time_at_all():
    # Not zero weeks. None. A nullified pathway leaves the numerator and the
    # denominator together, and 0 weeks silently improves compliance.
    out = snapshot([("P1", "2025-01-06", "2025-02-17", True)])
    assert out["weeks_waiting"].to_list() == [None]
    assert out["within_18_weeks"].to_list() == [None]


def test_breach_bands_are_closed_at_the_top():
    out = snapshot([
        ("P1", "2026-01-07", None, False),   # 11
        ("P2", "2025-09-01", None, False),   # 30
        ("P3", "2025-02-01", None, False),   # 60
        ("P4", "2024-06-01", None, False),   # 95
    ])
    assert out["breach_band"].to_list() == ["0-17", "26-51", "52-64", "65+"]


def test_compliance_counts_incomplete_pathways_only():
    rows = [
        ("P1", "2026-01-07", None, False),          # incomplete, 11 weeks
        ("P2", "2025-06-01", None, False),          # incomplete, 43 weeks
        ("P3", "2025-01-06", "2025-02-17", False),  # completed - not in the PTL
        ("P4", "2025-01-06", "2025-02-17", True),   # nullified - counted nowhere
    ]
    measured = measures.measure_as_at(frame(rows), SNAPSHOT).with_columns(
        measures.pathway_status(pl.lit(False)).alias("pathway_status")
    )
    stats = measures.compliance(measured).collect()
    assert stats["incomplete"].to_list() == [2]
    assert stats["within_18_weeks"].to_list() == [1]
    assert stats["compliance_pct"].to_list() == [50.0]
