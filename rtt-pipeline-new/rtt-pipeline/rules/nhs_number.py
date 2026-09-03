"""
Modulus 11 validation of the NHS number.

Imports polars. Imports nothing from `transforms`, reads no dataset, knows no
path. That is what makes it testable in milliseconds.

The check: weight the first nine digits 10..2, sum, take the remainder mod 11,
subtract from 11. A result of 11 means a check digit of 0. A result of 10 means
the number is invalid - no digit can represent it.
"""
import polars as pl


def modulus_11_valid(col: str = "nhs_number") -> pl.Expr:
    """Boolean expression: does this NHS number pass Modulus 11?

    Returns an expression, not a value, so it composes into a with_columns and
    runs over the whole column at once. A row-wise Python function here would
    cost minutes at volume tier.
    """
    # Int32, not Int8: the weighted sum reaches ~450 and would silently overflow.
    digit = [pl.col(col).str.slice(i, 1).cast(pl.Int32, strict=False) for i in range(10)]

    weighted = sum(d * w for d, w in zip(digit[:9], range(10, 1, -1)))
    remainder = 11 - (weighted % 11)
    check_digit = pl.when(remainder == 11).then(pl.lit(0)).otherwise(remainder)

    well_formed = pl.col(col).str.contains(r"^\d{10}$")

    return (well_formed & (check_digit != 10) & (check_digit == digit[9])).fill_null(False)
