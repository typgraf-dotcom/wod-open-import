"""
Step 1 - Récupération des compétitions depuis scoring.fit
Affiche les événements futurs/live de manière lisible.
"""

import sys
import io
import requests
import json
from datetime import datetime

# Fix encodage Windows (cp1252 -> utf-8)
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")


API_URL = (
    "https://scoring-fit-prod-7a29180d25c8.herokuapp.com"
    "/api/leaderboard/competition/search-query"
)

DEFAULT_PARAMS = {
    "searchTerm": "",
    "ticketingPublished": "false",
    "period": "live/future",
    "pageNumber": 1,
    "pageSize": 50,
}


def fetch_competitions(params: dict = None) -> list:
    """Récupère les compétitions depuis l'API scoring.fit."""
    p = {**DEFAULT_PARAMS, **(params or {})}
    resp = requests.get(API_URL, params=p, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    # L'API renvoie soit une liste directe, soit {"data": [...]}
    if isinstance(data, list):
        return data
    return data.get("data", data.get("competitions", []))


def parse_iso(iso_str: str) -> datetime | None:
    """Parse une date ISO 8601 en datetime."""
    if not iso_str:
        return None
    try:
        return datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
    except ValueError:
        return None


def display_competition(comp: dict, index: int) -> None:
    """Affiche une compétition de façon lisible."""
    date_start = comp.get("date", {}).get("start", {})
    date_end   = comp.get("date", {}).get("end", {})

    start_day  = date_start.get("day", "?")
    start_hour = date_start.get("hour", "?")
    end_day    = date_end.get("day", "?")
    end_hour   = date_end.get("hour", "?")

    # Certains champs sont au 1er niveau, d'autres dans _event selon l'item
    event_sub = comp.get("_event") or {}

    name     = comp.get("name", "Sans nom")
    category = comp.get("category") or event_sub.get("category", "N/A")
    ctype    = comp.get("type", "N/A")          # online | inside
    country  = comp.get("country") or event_sub.get("country", "N/A")
    location = comp.get("location") or event_sub.get("location", "")
    icon     = comp.get("iconLink", "")
    btn_url  = comp.get("buttonLink", {}).get("url", "")
    total_p  = comp.get("total_participants", 0)
    event_id = comp.get("_id", "")

    location_str = f"{location}, {country}" if location else country

    print(f"\n{'='*60}")
    print(f"[{index:02d}] {name}")
    print(f"  ID       : {event_id}")
    print(f"  Catégorie: {category}  |  Type: {ctype}")
    print(f"  Lieu     : {location_str}")
    print(f"  Début    : {start_day} à {start_hour}")
    print(f"  Fin      : {end_day} à {end_hour}")
    print(f"  Inscrits : {total_p}")
    if btn_url:
        print(f"  Lien     : https://app.scoring.fit/leaderboard/{btn_url}")
    if icon:
        print(f"  Logo     : {icon}")

    # Infos ticketing si présentes
    ticketing = comp.get("_ticketing")
    if ticketing and isinstance(ticketing, dict):
        open_d  = ticketing.get("open_date", "")
        end_d   = ticketing.get("end_date", "")
        spots   = ticketing.get("total_spots")
        pub     = ticketing.get("publish", False)
        print(f"  Billetterie: publié={pub}  |  spots={spots or 'illimité'}")
        if open_d:
            print(f"    Inscriptions: {(open_d or '')[:10]} -> {(end_d or '')[:10]}")

    # Divisions
    divisions = comp.get("divisions", [])
    if divisions:
        div_names = [d.get("type", "?") for d in divisions]
        counts    = [str(d.get("participants_count", 0)) for d in divisions]
        print(f"  Divisions: {', '.join(div_names)}  ({', '.join(counts)} participants)")


def main():
    print("Récupération des compétitions scoring.fit...")
    print(f"URL: {API_URL}\n")

    try:
        competitions = fetch_competitions()
    except requests.RequestException as e:
        print(f"Erreur API: {e}")
        return

    print(f"Total récupéré: {len(competitions)} compétition(s)\n")

    if not competitions:
        print("Aucune compétition trouvée.")
        return

    # Tri par date de début
    def sort_key(c):
        iso = c.get("date", {}).get("start", {}).get("iso", "")
        return iso or "9999"

    competitions.sort(key=sort_key)

    for i, comp in enumerate(competitions, 1):
        display_competition(comp, i)

    print(f"\n{'='*60}")
    print(f"Total: {len(competitions)} compétitions")

    # Sauvegarde JSON pour inspection
    with open("competitions_raw.json", "w", encoding="utf-8") as f:
        json.dump(competitions, f, ensure_ascii=False, indent=2)
    print("Données brutes sauvegardées dans competitions_raw.json")


if __name__ == "__main__":
    main()
