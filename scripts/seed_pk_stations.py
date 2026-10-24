"""
Seed Pakistani (Islamabad) fuel stations + per-station prices.

Why this exists
---------------
/prices/compare and /prices/cheapest join PRICES -> STATIONS via price.station_id
and skip any price row whose station_id is falsy (routers/prices.py). The original
seed_test_device.py writes prices/petrol_global with station_id=None, so the
compare screen always came back empty. This seeder writes one price doc PER
station PER fuel type, which is the shape those endpoints actually query.

Usage (from repo root):
    python scripts/seed_pk_stations.py --key "C:/path/to/serviceAccount.json"
    python scripts/seed_pk_stations.py --key ... --force     # overwrite existing
    python scripts/seed_pk_stations.py --key ... --dry-run   # print, write nothing
    python scripts/seed_pk_stations.py --key ... --purge-city Lahore  # remove a city

Safety: refuses to run unless the key's project_id matches --project
(default fuelguard-1f9df) so it can never be pointed at the wrong project.
"""

import argparse
import json
import sys
from datetime import datetime, timezone

import firebase_admin
from firebase_admin import credentials, firestore

DEFAULT_PROJECT = "fuelguard-1f9df"

# Islamabad city centre (Blue Area) — matches LocationService.fallbackLatitude/Longitude.
CITY = "Islamabad"

# fuel_types must match the app's filter values exactly:
# petrol | diesel | premium | cng | lpg  (station_finder_screen.dart)
STATIONS = [
    # (id, name, address, lat, lng, phone, hours, {fuel: price_pkr})
    ("PSO-BLUEAREA",  "PSO Blue Area",             "Jinnah Avenue, Blue Area",           33.7106, 73.0551, "+925122073311", "24 Hours",
     {"petrol": 272.40, "diesel": 279.10, "premium": 312.50}),
    ("SHELL-F7",      "Shell Jinnah Super F-7",    "F-7 Markaz, Jinnah Super",           33.7180, 73.0560, "+925122651120", "24 Hours",
     {"petrol": 274.90, "diesel": 281.75, "premium": 318.20}),
    ("TOTAL-F10",     "TotalEnergies F-10",        "F-10 Markaz, Service Road West",     33.6960, 73.0130, "+925122293344", "24 Hours",
     {"petrol": 271.80, "diesel": 278.40, "premium": 310.90, "lpg": 254.00}),
    ("PSO-G9",        "PSO Karachi Company",       "G-9 Markaz, Karachi Company",        33.6900, 73.0290, "+925122855566", "05:00 - 01:00",
     {"petrol": 270.95, "diesel": 277.60}),
    ("APL-G11",       "Attock G-11 Markaz",        "G-11 Markaz, Service Road",          33.6680, 72.9930, "+925122232277", "24 Hours",
     {"petrol": 269.50, "diesel": 276.20, "cng": 198.00}),
    ("SHELL-I8",      "Shell I-8 Islamabad Filling Station", "Plot 49, Markaz Road, I-8", 33.666937, 73.072568, None, "24 Hours",
     {"petrol": 275.30, "diesel": 282.00, "premium": 321.40}),
    ("PSO-I8", "PSO Marwat Filling Station", "I-8 Markaz", 33.668048, 73.077308, None, None,
     {"petrol": 274.50, "diesel": 281.50}),
    ("ATTOCK-H8", "Attock Quality Filling Station", "Plot 1, Faiz Ahmad Faiz Road, H-8/2", 33.68067879, 73.06060553, None, None,
     {"petrol": 273.80, "diesel": 280.90}),
    ("TOTAL-F8", "Total Parco F-8 Markaz", "F-8 Markaz, near Kachari", 33.7131912, 73.0401127, None, None,
     {"petrol": 275.00, "diesel": 281.80}),
    ("PSO-E11",       "PSO E-11 Markaz",           "E-11/2 Main Boulevard",              33.7000, 72.9700, "+925122228833", "24 Hours",
     {"petrol": 273.10, "diesel": 280.05, "premium": 315.00}),
    ("GO-F11",        "GO F-11 Markaz",            "F-11 Markaz, Service Road",          33.6870, 72.9980, "+925122100209", "06:00 - 00:00",
     {"petrol": 269.90, "diesel": 276.85, "cng": 195.50}),
    ("HASCOL-H8",     "Hascol H-8",                "H-8/1, Kashmir Highway",             33.6650, 73.0730, "+925124440033", "24 Hours",
     {"petrol": 268.75, "diesel": 275.40, "lpg": 249.50}),
    ("TOTAL-FAISAL",  "TotalEnergies Faisal Ave",  "Faisal Avenue, near Zero Point",     33.6930, 73.0630, "+925122827244", "24 Hours",
     {"petrol": 272.00, "diesel": 278.90, "premium": 313.75}),
    ("BYCO-SADDAR",   "Byco Rawalpindi Saddar",    "The Mall, Saddar, Rawalpindi",       33.5950, 73.0480, "+925115870190", "24 Hours",
     {"petrol": 267.90, "diesel": 274.80}),
    ("SHELL-DHA2",    "Shell DHA Phase 2",         "Main GT Road, DHA Phase 2",          33.5400, 73.1600, "+925115412088", "24 Hours",
     {"petrol": 275.80, "diesel": 282.60, "premium": 322.10, "lpg": 258.25}),
    ("PSO-BAHRIA",    "PSO Bahria Town Phase 4",   "Bahria Town Phase 4, Rawalpindi",    33.5200, 73.1000, "+925115730077", "24 Hours",
     {"petrol": 273.60, "diesel": 280.40, "premium": 316.30}),
    ("APL-TARNOL",    "Attock Tarnol",             "GT Road, Tarnol Interchange",        33.6400, 72.8600, "+925122130466", "05:30 - 23:30",
     {"petrol": 270.40, "diesel": 277.15, "cng": 201.00}),
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--key", required=True, help="Path to Firebase service account JSON")
    ap.add_argument("--project", default=DEFAULT_PROJECT, help="Expected project_id guard")
    ap.add_argument("--force", action="store_true", help="Overwrite existing docs")
    ap.add_argument("--dry-run", action="store_true", help="Print planned writes only")
    ap.add_argument("--purge-city", help="Delete all stations in this city (and their prices), then exit")
    ap.add_argument("--station-id", action="append", help="Seed only this station ID; repeat for several stations")
    args = ap.parse_args()

    with open(args.key, encoding="utf-8") as fh:
        key_data = json.load(fh)
    actual = key_data.get("project_id")
    if actual != args.project:
        print(f"ABORT: key belongs to '{actual}' but --project is '{args.project}'.")
        print("Refusing to seed a project you did not name explicitly.")
        return 1
    print(f"Project: {actual}")

    selected = [s for s in STATIONS if not args.station_id or s[0] in args.station_id]
    if args.station_id and set(args.station_id) - {s[0] for s in selected}:
        ap.error('Unknown --station-id')

    if args.dry_run:
        total_prices = sum(len(s[7]) for s in selected)
        print(f"DRY RUN — would write {len(selected)} stations and {total_prices} price docs.")
        for sid, name, addr, lat, lng, _p, _h, prices in selected:
            listed = ", ".join(f"{k} {v:.2f}" for k, v in prices.items())
            print(f"  {sid:<14} {name:<28} ({lat},{lng})  {listed}")
        return 0

    firebase_admin.initialize_app(credentials.Certificate(args.key))
    db = firestore.client()
    now = datetime.now(timezone.utc)

    if args.purge_city:
        victims = db.collection("stations").where("city", "==", args.purge_city).get()
        ids = [d.id for d in victims]
        if not ids:
            print(f"No stations found in '{args.purge_city}'.")
            return 0
        dropped_prices = 0
        for sid in ids:
            for pd in db.collection("prices").where("station_id", "==", sid).get():
                pd.reference.delete()
                dropped_prices += 1
            db.collection("stations").document(sid).delete()
        print(f"Purged {len(ids)} '{args.purge_city}' stations and {dropped_prices} price docs.")
        return 0

    created_s = skipped_s = created_p = skipped_p = 0

    sources = {
        'SHELL-I8': 'https://find.shell.com/pk/fuel/10126336-i-8-islamabad-filling-station/en_US',
        'PSO-I8': 'https://psopk.com/en/ev-stations-locator',
        'ATTOCK-H8': 'https://ogra.org.pk/download/12639',
        'TOTAL-F8': 'https://pk.near-place.com/total-parco-petrol-station-islamabad',
    }
    print('All seeded prices are SAMPLE data, not verified current pump prices.')
    for sid, name, addr, lat, lng, phone, hours, prices in selected:
        sref = db.collection("stations").document(sid)
        if sref.get().exists and not args.force:
            skipped_s += 1
        else:
            sref.set({
                "name": name,
                "address": addr,
                "city": CITY,
                "latitude": lat,
                "longitude": lng,
                "contact_phone": phone,
                "manager_uid": None,
                "fuel_types_available": sorted(prices.keys()),
                "operating_hours": hours,
                "is_active": True,
                "seeded": True,
                "source_url": sources.get(sid),
                "created_at": now,
            })
            created_s += 1

        for fuel, price in prices.items():
            pid = f"{sid}_{fuel}"
            pref = db.collection("prices").document(pid)
            if pref.get().exists and not args.force:
                skipped_p += 1
                continue
            pref.set({
                "id": pid,
                "station_id": sid,          # REQUIRED — compare/cheapest skip null
                "fuel_type": fuel,
                "price_per_litre": price,
                "updated_by": "seed",
                "is_sample": True,
                "effective_from": now,
                "created_at": now,
            })
            created_p += 1
            history_id = f"seed_{pid}"
            history_ref = db.collection('price_history').document(history_id)
            if not history_ref.get().exists:
                history_ref.set({
                    'id': history_id, 'station_id': sid, 'fuel_type': fuel,
                    'price_per_litre': price, 'updated_by': 'sample_seed',
                    'is_sample': True, 'effective_from': now, 'created_at': now,
                })

    print(f"Stations: {created_s} written, {skipped_s} skipped")
    print(f"Prices:   {created_p} written, {skipped_p} skipped")
    if skipped_s or skipped_p:
        print("Re-run with --force to overwrite skipped docs.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
