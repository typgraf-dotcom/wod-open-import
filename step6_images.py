"""
Step 6 - Upload des logos (featured image) pour les 45 événements importés.

Pour chaque événement :
  1. Récupère iconLink depuis competitions_raw.json
     (fallback : API détail scoring.fit si iconLink absent)
  2. Télécharge l'image depuis scoring-images.s3.eu-west-3.amazonaws.com
  3. Upload dans la médiathèque WordPress (wp.uploadFile)
  4. Définit comme image à la une (post_thumbnail) du post
"""

import sys
import json
import time
import re
import mimetypes
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

DELAY_AFTER_UPLOAD   = 5    # secondes après wp.uploadFile
DELAY_AFTER_EDIT     = 5    # secondes après wp.editPost
DELAY_ON_ERROR       = 20


# ---------------------------------------------------------------------------
# XML-RPC (sans délai fixe — on gère manuellement pour uploader + editPost)
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


def guess_mime(url: str) -> str:
    ext  = url.split("?")[0].rsplit(".", 1)[-1].lower()
    mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg",
            "png": "image/png", "webp": "image/webp",
            "gif": "image/gif"}.get(ext, "image/jpeg")
    return mime


def get_icon_link(comp: dict, event_number: int | None) -> str | None:
    """Retourne iconLink depuis les données du comp, ou via l'API de détail."""
    # 1. Depuis le search API (competitions_raw.json)
    link = comp.get("iconLink", "")
    if link:
        return link

    # 2. Fallback : API de détail
    if not event_number:
        return None
    try:
        url  = SCORING_API_DETAIL.format(eventNumber=event_number)
        resp = requests.get(url, timeout=12)
        resp.raise_for_status()
        d = resp.json()
        return (d.get("leaderboard") or {}).get("iconLink", "") or None
    except Exception as e:
        print(f"    [API] Impossible de récupérer iconLink : {e}")
        return None


def upload_image(image_url: str, filename: str, title: str = "") -> int | None:
    """Télécharge et upload une image. Retourne l'attachment_id ou None."""
    # Téléchargement
    try:
        r = requests.get(image_url, timeout=30)
        r.raise_for_status()
        image_data = r.content
    except Exception as e:
        print(f"    [DL] Échec téléchargement : {e}")
        return None

    mime = guess_mime(image_url)

    # Upload WordPress
    try:
        result = wp_call_raw("uploadFile", {
            "name":      filename,
            "type":      mime,
            "bits":      xmlrpc.client.Binary(image_data),
            "overwrite": False,
        })
        time.sleep(DELAY_AFTER_UPLOAD)
        attachment_id = int(result.get("id", 0))
        return attachment_id if attachment_id else None
    except Exception as e:
        print(f"    [UPLOAD] Erreur : {e}")
        return None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    # Charger import_results.json
    try:
        with open("import_results.json", encoding="utf-8") as f:
            results = json.load(f)
    except FileNotFoundError:
        print("import_results.json introuvable."); return

    # Charger competitions_raw.json
    try:
        with open("competitions_raw.json", encoding="utf-8") as f:
            raw = json.load(f)
    except FileNotFoundError:
        print("competitions_raw.json introuvable."); return

    comp_by_slug = {make_slug(c): c for c in raw}
    to_process   = [(r["wp_id"], r["slug"], r["title"])
                    for r in results if r["action"] == "created"]

    print(f"Upload des images pour {len(to_process)} événements...\n")
    stats = {"uploaded": 0, "skipped_no_image": 0, "error": 0}

    for i, (wp_id, slug, title) in enumerate(to_process, 1):
        comp         = comp_by_slug.get(slug, {})
        event_number = comp.get("eventNumber")
        short        = title[:50]
        print(f"[{i:2}/{len(to_process)}] {short}")

        # 1. Récupérer l'iconLink
        icon_url = get_icon_link(comp, event_number)
        if not icon_url:
            print(f"    ↳ pas d'image disponible — ignoré")
            stats["skipped_no_image"] += 1
            continue

        # 2. Construire le nom de fichier
        ext      = icon_url.split("?")[0].rsplit(".", 1)[-1].lower() or "jpg"
        filename = f"{slug[:60]}.{ext}"

        # 3. Upload WordPress
        attachment_id = upload_image(icon_url, filename, title=title)
        if not attachment_id:
            print(f"    ↳ upload échoué")
            stats["error"] += 1
            time.sleep(DELAY_ON_ERROR)
            continue

        print(f"    ↳ media uploadé (id={attachment_id})")

        # 4. Définir comme featured image du post
        try:
            wp_call_raw("editPost", wp_id, {"post_thumbnail": attachment_id})
            time.sleep(DELAY_AFTER_EDIT)
            print(f"    ✓ image à la une définie")
            stats["uploaded"] += 1
        except Exception as e:
            print(f"    [ERR] editPost post_thumbnail : {e}")
            stats["error"] += 1
            time.sleep(DELAY_ON_ERROR)

    print(f"\n{'='*60}")
    print("Résumé images :")
    for k, v in stats.items():
        print(f"  {k:<22}: {v}")


if __name__ == "__main__":
    main()
