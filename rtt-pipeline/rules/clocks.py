"""
Clock derivation: the heart of the pipeline, and the only module in the repo
where getting it subtly wrong still produces plausible-looking numbers.

Three jobs:
  1. split a referral's RTT status events into one row per clock
  2. attach every conformed event to the clock whose window contains it
  3. decide whether a status 33 genuinely nullifies the pathway

Imports polars. Imports nothing from `transforms`. Every function takes its code
sets as a PARAMETER - there is no START_CODES literal in this file, because the
national code set lives in the ref_rtt_status feed and a code deploy is not how
you respond to it changing.

Dates are pl.Date throughout. Parsing happens once, in the clean layer.
"""
import polars as pl

# The one value this module does name, and only because it is a branch in the
# logic rather than a member of a set: 33 is the DNA-at-first-activity stop, and
# it is the only stop code whose effect is conditional.
DNA_FIRST_STOP_CODE = "33"


def assign_clock_seq(
    events: pl.LazyFrame,
    start_codes: list[str],
) -> pl.LazyFrame:
    """Number each referral's clocks: the running count of start codes.

    A referral is not a pathway. One referral can carry several clocks - start,
    stop, start, stop - and the running count of start codes IS the clock
    sequence.

    Events before the first start code land in clock_seq 0. They are not a
    clock; they are orphans, and the caller raises them as findings.
    """
    return (
        events
        # cum_sum over a group walks rows in their current order, so this sort
        # is load-bearing, not cosmetic. source_record_id breaks ties on
        # same-day events so two builds of the same data agree.
        .sort(["referral_id", "event_date", "source_record_id"])
        .with_columns(
            pl.col("rtt_status_code").is_in(start_codes)
            .cum_sum()
            .over("referral_id")
            .alias("clock_seq")
        )
    )


def build_pathways(
    sequenced: pl.LazyFrame,
    stop_codes: list[str],
) -> pl.LazyFrame:
    """One row per clock, at grain (referral_id, clock_seq).

    If your output row count equals your referral row count, the grain is wrong
    and every number downstream is wrong with it.
    """
    starts = (
        sequenced
        .filter(pl.col("clock_seq") > 0)
        .group_by(["referral_id", "clock_seq"])
        # min(), not first(). group_by makes no promise about the order groups
        # come back in, and first() is a coin toss that lands right in testing
        # and wrong at volume.
        .agg(pl.col("event_date").min().alias("clock_start_date"))
    )

    stops = (
        sequenced
        .filter((pl.col("clock_seq") > 0) & pl.col("rtt_status_code").is_in(stop_codes))
        .group_by(["referral_id", "clock_seq"])
        .agg(
            pl.col("event_date").min().alias("clock_stop_date"),
            # The stop code belonging to the EARLIEST stop, stated explicitly
            # rather than relying on row order surviving the group_by.
            pl.col("rtt_status_code").sort_by("event_date").first().alias("stop_code"),
        )
    )

    return (
        starts
        .join(stops, on=["referral_id", "clock_seq"], how="left")
        .with_columns(
            pl.format("{}-{}", pl.col("referral_id"), pl.col("clock_seq")).alias("pathway_id")
        )
        .sort(["referral_id", "clock_seq"])
    )


def attach_events_to_clocks(events: pl.LazyFrame, pathways: pl.LazyFrame) -> pl.LazyFrame:
    """Stamp each conformed event with the pathway whose window contains it.

    The window is [clock_start_date, clock_stop_date], open-ended while the
    clock runs. An event outside every window keeps a null pathway_id - it is
    still a real record, it is just not part of a clock. Note the shape: match,
    then join the match back on. An inner join and a filter would silently drop
    the unmatched events, and nobody counts the rows that were never there.
    """
    matched = (
        events.select("pathway_event_id", "referral_id", "event_date")
        .join(
            pathways.select("referral_id", "pathway_id", "clock_start_date", "clock_stop_date"),
            on="referral_id",
            how="inner",
        )
        .filter(
            (pl.col("event_date") >= pl.col("clock_start_date"))
            & (
                pl.col("clock_stop_date").is_null()
                | (pl.col("event_date") <= pl.col("clock_stop_date"))
            )
        )
        .sort(["pathway_event_id", "clock_start_date"])
        .unique(subset=["pathway_event_id"], keep="first")
        .select("pathway_event_id", "pathway_id")
    )

    return events.join(matched, on="pathway_event_id", how="left")


def resolve_dna_stops(pathways: pl.LazyFrame, events: pl.LazyFrame) -> pl.LazyFrame:
    """Decide what a status 33 actually did, and record why.

    A DNA nullifies the pathway - removed from the numerator AND the denominator
    - only when both of these hold:

      * it was the FIRST care activity on THIS clock. Not the patient's first
        ever appointment, and not merely the first event: a status code is not
        a care activity.
      * the appointment was demonstrably communicated.

    Anything else is a misrecorded stop. The clock did not stop, and somebody
    has to look at it. This function returns the verdict; the transform decides
    what to do about it. Rules judge, transforms route.

    Adds: dna_verdict (str|null), is_nullified (bool). Clears the stop where the
    verdict is that there was not one.
    """
    first_activity = (
        events
        .filter(pl.col("is_care_activity") & pl.col("pathway_id").is_not_null())
        .group_by("pathway_id")
        .agg(pl.col("event_date").min().alias("first_activity_date"))
    )

    communicated_dna = (
        events
        .filter(pl.col("is_care_activity") & pl.col("pathway_id").is_not_null())
        .join(pathways.select("pathway_id", "clock_stop_date"), on="pathway_id", how="inner")
        .filter(
            (pl.col("event_date") == pl.col("clock_stop_date"))
            & (pl.col("attendance_status") == "DNA")
            & pl.col("appointment_communicated")
        )
        .select("pathway_id")
        .unique()
        .with_columns(pl.lit(True).alias("dna_was_communicated"))
    )

    is_dna_stop = pl.col("stop_code") == DNA_FIRST_STOP_CODE
    stop_is_first_activity = pl.col("first_activity_date") == pl.col("clock_stop_date")
    communicated = pl.col("dna_was_communicated").fill_null(False)

    verdict = (
        pl.when(~is_dna_stop.fill_null(False)).then(pl.lit(None, dtype=pl.Utf8))
        .when(stop_is_first_activity & communicated).then(pl.lit("NULLIFIED"))
        .when(stop_is_first_activity).then(pl.lit("NOT_COMMUNICATED"))
        .otherwise(pl.lit("NOT_FIRST_ACTIVITY"))
    )

    misrecorded = pl.col("dna_verdict").is_in(["NOT_COMMUNICATED", "NOT_FIRST_ACTIVITY"])

    return (
        pathways
        .join(first_activity, on="pathway_id", how="left")
        .join(communicated_dna, on="pathway_id", how="left")
        .with_columns(verdict.alias("dna_verdict"))
        .with_columns(
            (pl.col("dna_verdict") == "NULLIFIED").fill_null(False).alias("is_nullified"),
            # A stop that cannot be justified is not a stop. The clock keeps
            # running - which puts the pathway back on the waiting list and
            # makes the headline number worse. That direction is the point:
            # nobody arrives at this rule by looking for a flattering answer.
            pl.when(misrecorded).then(pl.lit(None, dtype=pl.Date))
              .otherwise(pl.col("clock_stop_date")).alias("clock_stop_date"),
            pl.when(misrecorded).then(pl.lit(None, dtype=pl.Utf8))
              .otherwise(pl.col("stop_code")).alias("stop_code"),
        )
        .drop("first_activity_date", "dna_was_communicated")
    )
