"""
Step 5b - Correction extraction des prix.

Problème : extract_price ne regardait que specific_division_price.
  - Si le tableau est vide → pas de fallback sur ticketingDefault_price
  - Si division_price vaut null → idem, pas de fallback

Solution :
  1. specific_division_price : filtre les nulls et les zéros
  2. Fallback : ticketingDefault_price si aucun prix valide trouvé
  3. Prix 0 = gratuit → ignoré (pas affiché)
"""

import sys
import json
import time
import re
import xmlrpc.client
import requests

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

DELAY_BETWEEN_WP_CALLS = 6
DELAY_ON_ERROR         = 20


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
# Helpers
# ---------------------------------------------------------------------------
def make_slug(comp: dict) -> str:
    name = comp.get("name", "event").lower()
    sfid = comp.get("_id", "")[:8]
    slug = re.sub(r"[^a-z0-9]+", "-", name).strip("-")
    return f"{slug}-{sfid}"


def fetch_sf_detail(event_number) -> dict:
    try:
        url  = SCORING_API_DETAIL.format(eventNumber=event_number)
        resp = requests.get(url, timeout=12)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        print(f"    [API] {e}")
        return {}


def extract_price(detail: dict):
    """
    Retourne (min_price_int, max_price_int, price_str) ou (None, None, '').

    Logique :
      1. Collecte les division_price non-null et > 0 dans specific_division_price
      2. Si aucun trouvé, utilise ticketingDefault_price comme fallback (si > 0)
      3. Prix = 0 → gratuit → retourne (None, None, '')
    """
    values = []

    # 1. Prix par division (filtre nulls et zéros)
    for p in (detail.get("specific_division_price") or []):
        raw = p.get("division_price")
        if raw is None:
            continue
        try:
            fv = float(raw)
            if fv > 0:
                values.append(int(fv))
        except (TypeError, ValueError):
            pass

    # 2. Fallback : ticketingDefault_price
    if not values:
        raw_default = detail.get("ticketingDefault_price")
        try:
            fv = float(raw_default or 0)
            if fv > 0:
                values.append(int(fv))
        except (TypeError, ValueError):
            pass

    if not values:
        return None, None, ""

    lo, hi    = min(values), max(values)
    price_str = f"{lo} - {hi} €" if lo != hi else f"{lo} €"
    return lo, hi, price_str


def get_existing_price(post: dict) -> str:
    """Retourne la valeur actuelle de ova_mb_event_ticket_external_link_price."""
    for cf in post.get("custom_fields", []):
        if cf.get("key") == "ova_mb_event_ticket_external_link_price":
            return str(cf.get("value", "")).strip()
    return ""


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    try:
        with open("import_results.json", encoding="utf-8") as f:
            results = json.load(f)
        with open("competitions_raw.json", encoding="utf-8") as f:
            raw = json.load(f)
    except FileNotFoundError as e:
        print(e); return

    comp_by_slug = {make_slug(c): c for c in raw}
    to_process   = [(r["wp_id"], r["slug"], r["title"])
                    for r in results if r["action"] == "created"]

    print(f"Fix prix pour {len(to_process)} événements...\n")

    stats = {"updated": 0, "already_ok": 0, "no_price": 0, "error": 0}

    for i, (wp_id, slug, title) in enumerate(to_process, 1):
        comp         = comp_by_slug.get(slug, {})
        event_number = comp.get("eventNumber")
        short        = title[:50]
        print(f"[{i:2}/{len(to_process)}] {short}")

        # --- Scoring.fit détail ---
        detail = fetch_sf_detail(event_number) if event_number else {}
        min_p, max_p, price_str = extract_price(detail)

        if not price_str:
            print(f"    ↳ pas de prix disponible")
            stats["no_price"] += 1
            continue

        print(f"    prix détecté : {price_str}")

        # --- Récupérer le post WP ---
        try:
            post = wp_call("getPost", wp_id)
        except Exception as e:
            print(f"    [ERR getPost] {e}")
            stats["error"] += 1
            time.sleep(DELAY_ON_ERROR)
            continue

        existing = get_existing_price(post)

        # Vérifier si le prix est déjà correct
        if existing == price_str:
            print(f"    ↳ déjà correct ({existing})")
            stats["already_ok"] += 1
            continue

        if existing:
            print(f"    (ancienne valeur : {existing!r})")

        # Construire les custom_fields (chercher les meta_ids existants)
        meta_by_key: dict[str, str] = {}
        for cf in post.get("custom_fields", []):
            k = cf.get("key", "")
            if k and k not in meta_by_key:
                meta_by_key[k] = cf.get("id", "")

        def make_field(key: str, value) -> dict:
            f = {"key": key, "value": value}
            if key in meta_by_key:
                f["id"] = meta_by_key[key]
            return f

        custom_fields = [
            make_field("ova_mb_event_min_price",                  min_p),
            make_field("ova_mb_event_max_price",                  max_p),
            make_field("ova_mb_event_ticket_external_link_price", price_str),
        ]

        try:
            wp_call("editPost", wp_id, {"custom_fields": custom_fields})
            print(f"    ✓ prix mis à jour : {price_str}")
            stats["updated"] += 1
        except Exception as e:
            print(f"    [ERR editPost] {e}")
            stats["error"] += 1
            time.sleep(DELAY_ON_ERROR)

    print(f"\n{'='*60}")
    print("Résumé fix prix :")
    for k, v in stats.items():
        print(f"  {k:<12}: {v}")


if __name__ == "__main__":
    main()
