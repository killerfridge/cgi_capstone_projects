"""
Measurement rules. Short tests, and the last two are the ones that stop a
pipeline from quietly flattering itself.
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


def measured(rows, admitted=False):
    return measures.measure(frame(rows), SNAPSHOT, pl.lit(admitted)).collect()


def test_open_clock_is_measured_to_the_snapshot():
    out = measured([("P1", "2026-01-07", None, False)])
    assert out["weeks_waiting"].to_list() == [11]
    assert out["pathway_status"].to_list() == ["INCOMPLETE"]


def test_completed_weeks_round_down():
    # 17 weeks and 6 days is a 17-week wait. Round up here and you manufacture
    # a breach that never happened.
    out = measured([("P1", "2025-12-01", "2026-03-30", False)])
    assert out["weeks_waiting"].to_list() == [17]
    assert out["within_18_weeks"].to_list() == [True]


def test_eighteen_weeks_exactly_is_a_breach():
    # The standard is "within 18 weeks", so 18 completed weeks is outside it.
    # Off-by-one here moves the national figure and nothing else fails.
    out = measured([("P1", "2025-11-25", "2026-03-31", False)])
    assert out["weeks_waiting"].to_list() == [18]
    assert out["within_18_weeks"].to_list() == [False]


def test_nullified_pathway_has_no_waiting_time_at_all():
    # Not zero weeks. None. A nullified pathway leaves the numerator and the
    # denominator together, and giving it 0 weeks silently improves compliance -
    # the most flattering possible way to be wrong.
    out = measured([("P1", "2025-01-06", "2025-02-17", True)])
    assert out["weeks_waiting"].to_list() == [None]
    assert out["within_18_weeks"].to_list() == [None]
    assert out["pathway_status"].to_list() == ["NULLIFIED"]


def test_compliance_counts_incomplete_pathways_only():
    out = measured([
        ("P1", "2026-01-07", None, False),        # incomplete, 11 weeks
        ("P2", "2025-06-01", None, False),        # incomplete, 43 weeks
        ("P3", "2025-01-06", "2025-02-17", False),  # completed - not in the PTL
        ("P4", "2025-01-06", "2025-02-17", True),   # nullified - counted nowhere
    ])
    stats = measures.compliance(out.lazy()).collect()
    assert stats["incomplete"].to_list() == [2]
    assert stats["within_18_weeks"].to_list() == [1]
    assert stats["compliance_pct"].to_list() == [50.0]


def test_breach_bands_are_closed_at_the_top():
    out = measured([
        ("P1", "2026-01-07", None, False),   # 11
        ("P2", "2025-09-01", None, False),   # 30
        ("P3", "2025-02-01", None, False),   # 60
        ("P4", "2024-06-01", None, False),   # 95
    ])
    assert out["breach_band"].to_list() == ["0-17", "26-51", "52-64", "65+"]
