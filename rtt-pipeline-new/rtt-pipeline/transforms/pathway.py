"""
Pathway layer: turn an event stream into clocks, then measure them.

Wiring only. The clock rules are in rules.clocks, the measures in rules.measures,
and this file's whole job is to hand them the right frames in the right order
and route what they decide.

Read the order carefully - it is the part trainees get wrong. Events are
attached to clocks BEFORE the DNA question is asked, because "was this the first
care activity on this clock" is unanswerable until you know which clock the
activity is on.
"""
import datetime as dt

from transforms.api import Input, Output, transform

from rules import clocks, measures
from transforms.referral import FINDING_COLUMNS

SNAPSHOT_DATE = dt.date(2026, 3, 31)

STOP_REASON = {
    "30": "TREATED", "31": "ACTIVE_MONITORING", "32": "ACTIVE_MONITORING",
    "33": "DNA_FIRST", "34": "NO_TREATMENT", "35": "DECLINED", "36": "DIED",
}


@transform.using(
    conformed_event=Input("/rtt/clean/conformed_event"),
    clean_referral=Input("/rtt/clean/clean_referral"),
    clean_rtt_status=Input("/rtt/clean/clean_rtt_status"),
    pathway=Output("/rtt/pathway/pathway"),
    pathway_event=Output("/rtt/pathway/pathway_event"),
    pathway_findings=Output("/rtt/pathway/pathway_findings"),
)
def compute(
    conformed_event, clean_referral, clean_rtt_status,
    pathway, pathway_event, pathway_findings,
):
    import polars as pl

    events = conformed_event.polars(lazy=True)

    # The code sets come from the feed. There is no literal here and none in
    # rules/ either - when NHS England publishes a new status code, this is a
    # data change, not a release.
    status = clean_rtt_status.polars(lazy=True).collect()
    start_codes = status.filter(pl.col("clock_effect") == "START")["rtt_status_code"].to_list()
    stop_codes = status.filter(pl.col("clock_effect") == "STOP")["rtt_status_code"].to_list()

    sequenced = clocks.assign_clock_seq(
        events.filter(pl.col("event_source") == "PAS_RTT"), start_codes
    )

    # clock_seq 0 means a status event landed before any clock started on that
    # referral. It is not a pathway and it is not nothing: the event is dropped
    # and a human is told, because the alternative is a stop with no start.
    findings = [
        sequenced.filter(pl.col("clock_seq") == 0).select(
            pl.lit("EVENT_BEFORE_CLOCK_START").alias("reason_code"),
            pl.lit("MEDIUM").alias("severity"),
            pl.lit("raw_pathway_events").alias("source_dataset"),
            pl.col("source_record_id").alias("record_id"),
            pl.col("referral_id"),
            pl.format(
                "Status {} dated {}, before any clock start on this referral. "
                "Event discarded; pathway retained.",
                pl.col("rtt_status_code"), pl.col("event_date"),
            ).alias("detail"),
            pl.lit(False).alias("requires_judgement"),
        )
    ]

    pathways = clocks.build_pathways(sequenced, stop_codes)
    stamped = clocks.attach_events_to_clocks(events, pathways)
    pathways = clocks.resolve_dna_stops(pathways, stamped)

    # A stop that was cleared is a stop somebody has to look at. Note that both
    # of these put the pathway BACK on the waiting list.
    findings.append(
        pathways.filter(pl.col("dna_verdict") == "NOT_COMMUNICATED").select(
            pl.lit("STATUS_CONFLICT").alias("reason_code"),
            pl.lit("HIGH").alias("severity"),
            pl.lit("raw_pathway_events").alias("source_dataset"),
            pl.col("pathway_id").alias("record_id"),
            pl.col("referral_id"),
            pl.lit(
                "Status 33 recorded, but the appointment was not demonstrably "
                "communicated. Cannot nullify - clock continues."
            ).alias("detail"),
            pl.lit(True).alias("requires_judgement"),
        )
    )
    findings.append(
        pathways.filter(pl.col("dna_verdict") == "NOT_FIRST_ACTIVITY").select(
            pl.lit("DNA_NOT_FIRST_ACTIVITY").alias("reason_code"),
            pl.lit("HIGH").alias("severity"),
            pl.lit("raw_pathway_events").alias("source_dataset"),
            pl.col("pathway_id").alias("record_id"),
            pl.col("referral_id"),
            pl.lit(
                "Status 33 recorded, but an earlier care activity exists on "
                "this clock. Misrecorded - clock continues."
            ).alias("detail"),
            pl.lit(True).alias("requires_judgement"),
        )
    )

    # Clearing a stop moves the window, so the events have to be re-stamped
    # against the corrected clocks. Skip this and an event sits on a pathway
    # whose window no longer contains it.
    stamped = clocks.attach_events_to_clocks(events, pathways)

    # An admitted stop is one with an elective admission on the stop date.
    admissions = (
        stamped
        .filter(
            (pl.col("event_source") == "INPATIENT")
            & pl.col("admission_method").str.starts_with("ELECTIVE")
        )
        .select("referral_id", pl.col("event_date").alias("clock_stop_date"))
        .unique()
        .with_columns(pl.lit(True).alias("_admitted"))
    )

    referrals = clean_referral.polars(lazy=True).select(
        "referral_id", "nhs_number", "treatment_function_code",
        pl.col("provider_code_resolved").alias("provider_code"),
    )

    enriched = (
        pathways
        .join(referrals, on="referral_id", how="left")
        .join(admissions, on=["referral_id", "clock_stop_date"], how="left")
        .with_columns(
            pl.col("stop_code").replace_strict(STOP_REASON, default=None).alias("stop_reason")
        )
    )

    # Invariants only. No weeks_waiting, no breach_band, no within_18_weeks -
    # those are functions of a date, and this dataset is not measured as at any
    # date. publish.py freezes them for the snapshot; the Workshop app answers
    # the live question by comparing breach_18wk_date against today.
    measured = measures.add_invariants(
        enriched.with_columns(
            measures.pathway_status(pl.col("_admitted").fill_null(False)).alias("pathway_status")
        )
    )

    pathway.write_table(
        measured.select(
            "pathway_id", "referral_id", "clock_seq", "nhs_number",
            "treatment_function_code", "provider_code",
            "clock_start_date", "clock_stop_date", "stop_code", "stop_reason",
            "pathway_status", "is_nullified",
            "breach_18wk_date", "breach_52wk_date",
        ).sort("pathway_id").collect()
    )
    pathway_event.write_table(stamped.sort("pathway_event_id").collect())
    pathway_findings.write_table(
        pl.concat(findings, how="vertical").select(FINDING_COLUMNS).collect()
    )
