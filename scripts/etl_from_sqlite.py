"""ETL: ivp_local.db (rich SQLite) -> the IVP-AWS-Native contract shapes.

Reads the bundled ``data/ivp_local.db`` and projects its 15-table schema
onto the small contract the platform consumes (see shared/data/schema.sql), while
*fixing* the discrepancies that come from the source being stitched together from
several datasets (Olist e-commerce + a synthetic UK fleet):

  * Dates     - spend rows are 2016-2018 (Olist); remapped to a trailing 12 months.
                card rows have no timestamp; synthesised from ``transaction_hour``.
  * Currency  - spend amounts are BRL (source='olist'); converted to GBP. Fuel
                amounts/prices are already GBP and left untouched.
  * Names     - lower-cased / machine city + category names are canonicalised
                (e.g. "st helens" -> "St Helens", "online_retail" -> "Online Retail").
  * Locations - the contract carries no lat/lon, so the garbage route coordinates
                in the source are simply not loaded. ``city`` strings are cleaned.

Pure stdlib + deterministic: same db + same ref_date -> same output, so the seed
is reproducible and unit-testable offline. Run standalone to print row counts:

    python scripts/etl_from_sqlite.py [path/to/ivp_local.db]
"""
from __future__ import annotations

import datetime as _dt
import os
import sqlite3
from typing import Any

# The seed DB is bundled in this repo (data/ivp_local.db); the repo is fully
# self-contained and never reaches outside its own tree.
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DEFAULT_DB = os.path.join(_REPO_ROOT, "data", "ivp_local.db")

# Olist amounts are Brazilian Real; convert to GBP so the whole app is one currency.
BRL_TO_GBP = 0.16

# Canonical UK city names. Source cities arrive lower-cased and/or as suburbs;
# anything not listed is title-cased as a safe default.
_CITY_CANON = {
    "london": "London", "ldn": "London",
    "manchester": "Manchester",
    "birmingham": "Birmingham", "bir": "Birmingham",
    "leeds": "Leeds",
    "glasgow": "Glasgow", "gla": "Glasgow",
    "bristol": "Bristol", "bri": "Bristol",
    "liverpool": "Liverpool", "liv": "Liverpool", "bootle": "Liverpool", "st helens": "Liverpool",
    "sheffield": "Sheffield", "she": "Sheffield", "barnsley": "Sheffield",
    "nottingham": "Nottingham", "not": "Nottingham",
    "leicester": "Leicester", "lei": "Leicester", "wigston": "Leicester",
    "hinckley": "Leicester", "loughborough": "Leicester",
    "cardiff": "Cardiff", "car": "Cardiff", "barry": "Cardiff",
    "newcastle": "Newcastle", "new": "Newcastle", "gateshead": "Newcastle", "durham": "Newcastle",
    "hamilton": "Glasgow", "paisley": "Glasgow",
    "coventry": "Coventry",
}

# UK county/state -> the city we bucket fuel stations under.
_STATE_TO_CITY = {
    "Greater London": "London",
    "Greater Manchester": "Manchester",
    "West Midlands": "Birmingham",
    "Merseyside": "Liverpool",
    "South Yorkshire": "Sheffield",
    "Tyne and Wear": "Newcastle",
    "Nottinghamshire": "Nottingham",
    "Leicestershire": "Leicester",
    "Bristol": "Bristol",
}

# ISO-ish country codes on card transactions -> a human label for the city field.
_COUNTRY_LABEL = {
    "GB": "United Kingdom", "US": "United States", "CA": "Canada", "CN": "China",
    "RU": "Russia", "UA": "Ukraine", "NG": "Nigeria", "BR": "Brazil",
}


def _clean_city(raw: str | None) -> str:
    if not raw:
        return "Unknown"
    key = raw.strip().lower()
    return _CITY_CANON.get(key, raw.strip().title())


def _humanise(raw: str | None) -> str:
    """`online_retail` / `digital_services` -> `Online Retail` / `Digital Services`."""
    if not raw:
        return "Other"
    return raw.replace("_", " ").strip().title()


def _connect(db_path: str) -> sqlite3.Connection:
    if not os.path.exists(db_path):
        raise FileNotFoundError(f"SQLite DB not found: {db_path}")
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    return con


def _synth_card_id(driver_id: str) -> str:
    return f"CARD-{driver_id}"


def _synth_reg(vehicle_id: str) -> str:
    """Deterministic, plausible-looking UK plate from a vehicle id (e.g. VH0001)."""
    digits = "".join(ch for ch in vehicle_id if ch.isdigit()) or "0"
    n = int(digits)
    a = chr(65 + n % 26)
    b = chr(65 + (n // 26) % 26)
    return f"AB{10 + n % 90} {a}{b}X"


# ── transactions: fuel (timestamped, rich) + card (labelled fraud) ───────────────
def _fuel_transactions(con: sqlite3.Connection) -> list[dict[str, Any]]:
    stations = {r["station_id"]: r for r in con.execute("SELECT * FROM fuel_stations")}
    rows = []
    for r in con.execute("SELECT * FROM fuel_transactions ORDER BY transaction_time"):
        st = stations.get(r["station_id"])
        brand = st["brand"] if st else "Independent"
        city = _STATE_TO_CITY.get(st["state"], _clean_city(st["state"])) if st else "Unknown"
        is_anom = bool(r["is_anomaly"])
        rows.append({
            "txn_id": r["transaction_id"],
            "card_id": _synth_card_id(r["driver_id"]),
            "driver_id": r["driver_id"],
            "merchant": brand,
            "category": "fuel",
            "city": city,
            "amount_gbp": round(float(r["fuel_amount"]), 2),
            "occurred_at": str(r["transaction_time"]),  # already 2026, GBP
            "status": "approved",
            "is_anomaly": is_anom,
        })
    return rows


def _card_transactions(con: sqlite3.Connection, ref_date: _dt.date) -> list[dict[str, Any]]:
    # Map a card's owning user to a driver id where possible (NOT NULL in contract).
    user_driver = {
        r["id"]: r["driver_id"] for r in con.execute("SELECT id, driver_id FROM users")
    }
    rows = []
    for i, r in enumerate(con.execute("SELECT * FROM card_transactions ORDER BY transaction_id")):
        # No timestamp in source: synthesise a recent one, spread over ~90 days,
        # preserving the real transaction_hour. Deterministic via row index.
        day_offset = (i * 7) % 90
        hour = int(r["transaction_hour"] or 12)
        occurred = _dt.datetime.combine(
            ref_date - _dt.timedelta(days=day_offset), _dt.time(hour=hour)
        )
        is_anom = bool(r["label"])
        driver = user_driver.get(r["user_id"]) or r["user_id"]
        rows.append({
            "txn_id": r["transaction_id"],
            "card_id": r["card_id"],
            "driver_id": driver,
            "merchant": _humanise(r["merchant_category"]),
            "category": _humanise(r["merchant_category"]),
            "city": _COUNTRY_LABEL.get(r["merchant_country"], r["merchant_country"] or "Unknown"),
            "amount_gbp": round(float(r["amount"]), 2),
            "occurred_at": occurred.isoformat(sep=" "),
            "status": "blocked" if is_anom else "approved",
            "is_anomaly": is_anom,
        })
    return rows


def build_transactions(con: sqlite3.Connection, ref_date: _dt.date) -> list[dict[str, Any]]:
    return _fuel_transactions(con) + _card_transactions(con, ref_date)


# ── cards: synthesised master keyed by every card_id used in transactions ────────
def build_cards(con: sqlite3.Connection, transactions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    drivers = {r["driver_id"]: r for r in con.execute("SELECT * FROM drivers")}
    veh_by_driver = {
        r["assigned_driver_id"]: r
        for r in con.execute("SELECT * FROM vehicles WHERE assigned_driver_id IS NOT NULL")
    }
    _LIMIT_BY_TYPE = {
        "Tanker": 4000, "Container Truck": 3000, "Trailer": 3000,
        "Rigid Truck": 2000, "Mini Truck": 1200,
    }
    # (card_id, driver_id) pairs actually referenced, first occurrence wins.
    pairs: dict[str, str] = {}
    for t in transactions:
        pairs.setdefault(t["card_id"], t["driver_id"])

    cards = []
    for card_id, driver_id in pairs.items():
        drv = drivers.get(driver_id)
        veh = veh_by_driver.get(driver_id)
        # A card is suspended when its driver is on leave or vehicle is off-road.
        suspended = (drv and drv["driver_status"] != "Active") or (
            veh and veh["vehicle_status"] != "Active"
        )
        limit = _LIMIT_BY_TYPE.get(veh["vehicle_type"], 2000) if veh else 2000
        cards.append({
            "card_id": card_id,
            "driver_id": driver_id,
            "status": "suspended" if suspended else "active",
            "monthly_limit_gbp": float(limit),
            "vehicle_reg": _synth_reg(veh["vehicle_id"]) if veh else None,
        })
    return cards


# ── fuel_prices: one row per (city, station brand) from fuel_stations ─────────────
def build_fuel_prices(con: sqlite3.Connection, ref_date: _dt.date) -> list[dict[str, Any]]:
    seen: dict[tuple[str, str], dict[str, Any]] = {}
    for r in con.execute("SELECT * FROM fuel_stations"):
        city = _STATE_TO_CITY.get(r["state"], _clean_city(r["state"]))
        station = r["brand"]
        key = (city, station)
        price = round(float(r["fuel_price_per_litre"]), 3)
        # Keep the cheapest price per (city, brand) so the cost agent sees real spread.
        if key not in seen or price < seen[key]["price_per_litre_gbp"]:
            seen[key] = {
                "city": city,
                "station": station,
                "price_per_litre_gbp": price,
                "updated_at": ref_date.isoformat(),
            }
    return list(seen.values())


# ── spend_series: monthly GBP totals, remapped onto a trailing 12 months ─────────
def build_spend_series(
    con: sqlite3.Connection, ref_date: _dt.date, months: int = 12
) -> list[dict[str, Any]]:
    totals: dict[str, float] = {}
    for r in con.execute("SELECT purchase_timestamp, amount FROM spend_transactions"):
        ts = r["purchase_timestamp"]
        if not ts or r["amount"] is None:
            continue
        ym = str(ts)[:7]  # YYYY-MM
        totals[ym] = totals.get(ym, 0.0) + float(r["amount"]) * BRL_TO_GBP

    # Drop partial/sparse months at the head & tail of the Olist series (the dataset
    # ramps up and tapers off), so the remapped window is representative rather than
    # showing a near-empty final month. Keep months >= 30% of the median.
    if totals:
        ordered = sorted(totals.values())
        median = ordered[len(ordered) // 2]
        threshold = median * 0.3
        full = [m for m in sorted(totals) if totals[m] >= threshold]
    else:
        full = []

    # Take the most recent `months` full months and remap them onto the trailing
    # `months` calendar months ending at ref_date, preserving chronological order.
    recent = full[-months:]
    target_first = (ref_date.replace(day=1) - _dt.timedelta(days=31 * (len(recent) - 1))).replace(day=1)
    series = []
    cursor = target_first
    for src_month in recent:
        series.append({"month": cursor.isoformat(), "spend_gbp": round(totals[src_month], 2)})
        # advance one calendar month
        cursor = (cursor.replace(day=28) + _dt.timedelta(days=7)).replace(day=1)
    return series


# ── identity: users + auth_credentials (direct, already clean) ───────────────────
def build_users(con: sqlite3.Connection) -> list[dict[str, Any]]:
    return [
        {
            "id": r["id"], "org_id": r["org_id"], "name": r["name"],
            "email": r["email"], "role": r["role"], "department": r["department"],
            "region": r["region"], "manager_id": r["manager_id"], "driver_id": r["driver_id"],
        }
        for r in con.execute("SELECT * FROM users")
    ]


def build_auth_credentials(con: sqlite3.Connection) -> list[dict[str, Any]]:
    return [
        {
            "user_id": r["user_id"], "password_hash": r["password_hash"],
            "is_active": bool(r["is_active"]), "failed_attempts": int(r["failed_attempts"] or 0),
            "updated_at": str(r["updated_at"]) if r["updated_at"] else None,
        }
        for r in con.execute("SELECT * FROM auth_credentials")
    ]


# ── feedback (-> DynamoDB, not RDS) ──────────────────────────────────────────────
def build_feedback(con: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = []
    for r in con.execute("SELECT * FROM feedback"):
        created = r["created_at"]
        rows.append({
            "feedback_id": r["id"],
            "conversation_id": r["conversation_id"],
            "message_id": r["message_id"],
            "rating": r["rating"],
            "comment": r["comment"],
            "extra": r["extra"],
            "created_at": str(created) if created else None,
        })
    return rows


def build_all(db_path: str = DEFAULT_DB, ref_date: _dt.date | None = None) -> dict[str, list[dict[str, Any]]]:
    """Return every contract dataset, with all fixes applied. Deterministic."""
    ref = ref_date or _dt.date.today()
    con = _connect(db_path)
    try:
        transactions = build_transactions(con, ref)
        return {
            "transactions": transactions,
            "cards": build_cards(con, transactions),
            "fuel_prices": build_fuel_prices(con, ref),
            "spend_series": build_spend_series(con, ref),
            "users": build_users(con),
            "auth_credentials": build_auth_credentials(con),
            "feedback": build_feedback(con),
        }
    finally:
        con.close()


if __name__ == "__main__":
    import sys

    path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DB
    data = build_all(path)
    print(f"ETL from {path}\n")
    for name, rows in data.items():
        print(f"  {name:<18} {len(rows):>6} rows")
    print("\nSamples:")
    for name in ("transactions", "cards", "fuel_prices", "spend_series"):
        if data[name]:
            print(f"  {name}: {data[name][0]}")
