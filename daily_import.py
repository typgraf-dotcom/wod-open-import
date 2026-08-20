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

import sys, os, json, time, re, io, unicodedata, math
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
MAX_NEW     = 0              # Limite de nouveaux events créés (0 = illimité)

WP_URL      = "https://wod-open.com"
WP_USER     = "typgraf"
WP_APP_PASS = os.environ.get("WP_APP_PASS", "1Pyz cRXX sttO rKCx wZbB Zde7")
REST_URL    = f"{WP_URL}/wp-json/wp/v2"
REST_AUTH   = (WP_USER, WP_APP_PASS)

SF_SEARCH_URL = (
    "https://scoring-prod.up.railway.app"
    "/api/leaderboard/competition/search-query"
)
SF_DETAIL_URL = (
    "https://scoring-prod.up.railway.app"
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

_HERE        = Path(__file__).parent
RAW_FILE     = _HERE / "competitions_raw.json"
RESULTS_FILE = _HERE / "import_results.json"
LOGS_DIR     = _HERE / "logs"
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

# ── Tags villes : tag_id → (lat, lng) ─────────────────────
MAX_CITY_DIST_KM = 80
CITY_TAGS: dict[int, tuple[float, float]] = {
    166: (43.6047,  1.4442),   # Toulouse
    167: (44.8378, -0.5792),   # Bordeaux
    168: (43.2965,  5.3698),   # Marseille
    180: (42.6887,  2.8948),   # Perpignan
    181: (46.1591, -1.1520),   # La Rochelle
    182: (43.6119,  3.8772),   # Montpellier
    183: (47.7508,  7.3359),   # Mulhouse
    186: (48.8566,  2.3522),   # Paris
    187: (49.8941,  2.2958),   # Amiens
    190: (50.6292,  3.0573),   # Lille
    194: (48.5734,  7.7521),   # Strasbourg
    205: (43.7102,  7.2620),   # Nice
    207: (43.8367,  4.3601),   # Nîmes
    211: (45.7640,  4.8357),   # Lyon
    212: (45.8336,  1.2611),   # Limoges
    213: (48.2973,  4.0744),   # Troyes
    216: (48.1173, -1.6778),   # Rennes
    217: (48.0793,  7.3586),   # Colmar
    218: (43.9493,  4.8055),   # Avignon
    220: (49.4432,  1.0993),   # Rouen
    222: (47.2380,  6.0243),   # Besançon
    223: (46.8122,  1.6941),   # Châteauroux
    226: (47.3941,  0.6848),   # Tours
    227: (45.7797,  3.0863),   # Clermont-Ferrand
    230: (47.3220,  5.0415),   # Dijon
    231: (47.9960,  0.1966),   # Le Mans
    232: (45.8992,  6.1294),   # Annecy
    233: (49.1829, -0.3707),   # Caen
    234: (45.1885,  5.7245),   # Grenoble
    245: (48.3904, -4.4861),   # Brest
    246: (47.2184, -1.5536),   # Nantes
    249: (49.6333, -1.6167),   # Cherbourg
    250: (48.2658,  2.6939),   # Nemours
    251: (43.2951, -0.3708),   # Pau
    252: (43.4921, -1.4742),   # Bayonne
    253: (50.2917,  2.7819),   # Arras
    254: (45.4347,  4.3900),   # Saint-Étienne
    257: (43.6045,  2.2478),   # Castres
    258: (43.2130,  2.3491),   # Carcassonne
    261: (43.4832, -1.5586),   # Biarritz
    274: (47.4784, -0.5632),   # Angers
    275: (48.6921,  6.1844),   # Nancy
    276: (44.9334,  4.8924),   # Valence
    277: (46.5802,  0.3404),   # Poitiers
    281: (49.4938,  0.1079),   # Le Havre
    282: (47.9029,  1.9039),   # Orléans
    283: (44.5594,  6.0773),   # Gap
    284: (45.6757,  6.3928),   # Albertville
    285: (49.2583,  4.0317),   # Reims
    286: (44.8500,  0.4833),   # Bergerac
    287: (50.4333,  2.8333),   # Lens
    288: (49.4144,  2.8231),   # Compiègne
    289: (49.8483,  3.2847),   # Saint-Quentin
    290: (43.6939,  5.5030),   # Pertuis
    291: (51.0340,  2.3776),   # Dunkerque
    292: (46.9897,  3.1572),   # Nevers
    294: (47.0810,  2.3988),   # Bourges
    296: (48.6493, -2.0097),   # Saint-Malo
    297: (43.2727,  6.6406),   # Saint-Tropez
    298: (43.1258,  5.9306),   # Toulon
    299: (46.3240, -0.4617),   # Niort
    300: (49.5635,  3.6197),   # Laon
    301: (48.0698, -0.7687),   # Laval
    303: (44.0183,  1.3550),   # Montauban
    304: (50.3581,  3.5234),   # Valenciennes
    306: (46.2044,  6.1432),   # Genève
    308: (43.1840,  3.0003),   # Narbonne
    309: (47.7980,  3.5680),   # Auxerre
    311: (50.8503,  4.3517),   # Bruxelles
    313: (45.5646,  5.9178),   # Chambéry
    315: (49.0249,  1.1516),   # Évreux
    317: (46.1183,  3.4265),   # Vichy
    318: (45.6500,  0.1500),   # Angoulême
    319: (49.1193,  6.1727),   # Metz
    320: (50.7272,  1.6150),   # Boulogne-sur-Mer
    322: (50.9513,  1.8587),   # Calais
}

# ── Tags mots-clés : tag_id → regex ───────────────────────
KEYWORD_TAGS: dict[int, re.Pattern] = {
    188: re.compile(r'\bext[eé]rieur|outdoor|plage|lac\b',                    re.I),
    191: re.compile(r'\bd[eé]butants?\b|\bnovice\b|\bbeginner\b',             re.I),
    192: re.compile(r'\bmasters?\b|\bv[eé]t[eé]rans?\b|\b(?:40|35)\+',       re.I),
    195: re.compile(r'\bteens?\b|\badolescents?\b|\byouth\b',                 re.I),
    196: re.compile(r'\bfamille\b|\bfamily\b|\bparent\b',                     re.I),
    197: re.compile(r'\bnatation\b|\bnage\b|\bswim\b|\baqua\b|\btriathlon\b', re.I),
    206: re.compile(r'\bfemmes?\b|\bwomen\b|\bwoman\b|\bf[eé]minin\b',        re.I),
    228: re.compile(r'\bhyrox\b|\bhybrid.?race\b',                            re.I),
    255: re.compile(r'\bkids?\b',                                             re.I),
    256: re.compile(r'\benfants?\b|\bchildren\b|\bjuniors?\b',                re.I),
    259: re.compile(r'\bhalt[eé]rophilie\b|\bweightlift',                     re.I),
    260: re.compile(r'\b[eé]lite\b',                                          re.I),
    262: re.compile(r'\badaptive\b|\bpara[- ]athl|\bhandisport\b',            re.I),
}


def _haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlng = math.radians(lng2 - lng1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
         * math.sin(dlng / 2) ** 2)
    return R * 2 * math.asin(math.sqrt(a))


def find_tags(lat_s, lng_s, title: str, description: str = "") -> list[int]:
    """Retourne les IDs event_tag à associer : villes proches + mots-clés."""
    tags: set[int] = set()
    # Villes dans le rayon
    try:
        lat, lng = float(lat_s), float(lng_s)
        if lat and lng:
            for tag_id, (clat, clng) in CITY_TAGS.items():
                if _haversine_km(lat, lng, clat, clng) <= MAX_CITY_DIST_KM:
                    tags.add(tag_id)
    except (ValueError, TypeError):
        pass
    # Mots-clés dans le titre et la description
    text = f"{title} {description}"
    for tag_id, pattern in KEYWORD_TAGS.items():
        if pattern.search(text):
            tags.add(tag_id)
    return list(tags)


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


# Détection Hyrox : mots-clés dans le nom de l'event ou des divisions
# Exception : "(crossfit)" explicite dans le nom → CrossFit malgré "hybrid"
_HYROX_RE      = re.compile(r'hyrox|hybri', re.IGNORECASE)
_EXPLICIT_CF_RE = re.compile(r'\(crossfit\)', re.IGNORECASE)

# Détection compétitions internes (non ouvertes aux inscriptions externes)
INTERNAL_KW_RE = re.compile(
    r'\binterne\b|\binternal\b|\bmembres?\b|\bstaff\b|\bpriv[eé]\b',
    re.IGNORECASE
)


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
                 time_start: str, time_end: str) -> list:
    """Structure OVA pour ova_mb_event_calendar.

    Retourne une liste Python (6 champs) — envoyée telle quelle via XML-RPC
    pour que WordPress stocke un vrai tableau PHP (a:6:{...}) sans
    double-sérialisation. Ne PAS passer une string PHP ici : WordPress
    ré-sérialise les strings déjà sérialisées en s:NNN:"..." ce qui casse
    la lecture par OVA."""
    return [{
        'calendar_id': str(cal_id),
        'date': date_start,
        'end_date': date_end,
        'start_time': time_start,
        'end_time': time_end,
        'book_before_minutes': '0',
    }]

def extract_cal_id(php_str: str) -> str:
    m = re.search(r'"calendar_id";s:\d+:"(\d+)"', php_str or "")
    return m.group(1) if m else str(int(time.time()))


# ═══════════════════════════════════════════════════════════
# ▌ WordPress REST API
# ═══════════════════════════════════════════════════════════
def wp_rest(method: str, endpoint: str, **kwargs) -> dict:
    time.sleep(DELAY_WP)
    url = f"{REST_URL}/{endpoint}"
    r = getattr(requests, method)(url, auth=REST_AUTH, timeout=30, **kwargs)
    r.raise_for_status()
    return r.json()

def fetch_wp_slugs() -> set[str]:
    """Récupère tous les slugs d'events existants dans WP (toutes statuts)."""
    slugs: set[str] = set()
    page = 1
    while True:
        try:
            r = requests.get(
                f"{REST_URL}/events",
                auth=REST_AUTH,
                params={"per_page": 100, "page": page, "status": "publish,draft,pending,private,future,trash", "_fields": "slug"},
                timeout=30,
            )
            if r.status_code in (400, 404):
                break
            r.raise_for_status()
            data = r.json()
            if not data:
                break
            for ev in data:
                s = ev.get("slug", "")
                if s:
                    slugs.add(s)
            if len(data) < 100:
                break
            page += 1
        except Exception as e:
            log.warning(f"  [fetch_wp_slugs page={page}] {e}")
            break
    return slugs


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
    """Géocode avec plusieurs stratégies de fallback."""
    # 1. Chaîne complète
    res = _nomin_query(f"{location}, {country}")
    if res:
        return res
    # 2. Code postal seul
    postal = re.search(r'\b(\d{5})\b', location)
    if postal:
        res = _nomin_query(f"{postal.group(1)}, {country}")
        if res:
            return res
    # 3. Segments séparés par //, |, —, –, -
    for sep in ('//', '|', ' — ', ' – ', ' - '):
        if sep in location:
            for part in reversed(location.split(sep)):
                part = part.strip()
                if not part:
                    continue
                # Essai direct du segment
                res = _nomin_query(f"{part}, {country}")
                if res:
                    return res
                # Essai sans les mots fitness (ex: "CrossFit Castanet-Tolosan" → "Castanet-Tolosan")
                words = re.split(r'[\s,]+', part)
                cleaned = [w for w in words if w.lower() not in FITNESS_KW and len(w) > 2]
                if cleaned and cleaned != words:
                    res = _nomin_query(f"{' '.join(cleaned)}, {country}")
                    if res:
                        return res
    # 4. Suppression globale mots-clés fitness
    words   = re.split(r"[\s\-&,]+", location)
    cleaned = [w for w in words if w.lower() not in FITNESS_KW and len(w) > 2]
    if cleaned:
        res = _nomin_query(f"{' '.join(cleaned)}, {country}")
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
    _sh = (dates.get("start") or {}).get("hour", "")
    start_h = _sh if _sh and _sh not in ("00:00",) else "09:00"
    _eh = (dates.get("end") or {}).get("hour", "")
    end_h = _eh if _eh and _eh not in ("00:00", "23:59") else "18:00"

    ts_start = ts_from_dmY(start_d) if start_d else 0
    ts_end   = ts_from_dmY(end_d)   if end_d   else 0
    days_val = compute_event_days(start_d, end_d) if start_d and end_d else ""
    start_cal = start_d.replace("/", "-") if start_d else ""
    end_cal   = end_d.replace("/", "-")   if end_d   else ""

    cal_id   = str(int(time.time()))
    cal_val  = php_calendar(cal_id, start_cal, end_cal, start_h, end_h) if start_cal else []

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
    if min_p is not None:
        log.info(f"    💶 prix : {price_str}")

    # URL externe
    event_number = comp.get("eventNumber")
    btn_url = (lb.get("buttonLink") or {}).get("url", "")
    ext_url = f"https://scoring.fit/{btn_url}" if btn_url else (
        f"https://scoring.fit/{event_number}" if event_number else "")

    # Taxonomies
    comp_type = str(comp.get("type") or "").lower()
    comp_cat  = str(comp.get("category") or "").lower()
    divisions = lb.get("divisions") or []
    type_ids, cat_ids = _detect_types(comp_type, comp_cat, divisions, name=title)

    loc_terms: set[int] = set()
    country_tid = LOC_COUNTRY.get(country_l)
    if is_online:
        loc_terms.add(LOC_ONLINE)
    elif country_tid:
        loc_terms.add(country_tid)

    meta: dict = {
        "ova_mb_event_start_date_str":             str(ts_start),
        "ova_mb_event_end_date_str":               str(ts_end),
        "ova_mb_event_address":                    map_addr,
        "ova_mb_event_map_address":                map_addr,
        "ova_mb_event_ticket_external_link":       ext_url,
        "ova_mb_event_time_zone":                  "Europe/Paris",
        "ova_mb_event_event_type":                 "classic",
        "ova_mb_event_info_organizer":             "checked",
        "ova_mb_event_allow_cancellation_booking": "no",
        "ova_mb_event_ticket_link":                "ticket_external_link",
        "ova_mb_event_option_calendar":            "manual",
        "ova_mb_event_event_days":                 days_val,
        "ova_mb_event_calendar":                   cal_val,
        "ova_mb_event_name_organizer":             location or title,
        "ova_mb_event_phone_organizer":            "NC",
        "ova_mb_event_mail_organizer":             "NC",
        # Sans ce champ, eventlist/templates/loop/thumbnail.php fait
        # array_unshift() sur une chaîne vide (get_post_meta() pour une
        # clé jamais posée) → fatal error 500 sur les pages "événements
        # liés" affichant cet event. Incident du 20/08/2026.
        "ova_mb_event_gallery":                    [],
    }
    if min_p is not None:
        meta["ova_mb_event_min_price"] = str(min_p)
        meta["ova_mb_event_max_price"] = str(max_p)
        meta["ova_mb_event_price_desc"] = price_str
        meta["ova_mb_event_ticket_external_link_price"] = price_str

    return {
        "status":     POST_STATUS,
        "title":      title,
        "slug":       slug,
        "content":    description,
        "event_type": [int(i) for i in type_ids],
        "event_cat":  [int(i) for i in cat_ids],
        "event_loc":  list(loc_terms),
        "meta":       meta,
        # geo + région + featured_media ajoutés ensuite dans enrich_post
    }

def _is_internal(name: str, description: str = "") -> bool:
    """True si la compétition semble interne/fermée aux inscriptions externes."""
    return bool(INTERNAL_KW_RE.search(name) or INTERNAL_KW_RE.search(description))


def _is_hyrox(name: str, divisions: list | None = None) -> bool:
    """True si l'event est de type Hyrox/Hybrid (détection par nom + divisions).

    Règles (par ordre de priorité) :
      1. "hyrox" dans le nom → toujours Hyrox (même si "CrossFit" présent)
      2. "hybri" dans le nom SANS qualificateur "(crossfit)" → Hyrox
         L'exception ne couvre que "(crossfit)" en parenthèses littérales,
         pas le nom d'une box "CrossFit XXX" sans parenthèses.
      3. Un nom de division contient "hyrox"/"hybri" → Hyrox
    """
    if re.search(r'hyrox', name, re.IGNORECASE):
        return True
    if re.search(r'hybri', name, re.IGNORECASE) and not _EXPLICIT_CF_RE.search(name):
        return True
    return bool(divisions and any(
        _HYROX_RE.search(d.get("name") or "") for d in divisions
    ))


def _team_size_from_name(name: str) -> int:
    """
    Déduit la taille d'équipe depuis un nom de division scoring.fit.
    Gère :
      - Lettres genre : "HH"=2, "HF"=2, "HHF"=3, "HHHF"=4 (H=Homme, F=Femme, M=Male)
      - Mots genre slash : "HOMME/FEMME"=2, "HOMME/FEMME/FEMME"=3
      - Numérique : "ÉQUIPE DE 3"=3, "Team 3"=3
    """
    # Lettres genre consécutives (ex: "RX HH", "INTER HHF") — mot standalone
    m = re.search(r'\b([HMF]{2,6})\b', name)
    if m:
        return len(m.group(1))
    # Mots genre séparés par / (ex: "HOMME/FEMME", "FEMME/FEMME/HOMME")
    gender_words = re.findall(r'\b(?:homme|femme|male|female|man|woman)\b', name, re.IGNORECASE)
    if gender_words:
        return len(gender_words)
    # Numérique (ex: "ÉQUIPE DE 3")
    m = re.search(r'\b([2-9]|\d{2,})\b', name)
    if m:
        return int(m.group(1))
    return 2


def _detect_types(comp_type: str, comp_cat: str,
                  divisions: list | None = None,
                  name: str = "") -> tuple[list, list]:
    type_ids = []
    cat_ids: set[str] = set()

    if _is_hyrox(name, divisions):
        type_ids.append(str(TYPE_TAX["hyrox"]))
    else:
        type_ids.append(str(TYPE_TAX["crossfit"]))

    # Source prioritaire : divisions de l'API détail
    if divisions:
        for div in divisions:
            div_type  = (div.get("type") or "").lower()
            div_name  = (div.get("name") or "")
            div_lower = div_name.lower()
            is_team   = div_type == "team" or any(
                k in div_lower for k in ("team", "équipe", "equipe"))
            is_indiv  = div_type == "individual" or any(
                k in div_lower for k in ("indiv", "individual", "solo", "rx", "scaled"))
            if is_indiv and not is_team:
                cat_ids.add(str(CAT_MAP[1]))
            elif is_team:
                n = _team_size_from_name(div_name)
                cat_ids.add(str(CAT_MAP.get(n, CAT_MAP[2])))
        if cat_ids:
            return type_ids, list(cat_ids)

    # Fallback : comp_type / comp_cat de l'API search
    combined = f"{comp_cat} {name}"
    m = re.search(r'team[- _]?(\d)', combined, re.IGNORECASE)
    if m:
        cat_ids.add(str(CAT_MAP.get(int(m.group(1)), 136)))
    else:
        cat_ids.add(str(CAT_MAP[1]))
    return type_ids, list(cat_ids)

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
        result = wp_rest("post", "media",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Content-Type": mime,
            },
            data=image_data,
        )
        media_id = result.get("id")
        if media_id:
            wp_rest("patch", f"media/{media_id}", json={"alt_text": title})
        return media_id
    except Exception as e:
        log.warning(f"    [IMG upload] {e}")
        return None


# ═══════════════════════════════════════════════════════════
# ▌ Post-enrichissement (après création du post)
# ═══════════════════════════════════════════════════════════
def enrich_post(wp_id: int, detail: dict, title: str,
                image_url: str, slug: str) -> None:
    """Géocode + région + tags + image à la une via REST PATCH."""
    lb   = detail.get("leaderboard", {})
    pres = detail.get("presentation", {})

    location  = (pres.get("location") or "").strip()
    country   = (pres.get("country")  or "France").strip()
    country_l = country.lower()
    is_online = lb.get("type") == "online"

    patch: dict = {}

    # ── Géocodage ─────────────────────────────────────────────
    geo = {}
    if location and not is_online:
        geo = geocode_smart(location, country)
        if geo.get("lat"):
            log.info(f"    📍 {location}, {country} → {geo['lat']}, {geo['lng']}")
            patch.setdefault("meta", {}).update({
                "ova_mb_event_map_lat":     geo["lat"],
                "ova_mb_event_map_lng":     geo["lng"],
                "ova_mb_event_map_address": f"{location}, {country}",
            })

    # ── Région ────────────────────────────────────────────────
    event_loc_ids: set[int] = set()
    country_tid = LOC_COUNTRY.get(country_l)
    if is_online:
        event_loc_ids.add(LOC_ONLINE)
    elif country_tid:
        event_loc_ids.add(country_tid)
    if country_l == "france" and geo.get("state_slug"):
        region_tid = LOC_REGION.get(geo["state_slug"])
        if region_tid:
            event_loc_ids.add(region_tid)
            log.info(f"    🗺️  région : {geo['state_slug']} → {region_tid}")
    if event_loc_ids:
        patch["event_loc"] = list(event_loc_ids)

    # ── Tags (villes proches + mots-clés) ─────────────────────
    description = (pres.get("description") or "")
    tag_ids = find_tags(geo.get("lat", ""), geo.get("lng", ""), title, description)
    if tag_ids:
        patch["event_tag"] = tag_ids
        log.info(f"    🏷️  tags : {tag_ids}")

    if patch and not DRY_RUN:
        try:
            wp_rest("patch", f"events/{wp_id}", json=patch)
        except Exception as e:
            log.warning(f"    [enrich PATCH] {e}")

    # ── Image à la une ────────────────────────────────────────
    media_id = upload_image(image_url, slug, title)
    if media_id and not DRY_RUN:
        try:
            wp_rest("patch", f"events/{wp_id}", json={"featured_media": media_id})
            log.info(f"    🖼️  image {media_id} OK")
        except Exception as e:
            log.warning(f"    [image patch] {e}")


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

    # Récupérer aussi les slugs directement depuis WP (détecte les events
    # créés manuellement, mis à la corbeille, ou absents de import_results.json)
    log.info("  Scan des slugs WP existants...")
    wp_slugs = fetch_wp_slugs()
    existing_slugs |= wp_slugs
    log.info(f"  → {len(wp_slugs)} slugs WP chargés")

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

        # Filtre compétitions internes
        description = pres.get("description", "") or ""
        if _is_internal(title, description):
            log.info(f"  [SKIP interne] {title[:55]}")
            stats["skipped"] += 1
            continue

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

        # Créer dans WP via REST
        try:
            result = wp_rest("post", "events", json=payload)
            wp_id  = int(result["id"])
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

        if MAX_NEW and stats["created"] >= MAX_NEW:
            log.info(f"  [STOP] MAX_NEW={MAX_NEW} atteint.")
            break

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
