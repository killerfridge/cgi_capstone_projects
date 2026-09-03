"""
No Spark, no build, no Foundry. Runs in milliseconds against a frame you can
read on one screen.

The numbers here are in the 999-prefixed synthetic range, which is reserved for
test data and issued to nobody.
"""
import polars as pl

from rules.nhs_number import modulus_11_valid


def check(numbers):
    return (
        pl.DataFrame({"nhs_number": numbers})
        .select(modulus_11_valid())["nhs_number"]
        .to_list()
    )


def test_valid_numbers_pass():
    assert check(["9990000018", "9990000026"]) == [True, True]


def test_wrong_check_digit_fails():
    # Same number, last digit moved by one.
    assert check(["9990000019"]) == [False]


def test_malformed_input_is_false_not_null():
    # Nine digits, eleven digits, letters, empty, null. A null here propagates
    # into an is_not_null filter downstream and silently keeps a bad row.
    assert check(["999000001", "99900000188", "99900000AB", "", None]) == [False] * 5


def test_check_digit_of_ten_is_invalid():
    # A remainder of 1 gives a check digit of 10, which no single digit can
    # represent. The number is unissuable, not merely mistyped.
    # 999000000 weights to 243; 243 mod 11 is 1, so the check digit would be 10.
    # Every 9990000000-9990000009 is therefore invalid, whatever the last digit.
    assert check(["9990000000", "9990000005"]) == [False, False]
