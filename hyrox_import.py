"""
hyrox_import.py - Import hebdomadaire des events HYROX officiels (hyrox.com)

Source : https://hyrox.com/find-my-race/ (scraping léger, pas d'API officielle).
Vérifié : la liste complète des events est présente dans le HTML brut
(cf. docs/brief-import-hyrox-officiel.md, section 0) → pas besoin de
navigateur headless, requests + BeautifulSoup suffisent.

Enchaîne :
  1. Fetch + parsing de la page liste
  2. Filtrage géographique : géocodage du nom de ville extrait du titre,
     conservé si le pays réel est dans hyrox_countries.json (pas de liste
     de villes à maintenir — n'importe quelle ville de France ou d'un
     pays limitrophe suivi est prise automatiquement)
  3. Comparaison au state file (hyrox_state.json) :
       - nouvel event                              → créer le brouillon WP
       - "Find out more" → "Buy Tickets"            → réactivité : prix +
         update WP + notification email
       - slug HYROX changé (nouvelle édition, même page evergreen)
                                                     → tout rafraîchir
                                                       (dates, calendrier,
                                                       adresse, contenu)
       - sinon                                      → rien
  4. Sauvegarde du state file + email récapitulatif

Usage manuel  : python hyrox_import.py
Planificateur : cron hebdomadaire (voir .github/workflows/hyrox_import.yml)

Config rapide :
  DRY_RUN     = True   → simulation, aucune écriture WP
  POST_STATUS = "draft" → créer en brouillon (recommandé)
"""

import sys, os, json, time, re, io, math, unicodedata
import logging, smtplib, ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime, date, timedelta
from pathlib import Path

import requests
from bs4 import BeautifulSoup
try:
    from PIL import Image
    PIL_OK = True
except ImportError:
    PIL_OK = False
try:
    import anthropic
    ANTHROPIC_OK = True
except ImportError:
    ANTHROPIC_OK = False

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
REST_URL    = f"{WP_URL}/wp-json/wp/v2"
REST_AUTH   = (WP_USER, WP_APP_PASS)

# Reformulation de la description officielle (anti-duplicate-content SEO).
# Pas de clé en dur (contrairement à WP_APP_PASS/SMTP_PASSWORD ci-dessus) —
# credential externe à un tiers, doit venir de l'environnement uniquement.
ANTHROPIC_API_KEY   = os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL     = "claude-opus-5"

HYROX_LIST_URL = "https://hyrox.com/find-my-race/"
USER_AGENT     = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/124.0.0.0 Safari/537.36")
FETCH_RETRIES  = 3
FETCH_BACKOFF  = 5           # secondes, doublé à chaque retry
DELAY_WP       = 3
DELAY_NOMIN    = 1.3

# ── Notifications email (mêmes identifiants que daily_import.py) ──
EMAIL_ENABLED  = True
SMTP_HOST      = "smtp.gmail.com"
SMTP_PORT      = 587
SMTP_USER      = "typgraf@gmail.com"
SMTP_PASSWORD  = os.environ.get("SMTP_PASSWORD", "jupo hnqx xlhn eegt")
EMAIL_FROM     = SMTP_USER
EMAIL_TO       = "typgraf@gmail.com"

_HERE          = Path(__file__).parent
COUNTRIES_FILE = _HERE / "hyrox_countries.json"
STATE_FILE     = _HERE / "hyrox_state.json"
RAW_FILE       = _HERE / "hyrox_raw.json"
LOGS_DIR       = _HERE / "logs"

# ── Taxonomies WP (mêmes IDs que daily_import.py / cc_import.py) ──
TYPE_HYROX   = 238
LOC_COUNTRY  = {"france": 141, "suisse": 143, "belgique": 142, "allemagne": 241,
                "italie": 244, "espagne": 242, "pays-bas": 240}
# code ISO 3166-1 alpha-2 (renvoyé par Nominatim) → nom taxonomie event_loc
COUNTRY_CODE_TO_NAME = {
    "FR": "France", "BE": "Belgique", "CH": "Suisse", "DE": "Allemagne",
    "IT": "Italie", "ES": "Espagne", "NL": "Pays-bas",
    "LU": "Luxembourg", "MC": "Monaco", "AD": "Andorre",
}
# nom FR → nom anglais tel qu'il peut apparaître dans l'adresse scrapée
# (hyrox.com est en anglais) — sert à éviter un pays dupliqué dans l'adresse
COUNTRY_FR_TO_EN = {
    "Belgique": "Belgium", "Suisse": "Switzerland", "Allemagne": "Germany",
    "Italie": "Italy", "Espagne": "Spain", "Pays-bas": "Netherlands",
}
LOC_REGION   = {
    "auvergne-rhone-alpes": 153, "bourgogne-franche-comte": 151,
    "bretagne": 148,              "centre-val-de-loire": 150,
    "corse": 156,                 "grand-est": 147,
    "hauts-de-france": 144,       "ile-de-france": 146,
    "normandie": 145,             "nouvelle-aquitaine": 152,
    "occitanie": 154,             "pays-de-la-loire": 149,
    "provence-alpes-cote-dazur": 155,
    "provence-alpes-cote-d-azur": 155,
}
CAT_INDIVIDUEL = 136   # pas de distinction team/solo fiable sur la page liste

# Catégories HYROX standard : chaque event officiel propose Solo, Doubles
# et Relay (4) — pas de détection au cas par cas, ce sont les formats fixes
# de la compétition (demande utilisateur, cf. retour sur les brouillons).
EVENT_CAT_HYROX = [136, 137, 162]   # individuel, team-2, team-4

TAG_HYROX  = 228
LEVEL_TAGS = [204, 248, 203, 191]    # RX, Inter, Scaled, Débutant — HYROX
                                      # est ouvert à tous les niveaux

# ── Tags villes : mêmes IDs/coordonnées que daily_import.py/cc_import.py.
# Ne couvre que les villes FR/BE déjà taguées sur le site — un event en
# Allemagne/Italie/Espagne n'aura simplement pas de tag ville, sans impact.
MAX_CITY_DIST_KM = 80
CITY_TAGS: dict[int, tuple[float, float]] = {
    166: (43.6047,  1.4442), 167: (44.8378, -0.5792), 168: (43.2965,  5.3698),
    180: (42.6887,  2.8948), 181: (46.1591, -1.1520), 182: (43.6119,  3.8772),
    183: (47.7508,  7.3359), 186: (48.8566,  2.3522), 187: (49.8941,  2.2958),
    190: (50.6292,  3.0573), 194: (48.5734,  7.7521), 205: (43.7102,  7.2620),
    207: (43.8367,  4.3601), 211: (45.7640,  4.8357), 212: (45.8336,  1.2611),
    213: (48.2973,  4.0744), 216: (48.1173, -1.6778), 217: (48.0793,  7.3586),
    218: (43.9493,  4.8055), 220: (49.4432,  1.0993), 222: (47.2380,  6.0243),
    223: (46.8122,  1.6941), 226: (47.3941,  0.6848), 227: (45.7797,  3.0863),
    230: (47.3220,  5.0415), 231: (47.9960,  0.1966), 232: (45.8992,  6.1294),
    233: (49.1829, -0.3707), 234: (45.1885,  5.7245), 245: (48.3904, -4.4861),
    246: (47.2184, -1.5536), 249: (49.6333, -1.6167), 250: (48.2658,  2.6939),
    251: (43.2951, -0.3708), 252: (43.4921, -1.4742), 253: (50.2917,  2.7819),
    254: (45.4347,  4.3900), 257: (43.6045,  2.2478), 258: (43.2130,  2.3491),
    261: (43.4832, -1.5586), 274: (47.4784, -0.5632), 275: (48.6921,  6.1844),
    276: (44.9334,  4.8924), 277: (46.5802,  0.3404), 281: (49.4938,  0.1079),
    282: (47.9029,  1.9039), 283: (44.5594,  6.0773), 284: (45.6757,  6.3928),
    285: (49.2583,  4.0317), 286: (44.8500,  0.4833), 287: (50.4333,  2.8333),
    288: (49.4144,  2.8231), 289: (49.8483,  3.2847), 290: (43.6939,  5.5030),
    291: (51.0340,  2.3776), 292: (46.9897,  3.1572), 294: (47.0810,  2.3988),
    296: (48.6493, -2.0097), 297: (43.2727,  6.6406), 298: (43.1258,  5.9306),
    299: (46.3240, -0.4617), 300: (49.5635,  3.6197), 301: (48.0698, -0.7687),
    303: (44.0183,  1.3550), 304: (50.3581,  3.5234), 306: (46.2044,  6.1432),
    308: (43.1840,  3.0003), 309: (47.7980,  3.5680), 311: (50.8503,  4.3517),
    313: (45.5646,  5.9178), 315: (49.0249,  1.1516), 319: (49.1193,  6.1727),
    320: (50.7272,  1.6150), 322: (50.9513,  1.8587),
}

def _haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlng = math.radians(lng2 - lng1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
         * math.sin(dlng / 2) ** 2)
    return R * 2 * math.asin(math.sqrt(a))

def nearest_city_tag(lat_s: str, lng_s: str) -> int | None:
    try:
        lat, lng = float(lat_s), float(lng_s)
    except (ValueError, TypeError):
        return None
    best_id, best_dist = None, MAX_CITY_DIST_KM
    for tag_id, (clat, clng) in CITY_TAGS.items():
        d = _haversine_km(lat, lng, clat, clng)
        if d <= best_dist:
            best_id, best_dist = tag_id, d
    return best_id

# ── Traduction des noms de ville (Nominatim renvoie le nom local — ex.
# "Barcelona", "Milano", "Köln" — pas l'exonyme français attendu en titre).
# Liste volontairement limitée aux villes déjà rencontrées + grandes villes
# probables des pays suivis ; fallback = nom local si absent (sans danger,
# juste moins idiomatique).
FRENCH_CITY_NAMES = {
    "Barcelona": "Barcelone", "Milano": "Milan", "Roma": "Rome",
    "Torino": "Turin", "Napoli": "Naples", "Venezia": "Venise",
    "Firenze": "Florence", "Genova": "Gênes", "Verona": "Vérone",
    "München": "Munich", "Köln": "Cologne", "Frankfurt am Main": "Francfort",
    "Frankfurt": "Francfort", "Hamburg": "Hambourg", "Basel": "Bâle",
    "Zürich": "Zurich", "Wien": "Vienne", "Gent": "Gand",
    "Mechelen": "Malines", "Antwerpen": "Anvers", "Brugge": "Bruges",
    "Luik": "Liège", "Den Haag": "La Haye", "València": "Valence",
    "Sevilla": "Séville",
}

def french_city_name(local_name: str) -> str:
    return FRENCH_CITY_NAMES.get(local_name, local_name)

# ── Mois abrégés du site (anglais, avec point) → numéro ────
MONTH_MAP = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
_DATE_RE = re.compile(r'(\d{1,2})\.\s*([A-Za-z]+)\.?\s*(\d{4})')

# Détecte un suffixe de saison en fin de slug (ex: "-s26-27", "-26-27",
# "-0912") pour obtenir une clé de ville stable même si HYROX change le
# suffixe d'une édition à l'autre. Heuristique documentée dans le brief
# (section 3) : le slug seul n'est PAS une clé stable.
_SEASON_SUFFIX_RE = re.compile(r'-s?\d{2,4}(-\d{2,4})?$', re.IGNORECASE)


# ═══════════════════════════════════════════════════════════
# ▌ Logging
# ═══════════════════════════════════════════════════════════
LOGS_DIR.mkdir(exist_ok=True)
log_file = LOGS_DIR / f"hyrox_{datetime.now():%Y-%m-%d}.log"
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
def to_slug(text: str) -> str:
    nfkd = unicodedata.normalize("NFD", text.lower())
    ascii_ = "".join(c for c in nfkd if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z0-9]+", "-", ascii_).strip("-")

def normalize_date(raw: str) -> str:
    """'3. Feb. 2027' → '2027-02-03'. Vide si non parsable."""
    if not raw:
        return ""
    m = _DATE_RE.search(raw)
    if not m:
        return ""
    day, mon_txt, year = m.groups()
    month = MONTH_MAP.get(mon_txt[:3].lower())
    if not month:
        return ""
    try:
        return date(int(year), month, int(day)).isoformat()
    except ValueError:
        return ""

def compute_event_days(start_iso: str, end_iso: str) -> str:
    """'2027-02-03'/'2027-02-07' → '1770076800-1770163200-...' (timestamps
    minuit UTC, un par jour) — même format que daily_import.py."""
    if not start_iso or not end_iso:
        return ""
    s, e = date.fromisoformat(start_iso), date.fromisoformat(end_iso)
    days, cur = [], s
    while cur <= e and len(days) < 30:
        ts = int(datetime(cur.year, cur.month, cur.day).timestamp())
        days.append(str(ts))
        cur += timedelta(days=1)
    return "-".join(days) + "-" if days else ""

def calendar_meta(start_iso: str, end_iso: str) -> list:
    """Structure OVA pour ova_mb_event_calendar (liste, envoyée telle
    quelle via REST — cf. commentaire équivalent dans daily_import.py)."""
    if not start_iso:
        return []
    return [{
        "calendar_id":         str(int(time.time())),
        "date":                start_iso,
        "end_date":            end_iso or start_iso,
        "start_time":          "09:00",
        "end_time":            "18:00",
        "book_before_minutes": "0",
    }]

def slug_from_url(url: str) -> str:
    m = re.search(r'/event/([a-z0-9-]+)/?', url)
    return m.group(1) if m else ""

_CITY_GUESS_RE = re.compile(r'hyrox\s*(?:youngstars\s*)?(.*)$', re.IGNORECASE)

def city_guess(title: str) -> str:
    """Extrait le nom de ville probable du titre (ex: 'INTERSPORT HYROX
    BORDEAUX' → 'BORDEAUX'). Fiable sur ~98% des titres observés (le
    sponsor est toujours avant 'HYROX', la ville toujours après) — sert
    uniquement de requête de géocodage, jamais stocké tel quel comme nom
    officiel (cf. section 1 du brief : le titre n'est pas une source fiable
    pour le nom de ville seul)."""
    m = _CITY_GUESS_RE.search(title)
    guess = m.group(1).strip() if m else title
    return re.split(r'\s*\|\s*', guess)[0].strip()   # coupe "| Season 26/27"

def ville_normalisee(city_code: str, slug: str) -> str:
    """Clé de correspondance stable : code ville + slug sans le suffixe
    de saison. Gère le cas de plusieurs events distincts dans la même
    ville (ex: Paris classique, Paris Grand-Palais, Youngstars Paris)."""
    base = _SEASON_SUFFIX_RE.sub("", slug)
    return f"{city_code.lower()}-{base}"

def event_slug(title: str) -> str:
    """Le titre WP contient déjà HYROX + ville (+ qualificatif éventuel),
    le slug s'en déduit directement — pas besoin de suffixe supplémentaire."""
    return to_slug(title)

def venue_qualifier(city_guess_raw: str, city_local: str) -> str:
    """Récupère un éventuel qualificatif de lieu au-delà du nom de ville
    reconnu par le géocodage (ex: 'PARIS GRAND-PALAIS' + 'Paris' →
    'Grand-Palais') — nécessaire pour distinguer plusieurs events HYROX
    dans la même ville (ex: Paris classique vs Paris Grand-Palais)."""
    g, cl = city_guess_raw.strip(), city_local.strip()
    if cl and g.upper().startswith(cl.upper()):
        rest = g[len(cl):].strip(" -")
        if rest:
            return rest.title()
    return ""

def expected_title(ev: dict, city_name: str) -> str:
    """Titre WP tel que build_post() le générera — calculable sans fetch de
    la page détail (city_guess/venue_qualifier ne dépendent que du titre
    hyrox.com et du nom de ville déjà géocodé). Source unique utilisée à la
    fois pour la construction réelle et pour la pré-vérification anti-
    doublon dans main()."""
    city_fr = french_city_name(city_name)
    qualifier = venue_qualifier(city_guess(ev["nom_event"]), city_name)
    return f"HYROX{' Youngstars' if ev['is_youngstars'] else ''} {city_fr}" \
           f"{' ' + qualifier if qualifier else ''}".strip()


# ═══════════════════════════════════════════════════════════
# ▌ Fetch + parsing hyrox.com
# ═══════════════════════════════════════════════════════════
def fetch_hyrox_html() -> str:
    """GET avec retry/backoff (section 7 du brief)."""
    last_err = None
    delay = FETCH_BACKOFF
    for attempt in range(1, FETCH_RETRIES + 1):
        try:
            r = requests.get(HYROX_LIST_URL,
                             headers={"User-Agent": USER_AGENT}, timeout=20)
            r.raise_for_status()
            return r.text
        except Exception as e:
            last_err = e
            log.warning(f"  [fetch] tentative {attempt}/{FETCH_RETRIES} échouée : {e}")
            if attempt < FETCH_RETRIES:
                time.sleep(delay)
                delay *= 2
    raise RuntimeError(f"Échec fetch hyrox.com après {FETCH_RETRIES} tentatives : {last_err}")

def parse_hyrox_events(html: str) -> list[dict]:
    """Extrait chaque <article class="... event ..."> de la page liste."""
    soup = BeautifulSoup(html, "html.parser")
    events = []
    for art in soup.select("article.event"):
        link = art.select_one("h2 a")
        if not link:
            continue
        title = link.get_text(strip=True)
        url   = link.get("href", "").strip()
        slug  = slug_from_url(url)
        if not slug:
            continue

        img_el   = art.select_one("img")
        image_url = img_el.get("src", "") if img_el else ""

        code_el = art.select_one(".event_city_letter_code .w-post-elm-value")
        city_code = code_el.get_text(strip=True) if code_el else ""

        d1_el = art.select_one(".event_date_1 .w-post-elm-value")
        d3_el = art.select_one(".event_date_3 .w-post-elm-value")
        date_debut = normalize_date(d1_el.get_text(strip=True) if d1_el else "")
        date_fin   = normalize_date(d3_el.get_text(strip=True) if d3_el else "") or date_debut

        btn_el = art.select_one(".w-btn-label")
        statut_bouton = btn_el.get_text(strip=True) if btn_el else ""

        classes   = art.get("class", [])
        continent = next((c.replace("continent-", "") for c in classes
                          if c.startswith("continent-")), "")
        is_youngstars = "youngstars" in slug.lower()

        events.append({
            "nom_event":       title,
            "slug_hyrox":      slug,
            "url_event_hyrox": url,
            "image_url":       image_url,
            "city_code":       city_code,
            "date_debut":      date_debut,
            "date_fin":        date_fin,
            "statut_bouton":   statut_bouton,
            "continent":       continent,
            "is_youngstars":   is_youngstars,
        })
    return events


# ═══════════════════════════════════════════════════════════
# ▌ Filtrage géographique (pays réel, détecté par géocodage)
# ═══════════════════════════════════════════════════════════
def load_allowed_countries() -> set[str]:
    if not COUNTRIES_FILE.exists():
        log.warning(f"  [countries] {COUNTRIES_FILE} introuvable, aucun filtrage.")
        return set()
    data = json.loads(COUNTRIES_FILE.read_text(encoding="utf-8"))
    return {c.upper() for c in data.get("codes_pays", [])}

def filter_by_country(events: list[dict], allowed: set[str]) -> list[dict]:
    """Géocode le nom de ville extrait du titre (city_guess) et ne garde
    que les events dont le pays réel est dans la whitelist. Pré-filtre sur
    continent=="europe" pour éviter de géocoder ~60 events hors zone à
    chaque run (aucun des pays suivis n'est ailleurs qu'en Europe)."""
    kept = []
    candidates = [e for e in events if e.get("continent") == "europe"]
    log.info(f"    → {len(candidates)} events europe à géocoder pour filtrage pays")
    for ev in candidates:
        query = city_guess(ev["nom_event"])
        geo = geocode_free(query)
        if not geo.get("country_code"):
            continue
        if geo["country_code"] not in allowed:
            continue
        ev["_geo"] = geo
        ev["_city_name"] = query
        kept.append(ev)
    return kept


# ═══════════════════════════════════════════════════════════
# ▌ State file
# ═══════════════════════════════════════════════════════════
def load_state() -> dict:
    if not STATE_FILE.exists():
        return {}
    return json.loads(STATE_FILE.read_text(encoding="utf-8"))

def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2),
                          encoding="utf-8")


# ═══════════════════════════════════════════════════════════
# ▌ Géocodage Nominatim — sans contrainte de pays (le pays réel
# ▌ renvoyé sert justement à filtrer, cf. filter_by_country)
# ═══════════════════════════════════════════════════════════
def geocode_free(query: str) -> dict:
    time.sleep(DELAY_NOMIN)
    try:
        r = requests.get("https://nominatim.openstreetmap.org/search",
                         params={"q": query, "format": "json",
                                 "limit": 1, "addressdetails": 1},
                         headers={"User-Agent": "wod-open-import/1.0"},
                         timeout=10)
        data = r.json()
        if not data:
            return {}
        d = data[0]
        lat, lng = float(d["lat"]), float(d["lon"])
        addr  = d.get("address", {})
        state = addr.get("state") or addr.get("county") or ""
        city  = (addr.get("city") or addr.get("town") or addr.get("village")
                 or addr.get("municipality") or "")
        return {
            "lat": str(lat), "lng": str(lng),
            "country_code": (addr.get("country_code") or "").upper(),
            "state_slug":   to_slug(state) if state else "",
            "city_name":    city,
        }
    except Exception as e:
        log.warning(f"    [geocode] {query} : {e}")
        return {}


# ═══════════════════════════════════════════════════════════
# ▌ Page event individuelle : description, adresse, prix
# ═══════════════════════════════════════════════════════════
# Le prix billet vient de la page billetterie tierce (sous-domaine
# {pays}.hyrox.com), liée en clair dans le HTML de la page event dès que
# les inscriptions sont ouvertes (absente sinon — donc pas de fetch inutile
# tant que le statut est "Find out more"). Cette page est en Next.js avec
# SSR : les tickets (nom, prix, devise) sont dans le JSON __NEXT_DATA__,
# aucun rendu JS nécessaire. Vérifié sur Genève (80 CHF relay — correspond
# exactement au tarif déjà affiché manuellement sur l'ancienne page
# wod-open) et Bordeaux (EUR). On exclut spectateurs/tarifs solidaires/
# options photo pour ne garder que le tarif athlète le plus bas.
_TICKET_URL_RE = re.compile(r'https://[a-z]+\.hyrox\.com/event/[a-z0-9-]+\?useEmbed=true')
_PRICE_EXCLUDE_RE = re.compile(r'spectator|charity|photo|package', re.IGNORECASE)

def fetch_ticket_price(event_html: str) -> str:
    """'80 - 129 CHF' (fourchette, même convention que daily_import.py/
    cc_import.py) ou '' si billetterie pas encore ouverte /
    structure inattendue (jamais bloquant, jamais de prix inventé)."""
    m = _TICKET_URL_RE.search(event_html)
    if not m:
        return ""
    try:
        r = requests.get(m.group(0), headers={"User-Agent": USER_AGENT}, timeout=20)
        r.raise_for_status()
        nd = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', r.text, re.DOTALL)
        if not nd:
            return ""
        data = json.loads(nd.group(1))
        pp = data["props"]["pageProps"]
        tickets = pp.get("shop", {}).get("tickets", [])
        currency = pp.get("seller", {}).get("currency", "")
        prices = [t["price"] for t in tickets
                 if isinstance(t.get("price"), (int, float)) and t["price"] > 0
                 and not _PRICE_EXCLUDE_RE.search(t.get("name") or "")]
        if not prices or not currency:
            return ""
        lo, hi = min(prices), max(prices)
        return f"{lo:g} - {hi:g} {currency}" if lo != hi else f"{lo:g} {currency}"
    except Exception as e:
        log.warning(f"    [ticket price] {e}")
        return ""

def fetch_event_detail(event_url: str) -> dict:
    """Récupère description officielle (texte brut), adresse précise du
    lieu (section "Venue Information") et prix (billetterie tierce, si
    ouverte) depuis la page event individuelle."""
    try:
        r = requests.get(event_url, headers={"User-Agent": USER_AGENT}, timeout=15)
        r.raise_for_status()
    except Exception as e:
        log.warning(f"    [detail] {event_url} : {e}")
        return {"description": "", "venue_address": "", "price": ""}

    soup = BeautifulSoup(r.text, "html.parser")

    desc_el = soup.select_one(".event_description")
    description = desc_el.get_text("\n", strip=True) if desc_el else ""

    addr_el = soup.select_one(".event_map_address .w-post-elm-value")
    venue_address = addr_el.get_text(strip=True) if addr_el else ""

    price = fetch_ticket_price(r.text)

    return {"description": description, "venue_address": venue_address, "price": price}


# ═══════════════════════════════════════════════════════════
# ▌ Reformulation de la description (API Claude)
# ═══════════════════════════════════════════════════════════
_anthropic_client = None
if ANTHROPIC_OK and ANTHROPIC_API_KEY:
    _anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

_MOIS_FR = ["", "janvier", "février", "mars", "avril", "mai", "juin", "juillet",
           "août", "septembre", "octobre", "novembre", "décembre"]

def format_date_range_fr(date_debut: str, date_fin: str) -> str:
    """'2027-05-12'/'2027-05-16' → 'du 12 au 16 mai 2027' (même mois) ou
    'du 30 septembre au 4 octobre 2026' (mois différents)."""
    if not date_debut:
        return ""
    d1 = date.fromisoformat(date_debut)
    d2 = date.fromisoformat(date_fin) if date_fin else d1
    if d1 == d2:
        return f"le {d1.day} {_MOIS_FR[d1.month]} {d1.year}"
    if d1.month == d2.month and d1.year == d2.year:
        return f"du {d1.day} au {d2.day} {_MOIS_FR[d1.month]} {d1.year}"
    if d1.year == d2.year:
        return (f"du {d1.day} {_MOIS_FR[d1.month]} au "
                f"{d2.day} {_MOIS_FR[d2.month]} {d2.year}")
    return (f"du {d1.day} {_MOIS_FR[d1.month]} {d1.year} au "
            f"{d2.day} {_MOIS_FR[d2.month]} {d2.year}")

def _fallback_description(city_fr: str, dates_txt: str, venue_address: str) -> str:
    """Repli sans IA (clé absente ou appel échoué) : structure HTML minimale
    mais correcte, jamais de blocage de la création pour ce seul motif."""
    lieu = f"<br><strong>📍 Lieu : {venue_address}</strong>" if venue_address else ""
    return (
        f"<h2><strong>HYROX {city_fr}</strong></h2>"
        f"<p><strong>📅 Dates : {dates_txt}</strong>{lieu}</p>"
        f"<p>Compétition HYROX officielle à {city_fr}. Formats Singles Open, "
        f"Pro, Doubles et Relay — informations et billetterie sur le site "
        f"officiel HYROX.</p>"
    )

def generate_description(city_fr: str, date_debut: str, date_fin: str,
                         venue_address: str, statut_bouton: str,
                         official_text: str) -> str:
    """Reformule la description officielle HYROX en français, dans le
    format maison de wod-open.com (H2/H3, listes, gras) — cf. exemple
    fourni par l'utilisateur (hyrox-lyon-2026). Jamais une traduction/copie
    verbatim du texte source (duplicate content SEO). Repli silencieux si
    la clé API est absente ou l'appel échoue : ne doit jamais bloquer la
    création de l'event."""
    dates_txt = format_date_range_fr(date_debut, date_fin) or "à venir"
    if not (_anthropic_client and official_text):
        return _fallback_description(city_fr, dates_txt, venue_address)

    inscriptions = ("Inscriptions ouvertes"
                    if statut_bouton == "Buy Tickets"
                    else "Ouverture des inscriptions : bientôt")

    prompt = f"""Tu rédiges le texte de présentation d'une compétition HYROX pour wod-open.com, un site français qui référence les compétitions CrossFit/Hyrox/hybrid race. Voici un exemple RÉEL du format maison du site (event HYROX Lyon 2026), à reproduire EXACTEMENT dans sa structure HTML :

<h2><strong>HYROX LYON 2026 – La 1re édition débarque dans la capitale des Gaules !</strong></h2>
<p><strong>📅 Dates : du 21 au 24 mai 2026</strong><br><strong>📍 Lieu : Bd de l'Europe, 69680 Chassieu (Lyon)</strong></p>
<p><strong>Nouvelle ville, nouvelle énergie.</strong><br>Pour la <strong>première fois</strong>, <strong>HYROX pose ses valises à Lyon</strong> ! Quatre jours de pure intensité t'attendent, dans une ambiance électrique qui promet de marquer l'histoire de la discipline en France.</p>
<p>Que tu sois un athlète confirmé en quête de podium ou un curieux prêt à vivre <strong>ton premier défi HYROX</strong>, c'est le moment de te lancer :</p>
<ul>
<li><strong>Singles Open</strong> – pour découvrir le format à ton rythme</li>
<li><strong>Pro</strong> – pour te confronter aux meilleurs</li>
<li><strong>Doubles</strong> – en duo pour partager chaque effort</li>
<li><strong>Relay</strong> – en équipe de 4 pour une course stratégique</li>
</ul>
<hr>
<h3><strong>Infos clés</strong></h3>
<ul>
<li><strong>Ouverture des inscriptions : très bientôt</strong></li>
<li><strong>Start time & infos athlètes :</strong> disponibles 3 jours avant l'événement.</li>
<li><strong>Adresse :</strong> Bd de l'Europe, 69680 Chassieu (Lyon).</li>
</ul>
<hr>
<h3><strong>Pourquoi participer à HYROX Lyon ?</strong></h3>
<ul>
<li>Une <strong>première édition historique</strong> dans une ville sportive et dynamique.</li>
<li>Un format unique qui combine <strong>course + functional training</strong>.</li>
<li>Une ambiance incroyable où la communauté HYROX te pousse à tout donner.</li>
</ul>
<hr>
<p><strong>👉 Tu veux faire partie de l'histoire ?</strong><br>Rejoins cette <strong>première édition lyonnaise</strong>, que ce soit en solo ou en équipe.<br><strong>Les places vont partir vite – reste attentif à l'ouverture des inscriptions !</strong></p>

Maintenant rédige la même structure pour un NOUVEL event, avec ces faits (ne rien inventer au-delà) :
- Ville : {city_fr}
- Dates : {dates_txt}
- Adresse : {venue_address or "non communiquée"}
- Statut inscriptions : {inscriptions}
- Formats : Singles Open, Pro, Doubles, Relay (formats standards HYROX)

Voici la description officielle anglaise de hyrox.com pour cet event, à utiliser comme SOURCE D'INSPIRATION UNIQUEMENT (ton, angle, éventuelle mention de numéro d'édition si elle y figure) — ne la traduis PAS littéralement, le texte final doit être différent phrase par phrase (duplicate content SEO) :

---
{official_text}
---

Contraintes :
- Reproduis exactement la structure HTML de l'exemple (h2, p, ul/li, hr, strong, br) — balises HTML brutes, pas de markdown.
- Adapte le titre et les accroches à {city_fr} (pas de "1re édition" si le texte source ne le confirme pas — utilise une formulation neutre si le numéro d'édition n'est pas certain).
- Section "Infos clés" : reprends {inscriptions} tel quel pour la ligne inscriptions.
- N'invente aucun fait (numéro d'édition, prix, détails logistiques) absent des données fournies ou du texte source.
- Réponds UNIQUEMENT avec le HTML, sans commentaire ni balises ```html."""

    try:
        response = _anthropic_client.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=1400,
            messages=[{"role": "user", "content": prompt}],
        )
        text = next((b.text for b in response.content if b.type == "text"), "")
        text = re.sub(r'^```html\s*|\s*```$', '', text.strip())
        return text or _fallback_description(city_fr, dates_txt, venue_address)
    except Exception as e:
        log.warning(f"    [IA description] {e}")
        return _fallback_description(city_fr, dates_txt, venue_address)


# ═══════════════════════════════════════════════════════════
# ▌ WordPress REST API (même pattern que daily_import.py)
# ═══════════════════════════════════════════════════════════
def wp_rest(method: str, endpoint: str, **kwargs) -> dict:
    time.sleep(DELAY_WP)
    url = f"{REST_URL}/{endpoint}"
    r = getattr(requests, method)(url, auth=REST_AUTH, timeout=30, **kwargs)
    r.raise_for_status()
    return r.json()

def fetch_wp_slugs() -> set[str]:
    """Filet de sécurité : slugs de tous les events WP existants (tous
    statuts). Si le state file est perdu ou corrompu (ex: disque plein
    en fin de run), ça évite de recréer un doublon — WP ne rejette pas
    un slug déjà pris, il en génère un autre en silence sinon."""
    slugs: set[str] = set()
    page = 1
    while True:
        try:
            r = requests.get(
                f"{REST_URL}/events", auth=REST_AUTH,
                params={"per_page": 100, "page": page,
                        "status": "publish,draft,pending,private,future,trash",
                        "_fields": "slug"},
                timeout=30)
            if r.status_code in (400, 404):
                break
            r.raise_for_status()
            data = r.json()
            if not data:
                break
            slugs.update(ev.get("slug", "") for ev in data if ev.get("slug"))
            if len(data) < 100:
                break
            page += 1
        except Exception as e:
            log.warning(f"  [fetch_wp_slugs page={page}] {e}")
            break
    return slugs

def fetch_existing_hyrox_titles() -> list[tuple[int, str]]:
    """(id, titre) de tous les events WP dont le titre/contenu contient
    "hyrox" — détection fuzzy de doublon quand le slug généré ne collisionne
    pas littéralement avec une page déjà créée manuellement sous un autre
    nom (cf. fetch_wp_slugs, qui ne suffit pas seul, incident 20/08/2026)."""
    out: list[tuple[int, str]] = []
    page = 1
    while True:
        try:
            r = requests.get(
                f"{REST_URL}/events", auth=REST_AUTH,
                params={"per_page": 100, "page": page, "search": "hyrox",
                        "status": "publish,draft,pending,private,future",
                        "_fields": "id,title"},
                timeout=30)
            if r.status_code in (400, 404):
                break
            r.raise_for_status()
            data = r.json()
            if not data:
                break
            out.extend((ev["id"], ev.get("title", {}).get("rendered", "")) for ev in data)
            if len(data) < 100:
                break
            page += 1
        except Exception as e:
            log.warning(f"  [fetch_existing_hyrox_titles page={page}] {e}")
            break
    return out

def upload_image(image_url: str, slug: str, title: str) -> int | None:
    if not image_url:
        return None
    try:
        r = requests.get(image_url, headers={"User-Agent": USER_AGENT}, timeout=30)
        r.raise_for_status()
        raw = r.content
    except Exception as e:
        log.warning(f"    [IMG download] {e}")
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
            image_data, mime = buf.getvalue(), "image/png"
        except Exception:
            image_data, mime = raw, "image/jpeg"
    else:
        image_data, mime = raw, "image/jpeg"

    if DRY_RUN:
        log.info(f"    [DRY] upload image {filename}")
        return None
    try:
        result = wp_rest("post", "media",
            headers={"Content-Disposition": f'attachment; filename="{filename}"',
                     "Content-Type": mime},
            data=image_data)
        media_id = result.get("id")
        if media_id:
            wp_rest("patch", f"media/{media_id}", json={"alt_text": title})
        return media_id
    except Exception as e:
        log.warning(f"    [IMG upload] {e}")
        return None


# ═══════════════════════════════════════════════════════════
# ▌ Construction + création du post WP
# ═══════════════════════════════════════════════════════════
def build_post(ev: dict, city_fr: str, country_name: str, geo: dict, detail: dict,
               qualifier: str = "") -> dict:
    ts_start = int(datetime.fromisoformat(ev["date_debut"]).timestamp()) if ev["date_debut"] else 0
    ts_end   = int(datetime.fromisoformat(ev["date_fin"]).timestamp())   if ev["date_fin"]   else 0

    venue_addr = detail.get("venue_address") or ""
    if venue_addr:
        # évite "..., Switzerland, Suisse" : l'adresse scrapée est en
        # anglais, le pays qu'on ajoute est en français
        english_name = COUNTRY_FR_TO_EN.get(country_name, country_name)
        already_there = (country_name.lower() in venue_addr.lower()
                         or english_name.lower() in venue_addr.lower())
        map_addr = venue_addr if already_there else f"{venue_addr}, {country_name}"
    else:
        map_addr = f"{city_fr}, {country_name}"

    loc_terms: set[int] = set()
    country_tid = LOC_COUNTRY.get(country_name.lower())
    if country_tid:
        loc_terms.add(country_tid)
    if country_name.lower() == "france" and geo.get("state_slug"):
        region_tid = LOC_REGION.get(geo["state_slug"])
        if region_tid:
            loc_terms.add(region_tid)

    tag_ids = [TAG_HYROX] + LEVEL_TAGS
    city_tag = nearest_city_tag(geo.get("lat", ""), geo.get("lng", ""))
    if city_tag:
        tag_ids.append(city_tag)

    title = f"HYROX{' Youngstars' if ev['is_youngstars'] else ''} {city_fr}" \
            f"{' ' + qualifier if qualifier else ''}".strip()
    description = generate_description(city_fr, ev["date_debut"], ev["date_fin"],
                                       venue_addr, ev["statut_bouton"],
                                       detail.get("description", ""))
    price = detail.get("price") or "NC"

    meta = {
        "ova_mb_event_start_date_str":             str(ts_start),
        "ova_mb_event_end_date_str":               str(ts_end),
        "ova_mb_event_address":                    map_addr,
        "ova_mb_event_map_address":                map_addr,
        "ova_mb_event_ticket_external_link":       ev["url_event_hyrox"],
        "ova_mb_event_time_zone":                  "Europe/Paris",
        "ova_mb_event_event_type":                 "classic",
        "ova_mb_event_info_organizer":             "checked",
        "ova_mb_event_allow_cancellation_booking": "no",
        "ova_mb_event_ticket_link":                "ticket_external_link",
        "ova_mb_event_option_calendar":            "manual",
        "ova_mb_event_event_days":                 compute_event_days(ev["date_debut"], ev["date_fin"]),
        "ova_mb_event_calendar":                   calendar_meta(ev["date_debut"], ev["date_fin"]),
        "ova_mb_event_name_organizer":             "HYROX",
        "ova_mb_event_phone_organizer":            "NC",
        "ova_mb_event_mail_organizer":              "NC",
        "ova_mb_event_price_desc":                 price,
        # Sans ce champ, eventlist/templates/loop/thumbnail.php fait
        # array_unshift() sur une chaîne vide (get_post_meta() pour une clé
        # jamais posée) → fatal error 500 sur les pages "événements liés"
        # affichant cet event. Incident du 20/08/2026.
        "ova_mb_event_gallery":                    [],
    }
    if geo.get("lat"):
        meta["ova_mb_event_map_lat"] = geo["lat"]
        meta["ova_mb_event_map_lng"] = geo["lng"]

    return {
        "status":     POST_STATUS,
        "title":      title,
        "slug":       event_slug(title),
        "content":    description,
        "event_type": [TYPE_HYROX],
        "event_cat":  EVENT_CAT_HYROX,
        "event_loc":  list(loc_terms),
        "event_tag":  tag_ids,
        "meta":       meta,
    }

def _build_payload(ev: dict, city_name: str, country_name: str, geo: dict) -> dict:
    """Fetch détail + géocodage précis + construction du payload complet —
    factorisé entre création (create_wp_event) et rafraîchissement d'édition
    (refresh_wp_event), pour que les deux produisent exactement le même
    contenu à partir des mêmes données fraîchement scrapées."""
    city_fr = french_city_name(city_name)
    qualifier = venue_qualifier(city_guess(ev["nom_event"]), city_name)
    detail = fetch_event_detail(ev["url_event_hyrox"])

    # Re-géocode avec l'adresse précise du lieu (Venue Information) si
    # trouvée — bien plus précis que le géocodage ville seule fait lors
    # du filtrage pays. Repli sur ce dernier si l'adresse ne géocode pas.
    if detail.get("venue_address"):
        precise_geo = geocode_free(f"{detail['venue_address']}, {country_name}")
        if precise_geo.get("lat"):
            geo = precise_geo
            log.info(f"    📍 {detail['venue_address']} → {geo['lat']}, {geo['lng']}")
    elif geo.get("lat"):
        log.info(f"    📍 {city_fr}, {country_name} → {geo['lat']}, {geo['lng']}")

    return build_post(ev, city_fr, country_name, geo, detail, qualifier)

def create_wp_event(ev: dict, city_name: str, country_name: str, geo: dict) -> int | None:
    payload = _build_payload(ev, city_name, country_name, geo)

    if DRY_RUN:
        log.info(f"    [DRY] newPost → {payload['title']}")
        return 0

    try:
        result = wp_rest("post", "events", json=payload)
        wp_id = int(result["id"])
        log.info(f"    ✓ créé wp_id={wp_id}")
    except Exception as e:
        log.error(f"    [ERR newPost] {e}")
        return None

    media_id = upload_image(ev["image_url"], payload["slug"], payload["title"])
    if media_id and not DRY_RUN:
        try:
            wp_rest("patch", f"events/{wp_id}", json={"featured_media": media_id})
            log.info(f"    🖼️  image {media_id} OK")
        except Exception as e:
            log.warning(f"    [image patch] {e}")

    return wp_id

def refresh_wp_event(wp_id: int, ev: dict, city_name: str, country_name: str, geo: dict) -> bool:
    """Nouvelle édition (le slug HYROX a changé, même ville) : ré-applique
    tout le payload (titre, dates, calendrier, adresse, contenu, tags) sur
    la page existante — page "evergreen" par ville, cf. brief section 3.
    L'image à la une n'est PAS retouchée (conservée d'une édition à l'autre).

    Ne touche JAMAIS au statut : si la page a été publiée entre-temps
    (validation manuelle), un rafraîchissement de saison ne doit pas la
    repasser en brouillon. status n'est pertinent qu'à la création."""
    payload = _build_payload(ev, city_name, country_name, geo)
    payload.pop("status", None)
    if DRY_RUN:
        log.info(f"    [DRY] refresh wp_id={wp_id} → {payload['title']}")
        return True
    try:
        wp_rest("patch", f"events/{wp_id}", json=payload)
        return True
    except Exception as e:
        log.error(f"    [ERR refresh] {e}")
        return False

def update_wp_price(wp_id: int, price: str) -> None:
    if not wp_id or DRY_RUN:
        return
    try:
        wp_rest("patch", f"events/{wp_id}", json={
            "meta": {"ova_mb_event_price_desc": price or "NC"}
        })
    except Exception as e:
        log.warning(f"    [update prix] {e}")



# ═══════════════════════════════════════════════════════════
# ▌ Notification email
# ═══════════════════════════════════════════════════════════
def _send_email(subject: str, html: str) -> None:
    if not EMAIL_ENABLED:
        return
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"], msg["From"], msg["To"] = subject, EMAIL_FROM, EMAIL_TO
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

def notify_reactivation(items: list[dict]) -> None:
    """Un event HYROX vient d'ouvrir ses inscriptions."""
    if not items:
        return
    rows = "".join(
        f"<tr><td style='padding:4px 8px'>{it['nom_event']}</td>"
        f"<td style='padding:4px 8px'>{it.get('prix_connu') or '—'}</td>"
        f"<td style='padding:4px 8px'><a href='{it['url_event_hyrox']}'>hyrox.com</a></td></tr>"
        for it in items
    )
    html = f"""
    <html><body style='font-family:Arial,sans-serif;color:#333;max-width:700px'>
    <h2 style='border-bottom:2px solid #e74c3c;padding-bottom:8px'>
        🔔 Inscriptions HYROX ouvertes ({len(items)})
    </h2>
    <table border='1' cellspacing='0' style='border-collapse:collapse;font-size:13px'>
        <tr><th>Event</th><th>Prix</th><th>Lien</th></tr>{rows}
    </table>
    </body></html>"""
    _send_email(f"[wod-open] 🔔 {len(items)} inscription(s) HYROX ouverte(s)", html)

def send_summary_email(stats: dict, elapsed: float, warnings: list[str] | None = None) -> None:
    date_str = datetime.now().strftime("%d/%m/%Y %H:%M")
    status_color = "#27ae60" if stats["error"] == 0 else "#e74c3c"
    warn_html = ""
    if warnings:
        items = "".join(f"<li>{w}</li>" for w in warnings)
        warn_html = f"""
        <h3 style='color:#e67e22'>⚠️ Doublons possibles à vérifier ({len(warnings)})</h3>
        <ul style='font-size:13px;color:#555'>{items}</ul>"""
    html = f"""
    <html><body style='font-family:Arial,sans-serif;color:#333;max-width:700px'>
    <h2 style='border-bottom:2px solid {status_color};padding-bottom:8px'>
        Import HYROX officiel — {date_str}
    </h2>
    <table style='border-collapse:collapse'>
        <tr><td style='padding:4px 16px 4px 0'><b>✅ Nouveaux brouillons</b></td><td><b>{stats['created']}</b></td></tr>
        <tr><td style='padding:4px 16px 4px 0'>🔔 Réactivations (inscriptions ouvertes)</td><td><b>{stats['reactivated']}</b></td></tr>
        <tr><td style='padding:4px 16px 4px 0'>🔄 Nouvelles éditions rafraîchies</td><td><b>{stats['slug_updated']}</b></td></tr>
        <tr><td style='padding:4px 16px 4px 0'>⏭️ Inchangés</td><td><b>{stats['unchanged']}</b></td></tr>
        <tr><td style='padding:4px 16px 4px 0'>❌ Erreurs</td><td style='color:{"#e74c3c" if stats["error"] else "#27ae60"}'><b>{stats['error']}</b></td></tr>
        <tr><td style='padding:4px 16px 4px 0'>⏱️ Durée</td><td>{elapsed/60:.1f} min</td></tr>
    </table>
    {warn_html}
    <p style='font-size:11px;color:#999;margin-top:24px'>Log complet : {log_file}</p>
    </body></html>"""
    _send_email(
        f"[wod-open] Import HYROX du {date_str} — "
        f"{stats['created']} créés / {stats['reactivated']} réactivés",
        html)


# ═══════════════════════════════════════════════════════════
# ▌ Main
# ═══════════════════════════════════════════════════════════
def main():
    run_start = time.time()
    log.info("=" * 60)
    log.info(f"▶ Import HYROX officiel — {datetime.now():%d/%m/%Y %H:%M}")
    log.info(f"  DRY_RUN={DRY_RUN}  POST_STATUS={POST_STATUS}")
    log.info("=" * 60)

    # ── 1. Fetch + parse ────────────────────────────────────
    log.info("\n[1] Fetch hyrox.com/find-my-race...")
    html = fetch_hyrox_html()
    events = parse_hyrox_events(html)
    log.info(f"    {len(events)} events trouvés sur la page")
    RAW_FILE.write_text(json.dumps(events, ensure_ascii=False, indent=2), encoding="utf-8")

    # ── 2. Filtrage géographique (pays réel, par géocodage) ─
    allowed_countries = load_allowed_countries()
    log.info(f"    Pays suivis : {sorted(allowed_countries)}")
    filtered = filter_by_country(events, allowed_countries)
    log.info(f"    → {len(filtered)} après filtre pays")

    # ── 3. Comparaison au state file ────────────────────────
    log.info("\n[3] Comparaison au state file...")
    state = load_state()
    log.info("  Scan des slugs WP existants (filet de sécurité anti-doublon)...")
    wp_slugs = fetch_wp_slugs()
    log.info(f"  → {len(wp_slugs)} slugs WP chargés")
    log.info("  Scan des titres \"hyrox\" existants (détection fuzzy)...")
    known_ids = {e["wp_post_id"] for e in state.values()}
    existing_hyrox_titles = [(pid, t) for pid, t in fetch_existing_hyrox_titles()
                             if pid not in known_ids]
    log.info(f"  → {len(existing_hyrox_titles)} titres externes chargés")
    stats = {"created": 0, "reactivated": 0, "slug_updated": 0, "unchanged": 0, "error": 0}
    reactivations: list[dict] = []
    warnings: list[str] = []

    for ev in filtered:
        ville = ville_normalisee(ev["city_code"], ev["slug_hyrox"])
        geo        = ev["_geo"]
        city_name  = geo.get("city_name") or ev["_city_name"]
        country_nm = COUNTRY_CODE_TO_NAME.get(geo["country_code"], geo["country_code"])
        prev = state.get(ville)

        if prev is None:
            title = expected_title(ev, city_name)
            slug = event_slug(title)
            if slug in wp_slugs:
                log.info(f"  [SKIP doublon WP] {ev['nom_event'][:50]} — "
                         f"présent sur WP mais absent du state file, "
                         f"probablement suite à un run interrompu")
                continue

            # Détection fuzzy (au-delà du slug exact) : page(s) existante(s)
            # avec "hyrox" + la ville dans le titre, sous un slug différent
            # du nôtre — cf. incident Paris/Bordeaux/Nice du 20/08/2026, où
            # 3 events avaient déjà une page manuellement créée avec un
            # slug différent. Ne bloque pas la création (trop de faux
            # positifs possibles entre éditions/lieux différents dans la
            # même ville), juste une alerte à vérifier manuellement.
            city_fr = french_city_name(city_name)
            possible_dups = [(pid, t) for pid, t in existing_hyrox_titles
                             if city_fr.lower() in t.lower()]
            if possible_dups:
                msg = (f"Page(s) existante(s) possiblement en double pour "
                       f"\"{title}\" : " +
                       ", ".join(f"id={pid} ({t})" for pid, t in possible_dups))
                log.warning(f"  [⚠️ DOUBLON POSSIBLE] {msg}")
                warnings.append(msg)

            # ── Nouvel event ─────────────────────────────
            log.info(f"  [NEW] {ev['nom_event'][:55]} ({city_name}, {country_nm})")
            wp_id = create_wp_event(ev, city_name, country_nm, geo)
            if wp_id is None:
                stats["error"] += 1
                continue
            state[ville] = {
                "ville_normalisee":     ville,
                "slug_hyrox_actuel":    ev["slug_hyrox"],
                "wp_post_id":           wp_id,
                "dernier_statut_bouton": ev["statut_bouton"],
                "dernier_check":        datetime.now().isoformat(timespec="seconds"),
                "prix_connu":           "",
            }
            stats["created"] += 1
            continue

        # ── Event déjà connu ─────────────────────────────
        wp_id = prev.get("wp_post_id")

        if (prev.get("dernier_statut_bouton") == "Find out more"
                and ev["statut_bouton"] == "Buy Tickets"):
            log.info(f"  [🔔 RÉACTIVATION] {ev['nom_event'][:50]} ({city_name}) — inscriptions ouvertes")
            prix = fetch_event_detail(ev["url_event_hyrox"]).get("price", "")
            if prix:
                log.info(f"    💶 prix détecté : {prix}")
            update_wp_price(wp_id, prix)
            prev["prix_connu"] = prix
            reactivations.append({**ev, "prix_connu": prix})
            stats["reactivated"] += 1

        elif prev.get("slug_hyrox_actuel") != ev["slug_hyrox"]:
            log.info(f"  [🔄 NOUVELLE ÉDITION] {ev['nom_event'][:50]} : "
                     f"{prev.get('slug_hyrox_actuel')} → {ev['slug_hyrox']}")
            if refresh_wp_event(wp_id, ev, city_name, country_nm, geo):
                prev["slug_hyrox_actuel"] = ev["slug_hyrox"]
                prev["prix_connu"] = ""
                stats["slug_updated"] += 1
            else:
                stats["error"] += 1

        else:
            stats["unchanged"] += 1

        prev["dernier_statut_bouton"] = ev["statut_bouton"]
        prev["dernier_check"]         = datetime.now().isoformat(timespec="seconds")

    # ── 4. Sauvegarde state file ────────────────────────────
    if DRY_RUN:
        log.info("\n[DRY] state file non sauvegardé (simulation)")
    else:
        save_state(state)

    elapsed = time.time() - run_start
    log.info(f"\n{'='*60}")
    log.info(f"✅ Terminé en {elapsed:.0f}s")
    log.info(f"   créés       : {stats['created']}")
    log.info(f"   réactivés   : {stats['reactivated']}")
    log.info(f"   éditions MAJ: {stats['slug_updated']}")
    log.info(f"   inchangés   : {stats['unchanged']}")
    log.info(f"   erreurs     : {stats['error']}")
    log.info(f"   log         : {log_file}")

    # ── 5. Notifications ─────────────────────────────────────
    notify_reactivation(reactivations)
    send_summary_email(stats, elapsed, warnings)


if __name__ == "__main__":
    main()
