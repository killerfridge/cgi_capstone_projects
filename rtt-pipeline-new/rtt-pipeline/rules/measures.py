"""
Waiting time measurement and pathway classification.

Imports polars. Imports nothing from `transforms`.

The organising idea here is that there are TWO questions, and they want opposite
treatment:

  * "How long had this person waited as at 31 March?" is a statutory return. It
    must be FROZEN. Recompute it later and a published figure stops being
    reproducible, which is the one thing a statutory return may not do.

  * "How long has this person been waiting right now?" - a validator opening the
    list on a Tuesday in June - must be LIVE. Freeze it and every open pathway
    reports the wait it had on the day of the last build.

So: `pathway` carries the INVARIANTS, and `ptl_snapshot` carries the measures as
at its snapshot date. The invariants are the trick. An elapsed week count changes
every seven days; a breach DATE does not move at all unless the clock start does.
"Who is breaching today" becomes a comparison of a stored date against today,
which any query layer can filter and aggregate, rather than arithmetic somebody
has to perform at read time.

The two domain rules that trip people up are also here:

  * Weeks are counted per clock, and never summed across clocks. A referral with
    a 6-week clock and a 12-week clock is not an 18-week wait. It is two waits.
    The gap between them is deliberately excluded - that is what stopping a clock
    is for.

  * A nullified pathway has no waiting time at all. It leaves the numerator and
    the denominator together. Give it 0 weeks and it silently improves your
    compliance figure, which is the most flattering possible way to be wrong.
"""
import datetime as dt

import polars as pl

BREACH_BANDS = [(18, "0-17"), (26, "18-25"), (52, "26-51"), (65, "52-64")]
LONGEST_BAND = "65+"

# weeks = days // 7, so a pathway is breaching ON day 126, not the day after.
DAYS_18_WEEKS = 18 * 7      # 126
DAYS_52_WEEKS = 52 * 7      # 364


def add_invariants(pathways: pl.LazyFrame) -> pl.LazyFrame:
    """The dates at which this clock breaches. These do not move with the calendar.

    This is the whole point. `weeks_waiting` is a function of today, and today is
    not input data - no incremental run will ever revisit it, and a nightly build
    only makes it a day fresh. A breach date is a function of clock_start_date
    alone, so it is as stable as the fact it came from, and "who is breaching"
    reduces to `breach_18wk_date <= today` - an indexed date comparison that
    filters and aggregates without any read-time arithmetic.

    Nullified pathways get no breach dates. They are not waiting.
    """
    running = ~pl.col("is_nullified")
    return pathways.with_columns(
        pl.when(running)
          .then(pl.col("clock_start_date") + dt.timedelta(days=DAYS_18_WEEKS))
          .alias("breach_18wk_date"),
        pl.when(running)
          .then(pl.col("clock_start_date") + dt.timedelta(days=DAYS_52_WEEKS))
          .alias("breach_52wk_date"),
    )


def pathway_status(admitted_stops: pl.Expr) -> pl.Expr:
    """INCOMPLETE while the clock runs; how it stopped once it has.

    Note this is NOT time-dependent: a clock with no stop date is incomplete
    today, tomorrow and next year. It is a fact about the data, so it belongs in
    the dataset.

    `admitted_stops` is a boolean expression saying whether an elective admission
    sits on the stop date - passed in rather than computed here so this module
    never needs to know what an inpatient feed looks like.
    """
    return (
        pl.when(pl.col("is_nullified")).then(pl.lit("NULLIFIED"))
        .when(pl.col("clock_stop_date").is_null()).then(pl.lit("INCOMPLETE"))
        .when(admitted_stops).then(pl.lit("COMPLETED_ADMITTED"))
        .otherwise(pl.lit("COMPLETED_NON_ADMITTED"))
    )


# --------------------------------------------------------------- as at a date
#
# Everything below takes the date it is measuring AS AT as an argument. None of
# it is called when building `pathway`; it is called once, by publish.py, to
# freeze the snapshot.

def weeks_waiting_as_at(as_at: dt.date) -> pl.Expr:
    """Completed weeks on this clock: stop date if stopped, `as_at` if running.

    Integer division - a wait of 17 weeks and 6 days is 17 weeks, not 18. The
    18-week standard is a threshold on completed weeks, and rounding up here
    manufactures breaches that never happened.
    """
    end = pl.coalesce(pl.col("clock_stop_date"), pl.lit(as_at))
    return ((end - pl.col("clock_start_date")).dt.total_days() // 7).cast(pl.Int32)


def breach_band(col: str = "weeks_waiting") -> pl.Expr:
    """Reporting bands. Built from the table, so adding a band is a data edit."""
    expr = pl.when(pl.col(col) < BREACH_BANDS[0][0]).then(pl.lit(BREACH_BANDS[0][1]))
    for upper, label in BREACH_BANDS[1:]:
        expr = expr.when(pl.col(col) < upper).then(pl.lit(label))
    return expr.otherwise(pl.lit(LONGEST_BAND))


def measure_as_at(pathways: pl.LazyFrame, as_at: dt.date) -> pl.LazyFrame:
    """Freeze the elapsed measures at `as_at`. For the snapshot, and only the snapshot.

    Do not call this when building the `pathway` dataset. A frozen week count on
    an object a validator can edit is the trap this whole module is arranged to
    avoid: correct clock_start_date through an Action and the stale count sits
    beside it, unchanged and unchallenged.
    """
    unmeasured = pl.col("is_nullified")
    return pathways.with_columns(
        pl.when(unmeasured).then(None).otherwise(weeks_waiting_as_at(as_at)).alias("weeks_waiting"),
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


def breaching_as_at(as_at: dt.date, weeks: int = 18) -> pl.Expr:
    """The live question, answered without arithmetic: is this clock past its date?

    This is what the Workshop app filters on, and what a derived property or a
    function-backed column would otherwise have to compute per object per read.
    Here it is a comparison of two dates, one of them stored and indexed.
    """
    col = "breach_18wk_date" if weeks == 18 else "breach_52wk_date"
    return pl.col("clock_stop_date").is_null() & (pl.col(col) <= pl.lit(as_at))
