"""
daily_import.py - Import quotidien automatique wod-open.com

Enchaîne en un seul passage :
  1. Fetch scoring.fit API  → competitions_raw.json (mis à jour)
  2. Import WordPress       → uniquement les nouveaux events (anti-doublon)
  3. Pour chaque nouvel event :
       a. Enrichissement    : description, prix, calendrier (vraies heures)
       b. Image à la une   : upload + alt text
       c. Localisation     : géocodage lat/lng, région, adresse

Usage manuel  : python daily_import.py
Planificateur : voir setup_task.bat

Config rapide :
  DRY_RUN     = True   → simulation, aucune écriture WP
  POST_STATUS = "draft" → créer en brouillon (recommandé)
               "publish" → publier directement
"""

import sys, os, json, time, re, io, xmlrpc.client, unicodedata
import hashlib, logging, textwrap, smtplib, ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests
try:
    from PIL import Image
    PIL_OK = True
except ImportError:
    PIL_OK = False

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ═══════════════════════════════════════════════════════════
# ▌ Configuration
# ═══════════════════════════════════════════════════════════
DRY_RUN     = False          # True = simulation sans écriture WP
POST_STATUS = "draft"        # "draft" ou "publish"

WP_URL      = "https://wod-open.com"
WP_USER     = "typgraf"
WP_APP_PASS = os.environ.get("WP_APP_PASS", "1Pyz cRXX sttO rKCx wZbB Zde7")
XMLRPC_URL  = f"{WP_URL}/xmlrpc.php"
XMLRPC_AUTH = (WP_USER, WP_APP_PASS)

SF_SEARCH_URL = (
    "https://scoring-fit-prod-7a29180d25c8.herokuapp.com"
    "/api/leaderboard/competition/search-query"
)
SF_DETAIL_URL = (
    "https://scoring-fit-prod-7a29180d25c8.herokuapp.com"
    "/api/event/public-presentation/{eventNumber}"
)

COUNTRIES_FILTER = {"France", "Belgique", "Belgium", "Suisse", "Switzerland"}

# ── Notifications email ────────────────────────────────────
EMAIL_ENABLED  = True
SMTP_HOST      = "smtp.gmail.com"
SMTP_PORT      = 587
SMTP_USER      = "typgraf@gmail.com"
SMTP_PASSWORD  = os.environ.get("SMTP_PASSWORD", "jupo hnqx xlhn eegt")
EMAIL_FROM     = SMTP_USER
EMAIL_TO       = "typgraf@gmail.com"          # ← modifier si autre destinataire souhaité

RAW_FILE     = Path("competitions_raw.json")
RESULTS_FILE = Path("import_results.json")
LOGS_DIR     = Path("logs")
DELAY_WP     = 5
DELAY_NOMIN  = 1.3

# ── Taxonomies WP ──────────────────────────────────────────
TYPE_TAX = {"crossfit": 239, "hyrox": 238}
LOC_COUNTRY = {"france": 141, "belgique": 142, "belgium": 142,
               "suisse": 143, "switzerland": 143}
LOC_REGION  = {                       # state slug Nominatim → term_id
    "auvergne-rhone-alpes": 153, "bourgogne-franche-comte": 151,
    "bretagne": 148,              "centre-val-de-loire": 150,
    "corse": 156,                 "grand-est": 147,
    "hauts-de-france": 144,       "ile-de-france": 146,
    "la-reunion": 157,            "martinique": 158,
    "mayotte": 161,               "normandie": 145,
    "nouvelle-aquitaine": 152,    "occitanie": 154,
    "pays-de-la-loire": 149,
    "provence-alpes-cote-dazur": 155,
    "provence-alpes-cote-d-azur": 155,   # alias Nominatim
    "guadeloupe": 160,            "guyane": 159,
}
LOC_ONLINE = 279
CAT_MAP = {
    1: 136, 2: 137, 3: 140, 4: 162, 5: 164, 6: 193
}
FITNESS_KW = {
    "crossfit", "fitness", "training", "club", "box", "athletic",
    "gym", "wod", "fonctionnal", "functional", "salle", "sport",
    "race", "hyrox", "hybrid", "contest", "battle",
}
LAT_MIN, LAT_MAX, LNG_MIN, LNG_MAX = 41.0, 52.0, -6.0, 11.0


# ═══════════════════════════════════════════════════════════
# ▌ Logging
# ═══════════════════════════════════════════════════════════
LOGS_DIR.mkdir(exist_ok=True)
log_file = LOGS_DIR / f"daily_{datetime.now():%Y-%m-%d}.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.FileHandler(log_file, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger()


# ═══════════════════════════════════════════════════════════
# ▌ Helpers généraux
# ═══════════════════════════════════════════════════════════
def make_slug(comp: dict) -> str:
    name = comp.get("name", "event").lower()
    sfid = comp.get("_id", "")[:8]
    return re.sub(r"[^a-z0-9]+", "-", name).strip("-") + f"-{sfid}"

def normalize_title(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())

def to_slug(text: str) -> str:
    nfkd = unicodedata.normalize("NFD", text.lower())
    ascii_ = "".join(c for c in nfkd if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z0-9]+", "-", ascii_).strip("-")

def ts_from_dmY(date_str: str) -> int:
    """DD/MM/YYYY → Unix timestamp UTC minuit."""
    try:
        return int(datetime.strptime(date_str, "%d/%m/%Y")
                   .replace(tzinfo=timezone.utc).timestamp())
    except ValueError:
        return 0

def compute_event_days(day_start: str, day_end: str) -> str:
    """DD/MM/YYYY → '1777075200-1777161600-...' (timestamps minuit UTC)."""
    try:
        s = datetime.strptime(day_start, "%d/%m/%Y").replace(tzinfo=timezone.utc)
        e = datetime.strptime(day_end,   "%d/%m/%Y").replace(tzinfo=timezone.utc)
    except ValueError:
        return ""
    days, cur = [], s
    while cur <= e and len(days) < 30:
        days.append(str(int(cur.timestamp())))
        cur += timedelta(days=1)
    return "-".join(days) + "-" if days else ""

def php_calendar(cal_id: str, date_start: str, date_end: str,
                 time_start: str, time_end: str) -> str:
    """Valeur PHP sérialisée pour ova_mb_event_calendar."""
    def s(k: str, v: str) -> str:
        return f's:{len(k)}:"{k}";s:{len(v)}:"{v}";'
    inner = (s("calendar_id", str(cal_id)) + s("date", date_start)
             + s("end_date", date_end) + s("start_time", time_start)
             + s("end_time", time_end) + s("book_before_minutes", "0"))
    return f'a:1:{{i:0;a:6:{{{inner}}}}}'

def extract_cal_id(php_str: str) -> str:
    m = re.search(r'"calendar_id";s:\d+:"(\d+)"', php_str or "")
    return m.group(1) if m else str(int(time.time()))


# ═══════════════════════════════════════════════════════════
# ▌ WordPress XML-RPC
# ═══════════════════════════════════════════════════════════
def wp_call(method: str, *args):
    time.sleep(DELAY_WP)
    full  = f"wp.{method}" if not method.startswith("wp.") else method
    parms = ("", WP_USER, WP_APP_PASS) + args
    body  = xmlrpc.client.dumps(parms, methodname=full)
    r = requests.post(XMLRPC_URL, data=body.encode("utf-8"),
                      headers={"Content-Type": "text/xml; charset=utf-8"},
                      auth=XMLRPC_AUTH, timeout=30, allow_redirects=True)
    r.raise_for_status()
    result, _ = xmlrpc.client.loads(r.content)
    return result[0]

# (pas de prescan WP — trop lent pour un cron quotidien.
#  L'anti-doublon repose sur import_results.json local, suffisant en pratique.)


# ═══════════════════════════════════════════════════════════
# ▌ Scoring.fit API
# ═══════════════════════════════════════════════════════════
def fetch_competitions() -> list[dict]:
    """Récupère toutes les compétitions futures depuis scoring.fit."""
    all_comps: list[dict] = []
    for period in ("future", "live"):
        page = 1
        while True:
            try:
                r = requests.get(SF_SEARCH_URL, params={
                    "searchTerm": "", "ticketingPublished": "false",
                    "period": period, "pageNumber": page, "pageSize": 50,
                }, timeout=15)
                r.raise_for_status()
                data = r.json()
                items = data if isinstance(data, list) else data.get("data", [])
                if not items:
                    break
                all_comps.extend(items)
                if len(items) < 50:
                    break
                page += 1
            except Exception as e:
                log.warning(f"  [SF fetch] period={period} page={page}: {e}")
                break
    # Dédoublonner par _id
    seen, unique = set(), []
    for c in all_comps:
        if c["_id"] not in seen:
            seen.add(c["_id"])
            unique.append(c)
    return unique

def fetch_detail(event_number) -> dict:
    if not event_number:
        return {}
    try:
        r = requests.get(SF_DETAIL_URL.format(eventNumber=event_number), timeout=12)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.warning(f"  [SF detail {event_number}] {e}")
        return {}


# ═══════════════════════════════════════════════════════════
# ▌ Géocodage Nominatim
# ═══════════════════════════════════════════════════════════
def _nomin_query(q: str) -> dict | None:
    time.sleep(DELAY_NOMIN)
    try:
        r = requests.get("https://nominatim.openstreetmap.org/search",
                         params={"q": q, "format": "json", "limit": 1,
                                 "addressdetails": 1},
                         headers={"User-Agent": "wod-open-import/1.0"},
                         timeout=10)
        data = r.json()
        if not data:
            return None
        d = data[0]
        lat, lng = float(d["lat"]), float(d["lon"])
        if not (LAT_MIN <= lat <= LAT_MAX and LNG_MIN <= lng <= LNG_MAX):
            return None
        addr = d.get("address", {})
        state = addr.get("state") or addr.get("county") or ""
        return {"lat": str(lat), "lng": str(lng),
                "state_slug": to_slug(state) if state else ""}
    except Exception:
        return None

def geocode_smart(location: str, country: str) -> dict:
    """Géocode avec 3 stratégies de fallback."""
    res = _nomin_query(f"{location}, {country}")
    if res:
        return res
    # Fallback code postal
    postal = re.search(r'\b(\d{5})\b', location)
    if postal:
        res = _nomin_query(f"{postal.group(1)}, {country}")
        if res:
            return res
    # Fallback suppression mots-clés fitness
    words   = re.split(r"[\s\-&,]+", location)
    cleaned = [w for w in words if w.lower() not in FITNESS_KW and len(w) > 2]
    if cleaned:
        q = f"{' '.join(cleaned)}, {country}"
        res = _nomin_query(q)
        if res:
            return res
    return {}


# ═══════════════════════════════════════════════════════════
# ▌ Construction du post WP
# ═══════════════════════════════════════════════════════════
def build_post(comp: dict, detail: dict, slug: str) -> dict:
    """Construit le payload complet pour wp.newPost."""
    lb   = detail.get("leaderboard", {})
    pres = detail.get("presentation", {})

    title   = comp.get("name", "").strip()
    dates   = lb.get("date", {})
    start_d = (dates.get("start") or {}).get("day", "")
    end_d   = (dates.get("end")   or {}).get("day", "")
    start_h = (dates.get("start") or {}).get("hour", "08:00")
    end_h   = (dates.get("end")   or {}).get("hour", "18:00")

    ts_start = ts_from_dmY(start_d) if start_d else 0
    ts_end   = ts_from_dmY(end_d)   if end_d   else 0
    days_val = compute_event_days(start_d, end_d) if start_d and end_d else ""
    start_cal = start_d.replace("/", "-") if start_d else ""
    end_cal   = end_d.replace("/", "-")   if end_d   else ""

    cal_id   = str(int(time.time()))
    cal_val  = php_calendar(cal_id, start_cal, end_cal, start_h, end_h) if start_cal else "a:0:{}"

    # location/country : en priorité dans presentation (detail API),
    # sinon fallback sur _event (search API)
    ev       = comp.get("_event") or {}
    location = (pres.get("location") or ev.get("location") or "").strip()
    country  = (pres.get("country")  or ev.get("country")  or "").strip()
    country_l = country.lower()
    is_online = comp.get("type") == "online"

    # Adresse
    map_addr = f"{location}, {country}" if location and country else country

    # Description
    description = (pres.get("description") or "").strip()

    # Prix
    min_p, max_p, price_str = _extract_price(detail)

    # URL externe
    event_number = comp.get("eventNumber")
    btn_url = (lb.get("buttonLink") or {}).get("url", "")
    ext_url = f"https://scoring.fit/{btn_url}" if btn_url else (
        f"https://scoring.fit/{event_number}" if event_number else "")

    # Taxonomies
    comp_type = str(comp.get("type") or "").lower()
    comp_cat  = str(comp.get("category") or "").lower()
    type_ids, cat_ids = _detect_types(comp_type, comp_cat)

    loc_terms: set[str] = set()
    country_tid = LOC_COUNTRY.get(country_l)
    if is_online:
        loc_terms.add(str(LOC_ONLINE))
    elif country_tid:
        loc_terms.add(str(country_tid))

    custom_fields = [
        {"key": "ova_mb_event_start_date_str",               "value": ts_start},
        {"key": "ova_mb_event_end_date_str",                 "value": ts_end},
        {"key": "ova_mb_event_address",                      "value": map_addr},
        {"key": "ova_mb_event_map_address",                  "value": map_addr},
        {"key": "ova_mb_event_ticket_external_link",         "value": ext_url},
        {"key": "ova_mb_event_time_zone",                    "value": "Europe/Paris"},
        {"key": "ova_mb_event_event_type",                   "value": "classic"},
        {"key": "ova_mb_event_info_organizer",               "value": "checked"},
        {"key": "ova_mb_event_allow_cancellation_booking",   "value": "no"},
        {"key": "ova_mb_event_ticket",                       "value": "a:0:{}"},
        {"key": "ova_mb_event_ticket_link",                  "value": "ticket_external_link"},
        {"key": "ova_mb_event_option_calendar",              "value": "manual"},
        {"key": "ova_mb_event_calendar",                     "value": cal_val},
        {"key": "ova_mb_event_event_days",                   "value": days_val},
    ]
    if min_p is not None:
        custom_fields += [
            {"key": "ova_mb_event_min_price",                "value": min_p},
            {"key": "ova_mb_event_max_price",                "value": max_p},
            {"key": "ova_mb_event_ticket_external_link_price", "value": price_str},
        ]

    return {
        "post_type":      "event",
        "post_status":    POST_STATUS,
        "post_title":     title,
        "post_name":      slug,
        "post_content":   description,
        "terms": {
            "type":      type_ids,
            "event_cat": cat_ids,
            "event_loc": list(loc_terms),
        },
        "custom_fields": custom_fields,
    }

def _detect_types(comp_type: str, comp_cat: str) -> tuple[list, list]:
    type_ids = []
    cat_ids  = []
    combined = f"{comp_type} {comp_cat}"
    if "hyrox" in combined:
        type_ids.append(str(TYPE_TAX["hyrox"]))
    else:
        type_ids.append(str(TYPE_TAX["crossfit"]))
    # Catégorie : cherche "team" + chiffre, sinon individuel
    m = re.search(r'team[- _]?(\d)', combined)
    if m:
        n = int(m.group(1))
        cat_ids.append(str(CAT_MAP.get(n, 136)))
    else:
        cat_ids.append(str(CAT_MAP[1]))
    return type_ids, cat_ids

def _extract_price(detail: dict):
    values = []
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
    if not values:
        try:
            fv = float(detail.get("ticketingDefault_price") or 0)
            if fv > 0:
                values.append(int(fv))
        except (TypeError, ValueError):
            pass
    if not values:
        return None, None, ""
    lo, hi = min(values), max(values)
    return lo, hi, f"{lo} - {hi} €" if lo != hi else f"{lo} €"


# ═══════════════════════════════════════════════════════════
# ▌ Upload image
# ═══════════════════════════════════════════════════════════
def upload_image(image_url: str, slug: str, title: str) -> int | None:
    """Télécharge, convertit en PNG, upload sur WP, retourne l'attachment_id."""
    if not image_url:
        return None
    try:
        r = requests.get(image_url, timeout=30)
        r.raise_for_status()
        raw = r.content
    except Exception as e:
        log.warning(f"    [IMG download] {e}")
        return None

    # Conversion PNG via Pillow (contourne les filtres MIME de WP)
    filename = f"{slug[:40]}.png"
    if PIL_OK:
        try:
            img = Image.open(io.BytesIO(raw))
            if img.mode in ("CMYK",):
                img = img.convert("RGB")
            elif img.mode in ("P", "LA"):
                img = img.convert("RGBA")
            buf = io.BytesIO()
            img.save(buf, format="PNG", optimize=True)
            image_data = buf.getvalue()
            mime = "image/png"
        except Exception:
            image_data = raw
            mime = "image/jpeg"
    else:
        image_data = raw
        mime = "image/jpeg"

    if DRY_RUN:
        log.info(f"    [DRY] upload image {filename}")
        return None

    try:
        result = wp_call("uploadFile", {
            "name": filename, "type": mime,
            "bits": xmlrpc.client.Binary(image_data),
            "overwrite": False,
        })
        return int(result.get("id", 0)) or None
    except Exception as e:
        log.warning(f"    [IMG upload] {e}")
        return None


# ═══════════════════════════════════════════════════════════
# ▌ Post-enrichissement (après création du post)
# ═══════════════════════════════════════════════════════════
def enrich_post(wp_id: int, detail: dict, title: str,
                image_url: str, slug: str) -> None:
    """
    Enrichit un post WP après création :
    - Corrige les meta OVA-defaults (calendar, event_days)
    - Géocode et assigne la région
    - Upload l'image à la une + alt text
    """
    lb   = detail.get("leaderboard", {})
    pres = detail.get("presentation", {})

    location = (pres.get("location") or "").strip()
    country  = (pres.get("country")  or "France").strip()
    country_l = country.lower()
    is_online = lb.get("type") == "online"

    dates  = lb.get("date", {})
    start_d = (dates.get("start") or {}).get("day", "")
    end_d   = (dates.get("end")   or {}).get("day", "")
    start_h = (dates.get("start") or {}).get("hour", "08:00")
    end_h   = (dates.get("end")   or {}).get("hour", "18:00")
    start_cal = start_d.replace("/", "-") if start_d else ""
    end_cal   = end_d.replace("/", "-")   if end_d   else ""
    days_val  = compute_event_days(start_d, end_d) if start_d and end_d else ""

    # ── Récupère meta IDs (OVA crée des defaults à la création) ──
    try:
        post = wp_call("getPost", wp_id)
    except Exception as e:
        log.warning(f"    [getPost] {e}")
        return

    meta_first: dict[str, str] = {}
    meta_values: dict[str, str] = {}
    for cf in post.get("custom_fields", []):
        k = cf.get("key", "")
        if k and k not in meta_first:
            meta_first[k]  = cf.get("id")
            meta_values[k] = str(cf.get("value", ""))

    def add_field(key: str, value) -> dict:
        f = {"key": key, "value": value}
        if key in meta_first:
            f["id"] = meta_first[key]
        return f

    custom_fields = []

    # ── Reconstruire le calendrier avec les bons meta IDs ─────
    if start_cal and end_cal:
        cal_id  = extract_cal_id(meta_values.get("ova_mb_event_calendar", ""))
        cal_val = php_calendar(cal_id, start_cal, end_cal, start_h, end_h)
        custom_fields.append(add_field("ova_mb_event_calendar",      cal_val))
        custom_fields.append(add_field("ova_mb_event_event_days",    days_val))
        custom_fields.append(add_field("ova_mb_event_option_calendar", "manual"))
        custom_fields.append(add_field("ova_mb_event_ticket_link",   "ticket_external_link"))
        custom_fields.append(add_field("ova_mb_event_time_zone",     "Europe/Paris"))

    # ── Géocodage ─────────────────────────────────────────────
    geo = {}
    if location and not is_online:
        geo = geocode_smart(location, country)
        if geo.get("lat"):
            log.info(f"    📍 {location}, {country} → {geo['lat']}, {geo['lng']}")
            custom_fields.append(add_field("ova_mb_event_map_lat", geo["lat"]))
            custom_fields.append(add_field("ova_mb_event_map_lng", geo["lng"]))
            custom_fields.append(add_field("ova_mb_event_map_address", f"{location}, {country}"))

    # ── Terme région ──────────────────────────────────────────
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
    country_tid = LOC_COUNTRY.get(country_l)
    if country_tid:
        event_loc_ids.add(str(country_tid))
    if country_l == "france" and geo.get("state_slug"):
        region_tid = LOC_REGION.get(geo["state_slug"])
        if region_tid:
            event_loc_ids.add(str(region_tid))
            log.info(f"    🗺️  région : {geo['state_slug']} → {region_tid}")
    new_terms["event_loc"] = list(event_loc_ids)

    # ── Mise à jour post ──────────────────────────────────────
    if custom_fields or new_terms:
        if not DRY_RUN:
            try:
                wp_call("editPost", wp_id, {
                    "custom_fields": custom_fields,
                    "terms":         new_terms,
                })
            except Exception as e:
                log.warning(f"    [editPost enrich] {e}")

    # ── Image à la une ────────────────────────────────────────
    media_id = upload_image(image_url, slug, title)
    if media_id:
        if not DRY_RUN:
            try:
                wp_call("editPost", wp_id, {"post_thumbnail": media_id})
                # Alt text via REST
                requests.patch(
                    f"{WP_URL}/wp-json/wp/v2/media/{media_id}",
                    json={"alt_text": title}, auth=XMLRPC_AUTH, timeout=20,
                )
                log.info(f"    🖼️  image {media_id} / alt text OK")
            except Exception as e:
                log.warning(f"    [image post] {e}")


# ═══════════════════════════════════════════════════════════
# ▌ Notification email
# ═══════════════════════════════════════════════════════════
def send_summary_email(stats: dict, new_results: list, elapsed: float,
                       warnings: list) -> None:
    """Envoie un email récapitulatif après chaque run."""
    if not EMAIL_ENABLED:
        return
    try:
        date_str = datetime.now().strftime("%d/%m/%Y %H:%M")
        subject  = (
            f"[wod-open] Import du {date_str} — "
            f"{stats['created']} créés / {stats['error']} erreurs"
        )

        # ── Corps HTML ──────────────────────────────────────
        rows_created = ""
        for r in new_results:
            if r.get("action") == "created" and r.get("wp_id"):
                admin_url = f"{WP_URL}/wp-admin/post.php?post={r['wp_id']}&action=edit"
                rows_created += (
                    f"<tr><td style='padding:4px 8px'>{r['title']}</td>"
                    f"<td style='padding:4px 8px'>"
                    f"<a href='{admin_url}'>wp_id {r['wp_id']}</a></td></tr>"
                )

        warn_html = ""
        if warnings:
            items = "".join(f"<li>{w}</li>" for w in warnings[-20:])
            warn_html = f"""
            <h3 style='color:#e67e22'>⚠️ Avertissements ({len(warnings)})</h3>
            <ul style='font-size:13px;color:#555'>{items}</ul>"""

        status_color = "#27ae60" if stats["error"] == 0 else "#e74c3c"
        html = f"""
        <html><body style='font-family:Arial,sans-serif;color:#333;max-width:700px'>
        <h2 style='border-bottom:2px solid {status_color};padding-bottom:8px'>
            Import quotidien wod-open.com — {date_str}
        </h2>
        <table style='border-collapse:collapse;margin-bottom:16px'>
            <tr><td style='padding:4px 16px 4px 0'><b>✅ Events créés</b></td>
                <td style='color:{status_color};font-size:18px'><b>{stats['created']}</b></td></tr>
            <tr><td style='padding:4px 16px 4px 0'>⏭️ Ignorés (doublons)</td>
                <td><b>{stats['skipped']}</b></td></tr>
            <tr><td style='padding:4px 16px 4px 0'>❌ Erreurs</td>
                <td style='color:{"#e74c3c" if stats["error"] else "#27ae60"}'><b>{stats['error']}</b></td></tr>
            <tr><td style='padding:4px 16px 4px 0'>⏱️ Durée</td>
                <td>{elapsed/60:.1f} min</td></tr>
        </table>
        {"<h3>Nouveaux events créés</h3><table border='1' cellspacing='0' style='border-collapse:collapse;font-size:13px'>" + rows_created + "</table>" if rows_created else "<p><i>Aucun nouvel event aujourd'hui.</i></p>"}
        {warn_html}
        <p style='font-size:11px;color:#999;margin-top:24px'>
            Log complet : {log_file}<br>
            wod-open.com — import automatique scoring.fit
        </p>
        </body></html>"""

        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = EMAIL_FROM
        msg["To"]      = EMAIL_TO
        msg.attach(MIMEText(html, "html", "utf-8"))

        context = ssl.create_default_context()
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.ehlo()
            server.starttls(context=context)
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.sendmail(EMAIL_FROM, EMAIL_TO, msg.as_string())

        log.info(f"📧 Email envoyé → {EMAIL_TO}")
    except Exception as e:
        log.warning(f"  [email] Échec envoi : {e}")


# ═══════════════════════════════════════════════════════════
# ▌ Main
# ═══════════════════════════════════════════════════════════
def main():
    run_start = time.time()
    log.info("=" * 60)
    log.info(f"▶ Import quotidien wod-open.com — {datetime.now():%d/%m/%Y %H:%M}")
    log.info(f"  DRY_RUN={DRY_RUN}  POST_STATUS={POST_STATUS}")
    log.info("=" * 60)

    # ── 1. Fetch scoring.fit ───────────────────────────────
    log.info("\n[1] Fetch scoring.fit...")
    competitions = fetch_competitions()
    log.info(f"    {len(competitions)} compétitions récupérées")

    # Filtrer par pays
    # Le pays est dans _event.country (jamais à la racine dans l'API search)
    def get_country(c: dict) -> str:
        return (c.get("country") or (c.get("_event") or {}).get("country") or "")

    filtered = [
        c for c in competitions
        if get_country(c) in COUNTRIES_FILTER
        or str(c.get("type") or "") == "online"
    ]
    log.info(f"    → {len(filtered)} après filtre pays (FR/BE/CH + online)")

    # Sauvegarder
    RAW_FILE.write_text(json.dumps(competitions, ensure_ascii=False, indent=2),
                        encoding="utf-8")

    # ── 2. Charger résultats précédents ────────────────────
    existing_results: list[dict] = []
    if RESULTS_FILE.exists():
        existing_results = json.loads(RESULTS_FILE.read_text(encoding="utf-8"))
    existing_slugs  = {r["slug"] for r in existing_results
                       if r["action"] in ("created", "existing")}
    existing_titles = {normalize_title(r["title"])
                       for r in existing_results
                       if r["action"] in ("created", "existing")}

    wp_slugs, wp_titles = set(), set()   # anti-doublon via import_results.json uniquement

    # ── 3. Import ──────────────────────────────────────────
    log.info("\n[3] Import des nouveaux events...")
    new_results: list[dict] = []
    warnings:    list[str]  = []
    stats = {"created": 0, "skipped": 0, "error": 0}

    for comp in filtered:
        slug  = make_slug(comp)
        title = comp.get("name", "").strip()
        norm  = normalize_title(title)

        # Anti-doublon
        if slug in existing_slugs:
            log.info(f"  [SKIP doublon slug] {title[:50]}")
            stats["skipped"] += 1
            continue
        if norm in existing_titles:
            log.info(f"  [SKIP doublon titre] {title[:50]}")
            stats["skipped"] += 1
            continue

        event_number = comp.get("eventNumber")
        log.info(f"  [NEW] {title[:55]}  (eventNumber={event_number})")

        # Récupérer les détails
        detail    = fetch_detail(event_number)
        pres      = detail.get("presentation", {})
        image_url = (detail.get("leaderboard", {}).get("iconLink")
                     or pres.get("iconLink") or "")

        # Construire le post
        payload = build_post(comp, detail, slug)

        if DRY_RUN:
            log.info(f"    [DRY] newPost → {title}")
            new_results.append({
                "wp_id": 0, "slug": slug, "title": title,
                "action": "dry_run", "event_number": event_number,
            })
            stats["created"] += 1
            continue

        # Créer dans WP
        try:
            wp_id = int(wp_call("newPost", payload))
            log.info(f"    ✓ créé wp_id={wp_id}")
            new_results.append({
                "wp_id": wp_id, "slug": slug, "title": title,
                "action": "created", "event_number": event_number,
            })
            stats["created"] += 1
        except Exception as e:
            log.error(f"    [ERR newPost] {e}")
            warnings.append(f"[ERR newPost] {title[:60]} — {e}")
            stats["error"] += 1
            continue

        # ── 4. Enrichissement immédiat ─────────────────────
        log.info(f"    → enrichissement...")
        enrich_post(wp_id, detail, title, image_url, slug)

    # ── 5. Sauvegarder résultats cumulés ──────────────────
    all_results = existing_results + new_results
    RESULTS_FILE.write_text(json.dumps(all_results, ensure_ascii=False, indent=2),
                            encoding="utf-8")

    elapsed = time.time() - run_start
    log.info(f"\n{'='*60}")
    log.info(f"✅ Terminé en {elapsed:.0f}s")
    log.info(f"   créés   : {stats['created']}")
    log.info(f"   ignorés : {stats['skipped']}")
    log.info(f"   erreurs : {stats['error']}")
    log.info(f"   log     : {log_file}")

    # ── 6. Notification email ──────────────────────────────
    send_summary_email(stats, new_results, elapsed, warnings)


if __name__ == "__main__":
    main()
