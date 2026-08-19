# Brief technique — Import automatique des événements HYROX officiels

**Contexte :** wod-open.com importe déjà automatiquement les compétitions CrossFit depuis Scoring.fit et Competition Corner. Objectif : ajouter une 3e source, HYROX officiel (hyrox.com), pour être réactif sur la création des events et sur l'ouverture des inscriptions — sans dépendre d'une API (elle n'existe pas).

**Source :** `https://hyrox.com/find-my-race/`

---

## 0. Vérification préalable (à faire en premier, avant tout code)

La page a été testée via un outil de fetch qui peut exécuter du JS avant extraction. On ne sait pas encore si le HTML brut (sans navigateur) contient déjà la liste des events.

```bash
curl -s -A "Mozilla/5.0" https://hyrox.com/find-my-race/ | grep -o 'hyrox.com/event/[a-z0-9-]*' | head -20
```

- **Si la liste d'events apparaît** → scraping léger possible (requests + BeautifulSoup en Python, ou wp-cli + curl). Pas besoin de navigateur headless.
- **Si la liste est vide** → le contenu est injecté en JS après coup, il faudra un scraper headless (Playwright). Plus lourd mais même logique ensuite.

Ce test conditionne le choix technique de l'étape 1 — à faire avant d'écrire le reste du script.

---

## 1. Extraction des données

Pour chaque event trouvé sur la page liste, extraire :

| Champ | Exemple | Notes |
|---|---|---|
| `nom_event` | "LUCIS HYROX TOULOUSE" | inclut souvent le sponsor titre |
| `ville` | "Toulouse" | déduite du nom ou du code aéroport (TLS) |
| `slug_hyrox` | `hyrox-toulouse-s26-27` | **change à chaque saison**, ne pas l'utiliser comme clé stable |
| `date_debut` / `date_fin` | 3 fév 2027 – 7 fév 2027 | format à normaliser (le site mélange formats courts) |
| `url_event_hyrox` | `https://hyrox.com/event/hyrox-toulouse-s26-27/` | lien vers la page officielle |
| `image_url` | URL de la photo affichée | pour l'image à la une côté WP si souhaité |
| `statut_bouton` | `"Buy Tickets"` ou `"Find out more"` | **signal clé, voir section 3** |

Le champ `statut_bouton` est disponible directement sur la page liste, pas besoin d'aller sur chaque page event pour ça.

---

## 2. Filtrage géographique

Filtrer sur une **whitelist de villes/pays** que tu maintiens toi-même (fichier JSON ou CSV à côté du script, éditable sans toucher au code) :

```json
{
  "pays_inclus": ["France"],
  "villes_limitrophes": ["Genève", "Bâle", "Karlsruhe", "..."]
}
```

Raisonnement : le nom de ville seul dans le titre HYROX n'est pas toujours fiable (sponsors, accents variables) → mieux vaut matcher sur une liste blanche de villes cibles que sur une déduction pays automatique fragile.

---

## 3. Logique de non-duplication + réactivité inscriptions/prix

**Le vrai enjeu ici n'est pas seulement "ne pas recréer un event existant" mais aussi "détecter quand un event déjà connu passe de 'pas encore ouvert' à 'inscriptions ouvertes'."** Deux besoins différents, une seule table d'état.

### Fichier d'état (state file, JSON ou table WP custom)

Une ligne par event HYROX suivi, avec :

| Champ | Rôle |
|---|---|
| `ville_normalisee` | clé de correspondance stable (le slug HYROX change de saison en saison) |
| `slug_hyrox_actuel` | pour détecter si HYROX a changé le slug d'une édition à l'autre |
| `wp_post_id` | ID de la page événement correspondante sur wod-open.com (CPT `event`) |
| `dernier_statut_bouton` | `"Find out more"` ou `"Buy Tickets"` au dernier passage |
| `dernier_check` | date du dernier scraping |
| `prix_connu` | si affiché sur la page event individuelle, sinon vide |

### Logique à chaque run hebdomadaire

1. **Nouvel event non présent dans le state file** (par ville_normalisee) → créer le brouillon WP + ajouter la ligne au state file
2. **Event déjà présent** :
   - Si `dernier_statut_bouton == "Find out more"` et que le nouveau statut est `"Buy Tickets"` → **c'est le déclencheur de réactivité que tu veux** : les inscriptions viennent de s'ouvrir. Aller chercher la page event individuelle pour récupérer le prix s'il est affiché, mettre à jour la page WP, et te notifier (email ou log dans le livrable)
   - Si le slug HYROX a changé (nouvelle saison, même ville) → mettre à jour `url_event_hyrox` et `slug_hyrox_actuel` sans recréer de page, cohérent avec ta règle evergreen
   - Sinon → ne rien faire, juste mettre à jour `dernier_check`

Ça évite de re-scraper inutilement les pages individuelles de tous les events à chaque fois : seuls les nouveaux events et ceux encore en "Find out more" nécessitent d'aller chercher le détail. Une fois qu'un event passe en "Buy Tickets" et que le prix est capturé, il peut sortir de la surveillance active (garder un check mensuel léger seulement si tu veux suivre d'éventuels changements de prix early bird → tarif plein).

---

## 4. Fréquence

Cron hebdomadaire, un jour fixe (ex. lundi matin, avant ta routine de contenu quotidienne si tu veux que les nouveaux events alimentent d'éventuels articles).

---

## 5. Publication WordPress

Cohérent avec tes principes actuels :

- Écriture via `wp eval-file` avec `wp_set_current_user()` avant `wp_update_post()` / `wp_insert_post()` — pas de shell inline avec du HTML long
- Anti-hallucination : toutes les données viennent du scraping, aucune génération IA sur les faits (dates, prix, lieux) — pas de risque ici contrairement à la rédaction d'articles
- **Point à trancher toi-même** : publication directe en `publish`, ou passage en `draft` avec notification pour validation manuelle avant mise en ligne ? Ta routine de contenu SEO utilise un livrable de review avant publication — tu peux vouloir le même gate ici, ou au contraire publier direct puisque la donnée est factuelle et vérifiable (contrairement à du contenu généré). À toi de voir selon combien de confiance tu veux accorder à un scraper avant mise en prod.

---

## 6. Notification de réactivité

Pour le déclencheur "inscriptions ouvertes" (section 3), prévoir un canal de notification simple : email, ou entrée loggée dans le même type de livrable que ta routine quotidienne, pour que tu puisses réagir vite (ex. relayer sur Instagram, pousser en avant sur la homepage).

---

## 7. Robustesse

- User-Agent réaliste, délai raisonnable entre requêtes (pas de scraping agressif)
- Retry avec backoff en cas d'échec réseau
- Logging des erreurs de parsing (structure HTML peut changer sans prévenir)
- Alerte si le scraping échoue 2 runs de suite (signe que la structure de la page a changé)
