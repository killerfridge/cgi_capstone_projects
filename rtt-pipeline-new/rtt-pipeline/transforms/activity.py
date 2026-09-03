"""
Activity layer: three feeds with three shapes become one event stream.

Wiring only, and mostly a conformance exercise - but two decisions in here are
domain judgements, and both are easy to get backwards.

  1. A collapsed duplicate carries duplicate RTT STATUS events. Reparenting
     them onto the survivor would open a second clock and re-inflate the
     waiting list, which is the exact thing the deduplication was for. Discard
     them.

  2. The same duplicate also carries real CARE ACTIVITY - a clinic attendance
     that happened to a person. That must follow the survivor. Discard it and
     you lose the evidence that decides whether a later DNA nullifies.

Same referral, opposite treatment, because a status code is a claim about the
clock and an attendance is a claim about the world.
"""
from transforms.api import Input, Output, transform

from transforms.referral import FINDING_COLUMNS

CARE_ATTENDANCE = ["ATTENDED", "DNA"]


@transform.using(
    raw_pathway_events=Input("/rtt/raw/raw_pathway_events"),
    raw_outpatient=Input("/rtt/raw/raw_outpatient_attendances"),
    raw_inpatient=Input("/rtt/raw/raw_inpatient_admissions"),
    clean_referral=Input("/rtt/clean/clean_referral"),
    referral_findings=Input("/rtt/clean/referral_findings"),
    clean_rtt_status=Input("/rtt/clean/clean_rtt_status"),
    conformed_event=Output("/rtt/clean/conformed_event"),
    activity_findings=Output("/rtt/clean/activity_findings"),
)
def compute(
    raw_pathway_events, raw_outpatient, raw_inpatient, clean_referral,
    referral_findings, clean_rtt_status, conformed_event, activity_findings,
):
    import polars as pl

    known = clean_referral.polars(lazy=True).select("referral_id")

    # The dedup mapping, recovered from the findings dataset that referral.py
    # already wrote. No second source of truth, no shared state between jobs.
    dedup = (
        referral_findings.polars(lazy=True)
        .filter(pl.col("reason_code") == "DUPLICATE_REFERRAL_COLLAPSED")
        .select(
            pl.col("record_id").alias("referral_id"),
            pl.col("referral_id").alias("_survivor_id"),
        )
    )

    status_text = clean_rtt_status.polars(lazy=True).select(
        "rtt_status_code",
        pl.col("patient_facing_description").alias("_status_text"),
    )

    findings = []

    # ---------------------------------------------------------- RTT status feed
    status_events = raw_pathway_events.polars(lazy=True).select(
        pl.col("event_id").alias("source_record_id"),
        pl.col("referral_id").cast(pl.Utf8),
        pl.col("event_date").str.to_date("%Y-%m-%d"),
        pl.col("rtt_status_code").cast(pl.Utf8),
    ).join(dedup, on="referral_id", how="left")

    findings.append(
        status_events.filter(pl.col("_survivor_id").is_not_null()).select(
            pl.lit("DUPLICATE_EVENT_DISCARDED").alias("reason_code"),
            pl.lit("LOW").alias("severity"),
            pl.lit("raw_pathway_events").alias("source_dataset"),
            pl.col("source_record_id").alias("record_id"),
            pl.col("_survivor_id").alias("referral_id"),
            pl.format(
                "Status event on {}, which collapsed into {}. Discarded - "
                "reparenting it would open a duplicate clock.",
                pl.col("referral_id"), pl.col("_survivor_id"),
            ).alias("detail"),
            pl.lit(False).alias("requires_judgement"),
        )
    )

    status_events = status_events.filter(pl.col("_survivor_id").is_null()).drop("_survivor_id")
    findings.append(_orphans(pl, status_events, known, "raw_pathway_events"))

    status_stream = (
        status_events
        .join(known, on="referral_id", how="semi")
        .join(status_text, on="rtt_status_code", how="left")
        .select(
            "source_record_id", "referral_id", "event_date",
            pl.lit("PAS_RTT").alias("event_source"),
            "rtt_status_code",
            pl.lit(None, dtype=pl.Utf8).alias("attendance_status"),
            pl.lit(None, dtype=pl.Utf8).alias("admission_method"),
            pl.lit(None, dtype=pl.Boolean).alias("appointment_communicated"),
            # A status code is a claim about the clock, not a care activity.
            pl.lit(False).alias("is_care_activity"),
            pl.coalesce("_status_text", pl.lit("Unknown status")).alias("description"),
        )
    )

    # ------------------------------------------------------------- outpatients
    outpatient = raw_outpatient.polars(lazy=True).select(
        pl.col("appointment_id").alias("source_record_id"),
        pl.col("referral_id").replace("", None),
        pl.col("appointment_date").str.to_date("%Y-%m-%d").alias("event_date"),
        pl.col("attendance_status").cast(pl.Utf8),
        (pl.col("appointment_communicated") == "TRUE").alias("appointment_communicated"),
    ).join(dedup, on="referral_id", how="left").with_columns(
        # Care activity follows the survivor. It happened to a person.
        pl.coalesce("_survivor_id", "referral_id").alias("referral_id")
    ).drop("_survivor_id")

    findings.append(
        outpatient.filter(pl.col("referral_id").is_null()).select(
            pl.lit("NO_REFERRAL_LINK").alias("reason_code"),
            pl.lit("HIGH").alias("severity"),
            pl.lit("raw_outpatient_attendances").alias("source_dataset"),
            pl.col("source_record_id").alias("record_id"),
            pl.lit(None, dtype=pl.Utf8).alias("referral_id"),
            pl.lit(
                "Attendance carries no referral linkage - cannot be attributed "
                "to a pathway."
            ).alias("detail"),
            pl.lit(False).alias("requires_judgement"),
        )
    )
    findings.append(_orphans(pl, outpatient, known, "raw_outpatient_attendances"))

    outpatient_stream = (
        outpatient
        .join(known, on="referral_id", how="semi")
        .select(
            "source_record_id", "referral_id", "event_date",
            pl.lit("OUTPATIENT").alias("event_source"),
            pl.lit(None, dtype=pl.Utf8).alias("rtt_status_code"),
            "attendance_status",
            pl.lit(None, dtype=pl.Utf8).alias("admission_method"),
            "appointment_communicated",
            # Attended and did-not-attend are both care activity. A cancellation
            # is not - nobody was seen, and the clock is untouched either way.
            pl.col("attendance_status").is_in(CARE_ATTENDANCE).alias("is_care_activity"),
            pl.col("attendance_status").replace_strict(
                {
                    "ATTENDED": "Seen in clinic",
                    "DNA": "Did not attend",
                    "PATIENT_CANCELLED": "Patient cancelled - clock continues",
                    "PROVIDER_CANCELLED": "Hospital cancelled - clock continues",
                },
                default=pl.col("attendance_status"),
            ).alias("description"),
        )
    )

    # -------------------------------------------------------------- inpatients
    inpatient = raw_inpatient.polars(lazy=True).select(
        pl.col("admission_id").alias("source_record_id"),
        pl.col("referral_id").replace("", None),
        pl.col("admission_date").str.to_date("%Y-%m-%d").alias("event_date"),
        pl.col("admission_method").cast(pl.Utf8),
    ).join(dedup, on="referral_id", how="left").with_columns(
        pl.coalesce("_survivor_id", "referral_id").alias("referral_id")
    ).drop("_survivor_id").with_columns(
        pl.col("admission_method").str.starts_with("ELECTIVE").alias("_elective")
    )

    findings.append(_orphans(pl, inpatient, known, "raw_inpatient_admissions"))

    # An emergency admission with no referral is not an error to raise. It is a
    # person who came through the front door, and RTT does not cover it: keep it
    # visible, attached to nothing. An ELECTIVE admission with no referral is a
    # different animal - treatment nobody was referred for - and is dropped.
    inpatient_rows = pl.concat(
        [
            inpatient.filter(pl.col("referral_id").is_not_null())
                     .join(known, on="referral_id", how="semi"),
            inpatient.filter(pl.col("referral_id").is_null() & ~pl.col("_elective")),
        ],
        how="vertical",
    )

    inpatient_stream = (
        inpatient_rows
        .select(
            "source_record_id", "referral_id", "event_date",
            pl.lit("INPATIENT").alias("event_source"),
            pl.lit(None, dtype=pl.Utf8).alias("rtt_status_code"),
            pl.lit(None, dtype=pl.Utf8).alias("attendance_status"),
            "admission_method",
            pl.lit(None, dtype=pl.Boolean).alias("appointment_communicated"),
            pl.col("_elective").alias("is_care_activity"),
            pl.when(pl.col("_elective"))
              .then(pl.lit("Admitted for treatment"))
              .otherwise(pl.lit("Emergency admission - not part of the RTT pathway"))
              .alias("description"),
        )
    )

    stream = (
        pl.concat([status_stream, outpatient_stream, inpatient_stream], how="vertical")
        .sort(["referral_id", "event_date", "source_record_id"])
        .with_row_index("_row", offset=1)
        .with_columns(
            pl.format("PE{}", pl.col("_row").cast(pl.Utf8).str.zfill(5)).alias("pathway_event_id")
        )
        .drop("_row")
    )

    conformed_event.write_table(stream.collect())
    activity_findings.write_table(
        pl.concat(findings, how="vertical").select(FINDING_COLUMNS).collect()
    )


def _orphans(pl, events, known, source_dataset):
    """Events quoting a referral that is not in clean_referral.

    Referential breaks are HIGH and always a human's problem: either the
    referral feed is short or the event feed is inventing identifiers, and the
    pipeline cannot tell which.
    """
    return (
        events
        .filter(pl.col("referral_id").is_not_null())
        .join(known, on="referral_id", how="anti")
        .select(
            pl.lit("REFERRAL_NOT_FOUND").alias("reason_code"),
            pl.lit("HIGH").alias("severity"),
            pl.lit(source_dataset).alias("source_dataset"),
            pl.col("source_record_id").alias("record_id"),
            pl.col("referral_id"),
            pl.format(
                "Quotes referral {}, which is not in raw_referrals.",
                pl.col("referral_id"),
            ).alias("detail"),
            pl.lit(False).alias("requires_judgement"),
        )
    )
