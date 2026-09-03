"""
Publish layer: the PTL snapshot, and the split between work and housekeeping.

Wiring only.

The split is the thing worth arguing about. Every finding the pipeline raised
lands in one of two places:

  validation_task  a pathway needing a human decision, with a reason, an owner,
                   a state and an outcome. This is a worklist. It is the object
                   type a validator opens in the morning.

  build_log        what the pipeline resolved by itself - a duplicate collapsed,
                   an event discarded, a succession applied. Nobody is assigned
                   to it. It exists so that when a number looks wrong in six
                   months you can find out what the build did.

On the seed cohort that is 6 tasks against 3 log entries. On the full tier,
3,601 against 3,706. Route them all to one queue and a validator opens their
morning worklist to find 51% of it is noise, and stops reading it. Not
everything your pipeline notices is somebody's job.
"""
import datetime as dt

from transforms.api import Input, Output, transform

from rules import measures, providers

SNAPSHOT_DATE = dt.date(2026, 3, 31)

# Reasons the pipeline settled on its own. Everything else needs a person.
HOUSEKEEPING = [
    "DUPLICATE_REFERRAL_COLLAPSED",
    "DUPLICATE_EVENT_DISCARDED",
    "PROVIDER_SUCCESSION_APPLIED",
]


@transform.using(
    pathway=Input("/rtt/pathway/pathway"),
    clean_provider=Input("/rtt/clean/clean_provider"),
    clean_treatment_function=Input("/rtt/clean/clean_treatment_function"),
    referral_findings=Input("/rtt/clean/referral_findings"),
    activity_findings=Input("/rtt/clean/activity_findings"),
    pathway_findings=Input("/rtt/pathway/pathway_findings"),
    ptl_snapshot=Output("/rtt/publish/ptl_snapshot"),
    validation_task=Output("/rtt/publish/validation_task"),
    build_log=Output("/rtt/publish/build_log"),
)
def compute(
    pathway, clean_provider, clean_treatment_function,
    referral_findings, activity_findings, pathway_findings,
    ptl_snapshot, validation_task, build_log,
):
    import polars as pl

    pathways = pathway.polars(lazy=True)
    provider = clean_provider.polars(lazy=True)
    tfc = clean_treatment_function.polars(lazy=True).select(
        "treatment_function_code", "treatment_function_name"
    )

    # The PTL is read AS AT the snapshot date, so it must name an organisation
    # that exists on the snapshot date. Resolve the code as well as the name:
    # resolve only the name and you publish "RZB / Eastvale and Northmoor", a
    # pairing that never existed on any day.
    at_snapshot = providers.resolve_as_at(provider, SNAPSHOT_DATE).select(
        pl.col("provider_code"),
        pl.col("resolved_code"),
        pl.col("resolved_name"),
    )

    # The snapshot is the ONE place the elapsed measures are computed, and they
    # are frozen here on purpose: "as at 31 March" is a statutory return, and a
    # published figure that moves when you rebuild is not reproducible. The
    # Pathway object deliberately carries none of this - see rules/measures.py.
    incomplete = (
        measures.measure_as_at(
            pathways.filter(pl.col("pathway_status") == "INCOMPLETE"), SNAPSHOT_DATE
        )
        .join(at_snapshot, on="provider_code", how="left")
        .join(tfc, on="treatment_function_code", how="left")
    )

    ptl_snapshot.write_table(
        incomplete.select(
            pl.lit(SNAPSHOT_DATE).alias("snapshot_date"),
            "pathway_id", "nhs_number",
            "treatment_function_code", "treatment_function_name",
            pl.col("resolved_code").alias("provider_code"),
            pl.col("provider_code").alias("provider_code_as_referred"),
            pl.col("resolved_name").alias("provider_name"),
            "clock_start_date", "breach_18wk_date", "breach_52wk_date",
            "weeks_waiting", "breach_band", "within_18_weeks",
        )
        .sort("weeks_waiting", descending=True)
        .collect()
    )

    # Succession is applied silently in the data and loudly in the log. It is
    # MEDIUM and requires_judgement because an ODS adjacency is an inference,
    # and somebody should confirm the list moved to the right organisation.
    succession_findings = (
        incomplete
        .filter(pl.col("resolved_code") != pl.col("provider_code"))
        .join(provider.select("provider_code", "valid_to"), on="provider_code", how="left")
        .select(
            pl.lit("PROVIDER_SUCCESSION_APPLIED").alias("reason_code"),
            pl.lit("MEDIUM").alias("severity"),
            pl.lit("clean_provider").alias("source_dataset"),
            pl.col("pathway_id").alias("record_id"),
            pl.col("referral_id"),
            pl.format(
                "Referred to {}, which closed on {}. Resolved through "
                "succession to {} for the snapshot.",
                pl.col("provider_code"), pl.col("valid_to"), pl.col("resolved_code"),
            ).alias("detail"),
            pl.lit(True).alias("requires_judgement"),
        )
    )

    # Numbering is by (layer, record_id), not by arrival. Arrival order is
    # whatever the executor felt like today, and a task_id that changes between
    # builds of the same data is a task_id nobody can cite in a ticket.
    layers = [
        referral_findings.polars(lazy=True),
        activity_findings.polars(lazy=True),
        pathway_findings.polars(lazy=True),
        succession_findings,
    ]
    all_findings = pl.concat(
        [f.with_columns(pl.lit(i).alias("_layer")) for i, f in enumerate(layers)],
        how="vertical_relaxed",
    )

    pathway_of = pathways.select("referral_id", "pathway_id").unique(subset=["referral_id"])

    tasks = (
        all_findings
        .filter(~pl.col("reason_code").is_in(HOUSEKEEPING))
        .join(pathway_of, on="referral_id", how="left")
        .sort(["_layer", "record_id"])
        .with_row_index("_n", offset=1)
        .select(
            pl.format("VT{}", pl.col("_n").cast(pl.Utf8).str.zfill(4)).alias("task_id"),
            "pathway_id", "referral_id",
            pl.col("record_id").alias("source_record_id"),
            pl.col("reason_code").alias("raised_reason"),
            pl.lit(SNAPSHOT_DATE).alias("raised_at"),
            "severity", "detail", "requires_judgement",
            # The pipeline only ever creates a task OPEN. Everything below this
            # line is written by a validator, through an Action, against the
            # Ontology object - never by a build.
            pl.lit("OPEN").alias("status"),
            pl.lit(None, dtype=pl.Utf8).alias("assigned_to"),
            pl.lit(None, dtype=pl.Utf8).alias("outcome"),
            pl.lit(None, dtype=pl.Utf8).alias("resolved_by"),
            pl.lit(None, dtype=pl.Date).alias("resolved_at"),
        )
    )
    validation_task.write_table(tasks.collect())

    log = (
        all_findings
        .filter(pl.col("reason_code").is_in(HOUSEKEEPING))
        .sort(["_layer", "record_id"])
        .with_row_index("_n", offset=1)
        .select(
            pl.format("BL{}", pl.col("_n").cast(pl.Utf8).str.zfill(4)).alias("log_id"),
            pl.col("reason_code").alias("action_taken"),
            "source_dataset",
            "record_id",
            "detail",
            pl.lit(SNAPSHOT_DATE).alias("build_date"),
        )
    )
    build_log.write_table(log.collect())
