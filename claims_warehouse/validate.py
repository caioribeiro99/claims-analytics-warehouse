"""Record contracts for the three event streams.

Policy: validate, never repair. A record that breaks its contract is excluded
whole, with every reason it triggered. The fields that did pass are still typed,
so a rejected record stays easy to inspect. Nothing is imputed, rounded or
salvaged: "ten" is not 10, 19.999 is not 19.99, and 03/14/2025 is not a
timestamp. Numbers are checked exactly as delivered: the build parses JSON
numbers as Decimal, never as binary floats.

Reason vocabulary
    missing_<field>       absent, null, or an empty string
    bad_<field>           present but unusable: wrong JSON type (booleans and
                          numeric strings included), non-finite, more precision or
                          magnitude than the column allows, an unparseable
                          timestamp, text that is not valid Unicode, or a key sent
                          more than once in the same record
    nonpositive_<field>   well-typed, but impossible for the business
    negative_<field>
    not_an_object         the array element is not a JSON object

One field is nullable on purpose: a lookup's claim_id must be present, and null
means the lookup did not convert. An empty string or a non-string there is
bad_claim_id.

Each validator returns (typed_row, reasons). An empty reasons list means the
record honored its contract. Numbers come back as Decimal.
"""

from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal

TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%S"
_TIMESTAMP_SHAPE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")

CLAIM_FIELDS = (
    "claim_id",
    "provider_id",
    "product_code",
    "gross_amount",
    "units",
    "service_fee",
    "submitted_at",
)
LOOKUP_FIELDS = (
    "lookup_id",
    "claim_id",
    "product_code",
    "partner_code",
    "channel",
    "looked_up_at",
)
REVERSAL_FIELDS = ("reversal_id", "claim_id", "reversed_at")


def _sent_twice(record: dict, field: str) -> bool:
    # set by claims_warehouse.build.JsonObject; a plain dict cannot repeat a key
    return field in getattr(record, "duplicate_keys", ())


def _absent(value) -> bool:
    return value is None or value == ""


def _valid_unicode(value: str) -> bool:
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:  # e.g. a lone surrogate escape such as "\ud800"
        return False
    return True


def _text(record: dict, field: str, reasons: list[str]) -> str | None:
    """Required identifier or label."""
    value = record.get(field)
    if _sent_twice(record, field):
        reasons.append(f"bad_{field}")
        return None
    if _absent(value):
        reasons.append(f"missing_{field}")
        return None
    if not isinstance(value, str) or not _valid_unicode(value):
        reasons.append(f"bad_{field}")
        return None
    return value


def _number(
    record: dict, field: str, reasons: list[str], precision: int, scale: int
) -> Decimal | None:
    """Required JSON number that fits DECIMAL(precision, scale) without rounding."""
    value = record.get(field)
    if _sent_twice(record, field):
        reasons.append(f"bad_{field}")
        return None
    if _absent(value):
        reasons.append(f"missing_{field}")
        return None
    if isinstance(value, bool) or not isinstance(value, int | float | Decimal):
        reasons.append(f"bad_{field}")
        return None
    exact = value if isinstance(value, Decimal) else Decimal(repr(value))
    if (
        not exact.is_finite()
        or exact.as_tuple().exponent < -scale
        or abs(exact) >= Decimal(10) ** (precision - scale)
    ):
        reasons.append(f"bad_{field}")
        return None
    return exact


def _timestamp(record: dict, field: str, reasons: list[str]) -> str | None:
    """Required naive ISO-8601 timestamp with seconds, e.g. 2025-03-14T10:22:05."""
    value = record.get(field)
    if _sent_twice(record, field):
        reasons.append(f"bad_{field}")
        return None
    if _absent(value):
        reasons.append(f"missing_{field}")
        return None
    if not isinstance(value, str) or not _TIMESTAMP_SHAPE.fullmatch(value):
        reasons.append(f"bad_{field}")
        return None
    try:
        datetime.strptime(value, TIMESTAMP_FORMAT)
    except ValueError:
        reasons.append(f"bad_{field}")
        return None
    return value


def _nullable_reference(record: dict, field: str, reasons: list[str]) -> str | None:
    """A key that must be present; null is meaningful (no conversion happened)."""
    if _sent_twice(record, field):
        reasons.append(f"bad_{field}")
        return None
    if field not in record:
        reasons.append(f"missing_{field}")
        return None
    value = record[field]
    if value is None:
        return None
    if not isinstance(value, str) or value == "" or not _valid_unicode(value):
        reasons.append(f"bad_{field}")
        return None
    return value


def validate_claim(record) -> tuple[dict, list[str]]:
    if not isinstance(record, dict):
        return dict.fromkeys(CLAIM_FIELDS), ["not_an_object"]
    reasons: list[str] = []
    row = {
        "claim_id": _text(record, "claim_id", reasons),
        "provider_id": _text(record, "provider_id", reasons),
        "product_code": _text(record, "product_code", reasons),
        "gross_amount": _number(record, "gross_amount", reasons, precision=12, scale=2),
        "units": _number(record, "units", reasons, precision=12, scale=3),
        "service_fee": _number(record, "service_fee", reasons, precision=10, scale=2),
        "submitted_at": _timestamp(record, "submitted_at", reasons),
    }
    if row["gross_amount"] is not None and row["gross_amount"] <= 0:
        reasons.append("nonpositive_gross_amount")
    if row["units"] is not None and row["units"] <= 0:
        reasons.append("nonpositive_units")
    if row["service_fee"] is not None and row["service_fee"] < 0:
        reasons.append("negative_service_fee")
    return row, reasons


def validate_lookup(record) -> tuple[dict, list[str]]:
    if not isinstance(record, dict):
        return dict.fromkeys(LOOKUP_FIELDS), ["not_an_object"]
    reasons: list[str] = []
    row = {
        "lookup_id": _text(record, "lookup_id", reasons),
        "claim_id": _nullable_reference(record, "claim_id", reasons),
        "product_code": _text(record, "product_code", reasons),
        "partner_code": _text(record, "partner_code", reasons),
        # An open enumeration: any label is kept verbatim, only its absence is a violation.
        "channel": _text(record, "channel", reasons),
        "looked_up_at": _timestamp(record, "looked_up_at", reasons),
    }
    return row, reasons


def validate_reversal(record) -> tuple[dict, list[str]]:
    if not isinstance(record, dict):
        return dict.fromkeys(REVERSAL_FIELDS), ["not_an_object"]
    reasons: list[str] = []
    row = {
        "reversal_id": _text(record, "reversal_id", reasons),
        "claim_id": _text(record, "claim_id", reasons),
        "reversed_at": _timestamp(record, "reversed_at", reasons),
    }
    return row, reasons
