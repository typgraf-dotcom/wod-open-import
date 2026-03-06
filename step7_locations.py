"""
Step 7 - Mise à jour complète :
  - ova_mb_event_map_lat / ova_mb_event_map_lng  (Nominatim)
  - ova_mb_event_map_address                     (ville, pays)
  - ova_mb_event_time_zone                       (toujours Europe/Paris)
  - ova_mb_event_calendar                        (heures réelles depuis scoring.fit)
  - event_loc taxonomy                           (pays + région France)
  - Alt text image à la une                      (titre de l'événement)
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
DELAY_NOMIN  = 1.2   # Nominatim exige ≤ 1 req/s
DELAY_ERROR  = 20

# ─────────────────────────────────────────────
# Mapping Nominatim state slug → WP event_loc term ID (child de France=141)
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
    "provence-alpes-cote-dazur": 155,
    "guadeloupe":                160,
    "guyane":                    159,
}

# Pays → term ID top-level dans event_loc
COUNTRY_LOC_MAP = {
    "france":      141,
    "belgique":    142,
    "belgium":     142,
    "suisse":      143,
    "switzerland": 143,
}


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────
def make_slug(comp: dict) -> str:
    name = comp.get("name", "event").lower()
    sfid = comp.get("_id", "")[:8]
    slug = re.sub(r"[^a-z0-9]+", "-", name).strip("-")
    return f"{slug}-{sfid}"


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
    if not event_number:
        return {}
    try:
        url  = SCORING_API_DETAIL.format(eventNumber=event_number)
        resp = requests.get(url, timeout=12)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        print(f"    [API] {e}")
        return {}


def to_slug(text: str) -> str:
    """Nom de région → slug normalisé (supprime accents, minuscule, tirets)."""
    nfkd = unicodedata.normalize("NFD", text.lower())
    ascii_str = "".join(c for c in nfkd if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z0-9]+", "-", ascii_str).strip("-")


def geocode(city: str, country: str) -> dict:
    """
    Géocode via Nominatim OpenStreetMap (gratuit, sans clé).
    Retourne {"lat": str, "lng": str, "state_slug": str} ou {}.
    """
    time.sleep(DELAY_NOMIN)
    try:
        resp = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": f"{city}, {country}", "format": "json",
                    "limit": 1, "addressdetails": 1},
            headers={"User-Agent": "wod-open-import/1.0 (wod-open.com)"},
            timeout=10,
        )
        data = resp.json()
        if not data:
            return {}
        r    = data[0]
        addr = r.get("address", {})
        state_raw = addr.get("state") or addr.get("county") or ""
        return {
            "lat":        r["lat"],
            "lng":        r["lon"],
            "state_slug": to_slug(state_raw) if state_raw else "",
        }
    except Exception as e:
        print(f"    [Nominatim] {e}")
        return {}


def php_calendar(cal_id: str, date_start: str, date_end: str,
                 time_start: str, time_end: str) -> str:
    """
    Construit la valeur PHP sérialisée pour ova_mb_event_calendar.
    Dates au format DD-MM-YYYY, heures HH:MM.
    """
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


def extract_calendar_id(php_str: str) -> str:
    """Extrait le calendar_id existant depuis la valeur PHP sérialisée."""
    m = re.search(r'"calendar_id";s:\d+:"(\d+)"', php_str or "")
    return m.group(1) if m else str(int(time.time()))


def get_thumbnail_id(post: dict) -> int | None:
    """Récupère l'ID de l'image à la une depuis le post XML-RPC."""
    thumb = post.get("post_thumbnail")
    if isinstance(thumb, dict):
        try:
            return int(thumb.get("attachment_id", 0)) or None
        except (TypeError, ValueError):
            pass
    if isinstance(thumb, (int, str)):
        try:
            return int(thumb) or None
        except (TypeError, ValueError):
            pass
    # Fallback : custom_field _thumbnail_id
    for cf in post.get("custom_fields", []):
        if cf.get("key") == "_thumbnail_id":
            try:
                return int(cf["value"]) or None
            except (TypeError, ValueError):
                pass
    return None


# ─────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────
def main():
    try:
        with open("import_results.json", encoding="utf-8") as f:
            results = json.load(f)
        with open("competitions_raw.json", encoding="utf-8") as f:
            raw = json.load(f)
    except FileNotFoundError as e:
        print(e); return

    comp_by_slug = {make_slug(c): c for c in raw}
    events = [(r["wp_id"], r["slug"], r["title"])
              for r in results if r["action"] == "created"]

    print(f"Mise à jour localisation + horaires + alt text : {len(events)} événements\n")

    stats = {
        "geocoded":    0,
        "no_location": 0,
        "region_ok":   0,
        "alttext_ok":  0,
        "error":       0,
    }

    for i, (wp_id, slug, title) in enumerate(events, 1):
        comp         = comp_by_slug.get(slug, {})
        event_number = comp.get("eventNumber")
        is_online    = comp.get("type") == "online"
        short        = title[:52]
        print(f"[{i:2}/{len(events)}] {short}")

        # ── 1. Scoring.fit détail ──────────────────────────
        detail = fetch_sf_detail(event_number) if event_number else {}
        lb     = detail.get("leaderboard", {})
        pres   = detail.get("presentation", {})
        dates  = lb.get("date", {})

        # location et country sont dans presentation (pas leaderboard)
        location    = (pres.get("location") or "").strip()
        country     = (pres.get("country")  or "").strip()
        start_day   = (dates.get("start") or {}).get("day", "")   # DD/MM/YYYY
        end_day     = (dates.get("end")   or {}).get("day", "")
        start_hour  = (dates.get("start") or {}).get("hour", "08:00")
        end_hour    = (dates.get("end")   or {}).get("hour", "18:00")

        # Format calendrier : DD-MM-YYYY
        start_cal = start_day.replace("/", "-") if start_day else ""
        end_cal   = end_day.replace("/", "-")   if end_day   else ""

        # ── 2. Géocodage ───────────────────────────────────
        geo = {}
        if location and country and not is_online:
            geo = geocode(location, country)
            if geo.get("lat"):
                print(f"    📍 {location}, {country} → {geo['lat']}, {geo['lng']}  ({geo['state_slug']})")
                stats["geocoded"] += 1
            else:
                print(f"    ⚠️  géocodage échoué pour : {location}, {country}")
                stats["no_location"] += 1
        else:
            if is_online:
                print(f"    (événement en ligne, pas de géocodage)")
            else:
                print(f"    (pas de localisation disponible)")
            stats["no_location"] += 1

        # ── Terme de région ────────────────────────────────
        country_lower    = country.lower() if country else ""
        country_term_id  = COUNTRY_LOC_MAP.get(country_lower)
        region_term_id   = None
        if country_lower == "france" and geo.get("state_slug"):
            region_term_id = FRANCE_REGION_MAP.get(geo["state_slug"])
            if region_term_id:
                print(f"    🗺️  région : {geo['state_slug']} → term_id={region_term_id}")
                stats["region_ok"] += 1
            else:
                print(f"    ⚠️  région inconnue : {geo['state_slug']!r}")

        # ── 3. Fetch post WP ───────────────────────────────
        try:
            post = wp_call("getPost", wp_id)
        except Exception as e:
            print(f"    [ERR getPost] {e}")
            stats["error"] += 1
            time.sleep(DELAY_ERROR)
            continue

        # Meta IDs existants (premier par clé = prioritaire)
        meta_first: dict[str, str] = {}
        meta_values: dict[str, str] = {}
        for cf in post.get("custom_fields", []):
            k = cf.get("key", "")
            if k and k not in meta_first:
                meta_first[k]  = cf.get("id")
                meta_values[k] = str(cf.get("value", ""))

        def add_field(key: str, value) -> None:
            f = {"key": key, "value": value}
            if key in meta_first:
                f["id"] = meta_first[key]
            custom_fields.append(f)

        custom_fields = []

        # ── Lat / Lng ──────────────────────────────────────
        if geo.get("lat"):
            add_field("ova_mb_event_map_lat", geo["lat"])
            add_field("ova_mb_event_map_lng", geo["lng"])

        # ── Adresse (ville, pays) ──────────────────────────
        if location and country:
            add_field("ova_mb_event_map_address", f"{location}, {country}")

        # ── Timezone ───────────────────────────────────────
        add_field("ova_mb_event_time_zone", "Europe/Paris")

        # ── Calendrier avec heures réelles ─────────────────
        if start_cal and end_cal:
            cal_id    = extract_calendar_id(meta_values.get("ova_mb_event_calendar", ""))
            cal_value = php_calendar(cal_id, start_cal, end_cal, start_hour, end_hour)
            add_field("ova_mb_event_calendar", cal_value)
            print(f"    📅 calendrier : {start_cal} {start_hour} → {end_cal} {end_hour}")

        # ── Taxonomie event_loc : pays + région ────────────
        # wp.getPost peut retourner terms comme dict {tax: [termObj,...]}
        # ou comme liste [{term_id, taxonomy, ...}, ...]
        raw_terms = post.get("terms", {})
        new_terms: dict[str, list[str]] = {}
        if isinstance(raw_terms, dict):
            for tax, term_list in raw_terms.items():
                if isinstance(term_list, list):
                    new_terms[tax] = [str(t["term_id"]) for t in term_list]
        elif isinstance(raw_terms, list):
            for t in raw_terms:
                tax = t.get("taxonomy", "")
                if tax:
                    new_terms.setdefault(tax, []).append(str(t["term_id"]))
        event_loc_ids: set[str] = set(new_terms.get("event_loc", []))
        if country_term_id:
            event_loc_ids.add(str(country_term_id))
        if region_term_id:
            event_loc_ids.add(str(region_term_id))
        new_terms["event_loc"] = list(event_loc_ids)

        # ── 4. Mise à jour post WP ─────────────────────────
        try:
            wp_call("editPost", wp_id, {
                "custom_fields": custom_fields,
                "terms":         new_terms,
            })
            print(f"    ✓ post mis à jour")
        except Exception as e:
            print(f"    [ERR editPost] {e}")
            stats["error"] += 1
            time.sleep(DELAY_ERROR)
            continue

        # ── 5. Alt text image à la une ─────────────────────
        thumbnail_id = get_thumbnail_id(post)
        if thumbnail_id:
            try:
                resp = requests.patch(
                    f"{WP_URL}/wp-json/wp/v2/media/{thumbnail_id}",
                    json={"alt_text": title},
                    auth=XMLRPC_AUTH,
                    timeout=20,
                )
                if resp.status_code in (200, 201):
                    print(f"    🖼️  alt text défini (media {thumbnail_id})")
                    stats["alttext_ok"] += 1
                else:
                    print(f"    ⚠️  alt text HTTP {resp.status_code}")
            except Exception as e:
                print(f"    [ERR alt text] {e}")
        else:
            print(f"    (pas d'image à la une détectée)")

    print(f"\n{'='*60}")
    print("Résumé :")
    for k, v in stats.items():
        print(f"  {k:<14}: {v}")


if __name__ == "__main__":
    main()
