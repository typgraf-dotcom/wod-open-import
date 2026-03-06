"""
inject_existing_wp_events.py

Récupère tous les events existants dans WordPress et les injecte dans
import_results.json avec action="existing" pour éviter qu'ils soient
re-importés par daily_import.py.

Usage : python inject_existing_wp_events.py
"""

import sys, json, time, xmlrpc.client
from pathlib import Path
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

RESULTS_FILE = Path("import_results.json")
DELAY_WP     = 5
PAGE_SIZE    = 50


# ─────────────────────────────────────────────
# XML-RPC helper
# ─────────────────────────────────────────────
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


def safe_str(v) -> str:
    """Convertit DateTime XML-RPC ou toute valeur en str propre."""
    if isinstance(v, xmlrpc.client.DateTime):
        return str(v)
    return str(v) if v is not None else ""


# ─────────────────────────────────────────────
# Fetch tous les events WP
# ─────────────────────────────────────────────
def fetch_all_wp_events() -> list[dict]:
    all_posts = []
    page = 1
    while True:
        print(f"  page {page}...", end=" ", flush=True)
        try:
            posts = wp_call("getPosts", {
                "post_type":   "event",
                "post_status": "any",
                "number":      PAGE_SIZE,
                "offset":      (page - 1) * PAGE_SIZE,
                "fields":      ["post_id", "post_title", "post_name", "post_status"],
            })
        except Exception as e:
            print(f"\n[ERR page {page}] {e}")
            break

        if not posts:
            print("(vide)")
            break

        # Sérialisation safe : convertir les DateTime en str
        for p in posts:
            all_posts.append({
                "wp_id":  int(p.get("post_id", 0)),
                "title":  safe_str(p.get("post_title", "")),
                "slug":   safe_str(p.get("post_name", "")),
                "status": safe_str(p.get("post_status", "")),
                "action": "existing",
            })

        count = len(posts)
        print(f"{count} posts  (total {len(all_posts)})")
        if count < PAGE_SIZE:
            break
        page += 1

    return all_posts


# ─────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────
def main():
    print(f"Récupération des events WP existants...")

    # Charger import_results.json existant
    existing_results: list[dict] = []
    if RESULTS_FILE.exists():
        existing_results = json.loads(RESULTS_FILE.read_text(encoding="utf-8"))
    print(f"  import_results.json : {len(existing_results)} entrées existantes")

    # Slugs déjà enregistrés (tous types d'action)
    existing_slugs = {r["slug"] for r in existing_results}

    # Fetch WP
    wp_events = fetch_all_wp_events()
    print(f"\n  {len(wp_events)} events récupérés depuis WP")

    # Ajouter uniquement les events non déjà enregistrés
    added = 0
    for ev in wp_events:
        if ev["slug"] and ev["slug"] not in existing_slugs:
            existing_results.append(ev)
            existing_slugs.add(ev["slug"])
            added += 1

    print(f"  {added} nouveaux events injectés dans import_results.json")

    # Sauvegarder
    RESULTS_FILE.write_text(
        json.dumps(existing_results, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"  import_results.json mis à jour : {len(existing_results)} entrées au total")
    print("\nTerminé.")


if __name__ == "__main__":
    main()
