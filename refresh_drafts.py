"""
refresh_drafts.py — Nettoyage et réimport des events en brouillon (scoring.fit)

Pour chaque event en brouillon dans WordPress :
  1. Classifie : passé (end_date < aujourd'hui) ou à venir
  2. Events passés   → supprime définitivement de WP (jamais publiés, plus d'actualité)
  3. Events à venir  → supprime de WP + retire de import_results.json
                       → seront réimportés au prochain run de daily_import.py
                          avec toutes les améliorations récentes

Usage :
  python refresh_drafts.py          → simulation (DRY_RUN=True par défaut)
  python refresh_drafts.py --run    → exécution réelle
"""

import sys, os, json, time, logging, re
from datetime import datetime, timezone
from pathlib import Path

import requests

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ═══════════════════════════════════════════════════════════
# ▌ Configuration
# ═══════════════════════════════════════════════════════════
DRY_RUN = "--run" not in sys.argv   # python refresh_drafts.py --run pour exécuter

WP_URL      = "https://wod-open.com"
WP_USER     = "typgraf"
WP_APP_PASS = os.environ.get("WP_APP_PASS", "1Pyz cRXX sttO rKCx wZbB Zde7")
REST_URL    = f"{WP_URL}/wp-json/wp/v2"
REST_AUTH   = (WP_USER, WP_APP_PASS)

RESULTS_FILE    = Path("import_results.json")
CC_RESULTS_FILE = Path("cc_import_results.json")
LOGS_DIR        = Path("logs")
DELAY_WP        = 1.5   # délai entre appels WP (secondes)

NOW_TS = int(datetime.now(timezone.utc).timestamp())

# ═══════════════════════════════════════════════════════════
# ▌ Logging
# ═══════════════════════════════════════════════════════════
LOGS_DIR.mkdir(exist_ok=True)
log_file = LOGS_DIR / f"refresh_{datetime.now():%Y-%m-%d}.log"
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
# ▌ WordPress REST API
# ═══════════════════════════════════════════════════════════
def wp_get(endpoint: str, params: dict) -> list | dict:
    r = requests.get(f"{REST_URL}/{endpoint}", auth=REST_AUTH,
                     params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def wp_delete(wp_id: int) -> bool:
    """Supprime définitivement un event WP (force=true)."""
    time.sleep(DELAY_WP)
    try:
        r = requests.delete(
            f"{REST_URL}/events/{wp_id}",
            auth=REST_AUTH,
            params={"force": True},
            timeout=30,
        )
        r.raise_for_status()
        return True
    except Exception as e:
        log.warning(f"    [ERR delete wp_id={wp_id}] {e}")
        return False


# ═══════════════════════════════════════════════════════════
# ▌ Fetch des brouillons WP
# ═══════════════════════════════════════════════════════════
def fetch_all_drafts() -> list[dict]:
    """Récupère tous les events en brouillon depuis WP (toutes pages)."""
    drafts: list[dict] = []
    page = 1
    while True:
        try:
            data = wp_get("events", {
                "status": "draft",
                "per_page": 100,
                "page": page,
                "_fields": "id,slug,title,meta,featured_media",
            })
            if not data:
                break
            drafts.extend(data)
            if len(data) < 100:
                break
            page += 1
        except Exception as e:
            log.warning(f"  [ERR fetch page={page}] {e}")
            break
    return drafts


# ═══════════════════════════════════════════════════════════
# ▌ Helpers
# ═══════════════════════════════════════════════════════════
def is_past_event(meta: dict) -> bool:
    """True si la date de fin de l'event est antérieure à aujourd'hui."""
    end_ts = meta.get("ova_mb_event_end_date_str", "")
    try:
        ts = int(end_ts)
        return ts > 0 and ts < NOW_TS
    except (ValueError, TypeError):
        return False


def is_cc_event(slug: str) -> bool:
    """True si le slug correspond à un event CompetitionCorner (-cc-)."""
    return "-cc-" in slug


def extract_cc_id(slug: str) -> int | None:
    """Extrait le cc_id depuis un slug de type 'name-cc-12345'."""
    m = re.search(r'-cc-(\d+)$', slug)
    return int(m.group(1)) if m else None


def get_title(draft: dict) -> str:
    t = draft.get("title", {})
    return t.get("rendered", t) if isinstance(t, dict) else str(t)


# ═══════════════════════════════════════════════════════════
# ▌ import_results.json
# ═══════════════════════════════════════════════════════════
def load_results() -> list[dict]:
    if not RESULTS_FILE.exists():
        return []
    return json.loads(RESULTS_FILE.read_text(encoding="utf-8"))


def save_results(results: list[dict]) -> None:
    RESULTS_FILE.write_text(
        json.dumps(results, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def load_cc_results() -> list[dict]:
    if not CC_RESULTS_FILE.exists():
        return []
    return json.loads(CC_RESULTS_FILE.read_text(encoding="utf-8"))


def save_cc_results(results: list[dict]) -> None:
    CC_RESULTS_FILE.write_text(
        json.dumps(results, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


# ═══════════════════════════════════════════════════════════
# ▌ Main
# ═══════════════════════════════════════════════════════════
def main():
    log.info("=" * 60)
    log.info(f"▶ Refresh brouillons WP — {datetime.now():%d/%m/%Y %H:%M}")
    log.info(f"  DRY_RUN={DRY_RUN}")
    log.info(f"  (Passer --run pour exécuter réellement)")
    log.info("=" * 60)

    # ── 1. Récupérer tous les brouillons ──────────────────
    log.info("\n[1] Récupération des brouillons WP...")
    drafts = fetch_all_drafts()
    log.info(f"    {len(drafts)} brouillons trouvés")

    if not drafts:
        log.info("  Rien à faire.")
        return

    # ── 2. Classifier ─────────────────────────────────────
    past_sf, future_sf, past_cc, future_cc, unknown = [], [], [], [], []

    for d in drafts:
        slug = d.get("slug", "")
        meta = d.get("meta", {})
        cc   = is_cc_event(slug)
        past = is_past_event(meta)

        if cc:
            (past_cc if past else future_cc).append(d)
        elif slug:
            (past_sf if past else future_sf).append(d)
        else:
            unknown.append(d)

    log.info(f"\n    ┌─ scoring.fit ─────────────────────")
    log.info(f"    │  Passés    : {len(past_sf)}")
    log.info(f"    │  À venir   : {len(future_sf)}")
    log.info(f"    ├─ CompetitionCorner ───────────────")
    log.info(f"    │  Passés    : {len(past_cc)}")
    log.info(f"    │  À venir   : {len(future_cc)}")
    log.info(f"    └─ Inconnus  : {len(unknown)}")

    # ── 3. Charger import_results.json ────────────────────
    results = load_results()
    slugs_in_results = {r["slug"] for r in results}
    log.info(f"\n[2] import_results.json : {len(results)} entrées")

    # ── 4. Traiter les events passés (supprimer) ──────────
    all_past = past_sf + past_cc
    log.info(f"\n[3] Suppression des {len(all_past)} events passés...")

    deleted_past = 0
    for d in all_past:
        wp_id = d["id"]
        slug  = d.get("slug", "")
        title = get_title(d)
        end_ts = d.get("meta", {}).get("ova_mb_event_end_date_str", "?")
        try:
            end_str = datetime.fromtimestamp(int(end_ts)).strftime("%d/%m/%Y")
        except Exception:
            end_str = "?"
        log.info(f"  [PAST] {title[:55]}  (fin={end_str}, wp_id={wp_id})")
        if not DRY_RUN:
            if wp_delete(wp_id):
                deleted_past += 1

    # ── 5. Traiter les events à venir scoring.fit ─────────
    log.info(f"\n[4] Events à venir scoring.fit ({len(future_sf)}) → retrait de import_results.json...")

    removed_from_results = 0
    deleted_future = 0

    for d in future_sf:
        wp_id = d["id"]
        slug  = d.get("slug", "")
        title = get_title(d)
        in_results = slug in slugs_in_results

        log.info(f"  [FUTURE] {title[:50]}  (wp_id={wp_id}, slug={slug})")
        log.info(f"    → import_results.json : {'présent' if in_results else 'absent'}")

        if not DRY_RUN:
            # Supprimer de WP
            if wp_delete(wp_id):
                deleted_future += 1

    # Retirer tous les slugs future_sf de import_results.json en une fois
    future_sf_slugs = {d.get("slug", "") for d in future_sf}
    if not DRY_RUN and future_sf_slugs:
        before = len(results)
        results = [r for r in results if r.get("slug") not in future_sf_slugs]
        removed_from_results = before - len(results)
        save_results(results)
        log.info(f"  → {removed_from_results} entrées retirées de import_results.json")

    # ── 5. Traiter les events CC à venir ──────────────────
    cc_results = load_cc_results()
    log.info(f"\n[5] Events CC à venir ({len(future_cc)}) → suppression WP + retrait cc_import_results.json...")

    # Détecter les doublons CC (même cc_id, plusieurs wp_ids)
    cc_id_seen: dict[int, list] = {}
    for d in future_cc:
        cid = extract_cc_id(d.get("slug", ""))
        if cid:
            cc_id_seen.setdefault(cid, []).append(d["id"])
    duplicates = {cid: wids for cid, wids in cc_id_seen.items() if len(wids) > 1}
    if duplicates:
        log.info(f"  ⚠️  {len(duplicates)} cc_id(s) en doublon WP detectes")

    deleted_cc = 0
    cc_ids_to_remove: set[int] = set()

    for d in future_cc:
        wp_id = d["id"]
        slug  = d.get("slug", "")
        title = get_title(d)
        cid   = extract_cc_id(slug)
        log.info(f"  [CC] {title[:55]}  (wp_id={wp_id}, cc_id={cid})")
        if cid:
            cc_ids_to_remove.add(cid)
        if not DRY_RUN:
            if wp_delete(wp_id):
                deleted_cc += 1

    removed_cc = 0
    if not DRY_RUN and cc_ids_to_remove:
        before_cc = len(cc_results)
        cc_results = [r for r in cc_results if r.get("cc_id") not in cc_ids_to_remove]
        removed_cc = before_cc - len(cc_results)
        save_cc_results(cc_results)
        log.info(f"  → {removed_cc} entrees retirees de cc_import_results.json")

    # ── 6. Traiter les inconnus ────────────────────────────
    log.info(f"\n[6] Events inconnus ({len(unknown)}) → suppression WP...")
    deleted_unknown = 0
    for d in unknown:
        wp_id = d["id"]
        title = get_title(d)
        log.info(f"  [?] {title[:55]}  (wp_id={wp_id})")
        if not DRY_RUN:
            if wp_delete(wp_id):
                deleted_unknown += 1

    # ── 7. Résumé ─────────────────────────────────────────
    log.info(f"\n{'='*60}")
    if DRY_RUN:
        log.info("⚠️  SIMULATION — aucune modification effectuee")
        log.info(f"   Relancer avec --run pour executer")
    else:
        log.info(f"✅ Termine")
        log.info(f"   Brouillons passes supprimes         : {deleted_past}/{len(all_past)}")
        log.info(f"   Brouillons SF futurs supprimes      : {deleted_future}/{len(future_sf)}")
        log.info(f"   Retires de import_results.json      : {removed_from_results}")
        log.info(f"   Brouillons CC supprimes             : {deleted_cc}/{len(future_cc)}")
        log.info(f"   Retires de cc_import_results.json   : {removed_cc}")
        log.info(f"   Brouillons inconnus supprimes       : {deleted_unknown}/{len(unknown)}")
    log.info(f"\n-> Lancer ensuite :")
    log.info(f"   python daily_import.py   (reimporte les {len(future_sf)} events scoring.fit)")
    log.info(f"   python cc_import.py      (reimporte les ~{len(cc_ids_to_remove)} events CC uniques)")
    log.info(f"{'='*60}")


if __name__ == "__main__":
    main()
