"""
hyrox_backfill.py - Corrige les brouillons HYROX déjà créés (retour utilisateur)

Réapplique la logique actuelle de hyrox_import.py (titre traduit, description
reformulée, catégories/tags standard, adresse précise, prix "NC", calendrier)
aux events déjà présents dans hyrox_state.json, via PATCH WordPress.

Usage :
  python hyrox_backfill.py          → simulation (DRY_RUN=True par défaut)
  python hyrox_backfill.py --run    → exécution réelle (tout le payload)
  python hyrox_backfill.py --run --desc-only   → ne PATCH que le champ
      "content" (description) — pour ne pas retoucher titre/tags/etc déjà
      corrigés dans un run précédent, ex: après ajout d'ANTHROPIC_API_KEY
  python hyrox_backfill.py --run --price-only  → ne PATCH que le prix
      (meta.ova_mb_event_price_desc), et seulement si un vrai prix est
      trouvé (billetterie ouverte) — laisse "NC" tel quel sinon
  python hyrox_backfill.py --run --only tnf-hyrox-tenerife   → un seul event (debug)
"""

import sys, json, time
from pathlib import Path

import hyrox_import as h

DRY_RUN    = "--run" not in sys.argv
DESC_ONLY  = "--desc-only" in sys.argv
PRICE_ONLY = "--price-only" in sys.argv
_HERE = Path(__file__).parent


def main():
    only = None
    if "--only" in sys.argv:
        only = sys.argv[sys.argv.index("--only") + 1]

    state = h.load_state()
    raw   = json.loads((_HERE / "hyrox_raw.json").read_text(encoding="utf-8"))
    raw_by_slug = {ev["slug_hyrox"]: ev for ev in raw}

    print(f"DRY_RUN={DRY_RUN}  entrées state={len(state)}")

    done, errors = 0, 0
    for ville, entry in state.items():
        if only and ville != only:
            continue
        wp_id = entry.get("wp_post_id")
        if not wp_id:
            continue

        # Retrouve l'event brut correspondant via le slug hyrox stocké dans le state
        slug_hyrox = entry.get("slug_hyrox_actuel")
        ev = raw_by_slug.get(slug_hyrox)
        if not ev:
            print(f"  [SKIP] {ville} : event brut introuvable pour slug={slug_hyrox}")
            continue

        print(f"\n[{ville}] wp_id={wp_id}  {ev['nom_event']}")

        if PRICE_ONLY:
            price = h.fetch_event_detail(ev["url_event_hyrox"]).get("price", "")
            if not price:
                print("  [SKIP] pas de prix trouvé (billetterie pas encore ouverte)")
                continue
            print(f"  prix : {price}")
            if DRY_RUN:
                print("  [DRY] pas de PATCH envoyé")
                done += 1
                continue
            try:
                h.wp_rest("patch", f"events/{wp_id}", json={"meta": {"ova_mb_event_price_desc": price}})
                print("  ✓ PATCH OK")
                done += 1
            except Exception as e:
                print(f"  [ERR PATCH] {e}")
                errors += 1
            continue

        query = h.city_guess(ev["nom_event"])
        geo = h.geocode_free(query)
        if not geo.get("country_code"):
            print(f"  [ERR] géocodage échoué pour '{query}'")
            errors += 1
            continue

        city_local = geo.get("city_name") or query
        city_fr    = h.french_city_name(city_local)
        country_nm = h.COUNTRY_CODE_TO_NAME.get(geo["country_code"], geo["country_code"])
        qualifier  = h.venue_qualifier(query, city_local)

        detail = h.fetch_event_detail(ev["url_event_hyrox"])
        if detail.get("venue_address"):
            precise = h.geocode_free(f"{detail['venue_address']}, {country_nm}")
            if precise.get("lat"):
                geo = precise

        payload = h.build_post(ev, city_fr, country_nm, geo, detail, qualifier)
        # Ne jamais repasser un post publié en brouillon lors d'une correction.
        payload.pop("status", None)
        patch = {"content": payload["content"]} if DESC_ONLY else payload

        if DESC_ONLY:
            print(f"  contenu ({len(payload['content'])} car.) : {payload['content'][:160]}...")
        else:
            print(f"  titre    : {payload['title']}")
            print(f"  slug     : {payload['slug']}")
            print(f"  adresse  : {payload['meta']['ova_mb_event_address']}")
            print(f"  prix     : {payload['meta']['ova_mb_event_price_desc']}")
            print(f"  cat/tags : {payload['event_cat']} / {payload['event_tag']}")
            print(f"  contenu  : {payload['content'][:120]}...")

        if DRY_RUN:
            print("  [DRY] pas de PATCH envoyé")
            done += 1
            continue

        try:
            h.wp_rest("patch", f"events/{wp_id}", json=patch)
            print(f"  ✓ PATCH OK")
            done += 1
        except Exception as e:
            print(f"  [ERR PATCH] {e}")
            errors += 1

    print(f"\n{'='*50}\ncorrigés : {done}   erreurs : {errors}")


if __name__ == "__main__":
    main()
