"""Record contracts: what is missing, bad or impossible, and that nothing is coerced."""

import pytest

from claims_warehouse.build import parse_json, to_json
from claims_warehouse.validate import validate_claim, validate_lookup, validate_reversal
from tests.conftest import DROP, claim, lookup, reversal


def reasons_for(record, validator=validate_claim):
    return validator(record)[1]


def test_a_clean_claim_passes_and_keeps_its_values():
    row, reasons = validate_claim(claim())
    assert reasons == []
    assert row["gross_amount"] == 10.0
    assert row["submitted_at"] == "2025-04-01T10:00:00"


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        (DROP, "missing_gross_amount"),
        (None, "missing_gross_amount"),
        ("", "missing_gross_amount"),
        ("12.50", "bad_gross_amount"),  # a numeric string is not a number
        ("twelve", "bad_gross_amount"),
        (True, "bad_gross_amount"),  # neither is a boolean
        (float("nan"), "bad_gross_amount"),
        (float("inf"), "bad_gross_amount"),
        (19.999, "bad_gross_amount"),  # finer than cents: rejected, not rounded
        (10_000_000_000.0, "bad_gross_amount"),  # does not fit DECIMAL(12,2)
        (0, "nonpositive_gross_amount"),
        (-5.25, "nonpositive_gross_amount"),
    ],
)
def test_amount_contract(value, reason):
    assert reasons_for(claim(gross_amount=value)) == [reason]


def test_whole_quantities_may_arrive_as_int_or_float():
    assert reasons_for(claim(units=30)) == []
    assert reasons_for(claim(units=30.0)) == []
    assert reasons_for(claim(units=2.5)) == []
    assert reasons_for(claim(units=2.0005)) == ["bad_units"]


def test_zero_service_fee_is_a_valid_known_zero_but_negative_is_not():
    assert reasons_for(claim(service_fee=0)) == []
    assert reasons_for(claim(service_fee=-0.01)) == ["negative_service_fee"]


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        ("2025-04-01T10:00:00", None),
        ("2025-02-30T10:00:00", "bad_submitted_at"),  # right shape, impossible date
        ("2025-04-01 10:00:00", "bad_submitted_at"),  # the contract requires the T separator
        ("2025-04-01T10:00", "bad_submitted_at"),  # seconds are required
        ("2025-04-01T10:00:00Z", "bad_submitted_at"),  # offsets are outside the contract
        ("04/01/2025 10:00", "bad_submitted_at"),
        (1743501600, "bad_submitted_at"),  # epoch seconds are not coerced
        ("", "missing_submitted_at"),
        (None, "missing_submitted_at"),
        (DROP, "missing_submitted_at"),
    ],
)
def test_timestamp_contract(value, reason):
    assert reasons_for(claim(submitted_at=value)) == ([] if reason is None else [reason])


def test_identifiers_must_be_non_empty_strings():
    assert reasons_for(claim(provider_id=12345)) == ["bad_provider_id"]
    assert reasons_for(claim(provider_id="")) == ["missing_provider_id"]
    assert reasons_for(claim(claim_id=DROP)) == ["missing_claim_id"]


def test_every_violation_is_reported_not_just_the_first():
    record = claim(gross_amount="ten", units=True, product_code=None, submitted_at="yesterday")
    assert sorted(reasons_for(record)) == [
        "bad_gross_amount",
        "bad_submitted_at",
        "bad_units",
        "missing_product_code",
    ]


def test_fields_that_passed_stay_typed_on_a_rejected_record():
    row, reasons = validate_claim(claim(units="ten"))
    assert reasons == ["bad_units"]
    assert row["claim_id"] == "c1"
    assert row["units"] is None


def test_lookup_claim_reference_null_means_no_conversion():
    assert reasons_for(lookup(claim_id=None), validate_lookup) == []
    assert reasons_for(lookup(claim_id="c9"), validate_lookup) == []
    assert reasons_for(lookup(claim_id=""), validate_lookup) == ["bad_claim_id"]
    assert reasons_for(lookup(claim_id=42), validate_lookup) == ["bad_claim_id"]
    assert reasons_for(lookup(claim_id=DROP), validate_lookup) == ["missing_claim_id"]


def test_channel_is_an_open_enumeration_kept_verbatim():
    for label in ("web", "api", "Web", "phone", "kiosk"):
        assert reasons_for(lookup(channel=label), validate_lookup) == []
    assert reasons_for(lookup(channel=None), validate_lookup) == ["missing_channel"]


def test_reversal_contract():
    assert reasons_for(reversal(), validate_reversal) == []
    broken = reversal(claim_id=DROP, reversed_at="2025-13-01T00:00:00")
    assert reasons_for(broken, validate_reversal) == ["missing_claim_id", "bad_reversed_at"]


def test_a_record_that_is_not_an_object_is_rejected():
    for validator in (validate_claim, validate_lookup, validate_reversal):
        row, reasons = validator(["not", "a", "record"])
        assert reasons == ["not_an_object"]
        assert set(row.values()) == {None}


CLAIM_JSON = (
    '{"claim_id": "c1", "provider_id": "PRV10001", "product_code": "PRD-0001", "units": 1, '
    '"service_fee": 1.00, "submitted_at": "2025-04-01T10:00:00", %s}'
)


def reasons_from_json(extra_fields: str) -> list[str]:
    return validate_claim(parse_json(CLAIM_JSON % extra_fields))[1]


def test_numbers_are_checked_as_delivered_not_as_binary_floats():
    assert reasons_from_json('"gross_amount": 19.99') == []
    assert reasons_from_json('"gross_amount": 1.5e2') == []  # 150 in exponent notation
    # parsed as floats, both would round to plausible values before the contract saw them
    assert reasons_from_json('"gross_amount": 19.999999999999999') == ["bad_gross_amount"]
    assert reasons_from_json('"gross_amount": 1e-400') == ["bad_gross_amount"]


def test_a_key_sent_twice_is_bad_and_stays_visible_in_the_payload():
    record = parse_json(CLAIM_JSON % '"gross_amount": "ten", "gross_amount": 10.00')
    assert validate_claim(record)[1] == ["bad_gross_amount"]
    assert to_json(record).count('"gross_amount"') == 2


def test_the_stored_payload_keeps_numbers_exactly_as_delivered():
    record = parse_json('{"gross_amount": 19.999999999999999, "units": 30.0, "rows": 7}')
    assert to_json(record) == '{"gross_amount":19.999999999999999,"units":30.0,"rows":7}'


def test_the_stored_payload_keeps_exponent_and_signed_literals_as_delivered():
    record = parse_json('{"a": 1.5e1, "b": -0, "c": 1E+2}')
    assert to_json(record) == '{"a":1.5e1,"b":-0,"c":1E+2}'
    assert reasons_from_json('"gross_amount": 1.5e1') == []  # validated as 15


def test_text_that_is_not_valid_unicode_is_bad():
    assert reasons_for(claim(provider_id="\ud800")) == ["bad_provider_id"]
    assert reasons_for(lookup(claim_id="\udfff"), validate_lookup) == ["bad_claim_id"]
