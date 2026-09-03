"""
Waiting time measurement and pathway classification.

Imports polars. Imports nothing from `transforms`.

The two rules that trip people up are both here:

  * Weeks are counted per clock, and never summed across clocks. A referral
    with a 6-week clock and a 12-week clock is not an 18-week wait. It is two
    waits. The gap between them is deliberately excluded - that is what
    stopping a clock is for.

  * A nullified pathway has no waiting time at all. It leaves the numerator and
    the denominator together. Give it 0 weeks and it silently improves your
    compliance figure, which is the most flattering possible way to be wrong.
"""
import datetime as dt

import polars as pl

BREACH_BANDS = [(18, "0-17"), (26, "18-25"), (52, "26-51"), (65, "52-64")]
LONGEST_BAND = "65+"


def weeks_waiting(snapshot_date: dt.date) -> pl.Expr:
    """Completed weeks on this clock: stop date if stopped, snapshot if running.

    Integer division - a wait of 17 weeks and 6 days is 17 weeks, not 18. The
    18-week standard is a threshold on completed weeks, and rounding up here
    manufactures breaches.
    """
    end = pl.coalesce(pl.col("clock_stop_date"), pl.lit(snapshot_date))
    return ((end - pl.col("clock_start_date")).dt.total_days() // 7).cast(pl.Int32)


def breach_band(col: str = "weeks_waiting") -> pl.Expr:
    """Reporting bands. Built from the table, so adding a band is a data edit."""
    expr = pl.when(pl.col(col) < BREACH_BANDS[0][0]).then(pl.lit(BREACH_BANDS[0][1]))
    for upper, label in BREACH_BANDS[1:]:
        expr = expr.when(pl.col(col) < upper).then(pl.lit(label))
    return expr.otherwise(pl.lit(LONGEST_BAND))


def pathway_status(admitted_stops: pl.Expr) -> pl.Expr:
    """INCOMPLETE while the clock runs; how it stopped once it has.

    `admitted_stops` is a boolean expression saying whether an elective
    admission sits on the stop date - passed in rather than computed here so
    this module never needs to know what an inpatient feed looks like.
    """
    return (
        pl.when(pl.col("is_nullified")).then(pl.lit("NULLIFIED"))
        .when(pl.col("clock_stop_date").is_null()).then(pl.lit("INCOMPLETE"))
        .when(admitted_stops).then(pl.lit("COMPLETED_ADMITTED"))
        .otherwise(pl.lit("COMPLETED_NON_ADMITTED"))
    )


def measure(pathways: pl.LazyFrame, snapshot_date: dt.date, admitted_stops: pl.Expr) -> pl.LazyFrame:
    """Apply the measures, leaving a nullified pathway unmeasured.

    Derived at runtime from clock_start_date, which is an editable property on
    the Pathway object. Bake weeks_waiting into a dataset and a validator can
    correct the clock start through an Action while a stale week count sits
    beside it, unchanged and unchallenged.
    """
    unmeasured = pl.col("is_nullified")
    return pathways.with_columns(
        pl.when(unmeasured).then(None).otherwise(weeks_waiting(snapshot_date)).alias("weeks_waiting"),
        pathway_status(admitted_stops).alias("pathway_status"),
    ).with_columns(
        pl.when(unmeasured).then(None).otherwise(breach_band()).alias("breach_band"),
        pl.when(unmeasured).then(None).otherwise(pl.col("weeks_waiting") < 18).alias("within_18_weeks"),
    )


def compliance(pathways: pl.LazyFrame) -> pl.LazyFrame:
    """Percentage of the incomplete list waiting under 18 weeks.

    Denominator is INCOMPLETE pathways only. Nullified pathways are already
    absent - they were never measured. Completed pathways belong to a different
    published measure and do not go in here.
    """
    return (
        pathways
        .filter(pl.col("pathway_status") == "INCOMPLETE")
        .select(
            pl.len().alias("incomplete"),
            pl.col("within_18_weeks").sum().alias("within_18_weeks"),
            (100 * pl.col("within_18_weeks").mean()).round(1).alias("compliance_pct"),
        )
    )
