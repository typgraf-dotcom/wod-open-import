"""
Step 3 - Import scoring.fit → WordPress via XML-RPC
Crée des événements WP (CPT "event" / thème Meup/OVA) depuis scoring.fit.

Filtres : France, Belgique, Suisse uniquement.
DRY_RUN = True par défaut — passer à False pour import réel.
"""

import sys
import io
import xmlrpc.client
import requests
import json
import re
from datetime import datetime, timezone, timedelta

# Fix encodage Windows (reconfigure sans remplacer stdout pour garder le flush)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
WP_URL      = "https://wod-open.com"
WP_USER     = "typgraf"
WP_APP_PASS = "1Pyz cRXX sttO rKCx wZbB Zde7"

# Pays importés (les autres sont ignorés)
FILTER_COUNTRIES = {"France", "Belgique", "Belgium", "Suisse", "Switzerland"}

# Mode dry-run : True = affiche sans rien créer
DRY_RUN = False

# Comportement si un événement avec ce slug existe déjà
ON_DUPLICATE = "skip"   # "skip" | "update"

# Upload des logos (False = plus rapide, faire un 2e passage ensuite)
UPLOAD_IMAGES = False

# Désactiver le pré-scan WP pour minimiser les requêtes (moins de risque de blocage)
# Les doublons seront détectés lors de la création (newPost retourne une Fault si doublon slug)
SKIP_PRESCAN = True

SCORING_FIT_URL = "https://scoring.fit"

# ---------------------------------------------------------------------------
# Taxonomies — IDs connus (évite des requêtes supplémentaires)
# ---------------------------------------------------------------------------
# event_loc
LOC_IDS = {
    "France":      141,
    "Belgique":    142,
    "Belgium":     142,
    "Suisse":      143,
    "Switzerland": 143,
    "Réunion":     157,
    "Martinique":  158,
}
LOC_ONLINE_ID = 279   # "En ligne"

# type (crossfit / hyrox)
TYPE_IDS = {
    "Functional Fitness": 239,   # "Crossfit"
    "Hybrid Race":        238,   # "Hyrox"
}

# event_cat (individuel par défaut — on n'a pas la taille des équipes)
CAT_INDIVIDUEL_ID = 136

# ---------------------------------------------------------------------------
# XML-RPC
# ---------------------------------------------------------------------------
import time

XMLRPC_URL          = f"{WP_URL}/xmlrpc.php"
XMLRPC_AUTH         = (WP_USER, WP_APP_PASS)   # HTTP Basic Auth
DELAY_BETWEEN_CALLS = 3    # secondes entre chaque appel WP
DELAY_ON_ERROR      = 10   # secondes d'attente supplémentaire en cas d'erreur


def wp_call(method: str, *args):
    """
    Appel XML-RPC via requests + HTTP Basic Auth.
    - HTTP Basic Auth contourne les blocages Wordfence sur xmlrpc.client
    - Les credentials WP sont aussi passés en params méthode (requis par WordPress)
    - Préfixe 'wp.' automatique si absent
    """
    time.sleep(DELAY_BETWEEN_CALLS)
    full_method = f"wp.{method}" if not method.startswith("wp.") else method
    # WordPress attend : blog_id, username, password, [données...]
    wp_params = ("", WP_USER, WP_APP_PASS) + args
    payload = xmlrpc.client.dumps(wp_params, methodname=full_method)
    resp = requests.post(
        XMLRPC_URL,
        data=payload.encode("utf-8"),
        headers={"Content-Type": "text/xml; charset=utf-8"},
        auth=XMLRPC_AUTH,    # HTTP Basic Auth en plus (bypass Wordfence)
        timeout=30,
        allow_redirects=True,
    )
    resp.raise_for_status()
    result, _ = xmlrpc.client.loads(resp.content)
    return result[0]


# ---------------------------------------------------------------------------
# Scoring.fit
# ---------------------------------------------------------------------------
SCORING_API = (
    "https://scoring-fit-prod-7a29180d25c8.herokuapp.com"
    "/api/leaderboard/competition/search-query"
)


def fetch_competitions() -> list:
    resp = requests.get(SCORING_API, params={
        "searchTerm": "",
        "ticketingPublished": "false",
        "period": "live/future",
        "pageNumber": 1,
        "pageSize": 50,
    }, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    return data if isinstance(data, list) else data.get("data", [])


def filter_by_country(competitions: list) -> list:
    result = []
    for comp in competitions:
        ev = comp.get("_event") or {}
        country = comp.get("country") or ev.get("country", "")
        if country in FILTER_COUNTRIES:
            result.append(comp)
    return result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def to_timestamp(iso_str: str) -> int:
    """ISO 8601 → Unix timestamp (secondes)."""
    if not iso_str:
        return 0
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return int(dt.timestamp())
    except ValueError:
        return 0


def php_calendar(cal_id: str, date_start: str, date_end: str,
                 time_start: str, time_end: str) -> str:
    """Génère ova_mb_event_calendar en PHP sérialisé."""
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
    """DD/MM/YYYY → "ts1-ts2-...-" (minuit UTC, plafonné à 30 jours)."""
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
    """Slug WP unique : nom normalisé + 8 premiers chars de l'ID scoring.fit."""
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


# ---------------------------------------------------------------------------
# Upload image → médiathèque WP
# ---------------------------------------------------------------------------
def upload_image(image_url: str, comp_id: str) -> int | None:
    try:
        r = requests.get(image_url, timeout=15)
        r.raise_for_status()
        ext      = image_url.rsplit(".", 1)[-1].split("?")[0]
        filename = f"scoringfit-{comp_id[:8]}.{ext}"
        ct       = r.headers.get("Content-Type", "image/jpeg")
        result   = proxy.wp.uploadFile(*CREDS, {
            "name": filename,
            "type": ct,
            "bits": xmlrpc.client.Binary(r.content),
        })
        mid = int(result.get("id", 0))
        return mid if mid else None
    except Exception as e:
        print(f"      Logo upload échoué: {e}")
        return None


# ---------------------------------------------------------------------------
# Doublons : slugs ET titres déjà en base
# ---------------------------------------------------------------------------
def get_existing_events() -> tuple[set, set]:
    """Retourne (slugs, titres_normalisés) de tous les événements WP existants."""
    import time
    slugs  = set()
    titles = set()
    offset = 0
    batch  = 20   # petits lots pour éviter les timeouts serveur
    while True:
        try:
            posts = wp_call("getPosts", {
                "post_type":   "event",
                "post_status": "any",
                "number":      batch,
                "offset":      offset,
            })
        except Exception as e:
            print(f"   Abandon à offset={offset}: {e}")
            return slugs, titles

        if not posts:
            break
        for p in posts:
            slugs.add(p.get("post_name", ""))
            t = p.get("post_title", "").strip().lower()
            titles.add(t)
        print(f"   ... {offset + len(posts)} événements chargés", end="\r")
        if len(posts) < batch:
            break
        offset += batch
    print()
    return slugs, titles


def normalize_title(t: str) -> str:
    return t.strip().lower()


# ---------------------------------------------------------------------------
# Construction du payload XML-RPC
# ---------------------------------------------------------------------------
def build_payload(comp: dict, media_id: int | None = None) -> dict:
    name     = comp.get("name", "").strip()
    country  = get_country(comp)
    location = get_location(comp)
    category = get_category(comp)
    ctype    = comp.get("type", "")          # "online" | "inside"

    start_iso = comp.get("date", {}).get("start", {}).get("iso", "")
    end_iso   = comp.get("date", {}).get("end", {}).get("iso", "")

    date_s     = comp.get("date", {}).get("start", {})
    date_e     = comp.get("date", {}).get("end",   {})
    day_start  = date_s.get("day",  "")        # "DD/MM/YYYY"
    day_end    = date_e.get("day",  "") or day_start
    time_start = date_s.get("hour", "00:00")   # "HH:MM"
    time_end   = date_e.get("hour", "23:59")

    # Calendrier (format OVA Events)
    cal_id     = str(int(time.time()))
    cal_start  = day_start.replace("/", "-")   # "DD-MM-YYYY"
    cal_end    = day_end.replace("/", "-")
    calendar   = php_calendar(cal_id, cal_start, cal_end, time_start, time_end) if day_start else ""
    event_days = compute_event_days(day_start, day_end) if day_start else ""

    # Adresse : location = nom du lieu, address = lieu + pays
    address      = f"{location}, {country}".strip(", ") if location else country
    map_address  = location if location else country   # nom du lieu seul pour la carte

    event_number = comp.get("eventNumber", "")
    ext_url = f"{SCORING_FIT_URL}/{event_number}" if event_number else ""

    # --- Custom fields meta ---
    custom_fields = [
        {"key": "ova_mb_event_start_date_str",            "value": to_timestamp(start_iso)},
        {"key": "ova_mb_event_end_date_str",              "value": to_timestamp(end_iso)},
        {"key": "ova_mb_event_address",                   "value": address},
        {"key": "ova_mb_event_map_address",               "value": map_address},
        {"key": "ova_mb_event_ticket_external_link",      "value": ext_url},
        {"key": "ova_mb_event_ticket_link",               "value": "ticket_external_link"},
        {"key": "ova_mb_event_time_zone",                 "value": "Europe/Paris"},
        {"key": "ova_mb_event_event_type",                "value": "classic"},
        {"key": "ova_mb_event_info_organizer",            "value": "checked"},
        {"key": "ova_mb_event_allow_cancellation_booking","value": "no"},
        {"key": "ova_mb_event_ticket",                    "value": "a:0:{}"},
        {"key": "ova_mb_event_option_calendar",           "value": "manual"},
        {"key": "ova_mb_event_event_days",                "value": event_days},
        {"key": "ova_mb_event_calendar",                  "value": calendar},
        # Traçabilité
        {"key": "scoringfit_id",                          "value": comp.get("_id", "")},
        {"key": "scoringfit_category",                    "value": category},
        {"key": "scoringfit_type",                        "value": ctype},
        {"key": "scoringfit_imported",                    "value": "1"},
    ]

    # --- Taxonomies (par IDs connus) ---
    loc_id  = LOC_IDS.get(country)
    type_id = TYPE_IDS.get(category)

    term_ids = {}
    if loc_id:
        term_ids["event_loc"] = [loc_id]
    if ctype == "online" and LOC_ONLINE_ID not in (term_ids.get("event_loc") or []):
        term_ids.setdefault("event_loc", []).append(LOC_ONLINE_ID)
    if type_id:
        term_ids["type"] = [type_id]

    # event_cat : individuel par défaut
    # Si toutes les divisions sont "team" → on garde individuel (taille inconnue)
    term_ids["event_cat"] = [CAT_INDIVIDUEL_ID]

    payload = {
        "post_type":     "event",
        "post_title":    name,
        "post_name":     make_slug(comp),
        "post_status":   "draft",   # Vérification manuelle avant publication
        "custom_fields": custom_fields,
        "terms":         term_ids,
    }

    if media_id:
        payload["post_thumbnail"] = media_id

    return payload


# ---------------------------------------------------------------------------
# Import d'une compétition
# ---------------------------------------------------------------------------
def import_one(comp: dict, existing_slugs: set, existing_titles: set) -> dict:
    slug  = make_slug(comp)
    title = comp.get("name", "?").strip()
    res   = {"title": title, "slug": slug, "action": None, "wp_id": None, "error": None}

    # Protection doublon : par slug (import précédent) OU par titre (créé manuellement)
    title_norm = normalize_title(title)
    if ON_DUPLICATE == "skip":
        if slug in existing_slugs:
            res["action"] = "skipped (slug)"
            return res
        if title_norm in existing_titles:
            res["action"] = "skipped (titre existant)"
            return res

    if DRY_RUN:
        res["action"] = "dry_run"
        return res

    # Upload logo (optionnel — désactiver si le serveur est lent)
    media_id = None
    icon_url = comp.get("iconLink", "")
    if UPLOAD_IMAGES and icon_url:
        media_id = upload_image(icon_url, comp.get("_id", ""))

    payload = build_payload(comp, media_id)

    try:
        if (slug in existing_slugs or title_norm in existing_titles) and ON_DUPLICATE == "update":
            # Trouver l'ID WP du post existant
            posts = wp_call("getPosts", {
                "post_type": "event", "post_status": "any",
                "number": 1, "search": slug,
            })
            if posts:
                wp_id = int(posts[0]["post_id"])
                payload["ID"] = wp_id
                wp_call("editPost", wp_id, payload)
                res["action"] = "updated"
                res["wp_id"]  = wp_id
                return res

        wp_id = wp_call("newPost", payload)
        res["action"] = "created"
        res["wp_id"]  = int(wp_id)
        existing_slugs.add(slug)
        existing_titles.add(title_norm)

    except xmlrpc.client.Fault as e:
        res["action"] = "error"
        res["error"]  = f"Fault {e.faultCode}: {e.faultString}"
        time.sleep(DELAY_ON_ERROR)
    except Exception as e:
        res["action"] = "error"
        res["error"]  = str(e)
        time.sleep(DELAY_ON_ERROR)  # pause extra si le serveur est surchargé

    return res


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    mode = "[DRY RUN — aucune modification]" if DRY_RUN else "[MODE RÉEL]"
    print(f"Import scoring.fit → WordPress  {mode}")
    print(f"Pays : {', '.join(sorted(FILTER_COUNTRIES))}")
    print(f"Doublons : {ON_DUPLICATE}\n")

    # 1. Fetch scoring.fit
    print("1. Récupération scoring.fit...")
    all_comps    = fetch_competitions()
    competitions = filter_by_country(all_comps)
    print(f"   {len(all_comps)} total → {len(competitions)} après filtre pays\n")

    # 2. Événements existants (optionnel — désactivable via SKIP_PRESCAN)
    if not DRY_RUN and not SKIP_PRESCAN:
        print("2. Récupération des événements WP existants...")
        existing_slugs, existing_titles = get_existing_events()
        print(f"   {len(existing_slugs)} événements en base (protection doublons slug + titre)\n")
    else:
        existing_slugs, existing_titles = set(), set()
        label = "[DRY RUN]" if DRY_RUN else "[SKIP_PRESCAN=True]"
        print(f"2. {label} pré-scan ignoré — doublons détectés à la création\n")

    # 3. Import
    print("3. Traitement des compétitions :")
    results = []
    stats   = {"created": 0, "updated": 0, "skipped": 0, "dry_run": 0, "error": 0}

    for comp in competitions:
        res    = import_one(comp, existing_slugs, existing_titles)
        action = res["action"]
        stats[action] = stats.get(action, 0) + 1

        ICONS = {"created": "OK", "updated": "MAJ",
                 "skipped (slug)": "--", "skipped (titre existant)": "==",
                 "dry_run": "??", "error": "ERR"}
        mark = ICONS.get(action, "?")
        line = f"  [{mark}] {res['title'][:52]:<52} {action}"
        if res.get("wp_id"):
            line += f" (id={res['wp_id']})"
        if res.get("error"):
            line += f"\n        {res['error']}"
        print(line)
        results.append(res)

    # 4. Résumé
    print(f"\n{'='*60}")
    print("Résumé :")
    for k, v in stats.items():
        if v:
            print(f"  {k:<12}: {v}")

    with open("import_results.json", "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print("\nRapport sauvegardé : import_results.json")

    if DRY_RUN:
        print("\nPour lancer l'import réel : passer DRY_RUN = False")


if __name__ == "__main__":
    main()
