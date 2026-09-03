"""
GIVEN. This file ships in the starter repository - trainees read it, they do not
write it. It is here because typing three reference feeds teaches nothing that
the rest of Project 1 does not teach better, and the two days are tight.

Read it anyway: it is where START_CODES and STOP_CODES come from, and knowing
that is load-bearing from step 5 onwards.

---

Reference layer: type the three reference feeds and infer provider succession.

Wiring only. It reads, calls rules, and writes. Every judgement in this file
came from `rules.providers`; nothing about RTT is decided here.

`clean_rtt_status` is the important one. It is where START_CODES and STOP_CODES
come from, and the reason no module in `rules/` contains a literal set of status
codes. Reference data classifies; it does not decide - code 33 is classified
STOP, and whether it stops anything is a judgement made in rules.clocks.
"""
import datetime as dt

from transforms.api import Input, Output, transform

from rules import providers

SNAPSHOT_DATE = dt.date(2026, 3, 31)


@transform.using(
    raw_rtt_status=Input("/rtt/raw/ref_rtt_status"),
    raw_treatment_function=Input("/rtt/raw/ref_treatment_function"),
    raw_providers=Input("/rtt/raw/raw_ods_providers"),
    clean_rtt_status=Output("/rtt/clean/clean_rtt_status"),
    clean_treatment_function=Output("/rtt/clean/clean_treatment_function"),
    clean_provider=Output("/rtt/clean/clean_provider"),
)
def compute(
    raw_rtt_status,
    raw_treatment_function,
    raw_providers,
    clean_rtt_status,
    clean_treatment_function,
    clean_provider,
):
    import polars as pl

    status = raw_rtt_status.polars(lazy=True).select(
        pl.col("rtt_status_code").cast(pl.Utf8),
        pl.col("official_description").cast(pl.Utf8),
        pl.col("clock_effect").cast(pl.Utf8),
        pl.col("patient_facing_description").cast(pl.Utf8),
    )
    clean_rtt_status.write_table(status.collect())

    tfc = raw_treatment_function.polars(lazy=True).select(
        pl.col("treatment_function_code").cast(pl.Utf8),
        pl.col("treatment_function_name").cast(pl.Utf8),
        (pl.col("is_consultant_led") == "Y").alias("is_consultant_led"),
    )
    clean_treatment_function.write_table(tfc.collect())

    typed = raw_providers.polars(lazy=True).select(
        pl.col("provider_code").cast(pl.Utf8),
        pl.col("provider_name").cast(pl.Utf8),
        pl.col("org_type").cast(pl.Utf8),
        pl.col("valid_from").str.to_date("%Y-%m-%d"),
        # Empty string is not a date and is not null until you say so. Left
        # alone it becomes a parse failure at volume tier and a silent null here.
        pl.col("valid_to").replace("", None).str.to_date("%Y-%m-%d"),
    )

    with_successors = providers.infer_successors(typed)
    resolved = providers.resolve_as_at(with_successors, SNAPSHOT_DATE)

    clean_provider.write_table(
        resolved.select(
            "provider_code", "provider_name", "org_type",
            "valid_from", "valid_to", "is_current", "successor_code",
            "resolved_code", "resolved_name",
        ).sort("provider_code").collect()
    )
