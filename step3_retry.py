"""
Step 3 (retry) - Relance uniquement les événements en erreur.
Lit import_results.json, identifie les slugs échoués, et retente leur création.
Délais plus longs pour éviter les 502.
"""

import sys
import io
import xmlrpc.client
import requests
import json
import re
import time
from datetime import datetime

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ---------------------------------------------------------------------------
# Configuration (identique à step3_import.py)
# ---------------------------------------------------------------------------
WP_URL      = "https://wod-open.com"
WP_USER     = "typgraf"
WP_APP_PASS = "1Pyz cRXX sttO rKCx wZbB Zde7"

FILTER_COUNTRIES = {"France", "Belgique", "Belgium", "Suisse", "Switzerland"}
SCORING_FIT_URL  = "https://scoring.fit"
SCORING_API      = (
    "https://scoring-fit-prod-7a29180d25c8.herokuapp.com"
    "/api/leaderboard/competition/search-query"
)

# Délais plus généreux pour le retry
DELAY_BETWEEN_CALLS = 8    # secondes entre chaque appel WP
DELAY_ON_ERROR      = 30   # secondes d'attente en cas d'erreur

XMLRPC_URL  = f"{WP_URL}/xmlrpc.php"
XMLRPC_AUTH = (WP_USER, WP_APP_PASS)

# Taxonomies
LOC_IDS = {
    "France":      141,
    "Belgique":    142,
    "Belgium":     142,
    "Suisse":      143,
    "Switzerland": 143,
    "Réunion":     157,
    "Martinique":  158,
}
LOC_ONLINE_ID    = 279
TYPE_IDS = {
    "Functional Fitness": 239,
    "Hybrid Race":        238,
}
CAT_INDIVIDUEL_ID = 136


# ---------------------------------------------------------------------------
# XML-RPC (même implémentation que step3_import.py)
# ---------------------------------------------------------------------------
def wp_call(method: str, *args):
    time.sleep(DELAY_BETWEEN_CALLS)
    full_method = f"wp.{method}" if not method.startswith("wp.") else method
    wp_params   = ("", WP_USER, WP_APP_PASS) + args
    payload     = xmlrpc.client.dumps(wp_params, methodname=full_method)
    resp = requests.post(
        XMLRPC_URL,
        data=payload.encode("utf-8"),
        headers={"Content-Type": "text/xml; charset=utf-8"},
        auth=XMLRPC_AUTH,
        timeout=30,
        allow_redirects=True,
    )
    resp.raise_for_status()
    result, _ = xmlrpc.client.loads(resp.content)
    return result[0]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def to_timestamp(iso_str: str) -> int:
    if not iso_str:
        return 0
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return int(dt.timestamp())
    except ValueError:
        return 0


def make_slug(comp: dict) -> str:
    name = comp.get("name", "event").lower()
    sfid = comp.get("_id", "")[:8]
    slug = re.sub(r"[^a-z0-9]+", "-", name).strip("-")
    return f"{slug}-{sfid}"


def get_country(comp: dict) -> str:
    ev = comp.get("_event") or {}
    return comp.get("country") or ev.get("country", "")


def get_location(comp: dict) -> str:
    ev = comp.get("_event") or {}
    return comp.get("location") or ev.get("location", "")


def get_category(comp: dict) -> str:
    ev = comp.get("_event") or {}
    return comp.get("category") or ev.get("category", "")


def build_payload(comp: dict) -> dict:
    name     = comp.get("name", "").strip()
    country  = get_country(comp)
    location = get_location(comp)
    category = get_category(comp)
    ctype    = comp.get("type", "")

    start_iso = comp.get("date", {}).get("start", {}).get("iso", "")
    end_iso   = comp.get("date", {}).get("end", {}).get("iso", "")

    address = f"{location}, {country}".strip(", ") if location else country

    event_number = comp.get("eventNumber", "")
    ext_url = f"{SCORING_FIT_URL}/{event_number}" if event_number else ""

    custom_fields = [
        {"key": "ova_mb_event_start_date_str",            "value": to_timestamp(start_iso)},
        {"key": "ova_mb_event_end_date_str",              "value": to_timestamp(end_iso)},
        {"key": "ova_mb_event_address",                   "value": address},
        {"key": "ova_mb_event_map_address",               "value": address},
        {"key": "ova_mb_event_ticket_external_link",      "value": ext_url},
        {"key": "ova_mb_event_time_zone",                 "value": "Europe/Paris"},
        {"key": "ova_mb_event_event_type",                "value": "classic"},
        {"key": "ova_mb_event_info_organizer",            "value": "checked"},
        {"key": "ova_mb_event_allow_cancellation_booking","value": "no"},
        {"key": "ova_mb_event_ticket",                    "value": "a:0:{}"},
        {"key": "scoringfit_id",                          "value": comp.get("_id", "")},
        {"key": "scoringfit_category",                    "value": category},
        {"key": "scoringfit_type",                        "value": ctype},
        {"key": "scoringfit_imported",                    "value": "1"},
    ]

    loc_id  = LOC_IDS.get(country)
    type_id = TYPE_IDS.get(category)

    term_ids = {}
    if loc_id:
        term_ids["event_loc"] = [loc_id]
    if ctype == "online" and LOC_ONLINE_ID not in (term_ids.get("event_loc") or []):
        term_ids.setdefault("event_loc", []).append(LOC_ONLINE_ID)
    if type_id:
        term_ids["type"] = [type_id]
    term_ids["event_cat"] = [CAT_INDIVIDUEL_ID]

    return {
        "post_type":     "event",
        "post_title":    name,
        "post_name":     make_slug(comp),
        "post_status":   "draft",
        "custom_fields": custom_fields,
        "terms":         term_ids,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    # 1. Charger les résultats précédents
    try:
        with open("import_results.json", encoding="utf-8") as f:
            previous = json.load(f)
    except FileNotFoundError:
        print("import_results.json introuvable — lancer step3_import.py d'abord.")
        return

    failed_slugs = {r["slug"] for r in previous if r["action"] == "error"}
    print(f"Retry des {len(failed_slugs)} événements en erreur\n")
    if not failed_slugs:
        print("Aucun événement en erreur — rien à faire.")
        return

    # 2. Récupérer les compétitions scoring.fit
    print("Récupération scoring.fit...")
    resp = requests.get(SCORING_API, params={
        "searchTerm": "", "ticketingPublished": "false",
        "period": "live/future", "pageNumber": 1, "pageSize": 50,
    }, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    all_comps = data if isinstance(data, list) else data.get("data", [])

    # Filtrer par pays ET par slug échoué
    to_retry = []
    for comp in all_comps:
        ev      = comp.get("_event") or {}
        country = comp.get("country") or ev.get("country", "")
        if country not in FILTER_COUNTRIES:
            continue
        slug = make_slug(comp)
        if slug in failed_slugs:
            to_retry.append(comp)

    print(f"{len(to_retry)} événements à retenter (sur {len(failed_slugs)} échoués)\n")

    if len(to_retry) < len(failed_slugs):
        found = {make_slug(c) for c in to_retry}
        missing = failed_slugs - found
        print(f"Attention : {len(missing)} slugs introuvables dans l'API (peut-être supprimés côté scoring.fit) :")
        for s in sorted(missing):
            print(f"  - {s}")
        print()

    # 3. Retry
    print("Traitement :")
    results_map = {r["slug"]: r for r in previous}
    stats = {"created": 0, "error": 0, "skipped_not_found": 0}

    for comp in to_retry:
        slug  = make_slug(comp)
        title = comp.get("name", "?").strip()
        payload = build_payload(comp)

        try:
            wp_id = wp_call("newPost", payload)
            wp_id = int(wp_id)
            print(f"  [OK] {title[:52]:<52} created (id={wp_id})")
            results_map[slug]["action"] = "created"
            results_map[slug]["wp_id"]  = wp_id
            results_map[slug]["error"]  = None
            stats["created"] += 1
        except xmlrpc.client.Fault as e:
            err = f"Fault {e.faultCode}: {e.faultString}"
            print(f"  [ERR] {title[:52]:<52} {err}")
            results_map[slug]["error"] = err
            stats["error"] += 1
            time.sleep(DELAY_ON_ERROR)
        except Exception as e:
            err = str(e)
            print(f"  [ERR] {title[:52]:<52} {err}")
            results_map[slug]["error"] = err
            stats["error"] += 1
            time.sleep(DELAY_ON_ERROR)

    # 4. Résumé
    print(f"\n{'='*60}")
    print("Résumé retry :")
    for k, v in stats.items():
        if v:
            print(f"  {k:<20}: {v}")

    # Sauvegarde résultats mis à jour
    updated = list(results_map.values())
    with open("import_results.json", "w", encoding="utf-8") as f:
        json.dump(updated, f, ensure_ascii=False, indent=2)
    print("\nRapport mis à jour : import_results.json")


if __name__ == "__main__":
    main()
