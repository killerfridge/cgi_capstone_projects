"""
Referral layer: deduplicate, validate the identifier, emit findings.

Wiring only.

Two things to notice.

First, the findings dataset. The reference solution collects exceptions in a
Python list because it runs in one process. A pipeline cannot do that - each
transform is its own job - so every transform that notices something emits a
findings row, and publish.py unions them. Reaching for a module-level list here
is the single most common thing a Databricks notebook habit produces, and it
works right up until the transform runs on more than one node.

Second, a duplicate is collapsed, not deleted. The finding carries both ids -
record_id is the referral that lost, referral_id is the one that survived - so
the finding IS the mapping that activity.py needs. Your build log is data.
"""
import datetime as dt

from transforms.api import Input, Output, transform

from rules.nhs_number import modulus_11_valid

DEDUP_WINDOW_DAYS = 30      # a parameter, deliberately. Justify it in the README.
SNAPSHOT_DATE = dt.date(2026, 3, 31)

FINDING_COLUMNS = [
    "reason_code", "severity", "source_dataset", "record_id",
    "referral_id", "detail", "requires_judgement",
]


@transform.using(
    raw_referrals=Input("/rtt/raw/raw_referrals"),
    clean_provider=Input("/rtt/clean/clean_provider"),
    clean_referral=Output("/rtt/clean/clean_referral"),
    referral_findings=Output("/rtt/clean/referral_findings"),
)
def compute(raw_referrals, clean_provider, clean_referral, referral_findings):
    import polars as pl

    referrals = (
        raw_referrals.polars(lazy=True)
        .select(
            pl.col("referral_id").cast(pl.Utf8),
            pl.col("nhs_number").cast(pl.Utf8),
            pl.col("referral_received_date").str.to_date("%Y-%m-%d"),
            pl.col("referral_source").cast(pl.Utf8),
            pl.col("treatment_function_code").cast(pl.Utf8),
            pl.col("provider_code").cast(pl.Utf8),
            pl.col("priority").cast(pl.Utf8),
        )
        .with_columns(modulus_11_valid().alias("nhs_number_valid"))
    )

    # ---- deduplicate: same patient, same treatment function, within the window
    #
    # The survivor is the EARLIEST referral, because its clock started earlier
    # and keeping the later one shortens the recorded wait. Every deduplication
    # rule has a direction; this one has to favour the patient.
    # Gaps and islands, the same shape you would write in SQL. A gap wider than
    # the window opens a new island; everything inside an island collapses onto
    # the island's first referral.
    #
    # State the semantics in your README, because there is a second reading:
    # "within 30 days of the SURVIVOR" rather than "within 30 days of the row
    # before". They differ once three referrals arrive 25 days apart - islands
    # collapse all three, the other reading keeps the third. Neither is wrong.
    # Not knowing which one you implemented is.
    group = ["nhs_number", "treatment_function_code"]
    ordered = (
        referrals
        .sort(group + ["referral_received_date", "referral_id"])
        .with_columns(
            (
                (pl.col("referral_received_date")
                 - pl.col("referral_received_date").shift(1).over(group)
                 ).dt.total_days() > DEDUP_WINDOW_DAYS
            ).fill_null(True).alias("_opens_island")
        )
        .with_columns(pl.col("_opens_island").cum_sum().over(group).alias("_island"))
        .with_columns(
            # first() over the island, not shift(1): a chain of duplicates must
            # all point at the surviving referral, never at another duplicate.
            pl.col("referral_id").first().over(group + ["_island"]).alias("_survivor_id"),
            pl.col("referral_received_date").first().over(group + ["_island"]).alias("_survivor_date"),
            (~pl.col("_opens_island")).alias("_is_duplicate"),
        )
    )

    survivors = ordered.filter(~pl.col("_is_duplicate"))
    duplicates = ordered.filter(pl.col("_is_duplicate"))

    # ---- resolve the provider AS AT THE REFERRAL DATE
    #
    # The code was valid when the referral was made. Resolving it forward to
    # today here would lose the fact that it was ever referred anywhere else,
    # and the snapshot needs both. publish.py resolves the other one.
    provider = clean_provider.polars(lazy=True).select(
        pl.col("provider_code"),
        pl.col("resolved_code").alias("provider_code_resolved"),
    )

    clean_referral.write_table(
        survivors
        .join(provider, on="provider_code", how="left")
        .select(
            "referral_id", "nhs_number", "nhs_number_valid", "referral_received_date",
            "referral_source", "treatment_function_code", "provider_code",
            pl.coalesce("provider_code_resolved", "provider_code").alias("provider_code_resolved"),
            "priority",
        )
        .sort("referral_id")
        .collect()
    )

    # ---- findings
    dup_findings = duplicates.select(
        pl.lit("DUPLICATE_REFERRAL_COLLAPSED").alias("reason_code"),
        pl.lit("LOW").alias("severity"),
        pl.lit("raw_referrals").alias("source_dataset"),
        pl.col("referral_id").alias("record_id"),
        pl.col("_survivor_id").alias("referral_id"),
        pl.format(
            "Same patient and treatment function as {}, {} days apart. "
            "Collapsed, earlier clock start retained.",
            pl.col("_survivor_id"),
            (pl.col("referral_received_date") - pl.col("_survivor_date")).dt.total_days(),
        ).alias("detail"),
        pl.lit(False).alias("requires_judgement"),
    )

    # An invalid NHS number is a Validation Task, not a deletion. The identifier
    # is wrong; the patient is not. Drop the row and the waiting list is short by
    # one real person - the error nobody notices because the number improves.
    nhs_findings = survivors.filter(~pl.col("nhs_number_valid")).select(
        pl.lit("INVALID_NHS_NUMBER").alias("reason_code"),
        pl.lit("MEDIUM").alias("severity"),
        pl.lit("raw_referrals").alias("source_dataset"),
        pl.col("referral_id").alias("record_id"),
        pl.col("referral_id"),
        pl.lit(
            "NHS number fails the Modulus 11 check. Pathway retained - the "
            "identifier is wrong, the patient is not."
        ).alias("detail"),
        pl.lit(True).alias("requires_judgement"),
    )

    referral_findings.write_table(
        pl.concat([dup_findings, nhs_findings], how="vertical")
        .select(FINDING_COLUMNS)
        .collect()
    )
