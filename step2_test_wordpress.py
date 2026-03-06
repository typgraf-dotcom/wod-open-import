"""
Step 2 - Test de connexion à l'API WordPress REST
+ Exploration du custom post type "event" (Ova/Meup theme)

Objectifs :
  1. Vérifier l'authentification avec Application Password
  2. Lister les post types disponibles (en tant qu'admin)
  3. Trouver le bon endpoint pour le CPT "event"
  4. Afficher la structure d'un événement existant (champs meta)
"""

import sys
import io
import requests
import json
from requests.auth import HTTPBasicAuth

# Fix encodage Windows (cp1252 -> utf-8)
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
WP_URL      = "https://wod-open.com"
WP_USER     = "typgraf"
WP_APP_PASS = "1Pyz cRXX sttO rKCx wZbB Zde7"   # Application Password

auth = HTTPBasicAuth(WP_USER, WP_APP_PASS)
HEADERS = {"Content-Type": "application/json"}

REST_BASE = f"{WP_URL}/wp-json"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def get(endpoint: str, params: dict = None) -> dict | list | None:
    url = f"{REST_BASE}{endpoint}"
    resp = requests.get(url, auth=auth, headers=HEADERS, params=params, timeout=15)
    if resp.status_code == 401:
        print(f"  [401] Non autorisé — vérifier username / application password")
        return None
    if resp.status_code == 404:
        print(f"  [404] Endpoint introuvable : {url}")
        return None
    resp.raise_for_status()
    return resp.json()


def section(title: str):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
def test_auth():
    section("1. Test d'authentification")
    data = get("/wp/v2/users/me")
    if data:
        print(f"  Connecté en tant que : {data.get('name')} (id={data.get('id')})")
        print(f"  Rôles : {data.get('roles', [])}")
        return True
    return False


def list_post_types():
    section("2. Post types disponibles (avec support REST)")
    data = get("/wp/v2/types", {"context": "edit"})
    if not data:
        return {}
    print(f"  {'Slug':<25} {'Rest Base':<30} {'Nom'}")
    print(f"  {'-'*70}")
    for slug, info in data.items():
        rest_base = info.get("rest_base", "—")
        label     = info.get("name", "")
        print(f"  {slug:<25} {rest_base:<30} {label}")
    return data


def find_event_endpoint(post_types: dict) -> str | None:
    """Cherche le bon endpoint pour le CPT event du thème Ova/Meup."""
    section("3. Recherche du CPT 'event'")

    # Candidats à tester
    candidates = []

    # D'abord ce qu'on trouve dans les post types connus
    for slug, info in post_types.items():
        if "event" in slug.lower():
            candidates.append((slug, info.get("rest_base", slug)))

    # Candidats supplémentaires communs aux thèmes event-oriented
    for slug in ["ova_event", "event", "events", "tribe_events", "em_event"]:
        if slug not in [c[0] for c in candidates]:
            candidates.append((slug, slug))

    print(f"  Candidats testés: {[c[0] for c in candidates]}")
    found_endpoint = None

    for slug, rest_base in candidates:
        url = f"{REST_BASE}/wp/v2/{rest_base}"
        resp = requests.get(
            url,
            auth=auth,
            headers=HEADERS,
            params={"context": "edit", "per_page": 1},
            timeout=10,
        )
        status = resp.status_code
        mark = "✓" if status == 200 else "✗"
        count = ""
        if status == 200:
            data = resp.json()
            count = f"  ({len(data)} événement(s) retourné(s))"
            if not found_endpoint:
                found_endpoint = rest_base
        print(f"  [{mark}] /wp/v2/{rest_base:<25} → HTTP {status}{count}")

    return found_endpoint


def inspect_event(rest_base: str):
    """Affiche la structure complète d'un événement existant."""
    section(f"4. Structure d'un événement existant (/wp/v2/{rest_base})")
    events = get(f"/wp/v2/{rest_base}", {"context": "edit", "per_page": 1})
    if not events or not isinstance(events, list) or len(events) == 0:
        print("  Aucun événement trouvé — impossible d'inspecter la structure.")
        print("  Essai avec per_page=3 sans contexte edit...")
        events = get(f"/wp/v2/{rest_base}", {"per_page": 1})

    if not events or not isinstance(events, list) or len(events) == 0:
        print("  Toujours aucun événement.")
        return

    ev = events[0]
    print(f"\n  Événement: {ev.get('title', {}).get('rendered', 'Sans titre')}")
    print(f"  ID WP    : {ev.get('id')}")
    print(f"  Statut   : {ev.get('status')}")
    print(f"  Slug     : {ev.get('slug')}")

    # Champs meta
    meta = ev.get("meta", {})
    if meta:
        print(f"\n  Champs meta ({len(meta)} champs):")
        for key, val in meta.items():
            val_str = str(val)[:80] + ("…" if len(str(val)) > 80 else "")
            print(f"    {key:<45} = {val_str}")
    else:
        print("\n  Pas de champs meta exposés dans la réponse REST.")
        print("  (Le thème utilise peut-être des champs ACF ou des meta non exposés)")

    # Champs de premier niveau intéressants
    print("\n  Tous les champs de premier niveau:")
    for key in ev.keys():
        if key not in ("content", "excerpt", "_links"):
            val = ev[key]
            val_str = str(val)[:100]
            print(f"    {key:<30} : {val_str}")

    # Sauvegarde pour inspection manuelle
    with open("wp_event_sample.json", "w", encoding="utf-8") as f:
        json.dump(ev, f, ensure_ascii=False, indent=2)
    print("\n  Structure complète sauvegardée dans wp_event_sample.json")


def check_schema(rest_base: str):
    """Affiche le schéma JSON du post type pour connaître tous les champs acceptés."""
    section(f"5. Schéma JSON de /wp/v2/{rest_base}")
    url = f"{REST_BASE}/wp/v2/{rest_base}"
    resp = requests.options(url, auth=auth, headers=HEADERS, timeout=10)
    if resp.status_code != 200:
        print(f"  OPTIONS non supporté (HTTP {resp.status_code})")
        return
    schema = resp.json()
    props = schema.get("schema", {}).get("properties", {})
    if props:
        print(f"  Propriétés du schéma ({len(props)} champs):")
        for name, info in props.items():
            desc = info.get("description", "")[:60]
            print(f"    {name:<30} {info.get('type','?'):<15} {desc}")
        with open("wp_event_schema.json", "w", encoding="utf-8") as f:
            json.dump(schema, f, ensure_ascii=False, indent=2)
        print("\n  Schéma complet sauvegardé dans wp_event_schema.json")
    else:
        print("  Schéma vide ou non disponible.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    print("Test de connexion à l'API REST WordPress")
    print(f"Site : {WP_URL}")
    print(f"User : {WP_USER}")

    if not test_auth():
        print("\nÉchec de l'authentification. Vérifier les credentials.")
        sys.exit(1)

    post_types = list_post_types()
    rest_base  = find_event_endpoint(post_types or {})

    if rest_base:
        print(f"\n  Endpoint trouvé : /wp/v2/{rest_base}")
        inspect_event(rest_base)
        check_schema(rest_base)
    else:
        section("Endpoint non trouvé")
        print("  Le CPT 'event' n'est pas exposé via l'API REST.")
        print("  Solutions possibles :")
        print("    1. Activer 'show_in_rest => true' dans le CPT (via functions.php ou plugin)")
        print("    2. Utiliser un plugin comme 'WP REST API Controller'")
        print("    3. Créer les événements via wp-admin avec WP-CLI")
        print("    4. Utiliser le endpoint générique /wp/v2/posts si le CPT hérite de post")


if __name__ == "__main__":
    main()
