"""
Step 4 - Mise à jour des événements importés : ajout des champs calendrier.

Ajoute aux 45 événements créés :
  - ova_mb_event_calendar        (dates DD-MM-YYYY + heures, format PHP sérialisé)
  - ova_mb_event_event_days      (timestamps des jours, ex: "1777075200-")
  - ova_mb_event_option_calendar = "manual"
  - ova_mb_event_ticket_link     = "ticket_external_link"

Note : ces champs n'existant pas encore, wp.editPost les crée sans doublon.
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

DELAY_BETWEEN_CALLS = 8
DELAY_ON_ERROR      = 30


# ---------------------------------------------------------------------------
# XML-RPC
# ---------------------------------------------------------------------------
def wp_call(method: str, *args):
    time.sleep(DELAY_BETWEEN_CALLS)
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
# Helpers
# ---------------------------------------------------------------------------
def make_slug(comp: dict) -> str:
    name = comp.get("name", "event").lower()
    sfid = comp.get("_id", "")[:8]
    slug = re.sub(r"[^a-z0-9]+", "-", name).strip("-")
    return f"{slug}-{sfid}"


def php_calendar(cal_id: str, date_start: str, date_end: str,
                 time_start: str, time_end: str) -> str:
    """
    Génère la valeur PHP sérialisée pour ova_mb_event_calendar.

    Format attendu (exemple réel) :
    a:1:{i:0;a:6:{s:11:"calendar_id";s:10:"1771774426";
      s:4:"date";s:10:"25-04-2026";s:8:"end_date";s:10:"25-04-2026";
      s:10:"start_time";s:5:"08:00";s:8:"end_time";s:5:"18:00";
      s:19:"book_before_minutes";s:1:"0";}}
    """
    def s(key: str, val: str) -> str:
        return f's:{len(key)}:"{key}";s:{len(val)}:"{val}";'

    inner = (
        s("calendar_id",        str(cal_id))   +
        s("date",               date_start)    +
        s("end_date",           date_end)      +
        s("start_time",         time_start)    +
        s("end_time",           time_end)      +
        s("book_before_minutes","0")
    )
    return f'a:1:{{i:0;a:6:{{{inner}}}}}'


def compute_event_days(day_start: str, day_end: str) -> str:
    """
    Génère ova_mb_event_event_days : "ts1-ts2-...-"
    Entrée : "DD/MM/YYYY"  (format scoring.fit)
    Chaque timestamp = minuit UTC du jour concerné.
    Plafonné à 30 jours pour les compétitions longues.
    """
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


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    # 1. Charger les résultats d'import
    try:
        with open("import_results.json", encoding="utf-8") as f:
            results = json.load(f)
    except FileNotFoundError:
        print("import_results.json introuvable — lancer step3_import.py d'abord.")
        return

    # 2. Charger les données brutes scoring.fit
    try:
        with open("competitions_raw.json", encoding="utf-8") as f:
            raw = json.load(f)
    except FileNotFoundError:
        print("competitions_raw.json introuvable — lancer step1_fetch_scoringfit.py d'abord.")
        return

    comp_by_slug = {make_slug(c): c for c in raw}

    # 3. Filtrer les événements créés
    to_update = [(r["wp_id"], r["slug"], r["title"])
                 for r in results if r["action"] == "created"]
    print(f"Mise à jour de {len(to_update)} événements (calendrier + ticket_link)...\n")

    stats      = {"updated": 0, "error": 0, "not_found": 0}
    base_cal   = int(time.time())   # timestamp unique pour calendar_id

    for i, (wp_id, slug, title) in enumerate(to_update):
        comp = comp_by_slug.get(slug)
        if not comp:
            print(f"  [??] {title[:55]} — slug introuvable dans competitions_raw.json")
            stats["not_found"] += 1
            continue

        date_s     = comp.get("date", {}).get("start", {})
        date_e     = comp.get("date", {}).get("end",   {})
        day_start  = date_s.get("day",  "")       # "DD/MM/YYYY"
        day_end    = date_e.get("day",  "") or day_start
        time_start = date_s.get("hour", "00:00")  # "HH:MM"
        time_end   = date_e.get("hour", "23:59")

        if not day_start:
            print(f"  [??] {title[:55]} — date introuvable")
            stats["not_found"] += 1
            continue

        cal_start = day_start.replace("/", "-")   # "DD-MM-YYYY"
        cal_end   = day_end.replace("/", "-")

        cal_id     = str(base_cal + i)
        calendar   = php_calendar(cal_id, cal_start, cal_end, time_start, time_end)
        event_days = compute_event_days(day_start, day_end)

        new_fields = [
            {"key": "ova_mb_event_calendar",        "value": calendar},
            {"key": "ova_mb_event_event_days",       "value": event_days},
            {"key": "ova_mb_event_option_calendar",  "value": "manual"},
            {"key": "ova_mb_event_ticket_link",      "value": "ticket_external_link"},
        ]

        try:
            wp_call("editPost", wp_id, {"custom_fields": new_fields})
            print(f"  [OK] {title[:55]:<55} (id={wp_id})")
            stats["updated"] += 1
        except xmlrpc.client.Fault as e:
            print(f"  [ERR] {title[:50]} — Fault {e.faultCode}: {e.faultString}")
            stats["error"] += 1
            time.sleep(DELAY_ON_ERROR)
        except Exception as e:
            print(f"  [ERR] {title[:50]} — {e}")
            stats["error"] += 1
            time.sleep(DELAY_ON_ERROR)

    print(f"\n{'='*60}")
    print("Résumé :")
    for k, v in stats.items():
        if v:
            print(f"  {k:<12}: {v}")


if __name__ == "__main__":
    main()
