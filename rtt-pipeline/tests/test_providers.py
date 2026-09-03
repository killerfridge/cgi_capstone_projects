"""
Organisation succession. Small, and the source of the ugliest defect in the
sample data - a row reading "RZB / Eastvale and Northmoor", a code and a name
that never coexisted on any day.
"""
import datetime as dt

import polars as pl

from rules import providers

SNAPSHOT = dt.date(2026, 3, 31)


def frame(rows):
    """rows: (code, name, valid_from, valid_to|None)"""
    return pl.LazyFrame(
        {
            "provider_code": [r[0] for r in rows],
            "provider_name": [r[1] for r in rows],
            "org_type": ["TRUST"] * len(rows),
            "valid_from": [dt.date.fromisoformat(r[2]) for r in rows],
            "valid_to": [dt.date.fromisoformat(r[3]) if r[3] else None for r in rows],
        },
        schema_overrides={"valid_to": pl.Date},
    )


CLOSED_AND_SUCCEEDED = [
    ("RZB", "Northmoor NHS Trust", "2010-04-01", "2025-09-30"),
    ("RZD", "Eastvale and Northmoor NHS Trust", "2025-10-01", None),
    ("RZC", "Eastvale NHS Trust", "2012-04-01", None),
]


def test_adjacency_infers_the_successor():
    out = providers.infer_successors(frame(CLOSED_AND_SUCCEEDED)).collect()
    row = out.filter(pl.col("provider_code") == "RZB")
    assert row["successor_code"].to_list() == ["RZD"]
    assert row["is_current"].to_list() == [False]


def test_an_open_organisation_has_no_successor():
    out = providers.infer_successors(frame(CLOSED_AND_SUCCEEDED)).collect()
    row = out.filter(pl.col("provider_code") == "RZC")
    assert row["successor_code"].to_list() == [None]
    assert row["is_current"].to_list() == [True]


def test_a_closed_code_resolves_to_its_successor_at_the_snapshot():
    out = providers.resolve_as_at(
        providers.infer_successors(frame(CLOSED_AND_SUCCEEDED)), SNAPSHOT
    ).collect()
    row = out.filter(pl.col("provider_code") == "RZB")
    # Both, or neither. Resolving the name alone is the defect.
    assert row["resolved_code"].to_list() == ["RZD"]
    assert row["resolved_name"].to_list() == ["Eastvale and Northmoor NHS Trust"]


def test_resolution_is_as_at_a_date_not_as_at_today():
    # Asked as at 2025-06-30, RZB was open and resolves to itself. The PTL and
    # the referral row ask this question about different dates on purpose.
    out = providers.resolve_as_at(
        providers.infer_successors(frame(CLOSED_AND_SUCCEEDED)), dt.date(2025, 6, 30)
    ).collect()
    row = out.filter(pl.col("provider_code") == "RZB")
    assert row["resolved_code"].to_list() == ["RZB"]
    assert row["resolved_name"].to_list() == ["Northmoor NHS Trust"]


def test_a_chain_of_two_successions_resolves_to_the_end():
    chain = [
        ("R01", "Old Trust", "2000-04-01", "2020-03-31"),
        ("R02", "Middle Trust", "2020-04-01", "2023-03-31"),
        ("R03", "Current Trust", "2023-04-01", None),
    ]
    out = providers.resolve_as_at(
        providers.infer_successors(frame(chain)), SNAPSHOT
    ).collect()
    assert out.filter(pl.col("provider_code") == "R01")["resolved_code"].to_list() == ["R03"]
