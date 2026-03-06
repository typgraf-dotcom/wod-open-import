"""
Step 7b - Retry géocodage pour les 15 events qui ont échoué.

Améliorations vs step7 :
  1. Validation bbox Europe (41-52°N, -6-11°E) → évite fausses coords
  2. Fallback 1 : extraction du code postal (5 chiffres)
  3. Fallback 2 : suppression des mots-clés fitness du nom de salle
  4. Alias "provence-alpes-cote-d-azur" → id=155

Events ciblés (géocodage échoué ou mauvaises coords) :
  wp_id : 19520, 19522, 19526, 19564, 19530, 19532, 19538,
          19570, 19546, 19578, 19580, 19592, 19556, 19596, 19558
"""

import sys, json, time, re, xmlrpc.client, unicodedata
import requests

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ─────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────
WP_URL      = "https://wod-open.com"
WP_USER     = "typgraf"
WP_APP_PASS = "1Pyz cRXX sttO rKCx wZbB Zde7"
XMLRPC_URL  = f"{WP_URL}/xmlrpc.php"
XMLRPC_AUTH = (WP_USER, WP_APP_PASS)

SCORING_API_DETAIL = (
    "https://scoring-fit-prod-7a29180d25c8.herokuapp.com"
    "/api/event/public-presentation/{eventNumber}"
)

DELAY_WP     = 6
DELAY_NOMIN  = 1.3
DELAY_ERROR  = 20

# Events à retraiter (wp_id → eventNumber, titre)
RETRY_EVENTS = [
    (19520, 2966, "OPEN PARTY by CrossFit Aubière"),
    (19522, 2980, "HYROX CHALLENGE #3"),
    (19526, 2812, "CrossFit Bailleul Battle"),
    (19564, 2840, "Magic Hyrox Challenge"),        # mauvaises coords
    (19530, 2669, "Pertuis Contest"),               # slug PACA manquant
    (19532, 2696, "Tiger Contest 8"),
    (19538, 2921, "FIGHTER CONTEST VOL.7"),
    (19570, 2897, "24 HEURES DE BIKEERG"),
    (19546, 2725, "HYROX RACE by Calade CrossFit"),
    (19578, 2770, "ERGO CHALLENGE"),
    (19580, 2808, "RACE SIMULATION HYROX VILLEFONTAINE #1"),
    (19592, 2778, "GAMES OF THE NORTH 2026 Finale"),
    (19556, 2944, "2GEN WEIGHTLIFTING EXPERIENCE"),
    (19596, 2879, "WoD By Night 2"),
    (19558, 2909, "Sanzaru Hyrox Race"),
]

# ─────────────────────────────────────────────
# Mapping région
# ─────────────────────────────────────────────
FRANCE_REGION_MAP = {
    "auvergne-rhone-alpes":      153,
    "bourgogne-franche-comte":   151,
    "bretagne":                  148,
    "centre-val-de-loire":       150,
    "corse":                     156,
    "grand-est":                 147,
    "hauts-de-france":           144,
    "ile-de-france":             146,
    "la-reunion":                157,
    "martinique":                158,
    "mayotte":                   161,
    "normandie":                 145,
    "nouvelle-aquitaine":        152,
    "occitanie":                 154,
    "pays-de-la-loire":          149,
    # PACA → l'apostrophe dans "Côte d'Azur" génère "d-azur" en slug
    "provence-alpes-cote-dazur":   155,
    "provence-alpes-cote-d-azur":  155,   # alias Nominatim
    "guadeloupe":                160,
    "guyane":                    159,
}

COUNTRY_LOC_MAP = {
    "france": 141, "belgique": 142, "belgium": 142,
    "suisse": 143, "switzerland": 143,
}

# Mots-clés fitness à supprimer pour le fallback géocodage
FITNESS_KEYWORDS = {
    "crossfit", "fitness", "training", "club", "box",
    "athletic", "gym", "wod", "fonctionnal", "functional",
    "salle", "sport", "race", "hyrox", "hybrid", "contest",
    "battle", "moana", "sanzaru", "forge", "craf2s",
}

# Boîte englobante France + Belgique + Suisse (+ un peu de marge)
LAT_MIN, LAT_MAX =  41.0, 52.0
LNG_MIN, LNG_MAX =  -6.0, 11.0


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────
def to_slug(text: str) -> str:
    nfkd = unicodedata.normalize("NFD", text.lower())
    ascii_str = "".join(c for c in nfkd if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z0-9]+", "-", ascii_str).strip("-")


def wp_call(method: str, *args):
    time.sleep(DELAY_WP)
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


def fetch_sf_detail(event_number) -> dict:
    try:
        url  = SCORING_API_DETAIL.format(eventNumber=event_number)
        resp = requests.get(url, timeout=12)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        print(f"    [API] {e}")
        return {}


def _nominatim_query(q: str) -> dict | None:
    """Lance une requête Nominatim et retourne le résultat si dans le bbox."""
    time.sleep(DELAY_NOMIN)
    try:
        resp = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": q, "format": "json", "limit": 1, "addressdetails": 1},
            headers={"User-Agent": "wod-open-import/1.0 (wod-open.com)"},
            timeout=10,
        )
        data = resp.json()
        if not data:
            return None
        r = data[0]
        lat, lng = float(r["lat"]), float(r["lon"])
        if not (LAT_MIN <= lat <= LAT_MAX and LNG_MIN <= lng <= LNG_MAX):
            print(f"    ⚠️  hors bbox ({lat:.2f}, {lng:.2f}) — ignoré")
            return None
        addr = r.get("address", {})
        state_raw = addr.get("state") or addr.get("county") or ""
        return {
            "lat":        str(lat),
            "lng":        str(lng),
            "state_slug": to_slug(state_raw) if state_raw else "",
        }
    except Exception as e:
        print(f"    [Nominatim] {e}")
        return None


def geocode_smart(location: str, country: str) -> dict:
    """
    Géocode avec 3 stratégies de fallback :
    1. Chaîne complète
    2. Code postal 5 chiffres extrait
    3. Suppression des mots-clés fitness
    """
    print(f"    → tentative 1 : {location!r}")
    res = _nominatim_query(f"{location}, {country}")
    if res:
        return res

    # Fallback 2 : code postal
    postal = re.search(r'\b(\d{5})\b', location)
    if postal:
        q = f"{postal.group(1)}, {country}"
        print(f"    → tentative 2 (code postal) : {q!r}")
        res = _nominatim_query(q)
        if res:
            return res

    # Fallback 3 : supprimer mots-clés fitness, garder le reste
    words   = re.split(r"[\s\-&,]+", location)
    cleaned = [w for w in words
               if w.lower() not in FITNESS_KEYWORDS and len(w) > 2]
    if cleaned:
        q = f"{' '.join(cleaned)}, {country}"
        if q != f"{location}, {country}":
            print(f"    → tentative 3 (keywords supprimés) : {q!r}")
            res = _nominatim_query(q)
            if res:
                return res

    return {}


# ─────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────
def main():
    print(f"Retry géocodage pour {len(RETRY_EVENTS)} événements\n")
    stats = {"geocoded": 0, "region_ok": 0, "skipped": 0, "error": 0}

    for i, (wp_id, event_number, title) in enumerate(RETRY_EVENTS, 1):
        short = title[:52]
        print(f"[{i:2}/{len(RETRY_EVENTS)}] {short}")

        # 1. Fetch scoring.fit détail
        detail = fetch_sf_detail(event_number)
        pres   = detail.get("presentation", {})
        location = (pres.get("location") or "").strip()
        country  = (pres.get("country")  or "France").strip()

        if not location:
            print(f"    → pas de location dans l'API, ignoré")
            stats["skipped"] += 1
            continue

        print(f"    location API : {location!r}")

        # 2. Géocodage intelligent
        geo = geocode_smart(location, country)
        if not geo.get("lat"):
            print(f"    ✗ géocodage impossible")
            stats["skipped"] += 1
            continue

        print(f"    📍 {geo['lat']}, {geo['lng']}  ({geo['state_slug']})")
        stats["geocoded"] += 1

        # 3. Terme région
        country_lower   = country.lower()
        country_term_id = COUNTRY_LOC_MAP.get(country_lower, 141)
        region_term_id  = None
        if country_lower == "france" and geo.get("state_slug"):
            region_term_id = FRANCE_REGION_MAP.get(geo["state_slug"])
            if region_term_id:
                print(f"    🗺️  région : {geo['state_slug']} → term_id={region_term_id}")
                stats["region_ok"] += 1
            else:
                print(f"    ⚠️  région inconnue : {geo['state_slug']!r}")

        # 4. Fetch post WP
        try:
            post = wp_call("getPost", wp_id)
        except Exception as e:
            print(f"    [ERR getPost] {e}")
            stats["error"] += 1
            time.sleep(DELAY_ERROR)
            continue

        # Meta IDs existants
        meta_first: dict[str, str] = {}
        for cf in post.get("custom_fields", []):
            k = cf.get("key", "")
            if k and k not in meta_first:
                meta_first[k] = cf.get("id")

        def make_field(key: str, value) -> dict:
            f = {"key": key, "value": value}
            if key in meta_first:
                f["id"] = meta_first[key]
            return f

        custom_fields = [
            make_field("ova_mb_event_map_lat",     geo["lat"]),
            make_field("ova_mb_event_map_lng",     geo["lng"]),
            make_field("ova_mb_event_map_address", f"{location}, {country}"),
        ]

        # Terms
        raw_terms = post.get("terms", {})
        new_terms: dict[str, list[str]] = {}
        if isinstance(raw_terms, dict):
            for tax, tl in raw_terms.items():
                if isinstance(tl, list):
                    new_terms[tax] = [str(t["term_id"]) for t in tl]
        elif isinstance(raw_terms, list):
            for t in raw_terms:
                tax = t.get("taxonomy", "")
                if tax:
                    new_terms.setdefault(tax, []).append(str(t["term_id"]))

        event_loc_ids: set[str] = set(new_terms.get("event_loc", []))
        event_loc_ids.add(str(country_term_id))
        if region_term_id:
            event_loc_ids.add(str(region_term_id))
        new_terms["event_loc"] = list(event_loc_ids)

        # 5. Mise à jour WP
        try:
            wp_call("editPost", wp_id, {
                "custom_fields": custom_fields,
                "terms":         new_terms,
            })
            print(f"    ✓ mis à jour")
        except Exception as e:
            print(f"    [ERR editPost] {e}")
            stats["error"] += 1
            time.sleep(DELAY_ERROR)

    print(f"\n{'='*55}")
    print("Résumé retry géocodage :")
    for k, v in stats.items():
        print(f"  {k:<12}: {v}")


if __name__ == "__main__":
    main()
