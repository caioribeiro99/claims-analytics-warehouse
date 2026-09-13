"""Deterministic synthetic sources for the claims warehouse.

    python -m claims_warehouse.synthetic --out data/generated [--seed 20250101]

Northstar is a fictional business. Referral partners send price lookups;
some lookups convert into claims submitted by providers; some claims are
reversed later. This module writes the files such a business would receive:

    claims/claims-YYYY-MM.json                  JSON arrays, one file per month
    lookups/lookups-YYYY-MM-{a,b}.json          two files per month
    reversals/reversals-YYYY-MM.json            by month of the reversal
    providers/providers.csv                     in-network providers
    partners/partners.csv                       partner payout terms
    products/products.csv                       product catalog
    reference_costs/publication-YYYY-MM-DD.csv  weekly unit-cost publications
    _expected.json                              what a correct warehouse must contain

Edge cases are injected on purpose: malformed records, conflicting and exactly
redelivered claim ids, out-of-network providers, orphan references, reused
reversal ids, linked events out of time order, restated and missing reference
costs, unknown partner terms, known-zero fees. `_expected.json` is the
generator's own account of what a correct warehouse contains. Record-level
defects, economics and reference costs follow from how each record was
constructed, not from the warehouse's validators or SQL. Scope and status
precedence are necessarily a second statement of the model's rules, so the
hand-written cases in tests/test_scope.py and tests/test_conversion.py pin
those rules as well.

Determinism: output depends only on the seed. Every draw goes through
random.Random.random(), whose sequence Python guarantees across versions, with
exact integer/Decimal arithmetic on top (no random.choice or gauss, no
math.log/exp). The files are byte-identical on every platform, and
data/synthetic-manifest.json pins their hashes.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import random
import shutil
import sys
from bisect import bisect_right
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal
from itertools import accumulate
from pathlib import Path

DEFAULT_SEED = 20250101
WINDOW_START = datetime(2025, 1, 1)
WINDOW_END = datetime(2025, 10, 1)  # exclusive
FIRST_PUBLICATION = date(2024, 12, 30)  # a Monday; cost publications are weekly
PUBLICATIONS = 40
EXPECTED_FILE = "_expected.json"
ISO_FORMAT = "%Y-%m-%dT%H:%M:%S"
CENT = Decimal("0.01")

NETWORKS = (
    ("Redwood", 12),
    ("Cedar", 10),
    ("Aspen", 8),
    ("Willow", 7),
    ("Juniper", 6),
    ("Maple", 4),
)
REGIONS = ("North", "South", "East", "West", "Central")
OUT_OF_NETWORK_PROVIDERS = 5
OUT_OF_NETWORK_SHARE = 0.014  # claims submitted by providers outside the network
DIRECT_CLAIMS_PER_DAY = 1.8  # claims with no lookup behind them (unattributed)
NOISE_CHANNELS = ("Web", "phone", "email", "unknown")
NOISE_CHANNEL_SHARE = 0.012
NONCANONICAL_CODE_SHARE = 0.004  # lookups sending a lower-cased product code
WEEKDAY_FACTOR = (1.00, 1.03, 1.01, 0.99, 1.06, 0.74, 0.58)
HOUR_WEIGHTS = (1, 1, 1, 1, 1, 2, 3, 5, 7, 8, 8, 8, 8, 8, 8, 7, 7, 7, 6, 6, 5, 4, 3, 2)
REVERSAL_LAG_SECONDS = (  # (from, to, weight)
    (600, 2 * 86400, 30),
    (2 * 86400, 7 * 86400, 35),
    (7 * 86400, 14 * 86400, 20),
    (14 * 86400, 30 * 86400, 12),
    (30 * 86400, 60 * 86400, 3),
)
WORD_AMOUNTS = ("twelve dollars", "N/A", "forty-two", "TBD")
WORD_UNITS = ("ten", "a dozen", "two boxes", "several")


@dataclass(frozen=True)
class Partner:
    code: str
    name: str
    flat_payout_cents: int | None
    revenue_share_pct: int | None
    channel: str
    daily_lookups: float
    conversion: float
    launch: date | None = None
    listed: bool = True  # present in partners.csv


PARTNERS = (
    Partner("ORION", "Orion Referrals", 75, None, "api", 42, 0.44),
    Partner("VEGA", "Vega Coupons", None, 35, "web", 92, 0.12),
    Partner("CAPELLA", "Capella Navigator", None, 60, "api", 30, 0.40),
    Partner("ALTAIR", "Altair Rewards", 0, None, "web", 40, 0.21),
    Partner("DENEB", "Deneb Apps", None, 15, "web", 34, 0.29, launch=date(2025, 4, 1)),
    Partner("RIGEL", "Rigel Benefits", 40, None, "api", 24, 0.33),
    Partner("MIRA", "Mira Labs", 50, None, "web", 0, 0.0),  # signed, no traffic yet
    Partner("CASTOR", "Castor Deals", None, None, "web", 3, 0.25, listed=False),  # no terms on file
)

CATEGORIES = {
    # count, unit of measure mix, unit cost band (1e-5 dollars), service fee band (cents),
    # popularity band (x100), reversal probability, conversion factor
    "generic": {
        "count": 22,
        "uom": (("EA", 70), ("ML", 15), ("G", 15)),
        "cost": (3_000, 120_000),
        "fee": (90, 350),
        "popularity": (100, 600),
        "reversal": 0.035,
        "conversion": 1.05,
    },
    "brand": {
        "count": 10,
        "uom": (("EA", 80), ("ML", 20)),
        "cost": (150_000, 1_800_000),
        "fee": (250, 850),
        "popularity": (60, 250),
        "reversal": 0.06,
        "conversion": 0.95,
    },
    "specialty": {
        "count": 4,
        "uom": (("EA", 100),),
        "cost": (6_000_000, 65_000_000),
        "fee": (900, 2_800),
        "popularity": (15, 50),
        "reversal": 0.11,
        "conversion": 0.60,
    },
}
FORMS = {"EA": "tablet", "ML": "solution", "G": "cream"}
UNIT_SIZES = {
    "EA": ((30, 40), (90, 20), (60, 14), (15, 10), (10, 8), (100, 8)),
    "ML": (("5", 18), ("10", 26), ("2.5", 8), ("100", 24), ("150", 14), ("240", 10)),
    "G": ((15, 35), (30, 35), (45, 15), (60, 15)),
    "specialty": ((1, 55), (2, 30), (4, 15)),
}

CLAIM_DEFECTS = {  # defect -> records to corrupt
    "word_amount": 24,
    "sub_cent_amount": 6,
    "zero_amount": 14,
    "negative_amount": 12,
    "word_units": 18,
    "boolean_units": 5,
    "zero_units": 10,
    "negative_units": 11,
    "negative_fee": 9,
    "absent_fee": 13,
    "null_product": 10,
    "empty_provider": 8,
    "numeric_provider": 4,
    "impossible_date": 9,
    "us_format_date": 11,
    "epoch_date": 6,
    "absent_date": 7,
    "absent_claim_id": 5,
}
CLAIM_DEFECT_COMBOS = {("word_amount", "word_units"): 7, ("null_product", "us_format_date"): 4}
LOOKUP_DEFECTS = {
    "null_partner": 30,
    "absent_partner": 22,
    "impossible_time": 18,
    "truncated_time": 16,
    "empty_time": 9,
    "numeric_claim_id": 7,
    "empty_claim_id": 5,
    "absent_claim_id": 4,
    "null_channel": 8,
    "absent_lookup_id": 4,
    "null_product": 6,
}
REVERSAL_DEFECTS = {
    "absent_claim_id": 5,
    "numeric_claim_id": 2,
    "impossible_time": 6,
    "absent_reversal_id": 3,
}


def iso(moment: datetime) -> str:
    return moment.strftime(ISO_FORMAT)


class Rng:
    """Version-stable sampling built only on random.Random.random()."""

    def __init__(self, seed: int) -> None:
        self._random = random.Random(seed)

    def unit(self) -> float:
        return self._random.random()

    def chance(self, probability: float) -> bool:
        return self._random.random() < probability

    def integer(self, low: int, high: int) -> int:
        """Uniform integer in [low, high]."""
        return low + int(self._random.random() * (high - low + 1))

    def uniform(self, low: float, high: float) -> float:
        return low + (high - low) * self._random.random()

    def pick(self, items):
        return items[int(self._random.random() * len(items))]

    def distinct_indices(self, size: int, k: int) -> list[int]:
        if k > size:
            raise ValueError(f"cannot draw {k} distinct indices from {size}")
        chosen: list[int] = []
        seen: set[int] = set()
        while len(chosen) < k:
            index = int(self._random.random() * size)
            if index not in seen:
                seen.add(index)
                chosen.append(index)
        return chosen

    def uuid(self) -> str:
        digits = "".join(f"{int(self._random.random() * 65536):04x}" for _ in range(8))
        variant = "89ab"[int(digits[16], 16) % 4]
        return (
            f"{digits[0:8]}-{digits[8:12]}-4{digits[13:16]}-"
            f"{variant}{digits[17:20]}-{digits[20:32]}"
        )


class Weighted:
    """Weighted choice over a precomputed cumulative table."""

    def __init__(self, items, weights) -> None:
        self.items = list(items)
        self.cumulative = list(accumulate(float(weight) for weight in weights))

    def draw(self, rng: Rng):
        position = bisect_right(self.cumulative, rng.unit() * self.cumulative[-1])
        return self.items[min(position, len(self.items) - 1)]


@dataclass
class Provider:
    provider_id: str
    network: str
    region: str
    volume: float


@dataclass
class Product:
    code: str
    name: str
    category: str
    unit: str
    popularity: float
    fee_waived: bool = False  # the platform charges no service fee: a known zero
    published: bool = True  # False: no reference cost was ever published
    cost_points: list[tuple[date, int]] = field(default_factory=list)  # (effective, 1e-5 $)
    announced: dict[date, date] = field(default_factory=dict)  # effective -> announced on
    restated: dict[date, tuple[int, int]] = field(default_factory=dict)  # -> (wrong cost, fix pub)
    listed_in: dict[date, list[int]] = field(default_factory=dict)  # -> publication indexes

    def unit_cost(self, on: date) -> Decimal:
        """True unit cost on a date (the corrected value when a price was restated)."""
        cost = self.cost_points[0][1]
        for effective, value in self.cost_points:
            if effective <= on:
                cost = value
        return Decimal(cost) / 100_000


@dataclass
class Record:
    when: datetime  # event time as generated, before any corruption
    data: dict  # the payload exactly as written
    reasons: list[str] = field(default_factory=list)  # contract violations injected
    partner: str | None = None  # claims: partner whose lookup produced the claim
    product: Product | None = None  # claims: the catalog product
    protected: bool = False  # already carries a deliberate edge case
    claim: Record | None = None  # lookups: the claim this lookup produced
    location: str = ""  # "file.json:row", assigned at layout
    order: tuple[str, int] = ("", 0)  # (file, row): the order the build loads records in


class _World:
    def __init__(self, seed: int) -> None:
        self.seed = seed
        self.rng = Rng(seed)
        self.injected: dict[str, Counter] = {
            "claims": Counter(),
            "lookups": Counter(),
            "reversals": Counter(),
        }
        self.claims: list[Record] = []
        self.lookups: list[Record] = []
        self.reversals: list[Record] = []
        self._build_providers()
        self._build_products()
        self.publication_dates = [
            FIRST_PUBLICATION + timedelta(weeks=week) for week in range(PUBLICATIONS)
        ]
        self._schedule_publications()
        self.product_picker = Weighted(self.products, [p.popularity for p in self.products])
        self.provider_picker = Weighted(self.providers, [p.volume for p in self.providers])
        self.unit_pickers = {
            key: Weighted([Decimal(str(size)) for size, _ in sizes], [w for _, w in sizes])
            for key, sizes in UNIT_SIZES.items()
        }
        self.lag_picker = Weighted(
            [(low, high) for low, high, _ in REVERSAL_LAG_SECONDS],
            [weight for *_, weight in REVERSAL_LAG_SECONDS],
        )
        self.hour_picker = Weighted(range(24), HOUR_WEIGHTS)

    # ------------------------------------------------------------ reference data

    def _new_provider_id(self, used: set[str]) -> str:
        while True:
            provider_id = f"PRV{self.rng.integer(10000, 99999)}"
            if provider_id not in used:
                used.add(provider_id)
                return provider_id

    def _build_providers(self) -> None:
        used: set[str] = set()
        self.providers = [
            Provider(
                self._new_provider_id(used),
                network,
                self.rng.pick(REGIONS),
                self.rng.integer(50, 200) / 100,
            )
            for network, count in NETWORKS
            for _ in range(count)
        ]
        self.out_of_network = [self._new_provider_id(used) for _ in range(OUT_OF_NETWORK_PROVIDERS)]
        self.in_network_ids = {provider.provider_id for provider in self.providers}

    def _build_products(self) -> None:
        slots = [name for name, spec in CATEGORIES.items() for _ in range(spec["count"])]
        order = [slots.pop(int(self.rng.unit() * len(slots))) for _ in range(len(slots))]
        per_category: Counter = Counter()
        self.products: list[Product] = []
        for index, category in enumerate(order):
            spec = CATEGORIES[category]
            unit = Weighted(*zip(*spec["uom"], strict=True)).draw(self.rng)
            per_category[category] += 1
            product = Product(
                code=f"PRD-{1001 + index}",
                name=f"{category.capitalize()} {FORMS[unit]} {per_category[category]:02d}",
                category=category,
                unit=unit,
                popularity=self.rng.integer(*spec["popularity"]) / 100,
            )
            product.cost_points = self._cost_history(product)
            self.products.append(product)

        generics = [p for p in self.products if p.category == "generic"]
        brands = [p for p in self.products if p.category == "brand"]
        min(generics[4:], key=lambda p: p.popularity).fee_waived = True
        for product in (generics[2], brands[0]):
            product.published = False
        late = generics[3]  # first published price takes effect mid-window
        late.cost_points = [(date(2025, 4, 16), late.cost_points[0][1])]
        upcoming = brands[1]  # a price announced before it takes effect
        upcoming_date = date(2025, 11, 5)
        upcoming.cost_points.append((upcoming_date, upcoming.cost_points[-1][1] * 108 // 100))
        upcoming.announced[upcoming_date] = date(2025, 9, 22)

    def _cost_history(self, product: Product) -> list[tuple[date, int]]:
        low, high = CATEGORIES[product.category]["cost"]
        cost = self.rng.integer(low, high)
        points = [(date(2024, 10, 1) + timedelta(days=self.rng.integer(0, 84)), cost)]
        changes = Weighted((0, 1, 2, 3, 4), (15, 25, 30, 20, 10)).draw(self.rng)
        week_slots = list(range(1, 37, 3))  # price changes at least three weeks apart
        weeks = sorted(
            week_slots.pop(int(self.rng.unit() * len(week_slots))) for _ in range(changes)
        )
        for week in weeks:
            effective = WINDOW_START.date() + timedelta(weeks=week, days=self.rng.integer(0, 6))
            move = self.rng.integer(2, 15) * (1 if self.rng.chance(0.6) else -1)
            cost = max(1, cost * (100 + move) // 100)
            points.append((effective, cost))
        return points

    def _schedule_publications(self) -> None:
        """Each weekly publication lists every product's current change-point, plus any
        change-point announced ahead of its effective date. A change-point first appears in
        the first publication on or after it becomes known (0-6 days after taking effect)."""
        for product in self.products:
            if not product.published:
                continue
            known_from = {}
            for effective, _ in product.cost_points:
                known = product.announced.get(effective)
                if known is None:
                    known = effective + timedelta(days=self.rng.integer(0, 6))
                known_from[effective] = self._first_publication_on_or_after(known)
            for index, published in enumerate(self.publication_dates):
                visible = [e for e, _ in product.cost_points if known_from[e] <= index]
                current = [e for e in visible if e <= published]
                listed = ([max(current)] if current else []) + [e for e in visible if e > published]
                for effective in listed:
                    product.listed_in.setdefault(effective, []).append(index)
            unlisted = [e for e, _ in product.cost_points if e not in product.listed_in]
            if unlisted:
                raise AssertionError(f"{product.code}: change-points never published: {unlisted}")

        eligible = [
            (product, effective)
            for product in self.products
            if product.published
            for effective, _ in product.cost_points
            if len(product.listed_in.get(effective, ())) >= 3
        ]
        for index in self.rng.distinct_indices(len(eligible), 5):
            product, effective = eligible[index]
            listed = product.listed_in[effective]
            fixed_in = listed[self.rng.integer(1, 2)]
            true_cost = dict(product.cost_points)[effective]
            wrong = true_cost * (100 + self.rng.integer(8, 25)) // 100
            product.restated[effective] = (wrong, fixed_in)

    def _first_publication_on_or_after(self, day: date) -> int:
        for index, published in enumerate(self.publication_dates):
            if published >= day:
                return index
        return PUBLICATIONS  # never within the published series

    # ------------------------------------------------------------ event traffic

    def _second_of_day(self) -> int:
        return self.hour_picker.draw(self.rng) * 3600 + self.rng.integer(0, 3599)

    def _units(self, product: Product) -> Decimal:
        key = "specialty" if product.category == "specialty" else product.unit
        return self.unit_pickers[key].draw(self.rng)

    def _units_json(self, units: Decimal) -> int | float:
        # producers send whole quantities both as 30 and as 30.0
        if units == units.to_integral_value() and self.rng.chance(0.5):
            return int(units)
        return float(units)

    def _claim(self, product: Product, submitted: datetime, partner: str | None) -> Record:
        if self.rng.chance(OUT_OF_NETWORK_SHARE):
            provider_id = self.rng.pick(self.out_of_network)
        else:
            provider_id = self.provider_picker.draw(self.rng).provider_id
        units = self._units(product)
        markup = Decimal(self.rng.integer(115, 190)) / 100
        gross = (product.unit_cost(submitted.date()) * units * markup).quantize(CENT, ROUND_HALF_UP)
        fee = (
            Decimal(0)
            if product.fee_waived
            else Decimal(self.rng.integer(*self._fee(product))) / 100
        )
        record = Record(
            when=submitted,
            data={
                "claim_id": self.rng.uuid(),
                "provider_id": provider_id,
                "product_code": product.code,
                "gross_amount": float(max(CENT, gross)),
                "units": self._units_json(units),
                "service_fee": float(fee),
                "submitted_at": iso(submitted),
            },
            partner=partner,
            product=product,
        )
        self.claims.append(record)
        return record

    @staticmethod
    def _fee(product: Product) -> tuple[int, int]:
        return CATEGORIES[product.category]["fee"]

    def _conversion(self, partner: Partner, moment: datetime, product: Product, channel: str):
        rate = partner.conversion
        if partner.code == "ALTAIR" and moment >= datetime(2025, 7, 1):
            rate = 0.31  # a checkout redesign lifts conversion from July
        rate *= CATEGORIES[product.category]["conversion"]
        return rate * (0.3 if channel != partner.channel else 1.0)

    def _ramp(self, partner: Partner, day: datetime) -> float:
        if partner.launch is None:
            return 1.0
        months_live = (day.year - partner.launch.year) * 12 + day.month - partner.launch.month
        return min(1.0, 0.4 + 0.2 * months_live)

    def _lookup(self, partner: Partner, day: datetime) -> None:
        looked_up = day + timedelta(seconds=self._second_of_day())
        product = self.product_picker.draw(self.rng)
        channel = partner.channel
        if self.rng.chance(NOISE_CHANNEL_SHARE):
            channel = self.rng.pick(NOISE_CHANNELS)
        code = product.code.lower() if self.rng.chance(NONCANONICAL_CODE_SHARE) else product.code
        claim = None
        if self.rng.chance(self._conversion(partner, looked_up, product, channel)):
            submitted = looked_up + timedelta(seconds=self.rng.integer(120, 5400))
            if submitted < WINDOW_END:
                claim = self._claim(product, submitted, partner.code)
        self.lookups.append(
            Record(
                when=looked_up,
                data={
                    "lookup_id": self.rng.uuid(),
                    "claim_id": claim.data["claim_id"] if claim else None,
                    "product_code": code,
                    "partner_code": partner.code,
                    "channel": channel,
                    "looked_up_at": iso(looked_up),
                },
                claim=claim,
            )
        )

    def _reversal(self, claim_id: str, reversed_at: datetime) -> Record:
        record = Record(
            when=reversed_at,
            data={
                "reversal_id": self.rng.uuid(),
                "claim_id": claim_id,
                "reversed_at": iso(reversed_at),
            },
        )
        self.reversals.append(record)
        return record

    def _reversal_lag(self) -> timedelta:
        low, high = self.lag_picker.draw(self.rng)
        return timedelta(seconds=self.rng.integer(low, high))

    def generate_traffic(self) -> None:
        day = WINDOW_START
        while day < WINDOW_END:
            seasonal = (1 + 0.03 * (day.month - 1)) * WEEKDAY_FACTOR[day.weekday()]
            for partner in PARTNERS:
                if partner.daily_lookups == 0 or (partner.launch and day.date() < partner.launch):
                    continue
                volume = partner.daily_lookups * seasonal * self._ramp(partner, day)
                for _ in range(int(volume * self.rng.uniform(0.85, 1.15) + self.rng.unit())):
                    self._lookup(partner, day)
            direct = DIRECT_CLAIMS_PER_DAY * seasonal * self.rng.uniform(0.7, 1.3)
            for _ in range(int(direct + self.rng.unit())):
                product = self.product_picker.draw(self.rng)
                self._claim(product, day + timedelta(seconds=self._second_of_day()), None)
            day += timedelta(days=1)

    def generate_reversals(self) -> None:
        for claim in list(self.claims):
            probability = CATEGORIES[claim.product.category]["reversal"]
            if claim.partner == "VEGA":
                probability *= 1.6
            if self.rng.chance(probability):
                self._reversal(claim.data["claim_id"], claim.when + self._reversal_lag())

    # ------------------------------------------------------------ deliberate edge cases

    def inject_collisions(self) -> None:
        """Claim ids shared by different claims (quarantined) and redelivered broken copies
        of a valid claim (the copy is rejected, the original survives)."""
        in_network = [c for c in self.claims if c.data["provider_id"] in self.in_network_ids]
        picks = [in_network[i] for i in self.rng.distinct_indices(len(in_network), 33)]
        group_bases, redelivered = picks[:27], picks[27:]
        for group, base in enumerate(group_bases):
            size = 3 if group < 5 else 2
            base.protected = True
            for member in range(size - 1):
                product = self.product_picker.draw(self.rng)
                shift = timedelta(
                    days=self.rng.integer(-10, 10), seconds=self.rng.integer(0, 86399)
                )
                submitted = min(max(base.when + shift, WINDOW_START), WINDOW_END - timedelta(1))
                twin = self._claim(product, submitted, None)
                twin.data["claim_id"] = base.data["claim_id"]
                if group == 0 and member == 1:
                    twin.data["provider_id"] = self.rng.pick(self.out_of_network)
                elif twin.data["provider_id"] not in self.in_network_ids:
                    twin.data["provider_id"] = self.provider_picker.draw(self.rng).provider_id
                twin.protected = True
            self.injected["claims"][f"collision_group_of_{size}"] += 1

        reversed_ids = {r.data["claim_id"] for r in self.reversals}
        for base in group_bases[:4]:
            if base.data["claim_id"] not in reversed_ids:
                self._reversal(base.data["claim_id"], base.when + self._reversal_lag())
                self.injected["reversals"]["of_ambiguous_claim"] += 1

        for base in redelivered:
            self._broken_copy(base)
            self.injected["claims"]["broken_redelivery"] += 1
        # a broken copy of a colliding id is rejected for its own defect; the id stays ambiguous
        self._broken_copy(group_bases[5])
        self.injected["claims"]["broken_copy_of_ambiguous_id"] += 1

    def _broken_copy(self, base: Record) -> Record:
        """`base` delivered again with a broken quantity: that copy alone is rejected."""
        copy = Record(when=base.when, data=dict(base.data), product=base.product)
        copy.data["units"] = self.rng.pick(WORD_UNITS)
        copy.reasons.append("bad_units")
        base.protected = copy.protected = True
        self.claims.append(copy)
        return copy

    def _exact_copy(self, base: Record, delay_days: int) -> Record:
        """`base` delivered again unchanged, possibly in a later file."""
        copy = Record(
            when=base.when + timedelta(days=delay_days), data=dict(base.data), product=base.product
        )
        base.protected = copy.protected = True
        self.claims.append(copy)
        return copy

    def _corrupt_claim(self, record: Record, defect: str) -> None:
        data, when = record.data, record.when
        if defect == "word_amount":
            data["gross_amount"], reason = self.rng.pick(WORD_AMOUNTS), "bad_gross_amount"
        elif defect == "sub_cent_amount":
            exact = Decimal(repr(data["gross_amount"])) + Decimal("0.004")
            data["gross_amount"], reason = float(exact), "bad_gross_amount"
        elif defect == "zero_amount":
            data["gross_amount"], reason = 0, "nonpositive_gross_amount"
        elif defect == "negative_amount":
            data["gross_amount"], reason = -data["gross_amount"], "nonpositive_gross_amount"
        elif defect == "word_units":
            data["units"], reason = self.rng.pick(WORD_UNITS), "bad_units"
        elif defect == "boolean_units":
            data["units"], reason = True, "bad_units"
        elif defect == "zero_units":
            data["units"], reason = 0.0, "nonpositive_units"
        elif defect == "negative_units":
            data["units"], reason = -abs(data["units"]), "nonpositive_units"
        elif defect == "negative_fee":
            data["service_fee"], reason = -1.25, "negative_service_fee"
        elif defect == "absent_fee":
            del data["service_fee"]
            reason = "missing_service_fee"
        elif defect == "null_product":
            data["product_code"], reason = None, "missing_product_code"
        elif defect == "empty_provider":
            data["provider_id"], reason = "", "missing_provider_id"
        elif defect == "numeric_provider":
            data["provider_id"], reason = int(data["provider_id"][3:]), "bad_provider_id"
        elif defect == "impossible_date":
            data["submitted_at"] = f"{when:%Y}-02-30T{when:%H:%M:%S}"
            reason = "bad_submitted_at"
        elif defect == "us_format_date":
            data["submitted_at"], reason = f"{when:%m/%d/%Y %H:%M}", "bad_submitted_at"
        elif defect == "epoch_date":
            epoch_seconds = int((when - datetime(1970, 1, 1)).total_seconds())
            data["submitted_at"], reason = epoch_seconds, "bad_submitted_at"
        elif defect == "absent_date":
            del data["submitted_at"]
            reason = "missing_submitted_at"
        elif defect == "absent_claim_id":
            del data["claim_id"]
            reason = "missing_claim_id"
        else:
            raise ValueError(defect)
        record.reasons.append(reason)
        record.protected = True

    def inject_claim_defects(self) -> None:
        plan = [(name,) for name, count in CLAIM_DEFECTS.items() for _ in range(count)]
        plan += [combo for combo, count in CLAIM_DEFECT_COMBOS.items() for _ in range(count)]
        pool = [c for c in self.claims if not c.protected]
        for defects, index in zip(
            plan, self.rng.distinct_indices(len(pool), len(plan)), strict=True
        ):
            for defect in defects:
                self._corrupt_claim(pool[index], defect)
            self.injected["claims"]["+".join(defects)] += 1

        # out-of-network claims that also break their contract carry both kinds of reason
        outside = [
            c
            for c in self.claims
            if not c.protected and c.data["provider_id"] in self.out_of_network
        ]
        for index in self.rng.distinct_indices(len(outside), 5):
            self._corrupt_claim(outside[index], "impossible_date")
            self.injected["claims"]["out_of_network+impossible_date"] += 1

        # reversals that point at a rejected claim resolve to a status, never to economics
        reversed_ids = {r.data["claim_id"] for r in self.reversals}
        rejected = [
            c
            for c in self.claims
            if c.reasons and "claim_id" in c.data and c.data["claim_id"] not in reversed_ids
        ]
        for index in self.rng.distinct_indices(len(rejected), 3):
            claim = rejected[index]
            self._reversal(claim.data["claim_id"], claim.when + self._reversal_lag())
            self.injected["reversals"]["of_rejected_claim"] += 1

    def inject_reversal_edge_cases(self) -> None:
        natural = list(self.reversals)
        for index in self.rng.distinct_indices(len(natural), 18):
            first = natural[index]
            later = first.when + timedelta(days=self.rng.integer(1, 10))
            self._reversal(first.data["claim_id"], later)
            self.injected["reversals"]["second_reversal_same_claim"] += 1

        picks = self.rng.distinct_indices(len(self.reversals), 18)
        for original, reuse in zip(picks[0::2], picks[1::2], strict=True):
            self.reversals[reuse].data["reversal_id"] = self.reversals[original].data["reversal_id"]
            self.injected["reversals"]["reused_reversal_id_pair"] += 1

        window_seconds = int((WINDOW_END - WINDOW_START).total_seconds())
        for _ in range(14):
            moment = WINDOW_START + timedelta(seconds=self.rng.integer(0, window_seconds - 1))
            self._reversal(self.rng.uuid(), moment)
            self.injected["reversals"]["orphan_reversal"] += 1

    def inject_redeliveries_and_time_order(self) -> None:
        """Exact redeliveries, broken copies of out-of-network claims, and linked events
        timestamped out of order. All of them are legal or rejected on their own terms."""
        clean = [c for c in self.claims if not c.protected and not c.reasons]
        in_network = [c for c in clean if c.data["provider_id"] in self.in_network_ids]
        for index in self.rng.distinct_indices(len(in_network), 10):
            self._exact_copy(in_network[index], self.rng.integer(0, 20))
            self.injected["claims"]["exact_redelivery"] += 1

        outside = [c for c in clean if c.data["provider_id"] in self.out_of_network]
        picks = [outside[i] for i in self.rng.distinct_indices(len(outside), 3)]
        self._exact_copy(picks[0], 1)
        self.injected["claims"]["exact_redelivery_out_of_network"] += 1
        for base in picks[1:]:
            self._broken_copy(base)
            self.injected["claims"]["broken_copy_out_of_network"] += 1

        # a reversal dated before its claim was submitted: applied, and reported as an observation
        reversed_ids = {r.data.get("claim_id") for r in self.reversals}
        unreversed = [
            c
            for c in self.claims
            if not c.protected
            and not c.reasons
            and c.data["provider_id"] in self.in_network_ids
            and c.data["claim_id"] not in reversed_ids
        ]
        for index in self.rng.distinct_indices(len(unreversed), 3):
            claim = unreversed[index]
            early = claim.when - timedelta(seconds=self.rng.integer(3600, 3 * 86400))
            reversal = self._reversal(claim.data["claim_id"], early)
            claim.protected = reversal.protected = True
            self.injected["reversals"]["dated_before_claim"] += 1

        # a converting lookup timestamped after the claim it produced
        converted = [
            lookup
            for lookup in self.lookups
            if lookup.claim is not None
            and not lookup.protected
            and not lookup.claim.protected
            and not lookup.claim.reasons
            and lookup.claim.data["provider_id"] in self.in_network_ids
        ]
        for index in self.rng.distinct_indices(len(converted), 3):
            lookup = converted[index]
            late = lookup.claim.when + timedelta(seconds=self.rng.integer(60, 1800))
            lookup.data["looked_up_at"] = iso(late)
            lookup.protected = lookup.claim.protected = True
            self.injected["lookups"]["looked_up_after_claim"] += 1

    def inject_lookup_edge_cases(self) -> None:
        unconverted = [lookup for lookup in self.lookups if lookup.data["claim_id"] is None]
        for index in self.rng.distinct_indices(len(unconverted), 110):
            unconverted[index].data["claim_id"] = self.rng.uuid()
            self.injected["lookups"]["orphan_claim_reference"] += 1

        pool = [lookup for lookup in self.lookups if not lookup.protected]
        plan = [name for name, count in LOOKUP_DEFECTS.items() for _ in range(count)]
        for defect, index in zip(
            plan, self.rng.distinct_indices(len(pool), len(plan)), strict=True
        ):
            record, when = pool[index], pool[index].when
            data = record.data
            if defect == "null_partner":
                data["partner_code"], reason = None, "missing_partner_code"
            elif defect == "absent_partner":
                del data["partner_code"]
                reason = "missing_partner_code"
            elif defect == "impossible_time":
                data["looked_up_at"] = f"{when:%Y-%m-%d}T25:{when:%M:%S}"
                reason = "bad_looked_up_at"
            elif defect == "truncated_time":
                data["looked_up_at"], reason = f"{when:%Y-%m-%dT%H:%M}", "bad_looked_up_at"
            elif defect == "empty_time":
                data["looked_up_at"], reason = "", "missing_looked_up_at"
            elif defect == "numeric_claim_id":
                data["claim_id"], reason = self.rng.integer(10000, 99999), "bad_claim_id"
            elif defect == "empty_claim_id":
                data["claim_id"], reason = "", "bad_claim_id"
            elif defect == "absent_claim_id":
                del data["claim_id"]
                reason = "missing_claim_id"
            elif defect == "null_channel":
                data["channel"], reason = None, "missing_channel"
            elif defect == "absent_lookup_id":
                del data["lookup_id"]
                reason = "missing_lookup_id"
            elif defect == "null_product":
                data["product_code"], reason = None, "missing_product_code"
            else:
                raise ValueError(defect)
            record.reasons.append(reason)
            self.injected["lookups"][defect] += 1

    def inject_reversal_defects(self) -> None:
        plan = [name for name, count in REVERSAL_DEFECTS.items() for _ in range(count)]
        pool = [reversal for reversal in self.reversals if not reversal.protected]
        picks = self.rng.distinct_indices(len(pool), len(plan))
        for defect, index in zip(plan, picks, strict=True):
            record = pool[index]
            data, when = record.data, record.when
            if defect == "absent_claim_id":
                del data["claim_id"]
                reason = "missing_claim_id"
            elif defect == "numeric_claim_id":
                data["claim_id"], reason = self.rng.integer(10000, 99999), "bad_claim_id"
            elif defect == "impossible_time":
                data["reversed_at"] = f"{when:%Y}-13-{when:%dT%H:%M:%S}"
                reason = "bad_reversed_at"
            elif defect == "absent_reversal_id":
                del data["reversal_id"]
                reason = "missing_reversal_id"
            else:
                raise ValueError(defect)
            record.reasons.append(reason)
            self.injected["reversals"][defect] += 1

    def run(self) -> None:
        self.generate_traffic()
        self.generate_reversals()
        self.inject_collisions()
        self.inject_claim_defects()
        self.inject_reversal_edge_cases()
        self.inject_redeliveries_and_time_order()
        self.inject_lookup_edge_cases()
        self.inject_reversal_defects()

    # ------------------------------------------------------------ output

    def layout(self) -> dict[str, dict[str, list[Record]]]:
        naming = {
            "claims": lambda r: f"claims-{r.when:%Y-%m}.json",
            "lookups": lambda r: f"lookups-{r.when:%Y-%m}-{'a' if r.when.day <= 15 else 'b'}.json",
            "reversals": lambda r: f"reversals-{r.when:%Y-%m}.json",
        }
        files: dict[str, dict[str, list[Record]]] = {}
        for stream, records in (
            ("claims", self.claims),
            ("lookups", self.lookups),
            ("reversals", self.reversals),
        ):
            by_file: dict[str, list[Record]] = {}
            ordered = sorted(enumerate(records), key=lambda pair: (pair[1].when, pair[0]))
            for _, record in ordered:
                by_file.setdefault(naming[stream](record), []).append(record)
            for file_name, rows in by_file.items():
                for row, record in enumerate(rows, start=1):
                    record.location = f"{file_name}:{row}"
                    record.order = (file_name, row)
            files[stream] = by_file
        return files

    def published_cost_points(self, product: Product) -> list[tuple[date, Decimal]]:
        return [(effective, Decimal(cost) / 100_000) for effective, cost in product.cost_points]

    def publication_rows(self, index: int) -> list[list[str]]:
        rows = []
        for product in self.products:
            for effective, cost in product.cost_points:
                if index not in product.listed_in.get(effective, ()):
                    continue
                wrong, fixed_in = product.restated.get(effective, (cost, 0))
                value = Decimal(wrong if index < fixed_in else cost) / 100_000
                rows.append(
                    [
                        product.code,
                        product.unit,
                        f"{value:.5f}",
                        effective.isoformat(),
                        self.publication_dates[index].isoformat(),
                    ]
                )
        return rows

    def expectations(self) -> dict:
        """What a correct warehouse must contain, from how each record was built."""
        terms = {partner.code: partner for partner in PARTNERS if partner.listed}

        def text(value):
            return value if isinstance(value, str) and value != "" else None

        def typed(data):
            numbers = (Decimal(repr(data[f])) for f in ("gross_amount", "units", "service_fee"))
            return (data["provider_id"], data["product_code"], *numbers, data["submitted_at"])

        # identity: copies that disagree make an id ambiguous; identical later copies are
        # redeliveries of a claim already delivered (earlier in load order)
        valid = sorted((c for c in self.claims if not c.reasons), key=lambda c: c.order)
        versions: dict[str, set] = {}
        for claim in valid:
            versions.setdefault(claim.data["claim_id"], set()).add(typed(claim.data))
        ambiguous = {claim_id for claim_id, seen in versions.items() if len(seen) > 1}
        delivered: set[str] = set()
        redelivered: set[int] = set()  # id() of the later identical copies
        for claim in valid:
            if claim.data["claim_id"] in ambiguous:
                continue
            if claim.data["claim_id"] in delivered:
                redelivered.add(id(claim))
            delivered.add(claim.data["claim_id"])
        rejects: dict[str, dict[str, list[str]]] = {"claims": {}, "lookups": {}, "reversals": {}}
        in_scope: dict[str, Record] = {}
        source_ids: set[str] = set()
        outside_ids: set[str] = set()
        rejected_ids: set[str] = set()
        for claim in self.claims:
            claim_id = text(claim.data.get("claim_id"))
            provider = claim.data.get("provider_id")
            outside = (
                isinstance(provider, str) and provider != "" and provider not in self.in_network_ids
            )
            reasons = list(claim.reasons)
            if outside:
                reasons.append("out_of_network")
            if not claim.reasons and claim_id in ambiguous:
                reasons.append("ambiguous_duplicate_id")
            if id(claim) in redelivered:
                reasons.append("duplicate_redelivery")
            if claim_id:
                source_ids.add(claim_id)
            if claim.reasons and claim_id:
                rejected_ids.add(claim_id)
            elif not claim.reasons and outside:
                outside_ids.add(claim_id)
            if reasons:
                rejects["claims"][claim.location] = sorted(reasons)
            else:
                in_scope[claim_id] = claim

        def status_of(claim_id: str, success: str) -> str:
            if claim_id in in_scope:
                return success
            if claim_id in ambiguous:
                return "claim_ambiguous_id"
            if claim_id in outside_ids:
                return "claim_out_of_network"
            if claim_id in rejected_ids:
                return "claim_rejected"
            return "claim_not_found"

        lookup_status: Counter = Counter()
        lookup_exceptions: dict[str, str] = {}
        conversion: Counter = Counter()
        attribution: dict[str, str] = {}
        for lookup in self.lookups:
            if lookup.reasons:
                rejects["lookups"][lookup.location] = sorted(lookup.reasons)
                continue
            claim_id = lookup.data["claim_id"]
            conversion["valid_lookups"] += 1
            if claim_id is None:
                status = "no_claim"
            else:
                status = status_of(claim_id, "resolved")
                conversion["has_claim_reference"] += 1
                conversion["claim_resolved"] += claim_id in source_ids
                if claim_id in in_scope:
                    conversion["in_scope_conversion"] += 1
                    if claim_id in attribution:
                        raise AssertionError(f"two valid lookups for in-scope claim {claim_id}")
                    attribution[claim_id] = lookup.data["partner_code"]
            lookup_status[status] += 1
            if status not in ("no_claim", "resolved"):
                lookup_exceptions[lookup.location] = status

        reversal_status: Counter = Counter()
        reversal_exceptions: dict[str, str] = {}
        reversed_ids: set[str] = set()
        for reversal in self.reversals:
            if reversal.reasons:
                rejects["reversals"][reversal.location] = sorted(reversal.reasons)
                continue
            status = status_of(reversal.data["claim_id"], "applied")
            reversal_status[status] += 1
            if status == "applied":
                reversed_ids.add(reversal.data["claim_id"])
            else:
                reversal_exceptions[reversal.location] = status

        cost_points = {
            product.code: self.published_cost_points(product)
            for product in self.products
            if product.published
        }
        claims: Counter = Counter()
        money: Counter = Counter()
        reference_status: Counter = Counter()
        for claim_id, claim in in_scope.items():
            data = claim.data
            fee = Decimal(repr(data["service_fee"]))
            partner = attribution.get(claim_id)
            term = terms.get(partner)
            if partner is None:
                attribution_status, payout = "unattributed", None
            elif term is None:
                attribution_status, payout = "attributed_unknown_terms", None
            elif term.flat_payout_cents is not None:
                attribution_status, payout = "attributed", Decimal(term.flat_payout_cents) / 100
            else:
                attribution_status, payout = "attributed", fee * term.revenue_share_pct / 100
            claims[attribution_status] += 1
            claims["zero_service_fee"] += fee == 0
            claims["negative_retained_fee"] += payout is not None and fee - payout < 0

            submitted = datetime.strptime(data["submitted_at"], ISO_FORMAT)
            points = cost_points.get(data["product_code"], [])
            in_effect = [
                cost
                for effective, cost in points
                if datetime.combine(effective, time()) <= submitted
            ]
            if in_effect:
                ref_status, ref_cost = "matched", Decimal(repr(data["units"])) * in_effect[-1]
            else:
                ref_status = "before_first_effective_date" if points else "no_cost_history"
            reference_status[ref_status] += 1

            if claim_id in reversed_ids:
                claims["reversed"] += 1
                continue
            money["net_gross_amount"] += Decimal(repr(data["gross_amount"]))
            money["net_service_fee"] += fee
            if payout is not None:
                money["net_partner_payout"] += payout
                money["net_retained_fee"] += fee - payout
            if payout is None:
                money["unknown_payout_net_service_fee"] += fee
            if ref_status == "matched":
                money["net_reference_cost"] += ref_cost

        counts = {
            "claims": (len(self.claims), len(in_scope)),
            "lookups": (len(self.lookups), conversion["valid_lookups"]),
            "reversals": (len(self.reversals), sum(reversal_status.values())),
        }
        return {
            "seed": self.seed,
            "window": {
                "start": WINDOW_START.date().isoformat(),
                "end_exclusive": WINDOW_END.date().isoformat(),
            },
            "streams": {
                stream: {
                    "source_records": source,
                    "curated_records": curated,
                    "rejected_records": source - curated,
                }
                for stream, (source, curated) in counts.items()
            },
            "rejects": rejects,
            "injected": {stream: dict(sorted(c.items())) for stream, c in self.injected.items()},
            "claims": {
                "in_scope": len(in_scope),
                "reversed": claims["reversed"],
                "net": len(in_scope) - claims["reversed"],
                "attributed": claims["attributed"],
                "attributed_unknown_terms": claims["attributed_unknown_terms"],
                "unattributed": claims["unattributed"],
                "zero_service_fee": claims["zero_service_fee"],
                "negative_retained_fee": claims["negative_retained_fee"],
                "ambiguous_claim_ids": len(ambiguous),
                "reference_cost_status": dict(sorted(reference_status.items())),
            },
            "economics": {name: str(value) for name, value in sorted(money.items())},
            "conversion": dict(sorted(conversion.items())),
            "lookup_status": dict(sorted(lookup_status.items())),
            "lookup_exceptions": lookup_exceptions,
            "reversal_status": dict(sorted(reversal_status.items())),
            "reversal_exceptions": reversal_exceptions,
            "reference_costs": {
                "publications": PUBLICATIONS,
                "change_points": [
                    {
                        "product_code": product.code,
                        "effective_date": effective.isoformat(),
                        "unit_cost": f"{cost:.5f}",
                        "was_restated": effective in product.restated,
                    }
                    for product in self.products
                    if product.published
                    for effective, cost in self.published_cost_points(product)
                ],
            },
        }

    def write(self, out: Path) -> dict:
        files = self.layout()
        for stream, by_file in files.items():
            directory = out / stream
            directory.mkdir(parents=True)
            for file_name in sorted(by_file):
                body = ",\n".join(json.dumps(record.data) for record in by_file[file_name])
                (directory / file_name).write_bytes(f"[\n{body}\n]\n".encode())

        _write_csv(
            out / "providers" / "providers.csv",
            ["provider_id", "network", "region"],
            sorted([p.provider_id, p.network, p.region] for p in self.providers),
        )
        _write_csv(
            out / "partners" / "partners.csv",
            ["partner_code", "partner_name", "flat_payout_cents", "revenue_share_pct"],
            [
                [p.code, p.name, _blank(p.flat_payout_cents), _blank(p.revenue_share_pct)]
                for p in PARTNERS
                if p.listed
            ],
        )
        _write_csv(
            out / "products" / "products.csv",
            ["product_code", "product_name", "category", "unit_of_measure"],
            [[p.code, p.name, p.category, p.unit] for p in self.products],
        )
        for index, published in enumerate(self.publication_dates):
            _write_csv(
                out / "reference_costs" / f"publication-{published.isoformat()}.csv",
                [
                    "product_code",
                    "unit_of_measure",
                    "unit_cost",
                    "effective_date",
                    "published_date",
                ],
                self.publication_rows(index),
            )

        expected = self.expectations()
        (out / EXPECTED_FILE).write_bytes((json.dumps(expected, indent=2) + "\n").encode())
        return expected


def _blank(value) -> str:
    return "" if value is None else str(value)


def _write_csv(path: Path, header: list[str], rows: list[list[str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(header)
    writer.writerows(rows)
    path.write_bytes(buffer.getvalue().encode())


def _prepare_output(out: Path) -> None:
    if out.exists():
        if any(out.iterdir()) and not (out / EXPECTED_FILE).exists():
            raise SystemExit(f"refusing to overwrite {out}: it was not produced by this generator")
        shutil.rmtree(out)
    out.mkdir(parents=True)


def generate(out: Path, seed: int = DEFAULT_SEED) -> dict:
    """Write a complete synthetic source directory and return its expectations."""
    out = Path(out)
    _prepare_output(out)
    world = _World(seed)
    world.run()
    return world.write(out)


def file_hashes(directory: Path) -> dict[str, str]:
    directory = Path(directory)
    return {
        path.relative_to(directory).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    }


def manifest(directory: Path, seed: int) -> dict:
    return {"seed": seed, "files": file_hashes(directory)}


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Generate deterministic synthetic sources.")
    parser.add_argument("--out", type=Path, default=Path("data/generated"))
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument(
        "--check-manifest", type=Path, metavar="FILE", help="fail unless output matches FILE"
    )
    parser.add_argument(
        "--write-manifest", type=Path, metavar="FILE", help="pin the output hashes into FILE"
    )
    args = parser.parse_args(argv)

    expected = generate(args.out, args.seed)
    for stream, counts in expected["streams"].items():
        print(
            f"  {stream:<9} {counts['source_records']:>7,} records "
            f"({counts['rejected_records']:,} deliberately unusable)"
        )
    print(f"synthetic sources written to {args.out} (seed {args.seed})")

    current = manifest(args.out, args.seed)
    if args.write_manifest:
        args.write_manifest.write_text(json.dumps(current, indent=2) + "\n")
        print(f"manifest pinned: {args.write_manifest}")
    if args.check_manifest:
        pinned = json.loads(args.check_manifest.read_text())
        if pinned != current:
            changed = sorted(
                name
                for name in set(pinned["files"]) | set(current["files"])
                if pinned["files"].get(name) != current["files"].get(name)
            )
            sys.exit(
                f"output differs from {args.check_manifest} "
                f"(seed {pinned['seed']} vs {current['seed']}; {len(changed)} files: "
                f"{', '.join(changed[:5])}...). Run `make manifest` if the change is intentional."
            )
        print(f"determinism check passed: output matches {args.check_manifest}")


if __name__ == "__main__":
    main()
