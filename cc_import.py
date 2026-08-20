"""
cc_import.py - Import quotidien CompetitionCorner → wod-open.com

Récupère les events actifs/upcoming depuis l'API publique de
competitioncorner.net, filtre sur FR/BE/CH et importe les nouveaux
events en brouillon dans WordPress.

Usage manuel  : python cc_import.py
Planificateur : ajouter au setup_task.bat (même tâche ou tâche séparée)

Config rapide :
  DRY_RUN     = True   → simulation, aucune écriture WP
  POST_STATUS = "draft" → créer en brouillon (recommandé)
               "publish" → publier directement
"""

import sys, json, time, re, io, unicodedata, math
import os, logging, smtplib, ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime, timezone
from pathlib import Path

import requests
try:
    from curl_cffi import requests as curl_requests
    _cc_session = curl_requests.Session(impersonate="chrome124")
except ImportError:
    try:
        import cloudscraper
        _cc_session = cloudscraper.create_scraper(browser={"browser": "chrome", "platform": "windows"})
    except ImportError:
        _cc_session = requests
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

CC_BASE_URL   = "https://competitioncorner.net"
CC_API_URL    = f"{CC_BASE_URL}/api2/v1/events/filtered"
CC_DETAIL_URL = f"{CC_BASE_URL}/api2/v1/events/{{id}}"
CC_IMG_URL    = f"{CC_BASE_URL}/api2/v1/files/download?filename={{path}}"
CC_EVENT_URL  = f"{CC_BASE_URL}/events/{{id}}"

COUNTRIES_FILTER = {"FR", "BE", "CH"}   # codes ISO

# ── Notifications email ────────────────────────────────────
EMAIL_ENABLED  = True
SMTP_HOST      = "smtp.gmail.com"
SMTP_PORT      = 587
SMTP_USER      = "typgraf@gmail.com"
SMTP_PASSWORD  = os.environ.get("SMTP_PASSWORD", "jupo hnqx xlhn eegt")
EMAIL_FROM     = SMTP_USER
EMAIL_TO       = "typgraf@gmail.com"

_HERE        = Path(__file__).parent
RESULTS_FILE = _HERE / "cc_import_results.json"
LOGS_DIR     = _HERE / "logs"
DELAY_WP     = 5
DELAY_NOMIN  = 1.3

# ── Taxonomies WP ──────────────────────────────────────────
TYPE_TAX = {"crossfit": 239, "hybrid_race": 238, "hyrox": 238}

# Détection Hyrox par mots-clés (fallback si type API incorrect)
_HYROX_RE       = re.compile(r'hyrox|hybri', re.IGNORECASE)
_EXPLICIT_CF_RE = re.compile(r'\(crossfit\)', re.IGNORECASE)

# Détection compétitions internes (non ouvertes aux inscriptions externes)
INTERNAL_KW_RE = re.compile(
    r'\binterne\b|\binternal\b|\bmembres?\b|\bstaff\b|\bpriv[eé]\b',
    re.IGNORECASE
)
LOC_COUNTRY = {"FR": 141, "BE": 142, "CH": 143}
LOC_REGION  = {
    "auvergne-rhone-alpes": 153, "bourgogne-franche-comte": 151,
    "bretagne": 148,              "centre-val-de-loire": 150,
    "corse": 156,                 "grand-est": 147,
    "hauts-de-france": 144,       "ile-de-france": 146,
    "la-reunion": 157,            "martinique": 158,
    "mayotte": 161,               "normandie": 145,
    "nouvelle-aquitaine": 152,    "occitanie": 154,
    "pays-de-la-loire": 149,
    "provence-alpes-cote-dazur": 155,
    "provence-alpes-cote-d-azur": 155,
    "guadeloupe": 160,            "guyane": 159,
}
CAT_MAP = {1: 136, 2: 137, 3: 140, 4: 162, 5: 164, 6: 193}
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
    try:
        lat, lng = float(lat_s), float(lng_s)
        if lat and lng:
            for tag_id, (clat, clng) in CITY_TAGS.items():
                if _haversine_km(lat, lng, clat, clng) <= MAX_CITY_DIST_KM:
                    tags.add(tag_id)
    except (ValueError, TypeError):
        pass
    text = f"{title} {description}"
    for tag_id, pattern in KEYWORD_TAGS.items():
        if pattern.search(text):
            tags.add(tag_id)
    return list(tags)


# ═══════════════════════════════════════════════════════════
# ▌ Logging
# ═══════════════════════════════════════════════════════════
LOGS_DIR.mkdir(exist_ok=True)
log_file = LOGS_DIR / f"cc_{datetime.now():%Y-%m-%d}.log"
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
# ▌ Helpers
# ═══════════════════════════════════════════════════════════
def to_slug(text: str) -> str:
    nfkd = unicodedata.normalize("NFD", text.lower())
    ascii_ = "".join(c for c in nfkd if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z0-9]+", "-", ascii_).strip("-")

def normalize_title(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())

def make_slug(ev: dict) -> str:
    """Slug = slugify(name) + '-cc-' + id  (préfixe cc pour éviter les conflits avec scoring.fit)."""
    name = ev.get("name", "event").strip()
    ev_id = str(ev.get("id", ""))
    return to_slug(name) + f"-cc-{ev_id}"

def iso_to_ts(iso: str) -> int:
    """ISO datetime string → Unix timestamp UTC."""
    if not iso:
        return 0
    try:
        # Format: "2026-03-08T08:00:00"
        dt = datetime.strptime(iso[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except ValueError:
        return 0

def iso_to_date(iso: str) -> str:
    """ISO datetime → 'DD-MM-YYYY' for calendar."""
    if not iso:
        return ""
    try:
        return datetime.strptime(iso[:10], "%Y-%m-%d").strftime("%d-%m-%Y")
    except ValueError:
        return ""

def iso_to_time(iso: str, default: str = "09:00") -> str:
    """ISO datetime → 'HH:MM'. Retourne default si absent ou minuit UTC (= pas d'heure précisée)."""
    if not iso or len(iso) < 16:
        return default
    t = iso[11:16]
    return default if t == "00:00" else t

def compute_event_days(start_iso: str, end_iso: str) -> str:
    """ISO dates → '1777075200-1777161600-...' (timestamps minuit UTC)."""
    try:
        s = datetime.strptime(start_iso[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        e = datetime.strptime(end_iso[:10],   "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return ""
    from datetime import timedelta
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


# ═══════════════════════════════════════════════════════════
# ▌ CompetitionCorner API
# ═══════════════════════════════════════════════════════════
CC_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
    "Referer": "https://competitioncorner.net/",
    "Origin": "https://competitioncorner.net",
}


def fetch_cc_events() -> list[dict]:
    """Récupère les events actifs et upcoming depuis l'API CC publique (toutes les pages)."""
    all_events: list[dict] = []
    seen_ids: set[int] = set()

    for timing in ("active", "upcoming"):
        page = 1
        timing_total = 0
        while True:
            try:
                r = _cc_session.get(CC_API_URL, params={"timing": timing, "page": page},
                                    headers=CC_HEADERS, timeout=15)
                r.raise_for_status()
                events = r.json()
                if not isinstance(events, list):
                    log.warning(f"  [CC API] réponse inattendue pour timing={timing} page={page}")
                    break
                if not events:
                    break   # plus de pages
                added = 0
                for ev in events:
                    ev_id = ev.get("id")
                    if ev_id and ev_id not in seen_ids:
                        seen_ids.add(ev_id)
                        all_events.append(ev)
                        added += 1
                timing_total += added
                page += 1
                if len(events) < 10:
                    break   # dernière page (incomplète)
            except Exception as e:
                log.warning(f"  [CC fetch timing={timing} page={page}] {e}")
                break
        log.info(f"  timing={timing}: {timing_total} nouveaux events ({page - 1} pages)")

    return all_events


# ═══════════════════════════════════════════════════════════
# ▌ CompetitionCorner — détail d'un event (description + prix)
# ═══════════════════════════════════════════════════════════
def fetch_cc_event_detail(ev_id: int) -> dict:
    """Retourne le détail d'un event CC : description HTML, prix, organisateur."""
    try:
        time.sleep(0.5)   # politesse API
        r = _cc_session.get(CC_DETAIL_URL.format(id=ev_id), headers=CC_HEADERS, timeout=15)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.warning(f"    [CC detail ev_id={ev_id}] {e}")
        return {}


def _extract_price_cc(price, price_team, currency: str):
    """Retourne (min_p, max_p, price_str) depuis les prix CompetitionCorner."""
    curr = chr(8364) if (currency or 'EUR').upper() in ('EUR',) else (currency or 'EUR').upper()
    values = []
    for p in (price, price_team):
        if p is not None:
            try:
                fv = float(p)
                if fv > 0:
                    values.append(int(fv))
            except (TypeError, ValueError):
                pass
    if not values:
        return None, None, ''
    lo, hi = min(values), max(values)
    price_str = f'{lo} - {hi} {curr}' if lo != hi else f'{lo} {curr}'
    return lo, hi, price_str
def geocode_region(lat: str, lng: str, city: str, country: str) -> str:
    """
    Retourne le state_slug pour déterminer la région française.
    Utilise d'abord reverse geocoding sur les coords CC,
    puis fallback Nominatim search si pas de lat/lng.
    """
    time.sleep(DELAY_NOMIN)
    try:
        if lat and lng and float(lat) and float(lng):
            r = requests.get(
                "https://nominatim.openstreetmap.org/reverse",
                params={"lat": lat, "lon": lng, "format": "json", "addressdetails": 1},
                headers={"User-Agent": "wod-open-import/1.0"},
                timeout=10,
            )
            data = r.json()
            addr = data.get("address", {})
            state = addr.get("state") or addr.get("county") or ""
            if state:
                return to_slug(state)
    except Exception:
        pass
    # Fallback: search par ville
    try:
        time.sleep(DELAY_NOMIN)
        r = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": f"{city}, {country}", "format": "json", "limit": 1,
                    "addressdetails": 1},
            headers={"User-Agent": "wod-open-import/1.0"},
            timeout=10,
        )
        data = r.json()
        if data:
            addr = data[0].get("address", {})
            state = addr.get("state") or addr.get("county") or ""
            return to_slug(state) if state else ""
    except Exception:
        pass
    return ""


# ═══════════════════════════════════════════════════════════
# ▌ Taxonomie catégorie (format + eventTags)
# ═══════════════════════════════════════════════════════════
def _find_team_size(text: str) -> int:
    """
    Extrait la taille d'équipe depuis un texte (tags, titre...). Défaut 2.
    Gère :
      - Lettres genre : "HH"=2, "HF"=2, "HHF"=3, "HHHF"=4
      - Mots genre slash : "HOMME/FEMME"=2, "HOMME/FEMME/FEMME"=3
      - Numérique : "Team 3", "Équipe de 3", "3 personnes"
    """
    # Lettres genre (HH, HF, HHF, MMF…) — mot standalone
    m = re.search(r'\b([HMF]{2,6})\b', text)
    if m:
        return len(m.group(1))
    # Mots genre séparés par /
    gender_words = re.findall(r'\b(?:homme|femme|male|female|man|woman)\b', text, re.IGNORECASE)
    if gender_words:
        return len(gender_words)
    # Numérique après team/équipe
    m = re.search(r'(?:team|équipe|equipe)[^0-9]*([2-9]|\d{2,})', text, re.IGNORECASE)
    if m:
        return int(m.group(1))
    # Numérique avant person/personne/athlete
    m = re.search(r'([2-9]|\d{2,})\s*(?:person|personne|athlete|athlète)', text, re.IGNORECASE)
    if m:
        return int(m.group(1))
    return 2


def detect_category(ev: dict) -> list[str]:
    """Retourne les IDs event_cat selon format et eventTags."""
    fmt  = (ev.get("format") or "").lower()
    tags = [t.get("value", "") for t in (ev.get("eventTags") or [])]
    search_str = " ".join(tags + [ev.get("tags", ""), ev.get("name", "")])

    if fmt == "individual":
        return [str(CAT_MAP[1])]

    if fmt == "team":
        n = _find_team_size(search_str)
        return [str(CAT_MAP.get(n, CAT_MAP[2]))]

    if fmt == "both":
        n = _find_team_size(search_str)
        return [str(CAT_MAP[1]), str(CAT_MAP.get(n, CAT_MAP[2]))]

    return [str(CAT_MAP[1])]


def _is_internal(name: str, description: str = "") -> bool:
    """True si la compétition semble interne/fermée aux inscriptions externes."""
    return bool(INTERNAL_KW_RE.search(name) or INTERNAL_KW_RE.search(description))


# ═══════════════════════════════════════════════════════════
# ▌ Taxonomie type (crossfit/hyrox)
# ═══════════════════════════════════════════════════════════
def detect_type(ev: dict) -> list[str]:
    """Retourne l'ID de taxonomie type (crossfit ou hyrox).

    "hyrox" dans le nom est toujours décisif (même si le nom de la box
    contient "CrossFit"). L'exception "(crossfit)" en parenthèses
    ne s'applique qu'au mot "hybrid" seul, plus ambigu.
    """
    ev_type = (ev.get("type") or "").lower()
    name    = ev.get("name", "")
    tags    = " ".join(t.get("value", "") for t in (ev.get("eventTags") or []))
    combined = f"{name} {tags}"
    # hybrid_race → hyrox directement
    if ev_type == "hybrid_race":
        return [str(TYPE_TAX["hyrox"])]
    # "hyrox" explicite → toujours Hyrox
    if re.search(r'hyrox', combined, re.IGNORECASE):
        return [str(TYPE_TAX["hyrox"])]
    # "hybrid*" → Hyrox sauf qualificateur (crossfit) littéral
    if re.search(r'hybri', combined, re.IGNORECASE) and not _EXPLICIT_CF_RE.search(name):
        return [str(TYPE_TAX["hyrox"])]
    return [str(TYPE_TAX.get(ev_type, TYPE_TAX["crossfit"]))]


# ═══════════════════════════════════════════════════════════
# ▌ Upload image
# ═══════════════════════════════════════════════════════════
def upload_image(thumbnail: str, slug: str, title: str) -> int | None:
    """Télécharge l'image CC, upload sur WP, retourne attachment_id."""
    if not thumbnail:
        return None

    img_url = CC_IMG_URL.format(path=thumbnail)
    try:
        r = _cc_session.get(img_url, headers=CC_HEADERS, timeout=30)
        r.raise_for_status()
        raw = r.content
    except Exception as e:
        log.warning(f"    [IMG download] {img_url}: {e}")
        return None

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
# ▌ Construction du post WP
# ═══════════════════════════════════════════════════════════
def build_post(ev: dict, slug: str, detail: dict | None = None) -> dict:
    """Construit le payload complet pour wp.newPost."""
    detail    = detail or {}
    title     = ev.get("name", "").strip()
    loc       = ev.get("eventLocation") or {}
    country_c = loc.get("countryCode", "")
    city      = loc.get("city", "").strip()
    state     = loc.get("state", "").strip()
    country   = loc.get("country", "").strip()
    lat       = str(loc.get("lat") or "")
    lng       = str(loc.get("lng") or "")

    start_iso = ev.get("startDateTime", "")
    end_iso   = ev.get("endDateTime", "")

    ts_start  = iso_to_ts(start_iso)
    ts_end    = iso_to_ts(end_iso)
    start_cal = iso_to_date(start_iso)
    end_cal   = iso_to_date(end_iso)
    start_h   = iso_to_time(start_iso, "09:00")
    end_h     = iso_to_time(end_iso, "18:00")
    days_val  = compute_event_days(start_iso, end_iso)

    cal_id   = str(int(time.time()))
    cal_val  = php_calendar(cal_id, start_cal, end_cal, start_h, end_h) if start_cal else []

    # Adresse
    parts = [p for p in [city, state, country] if p]
    map_addr = ", ".join(parts)

    # URL externe
    ev_id   = ev.get("id", "")
    ext_url = CC_EVENT_URL.format(id=ev_id) if ev_id else ""

    # Description
    description  = detail.get("description", "") or ""
    post_content = description.strip()

    # Prix → champs meta dédiés (comme scoring.fit)
    price        = detail.get("registrationPrice")
    price_team   = detail.get("registrationPriceTeam")
    currency     = (detail.get("currency") or "eur").upper()
    min_p, max_p, price_str = _extract_price_cc(price, price_team, currency)

    # Taxonomies
    type_ids = detect_type(ev)
    cat_ids  = detect_category(ev)
    loc_terms: set[str] = set()
    country_tid = LOC_COUNTRY.get(country_c)
    if country_tid:
        loc_terms.add(str(country_tid))

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
        "ova_mb_event_name_organizer":             ev.get("venue") or detail.get("organizerName") or title,
        "ova_mb_event_phone_organizer":            "NC",
        "ova_mb_event_mail_organizer":             detail.get("organizerEmail") or "NC",
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

    # Lat/lng CC directement disponibles
    if lat and lng:
        meta["ova_mb_event_map_lat"] = lat
        meta["ova_mb_event_map_lng"] = lng

    # Tags (villes proches + mots-clés)
    tag_ids = find_tags(lat, lng, title, description)

    return {
        "status":     POST_STATUS,
        "title":      title,
        "slug":       slug,
        "content":    post_content,
        "event_type": [int(i) for i in type_ids],
        "event_cat":  [int(i) for i in cat_ids],
        "event_loc":  [int(i) for i in loc_terms],
        "event_tag":  tag_ids,
        "meta":       meta,
        # région + featured_media ajoutés dans enrich_post
    }


# ═══════════════════════════════════════════════════════════
# ▌ Enrichissement post-création (région France + image)
# ═══════════════════════════════════════════════════════════
def enrich_post(wp_id: int, ev: dict, slug: str, title: str) -> None:
    """Région française + image à la une via REST PATCH."""
    loc       = ev.get("eventLocation") or {}
    country_c = loc.get("countryCode", "")
    country   = loc.get("country", "").strip()
    city      = loc.get("city", "").strip()
    lat       = str(loc.get("lat") or "")
    lng       = str(loc.get("lng") or "")
    thumbnail = ev.get("thumbnail") or ev.get("image") or ""

    patch: dict = {}

    # ── Région française ──────────────────────────────────────
    event_loc_ids: set[int] = set()
    country_tid = LOC_COUNTRY.get(country_c)
    if country_tid:
        event_loc_ids.add(country_tid)
    if country_c == "FR" and (lat or city):
        state_slug = geocode_region(lat, lng, city, country)
        if state_slug:
            region_tid = LOC_REGION.get(state_slug)
            if region_tid:
                event_loc_ids.add(region_tid)
                log.info(f"    🗺️  région : {state_slug} → {region_tid}")
    if event_loc_ids:
        patch["event_loc"] = list(event_loc_ids)

    if patch and not DRY_RUN:
        try:
            wp_rest("patch", f"events/{wp_id}", json=patch)
        except Exception as e:
            log.warning(f"    [enrich PATCH] {e}")

    # ── Image à la une ────────────────────────────────────────
    media_id = upload_image(thumbnail, slug, title)
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
    if not EMAIL_ENABLED:
        return
    try:
        date_str = datetime.now().strftime("%d/%m/%Y %H:%M")
        subject  = (
            f"[wod-open/CC] Import du {date_str} — "
            f"{stats['created']} créés / {stats['error']} erreurs"
        )

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
            Import CompetitionCorner → wod-open.com — {date_str}
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
            wod-open.com — import automatique competitioncorner.net
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
    log.info(f"▶ Import CC wod-open.com — {datetime.now():%d/%m/%Y %H:%M}")
    log.info(f"  DRY_RUN={DRY_RUN}  POST_STATUS={POST_STATUS}")
    log.info("=" * 60)

    # ── 1. Fetch CompetitionCorner ─────────────────────────
    log.info("\n[1] Fetch competitioncorner.net...")
    all_events = fetch_cc_events()
    log.info(f"    {len(all_events)} events récupérés au total")

    # Filtrer par pays
    filtered = [
        ev for ev in all_events
        if (ev.get("eventLocation") or {}).get("countryCode") in COUNTRIES_FILTER
        and not ev.get("private", False)     # ignorer les events privés
    ]
    log.info(f"    → {len(filtered)} après filtre FR/BE/CH (hors privés)")

    if not filtered:
        log.info("  Aucun event à traiter. Fin.")
        elapsed = time.time() - run_start
        send_summary_email({"created": 0, "skipped": 0, "error": 0}, [], elapsed, [])
        return

    # ── 2. Charger résultats précédents ────────────────────
    existing_results: list[dict] = []
    if RESULTS_FILE.exists():
        existing_results = json.loads(RESULTS_FILE.read_text(encoding="utf-8"))
    existing_slugs  = {r["slug"] for r in existing_results
                       if r["action"] in ("created", "existing")}
    existing_titles = {normalize_title(r["title"])
                       for r in existing_results
                       if r["action"] in ("created", "existing")}
    existing_cc_ids = {r.get("cc_id") for r in existing_results
                       if r.get("cc_id") and r.get("action") in ("created", "existing")}

    log.info(f"    {len(existing_results)} entrées déjà dans cc_import_results.json")

    # Croiser aussi avec import_results.json (scoring.fit) pour éviter les doublons cross-source
    sf_results_file = Path("import_results.json")
    if sf_results_file.exists():
        sf_results = json.loads(sf_results_file.read_text(encoding="utf-8"))
        sf_titles = {normalize_title(r["title"])
                     for r in sf_results
                     if r.get("action") in ("created", "existing") and r.get("title")}
        existing_titles |= sf_titles
        log.info(f"    + {len(sf_titles)} titres depuis import_results.json (scoring.fit)")

    # ── 3. Import ──────────────────────────────────────────
    log.info("\n[3] Import des nouveaux events...")
    new_results: list[dict] = []
    warnings:    list[str]  = []
    stats = {"created": 0, "skipped": 0, "error": 0}

    for ev in filtered:
        cc_id = ev.get("id")
        slug  = make_slug(ev)
        title = ev.get("name", "").strip()
        norm  = normalize_title(title)
        loc   = ev.get("eventLocation") or {}
        country_c = loc.get("countryCode", "")

        # Anti-doublon
        if cc_id in existing_cc_ids:
            log.info(f"  [SKIP doublon cc_id] {title[:50]}")
            stats["skipped"] += 1
            continue
        if slug in existing_slugs:
            log.info(f"  [SKIP doublon slug] {title[:50]}")
            stats["skipped"] += 1
            continue
        if norm in existing_titles:
            log.info(f"  [SKIP doublon titre] {title[:50]}")
            stats["skipped"] += 1
            continue

        log.info(f"  [NEW] {title[:55]}  (cc_id={cc_id}, {country_c})")

        # Détail : description + prix
        detail = fetch_cc_event_detail(cc_id)
        price     = detail.get("registrationPrice")
        price_team = detail.get("registrationPriceTeam")
        currency  = (detail.get("currency") or "eur").upper()
        has_desc  = bool(detail.get("description"))
        log.info(f"    prix={price}/{price_team} {currency}  desc={'oui' if has_desc else 'non'}")

        # Filtre compétitions internes
        description = detail.get("description", "") or ""
        if _is_internal(title, description):
            log.info(f"  [SKIP interne] {title[:55]}")
            stats["skipped"] += 1
            continue

        payload = build_post(ev, slug, detail)

        if DRY_RUN:
            start_dt = ev.get("startDateTime", "")[:10]
            log.info(f"    [DRY] newPost → {title}  ({start_dt})")
            new_results.append({
                "wp_id": 0, "slug": slug, "title": title,
                "action": "dry_run", "cc_id": cc_id,
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
                "action": "created", "cc_id": cc_id,
            })
            stats["created"] += 1
        except Exception as e:
            log.error(f"    [ERR REST POST] {e}")
            warnings.append(f"[ERR REST POST] {title[:60]} — {e}")
            stats["error"] += 1
            continue

        # Enrichissement
        log.info(f"    → enrichissement (région + image)...")
        enrich_post(wp_id, ev, slug, title)

        if MAX_NEW and stats["created"] >= MAX_NEW:
            log.info(f"  [STOP] MAX_NEW={MAX_NEW} atteint.")
            break

    # ── 4. Sauvegarder résultats ───────────────────────────
    all_results = existing_results + new_results
    RESULTS_FILE.write_text(
        json.dumps(all_results, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    elapsed = time.time() - run_start
    log.info(f"\n{'='*60}")
    log.info(f"✅ Terminé en {elapsed:.0f}s")
    log.info(f"   créés   : {stats['created']}")
    log.info(f"   ignorés : {stats['skipped']}")
    log.info(f"   erreurs : {stats['error']}")
    log.info(f"   log     : {log_file}")

    # ── 5. Notification email ──────────────────────────────
    send_summary_email(stats, new_results, elapsed, warnings)


if __name__ == "__main__":
    main()
