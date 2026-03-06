"""
Step 6 (retry) - Retry des images échouées avec détection MIME par magic bytes.

Le problème : S3 retourne Content-Type=application/octet-stream pour certaines images.
WordPress vérifie le vrai type depuis le contenu du fichier et refuse si l'extension
ne correspond pas.

Solution : on lit les premiers octets du fichier (magic bytes) pour détecter
le vrai format, puis on utilise la bonne extension et le bon MIME type.
"""

import sys
import json
import time
import re
import io
import xmlrpc.client
import requests
from PIL import Image

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

DELAY_AFTER_UPLOAD = 5
DELAY_AFTER_EDIT   = 5
DELAY_ON_ERROR     = 20

# Slugs des events dont l'image a échoué au premier passage
FAILED_SLUGS = {
    "concours-interne-open-6992ee38",
    "open-party-by-crossfit-aubi-re-6980ad5e",
    "agnetz-hybrid-race-699f08df",
    "crossfit-bailleul-battle-6931a788",
    "magic-hyrox-challenge-69412bdd",
    "2l2n-training-camp-1-edition-2026-696df86a",
    "pertuis-contest-spring-dition-68df7bad",
    "cormeilles-fitness-battle-68f7964d",
    "fighter-contest-vol-7-696cd890",
    "24-heures-de-bikeerg-69610fa6",
    "parrot-battle-iv-68cc009a",
    "amplem-factory-5-spring-edition-6903724b",
    "hyrox-simulation-ch-teauroux-by-pr-6984bba4",
    "tricassium-row-contest-2-696b45e4",
    "hybrid-comp-692580c4",
    "orlinz-battle-3-6958192f",
    "games-of-the-north-2026-finale-691f9acc",
    "hyrox-event-2026-694723d7",
    "2gen-weightlifting-experience-69775cee",
    "sanzaru-hyrox-race-6966bd4a",
}


# ---------------------------------------------------------------------------
# Magic bytes → MIME + extension
# ---------------------------------------------------------------------------
def upload_via_rest(image_data: bytes, filename: str, mime: str) -> int | None:
    """Upload via REST API /wp/v2/media — chemin différent de XML-RPC."""
    try:
        resp = requests.post(
            f"{WP_URL}/wp-json/wp/v2/media",
            data=image_data,
            headers={
                "Content-Type":        mime,
                "Content-Disposition": f'attachment; filename="{filename}"',
            },
            auth=XMLRPC_AUTH,
            timeout=60,
        )
        if resp.status_code in (200, 201):
            time.sleep(DELAY_AFTER_UPLOAD)
            return resp.json().get("id")
        print(f"    [REST] {resp.status_code}: {resp.text[:120]}")
        return None
    except Exception as e:
        print(f"    [REST] exception : {e}")
        return None


def detect_image_type(data: bytes) -> tuple[str, str]:
    """Détecte le vrai type image depuis les premiers octets."""
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg", "jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png", "png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp", "webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif", "gif"
    # Fallback : on essaie quand même en jpeg
    return "image/jpeg", "jpg"


# ---------------------------------------------------------------------------
# XML-RPC
# ---------------------------------------------------------------------------
def wp_call_raw(method: str, *args):
    full_method = f"wp.{method}" if not method.startswith("wp.") else method
    wp_params   = ("", WP_USER, WP_APP_PASS) + args
    payload     = xmlrpc.client.dumps(wp_params, methodname=full_method)
    resp = requests.post(
        XMLRPC_URL, data=payload.encode("utf-8"),
        headers={"Content-Type": "text/xml; charset=utf-8"},
        auth=XMLRPC_AUTH, timeout=60, allow_redirects=True,
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


def get_icon_link(comp: dict, event_number) -> str | None:
    link = comp.get("iconLink", "")
    if link:
        return link
    if not event_number:
        return None
    try:
        url  = SCORING_API_DETAIL.format(eventNumber=event_number)
        resp = requests.get(url, timeout=12)
        resp.raise_for_status()
        return (resp.json().get("leaderboard") or {}).get("iconLink") or None
    except Exception:
        return None


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

    to_retry = [(r["wp_id"], r["slug"], r["title"])
                for r in results
                if r["action"] == "created" and r["slug"] in FAILED_SLUGS]

    print(f"Retry image upload pour {len(to_retry)} événements "
          f"(avec détection MIME par magic bytes)...\n")

    stats = {"uploaded": 0, "skipped": 0, "error": 0}

    for i, (wp_id, slug, title) in enumerate(to_retry, 1):
        comp         = comp_by_slug.get(slug, {})
        event_number = comp.get("eventNumber")
        short        = title[:50]
        print(f"[{i:2}/{len(to_retry)}] {short}")

        # Forcer l'API de détail (ignore iconLink du search API)
        # Les URLs .jpg du search API ont un profil S3 que PHP rejette
        icon_url = None
        if event_number:
            try:
                url  = SCORING_API_DETAIL.format(eventNumber=event_number)
                resp = requests.get(url, timeout=12)
                resp.raise_for_status()
                d = resp.json()
                icon_url = ((d.get("leaderboard") or {}).get("iconLink")
                            or (d.get("presentation") or {}).get("iconLink"))
            except Exception as e:
                print(f"    [API détail] {e}")
        if not icon_url:
            icon_url = comp.get("iconLink", "") or None
        if not icon_url:
            print(f"    ↳ pas d'image — ignoré")
            stats["skipped"] += 1
            continue

        # --- Télécharger ---
        try:
            r = requests.get(icon_url, timeout=30)
            r.raise_for_status()
            raw_data = r.content
        except Exception as e:
            print(f"    [DL] échec : {e}")
            stats["error"] += 1
            continue

        # --- Convertir en PNG via Pillow (contourne la détection JPEG côté PHP) ---
        try:
            img = Image.open(io.BytesIO(raw_data))
            if img.mode == "CMYK":
                img = img.convert("RGB")
            elif img.mode in ("P", "LA"):
                img = img.convert("RGBA")
            buf = io.BytesIO()
            img.save(buf, format="PNG", optimize=True)
            image_data = buf.getvalue()
            mime, ext  = "image/png", "png"
            print(f"    converti PNG : {len(raw_data)//1024}KB → {len(image_data)//1024}KB")
        except Exception as e:
            print(f"    [PIL] conversion impossible ({e}) — utilisation données brutes")
            image_data = raw_data
            mime, ext  = detect_image_type(image_data)

        filename  = f"{slug[:60]}.{ext}"
        print(f"    fichier : {filename}")

        # --- Upload via REST API (contourne le contrôle XML-RPC) ---
        attachment_id = upload_via_rest(image_data, filename, mime)
        if not attachment_id:
            # Fallback XML-RPC
            try:
                result = wp_call_raw("uploadFile", {
                    "name": filename, "type": mime,
                    "bits": xmlrpc.client.Binary(image_data), "overwrite": True,
                })
                time.sleep(DELAY_AFTER_UPLOAD)
                attachment_id = int(result.get("id", 0)) or None
            except Exception as e:
                print(f"    [UPLOAD] les deux méthodes ont échoué : {e}")
                stats["error"] += 1
                time.sleep(DELAY_ON_ERROR)
                continue

        if not attachment_id:
            print(f"    [UPLOAD] pas d'id retourné")
            stats["error"] += 1
            continue

        print(f"    ↳ media uploadé (id={attachment_id})")

        # --- Définir comme image à la une (XML-RPC) ---
        try:
            wp_call_raw("editPost", wp_id, {"post_thumbnail": attachment_id})
            time.sleep(DELAY_AFTER_EDIT)
            print(f"    ✓ image à la une définie")
            stats["uploaded"] += 1
        except Exception as e:
            print(f"    [ERR] editPost : {e}")
            stats["error"] += 1
            time.sleep(DELAY_ON_ERROR)

    print(f"\n{'='*60}")
    print("Résumé retry images :")
    for k, v in stats.items():
        print(f"  {k:<12}: {v}")


if __name__ == "__main__":
    main()
