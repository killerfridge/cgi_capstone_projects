"""
Organisation codes: inferring succession, and resolving a code as at a date.

Imports polars. Imports nothing from `transforms`.

ODS does not publish "RZB became RZD". It publishes RZB with a valid_to and RZD
with a valid_from, and leaves you to notice that one closed the day before the
other opened. That inference is a judgement about the domain, which is why it
lives here and not in the transform that happens to read the feed.
"""
import datetime as dt

import polars as pl

MAX_SUCCESSION_DEPTH = 5


def infer_successors(providers: pl.LazyFrame) -> pl.LazyFrame:
    """Add successor_code: the organisation that opened as this one closed.

    Adjacency is the whole rule - closed on the 30th, opened on the 1st. A merge
    is not always a succession and this will occasionally be wrong, which is
    exactly why applying it later raises a Validation Task rather than quietly
    rewriting the code.
    """
    # Every opening, not only the currently-open ones: a succession can be two
    # hops long, and keying on current organisations alone breaks the chain at
    # the first intermediate that has since closed too.
    openings = providers.select(
        pl.col("valid_from").alias("_opens_on"),
        pl.col("provider_code").alias("successor_code"),
    )
    return (
        providers
        .with_columns((pl.col("valid_to") + dt.timedelta(days=1)).alias("_opens_on"))
        .join(openings, on="_opens_on", how="left")
        .with_columns(pl.col("valid_to").is_null().alias("is_current"))
        .drop("_opens_on")
    )


def resolve_as_at(providers: pl.LazyFrame, on_date: dt.date) -> pl.LazyFrame:
    """Add resolved_code / resolved_name: the organisation to name at on_date.

    A code still open at on_date resolves to itself. A closed code follows its
    successor chain to whatever is open. Iterative rather than recursive because
    an expression cannot call itself, and bounded because an ODS cycle should
    surface as a bug and not as a hung build.

    A PTL read on 31 March must name an organisation that exists on 31 March.
    Resolve the name but not the code and you get a row reading "RZB / Eastvale
    and Northmoor" - a code and a name that never coexisted.
    """
    resolved = providers.with_columns(
        pl.col("provider_code").alias("resolved_code"),
        pl.col("provider_name").alias("resolved_name"),
        (pl.col("valid_to").is_null() | (pl.col("valid_to") >= on_date)).alias("_settled"),
    )

    for _ in range(MAX_SUCCESSION_DEPTH):
        resolved = (
            resolved
            .join(
                providers.select(
                    pl.col("provider_code").alias("resolved_code"),
                    pl.col("successor_code").alias("_next"),
                ),
                on="resolved_code",
                how="left",
            )
            .join(
                providers.select(
                    pl.col("provider_code").alias("_next"),
                    pl.col("provider_name").alias("_next_name"),
                    pl.col("valid_to").alias("_next_valid_to"),
                ),
                on="_next",
                how="left",
            )
            .with_columns(
                pl.when(pl.col("_settled") | pl.col("_next").is_null())
                  .then(pl.col("resolved_code")).otherwise(pl.col("_next")).alias("resolved_code"),
                pl.when(pl.col("_settled") | pl.col("_next").is_null())
                  .then(pl.col("resolved_name")).otherwise(pl.col("_next_name")).alias("resolved_name"),
                pl.when(pl.col("_settled") | pl.col("_next").is_null())
                  .then(pl.col("_settled"))
                  .otherwise(pl.col("_next_valid_to").is_null() | (pl.col("_next_valid_to") >= on_date))
                  .alias("_settled"),
            )
            .drop("_next", "_next_name", "_next_valid_to")
        )

    return resolved.drop("_settled")
