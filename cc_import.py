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

import sys, json, time, re, io, xmlrpc.client, unicodedata
import logging, smtplib, ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime, timezone
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

CC_BASE_URL  = "https://competitioncorner.net"
CC_API_URL   = f"{CC_BASE_URL}/api2/v1/events/filtered"
CC_IMG_URL   = f"{CC_BASE_URL}/api2/v1/files/download?filename={{path}}"
CC_EVENT_URL = f"{CC_BASE_URL}/events/{{id}}"

COUNTRIES_FILTER = {"FR", "BE", "CH"}   # codes ISO

# ── Notifications email ────────────────────────────────────
EMAIL_ENABLED  = True
SMTP_HOST      = "smtp.gmail.com"
SMTP_PORT      = 587
SMTP_USER      = "typgraf@gmail.com"
SMTP_PASSWORD  = os.environ.get("SMTP_PASSWORD", "jupo hnqx xlhn eegt")
EMAIL_FROM     = SMTP_USER
EMAIL_TO       = "typgraf@gmail.com"

RESULTS_FILE = Path("cc_import_results.json")
LOGS_DIR     = Path("logs")
DELAY_WP     = 5
DELAY_NOMIN  = 1.3

# ── Taxonomies WP ──────────────────────────────────────────
TYPE_TAX = {"crossfit": 239, "hybrid_race": 238, "hyrox": 238}
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

def iso_to_time(iso: str) -> str:
    """ISO datetime → 'HH:MM'."""
    if not iso or len(iso) < 16:
        return "08:00"
    return iso[11:16]

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


# ═══════════════════════════════════════════════════════════
# ▌ CompetitionCorner API
# ═══════════════════════════════════════════════════════════
def fetch_cc_events() -> list[dict]:
    """Récupère les events actifs et upcoming depuis l'API CC publique."""
    all_events: list[dict] = []
    seen_ids: set[int] = set()

    for timing in ("active", "upcoming"):
        try:
            r = requests.get(CC_API_URL, params={"timing": timing}, timeout=15)
            r.raise_for_status()
            events = r.json()
            if not isinstance(events, list):
                log.warning(f"  [CC API] réponse inattendue pour timing={timing}")
                continue
            added = 0
            for ev in events:
                ev_id = ev.get("id")
                if ev_id and ev_id not in seen_ids:
                    seen_ids.add(ev_id)
                    all_events.append(ev)
                    added += 1
            log.info(f"  timing={timing}: {len(events)} events ({added} nouveaux)")
        except Exception as e:
            log.warning(f"  [CC fetch timing={timing}] {e}")

    return all_events


# ═══════════════════════════════════════════════════════════
# ▌ Géocodage Nominatim (pour la région France)
# ═══════════════════════════════════════════════════════════
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
def detect_category(ev: dict) -> list[str]:
    """
    Retourne les IDs de taxonomie event_cat selon format et eventTags.
    - individual → [136]
    - team (taille déduite des tags) → [137/140/162/164/193]
    - both → [136, 137]
    """
    fmt = (ev.get("format") or "").lower()
    tags = [t.get("value", "") for t in (ev.get("eventTags") or [])]
    tags_str = " ".join(tags + [ev.get("tags", "")])

    if fmt == "both":
        return [str(CAT_MAP[1]), str(CAT_MAP[2])]  # individuel + team-2

    if fmt == "individual":
        return [str(CAT_MAP[1])]

    if fmt == "team":
        # Cherche "Team - N Person" dans les tags
        m = re.search(r"team.*?(\d)", tags_str, re.IGNORECASE)
        if m:
            n = int(m.group(1))
            return [str(CAT_MAP.get(n, CAT_MAP[2]))]
        return [str(CAT_MAP[2])]  # team-2 par défaut

    return [str(CAT_MAP[1])]


# ═══════════════════════════════════════════════════════════
# ▌ Taxonomie type (crossfit/hyrox)
# ═══════════════════════════════════════════════════════════
def detect_type(ev: dict) -> list[str]:
    """Retourne l'ID de taxonomie type."""
    ev_type = (ev.get("type") or "").lower()
    tid = TYPE_TAX.get(ev_type, TYPE_TAX["crossfit"])
    return [str(tid)]


# ═══════════════════════════════════════════════════════════
# ▌ Upload image
# ═══════════════════════════════════════════════════════════
def upload_image(thumbnail: str, slug: str, title: str) -> int | None:
    """Télécharge l'image CC, upload sur WP, retourne attachment_id."""
    if not thumbnail:
        return None

    img_url = CC_IMG_URL.format(path=thumbnail)
    try:
        r = requests.get(img_url, timeout=30)
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
# ▌ Construction du post WP
# ═══════════════════════════════════════════════════════════
def build_post(ev: dict, slug: str) -> dict:
    """Construit le payload complet pour wp.newPost."""
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
    start_h   = iso_to_time(start_iso)
    end_h     = iso_to_time(end_iso)
    days_val  = compute_event_days(start_iso, end_iso)

    cal_id   = str(int(time.time()))
    cal_val  = php_calendar(cal_id, start_cal, end_cal, start_h, end_h) if start_cal else "a:0:{}"

    # Adresse
    parts = [p for p in [city, state, country] if p]
    map_addr = ", ".join(parts)

    # URL externe
    ev_id   = ev.get("id", "")
    ext_url = CC_EVENT_URL.format(id=ev_id) if ev_id else ""

    # Taxonomies
    type_ids = detect_type(ev)
    cat_ids  = detect_category(ev)
    loc_terms: set[str] = set()
    country_tid = LOC_COUNTRY.get(country_c)
    if country_tid:
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

    # Lat/lng si disponibles
    if lat and lng:
        custom_fields += [
            {"key": "ova_mb_event_map_lat", "value": lat},
            {"key": "ova_mb_event_map_lng", "value": lng},
        ]

    return {
        "post_type":      "event",
        "post_status":    POST_STATUS,
        "post_title":     title,
        "post_name":      slug,
        "post_content":   "",
        "terms": {
            "type":      type_ids,
            "event_cat": cat_ids,
            "event_loc": list(loc_terms),
        },
        "custom_fields": custom_fields,
    }


# ═══════════════════════════════════════════════════════════
# ▌ Enrichissement post-création (région France + image)
# ═══════════════════════════════════════════════════════════
def enrich_post(wp_id: int, ev: dict, slug: str, title: str) -> None:
    """
    Après création du post :
    - Détermine la région française via Nominatim
    - Upload l'image à la une
    """
    loc       = ev.get("eventLocation") or {}
    country_c = loc.get("countryCode", "")
    country   = loc.get("country", "").strip()
    city      = loc.get("city", "").strip()
    lat       = str(loc.get("lat") or "")
    lng       = str(loc.get("lng") or "")
    thumbnail = ev.get("thumbnail") or ev.get("image") or ""

    start_iso = ev.get("startDateTime", "")
    end_iso   = ev.get("endDateTime", "")
    start_cal = iso_to_date(start_iso)
    end_cal   = iso_to_date(end_iso)
    start_h   = iso_to_time(start_iso)
    end_h     = iso_to_time(end_iso)
    days_val  = compute_event_days(start_iso, end_iso)

    # Récupère meta IDs existants
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

    # Reconstruire calendrier avec vrais meta IDs
    if start_cal and end_cal:
        cal_id  = extract_cal_id(meta_values.get("ova_mb_event_calendar", ""))
        cal_val = php_calendar(cal_id, start_cal, end_cal, start_h, end_h)
        custom_fields.append(add_field("ova_mb_event_calendar",        cal_val))
        custom_fields.append(add_field("ova_mb_event_event_days",      days_val))
        custom_fields.append(add_field("ova_mb_event_option_calendar", "manual"))
        custom_fields.append(add_field("ova_mb_event_ticket_link",     "ticket_external_link"))
        custom_fields.append(add_field("ova_mb_event_time_zone",       "Europe/Paris"))

    # Région française
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
    country_tid = LOC_COUNTRY.get(country_c)
    if country_tid:
        event_loc_ids.add(str(country_tid))

    if country_c == "FR" and (lat or city):
        state_slug = geocode_region(lat, lng, city, country)
        if state_slug:
            region_tid = LOC_REGION.get(state_slug)
            if region_tid:
                event_loc_ids.add(str(region_tid))
                log.info(f"    🗺️  région : {state_slug} → {region_tid}")

    new_terms["event_loc"] = list(event_loc_ids)

    if custom_fields or new_terms:
        if not DRY_RUN:
            try:
                wp_call("editPost", wp_id, {
                    "custom_fields": custom_fields,
                    "terms":         new_terms,
                })
            except Exception as e:
                log.warning(f"    [editPost enrich] {e}")

    # Image à la une
    media_id = upload_image(thumbnail, slug, title)
    if media_id:
        if not DRY_RUN:
            try:
                wp_call("editPost", wp_id, {"post_thumbnail": media_id})
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
                       if r.get("cc_id")}

    log.info(f"    {len(existing_results)} entrées déjà dans cc_import_results.json")

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

        payload = build_post(ev, slug)

        if DRY_RUN:
            start_dt = ev.get("startDateTime", "")[:10]
            log.info(f"    [DRY] newPost → {title}  ({start_dt})")
            new_results.append({
                "wp_id": 0, "slug": slug, "title": title,
                "action": "dry_run", "cc_id": cc_id,
            })
            stats["created"] += 1
            continue

        # Créer dans WP
        try:
            wp_id = int(wp_call("newPost", payload))
            log.info(f"    ✓ créé wp_id={wp_id}")
            new_results.append({
                "wp_id": wp_id, "slug": slug, "title": title,
                "action": "created", "cc_id": cc_id,
            })
            stats["created"] += 1
        except Exception as e:
            log.error(f"    [ERR newPost] {e}")
            warnings.append(f"[ERR newPost] {title[:60]} — {e}")
            stats["error"] += 1
            continue

        # Enrichissement
        log.info(f"    → enrichissement (région + image)...")
        enrich_post(wp_id, ev, slug, title)

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
