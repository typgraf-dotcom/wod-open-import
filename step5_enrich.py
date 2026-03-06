"""
Step 5 - Enrichissement des événements importés.

Pour chaque événement créé :
  1. Récupère le détail scoring.fit (description HTML, prix par division)
  2. Récupère le post WP pour obtenir les meta_ids existants
  3. MET À JOUR les champs calendrier en utilisant les IDs existants
     (fix : OVA theme crée a:0:{} par défaut — on écrase le bon enregistrement)
  4. Ajoute description, prix
"""

import sys
import json
import time
import re
import xmlrpc.client
import requests
from datetime import datetime, timezone, timedelta

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
WP_URL      = "https://wod-open.com"
WP_USER     = "typgraf"
WP_APP_PASS = "1Pyz cRXX sttO rKCx wZbB Zde7"
XMLRPC_URL  = f"{WP_URL}/xmlrpc.php"
XMLRPC_AUTH = (WP_USER, WP_APP_PASS)

SCORING_API_DETAIL = (
    "https://scoring-fit-prod-7a29180d25c8.herokuapp.com"
    "/api/event/public-presentation/{eventNumber}"
)

DELAY_BETWEEN_WP_CALLS = 8     # secondes entre chaque appel WordPress
DELAY_ON_ERROR         = 30    # secondes en cas d'erreur


# ---------------------------------------------------------------------------
# XML-RPC
# ---------------------------------------------------------------------------
def wp_call(method: str, *args):
    time.sleep(DELAY_BETWEEN_WP_CALLS)
    full_method = f"wp.{method}" if not method.startswith("wp.") else method
    wp_params   = ("", WP_USER, WP_APP_PASS) + args
    payload     = xmlrpc.client.dumps(wp_params, methodname=full_method)
    resp = requests.post(
        XMLRPC_URL, data=payload.encode("utf-8"),
        headers={"Content-Type": "text/xml; charset=utf-8"},
        auth=XMLRPC_AUTH, timeout=30, allow_redirects=True,
    )
    resp.raise_for_status()
    result, _ = xmlrpc.client.loads(resp.content)
    return result[0]


# ---------------------------------------------------------------------------
# Helpers calendrier (identique à step4)
# ---------------------------------------------------------------------------
def php_calendar(cal_id: str, date_start: str, date_end: str,
                 time_start: str, time_end: str) -> str:
    def s(key: str, val: str) -> str:
        return f's:{len(key)}:"{key}";s:{len(val)}:"{val}";'
    inner = (
        s("calendar_id",         str(cal_id))  +
        s("date",                date_start)   +
        s("end_date",            date_end)     +
        s("start_time",          time_start)   +
        s("end_time",            time_end)     +
        s("book_before_minutes", "0")
    )
    return f'a:1:{{i:0;a:6:{{{inner}}}}}'


def compute_event_days(day_start: str, day_end: str) -> str:
    try:
        s = datetime.strptime(day_start, "%d/%m/%Y").replace(tzinfo=timezone.utc)
        e = datetime.strptime(day_end,   "%d/%m/%Y").replace(tzinfo=timezone.utc)
    except ValueError:
        return ""
    days, cur = [], s
    while cur <= e and len(days) < 30:
        days.append(str(int(cur.timestamp())))
        cur += timedelta(days=1)
    return "-".join(days) + "-"


def make_slug(comp: dict) -> str:
    name = comp.get("name", "event").lower()
    sfid = comp.get("_id", "")[:8]
    slug = re.sub(r"[^a-z0-9]+", "-", name).strip("-")
    return f"{slug}-{sfid}"


# ---------------------------------------------------------------------------
# API scoring.fit détail
# ---------------------------------------------------------------------------
def fetch_sf_detail(event_number: int) -> dict:
    try:
        url  = SCORING_API_DETAIL.format(eventNumber=event_number)
        resp = requests.get(url, timeout=12)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        print(f"    [API] Erreur détail scoring.fit ({event_number}): {e}")
        return {}


def extract_price(detail: dict):
    """Retourne (min_price_int, max_price_int, price_str) ou (None,None,'')."""
    raw_prices = detail.get("specific_division_price", []) or []
    values = []
    for p in raw_prices:
        try:
            v = float(p["division_price"])
            values.append(int(v))
        except (TypeError, ValueError, KeyError):
            pass
    if not values:
        return None, None, ""
    lo, hi = min(values), max(values)
    price_str = f"{lo} - {hi} €" if lo != hi else f"{lo} €"
    return lo, hi, price_str


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    # 1. Charger import_results.json
    try:
        with open("import_results.json", encoding="utf-8") as f:
            results = json.load(f)
    except FileNotFoundError:
        print("import_results.json introuvable."); return

    # 2. Charger competitions_raw.json
    try:
        with open("competitions_raw.json", encoding="utf-8") as f:
            raw = json.load(f)
    except FileNotFoundError:
        print("competitions_raw.json introuvable."); return

    comp_by_slug = {make_slug(c): c for c in raw}
    to_enrich    = [(r["wp_id"], r["slug"], r["title"])
                    for r in results if r["action"] == "created"]

    print(f"Enrichissement de {len(to_enrich)} événements "
          f"(calendrier + description + prix)...\n")

    stats = {"updated": 0, "error": 0, "not_found": 0}

    for i, (wp_id, slug, title) in enumerate(to_enrich, 1):
        comp = comp_by_slug.get(slug)
        if not comp:
            print(f"[{i:2}] ??? {title[:50]} — slug introuvable")
            stats["not_found"] += 1
            continue

        event_number = comp.get("eventNumber")
        short = title[:50]
        print(f"[{i:2}/{len(to_enrich)}] {short}")

        # --- Scoring.fit détail (pas de delay, API externe rapide) ---
        detail      = fetch_sf_detail(event_number) if event_number else {}
        description = (detail.get("presentation") or {}).get("description", "") or ""
        min_p, max_p, price_str = extract_price(detail)

        if description:
            print(f"    desc   : {len(description)} chars")
        if price_str:
            print(f"    prix   : {price_str}")

        # --- Calendrier (depuis competitions_raw) ---
        date_s     = comp.get("date", {}).get("start", {})
        date_e     = comp.get("date", {}).get("end",   {})
        day_start  = date_s.get("day",  "")
        day_end    = date_e.get("day",  "") or day_start
        time_start = date_s.get("hour", "00:00")
        time_end   = date_e.get("hour", "23:59")

        cal_id     = str(int(time.time()) + i)
        cal_value  = php_calendar(cal_id,
                                  day_start.replace("/", "-"),
                                  day_end.replace("/", "-"),
                                  time_start, time_end) if day_start else ""
        days_value = compute_event_days(day_start, day_end)

        # --- Récupérer le post WP pour obtenir les meta IDs existants ---
        try:
            post = wp_call("getPost", wp_id)
        except Exception as e:
            print(f"    [ERR getPost] {e}")
            stats["error"] += 1
            time.sleep(DELAY_ON_ERROR)
            continue

        # Construire un dictionnaire key → premier meta_id (OVA défaut = le plus bas)
        # wp.getPost retourne TOUS les métas (y compris doublons), triés par meta_id
        meta_first: dict[str, str] = {}   # key → meta_id (string)
        for cf in post.get("custom_fields", []):
            k = cf.get("key", "")
            if k and not k.startswith("_") and k not in meta_first:
                meta_first[k] = cf.get("id", "")   # ID = premier enregistrement

        # --- Construire la liste de champs à mettre à jour ---
        custom_fields = []

        def add_field(key: str, value, meta_id=None):
            """Ajoute un champ : mise à jour si meta_id fourni, création sinon."""
            f = {"key": key, "value": value}
            # Utiliser l'ID existant pour écraser le bon enregistrement
            effective_id = meta_id or meta_first.get(key)
            if effective_id:
                f["id"] = effective_id
            custom_fields.append(f)

        # Calendrier — écraser le défaut OVA (a:0:{}) par notre valeur
        if cal_value:
            add_field("ova_mb_event_calendar",       cal_value)
            add_field("ova_mb_event_event_days",      days_value)
            add_field("ova_mb_event_option_calendar", "manual")

        # Prix (si disponible)
        if min_p is not None:
            add_field("ova_mb_event_min_price",                   min_p)
            add_field("ova_mb_event_max_price",                   max_p)
            add_field("ova_mb_event_ticket_external_link_price",  price_str)

        # --- Appel editPost ---
        payload: dict = {"custom_fields": custom_fields}
        if description:
            payload["post_content"] = description

        try:
            wp_call("editPost", wp_id, payload)
            tags = []
            if cal_value:  tags.append("calendrier")
            if description: tags.append("description")
            if price_str:  tags.append(f"prix {price_str}")
            print(f"    ✓ {', '.join(tags) if tags else 'aucun champ'}")
            stats["updated"] += 1
        except xmlrpc.client.Fault as e:
            print(f"    [ERR editPost] Fault {e.faultCode}: {e.faultString}")
            stats["error"] += 1
            time.sleep(DELAY_ON_ERROR)
        except Exception as e:
            print(f"    [ERR editPost] {e}")
            stats["error"] += 1
            time.sleep(DELAY_ON_ERROR)

    print(f"\n{'='*60}")
    print("Résumé final :")
    for k, v in stats.items():
        print(f"  {k:<12}: {v}")


if __name__ == "__main__":
    main()
