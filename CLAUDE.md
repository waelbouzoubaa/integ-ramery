# integ-ramery2 — base de prix DQE/BPU

Extraction de bordereaux de prix BTP (PDF) vers une base de prix unitaires
fiable, utilisable par l'équipe métier pour estimer un bordereau vierge.
Objectif permanent du projet : la **qualité de donnée**, pas la vitesse -
en cas de doute entre "aller plus vite" et "rester vérifiable", choisir
vérifiable.

## Pipeline

```
SharePoint (1 dossier racine = 1 agence)
   -> watcher (delta Graph API, retry reseau)
   -> extraction Gemini (JSON force, cache sur disque dans out/)
   -> PostgreSQL (source de verite, jamais modifiee a la main hors migrations)
   -> normalisation deterministe (unite/sous-famille/nombres/sens de comparaison)
   -> fusion des quasi-doublons (Gemini + cache de decisions)
   -> Streamlit (4 pages, voir plus bas)
```

Services Docker Compose : `postgres`, `watcher` (ingestion continue), `streamlit`
(port 8502). `docker compose up -d` pour tout lancer en local.

## Modèle de données - ce qui est fiable, ce qui est un avis

Le projet distingue explicitement deux couches (voir aussi `MEMORY.md` côté
utilisateur, section "science exacte vs science pas exacte") :

**Déterministe, jamais de bruit** : `normaliser()`, `normaliser_unite()`,
`normaliser_sous_famille()`, `memes_nombres()`/`_sens_comparaison()`,
`similarity()` de Postgres (pg_trgm - un score, mais du calcul pur, pas un
avis), tous les filtres appliqués AVANT que Gemini ne voie une paire.

**Avis de Gemini, peut se tromper** : le jugement pairwise "même produit ?"
(fusion), le score de cohérence d'un groupe (`groupes.seuil_confiance`),
l'arbitrage de la Recherche de prix.

### Tables clés

- `price_documents` : un PDF = une ligne. `agence` = dossier SharePoint de
  1er niveau (voir plus bas). `filename` unique, sans extension.
- `price_lines` : une ligne de prix brute. `designation` **n'est jamais
  modifiée** (traçabilité jusqu'au PDF source) - toute normalisation vit
  dans des colonnes séparées :
  - `unite_canonique`, `sous_famille_canonique` : colonnes **générées
    STORED**, recalculées automatiquement à partir de `unite`/`sous_famille`
    via `normaliser_unite()`/`normaliser_sous_famille()`.
  - `designation_canonique` : PAS générée, écrite par `fusion_designations.py`.
    NULL tant que la fusion n'a pas tourné sur cette ligne.
  - `fusion_manuelle` : un humain a verrouillé l'appartenance de cette ligne
    (validation de groupe, retrait, ajout manuel) - la fusion automatique ne
    la touche plus jamais.
  - `en_attente` : rattachée automatiquement à un groupe déjà validé (par
    ressemblance de texte, sans jugement Gemini pairwise), pas encore
    reconfirmée par un humain.
- `groupes` : un groupe = `(designation_canonique, sous_famille, unite)`.
  `valide` = verrouillé par un humain. `seuil_confiance` = score Gemini
  (0 à 1) sur la cohérence de l'ensemble - **un groupe non validé participe
  déjà au calcul du prix moyen**, valide ne veut pas dire moyenné, ça veut
  dire verrouillé.
- `fusion_decisions` : cache des paires déjà jugées (par Gemini ou à la
  main) - jamais repayé. `designation_a < designation_b` toujours.
- `echecs_traitement` : fichiers perdus (téléchargement/extraction/chargement)
  - le watcher avance son delta token que le fichier ait réussi ou non, sans
    cette table un échec disparaîtrait silencieusement.
- `parametres` : réglages ajustables depuis l'UI (seuil d'anomalie de prix,
  seuil d'auto-validation des groupes) - une seule ligne, id=1.

### Vue de prix : `prix_moyen_par_designation`

C'est un simple alias de `prix_moyen_par_designation_fn(p_agence DEFAULT NULL)`.
`p_agence = NULL` (le cas de la vue) = comportement identique à avant le
multi-agences, aucune régression. Passer une agence recalcule tout depuis
zéro sur son seul sous-ensemble (pas un filtre visuel après coup). La
fonction n'est PAS matérialisée : recalcul complet à chaque appel (~450ms
pour ~12 700 lignes actuellement - non problématique à cette échelle).

Correction d'anomalies : méthode IQR itérative (voir
`corriger_valeurs_aberrantes()`), déclenchée si le coefficient de variation
dépasse `parametres.seuil_cv_anomalie`. Un `*` sur une désignation dans
Prix Unitaires = au moins une valeur exclue automatiquement.

## Règle d'or : faire évoluer le schéma sans rien casser

**Ajouter une colonne à une table existante** : `ALTER TABLE ... ADD COLUMN
IF NOT EXISTS ...` écrit **directement dans `schema.sql`**, juste après le
`CREATE TABLE` concerné (voir `fusion_manuelle`, `en_attente`, `agence` comme
exemples). JAMAIS dans un script à part. `schema.sql` est rejoué en entier à
chaque démarrage du watcher (`ensure_schema()`) et par `deployer_vps.sh` -
un `ALTER ... IF NOT EXISTS` est sans risque à rejouer indéfiniment.

**Transformer des données déjà en base** (recalcul en masse, dédoublonnage,
backfill) : un nouveau `scripts/migration_<nom>.sql`, **idempotent**
(rejouable sans effet de bord au 2e passage - voir `migration_unites.sql`
pour le patron : dédoublonnage AVANT renommage, jamais l'inverse).
`deployer_vps.sh` rejoue TOUS les fichiers `scripts/migration_*.sql` à
CHAQUE déploiement, sans suivi de "déjà appliqué" - l'idempotence n'est pas
optionnelle.

**Pourquoi cet ordre (schema.sql avant les migrations) et pas l'inverse** :
une migration de données a presque toujours besoin des fonctions/colonnes
LES PLUS RÉCENTES pour recalculer juste - `migration_unites.sql` recalcule
des colonnes générées via `UPDATE table SET col = col`, qui utilise la
fonction `normaliser_unite()` **définie au moment de l'UPDATE** ; si
l'ancienne fonction était encore en place, le recalcul serait faux SANS
ERREUR (piège déjà rencontré en pratique). Tant que les ajouts de colonnes
restent dans `schema.sql` (jamais dans une migration), il n'y a jamais de
conflit d'ordre inverse.

**Colonnes générées STORED ne se recalculent PAS rétroactivement** quand la
fonction dont elles dépendent change - ni via `CREATE OR REPLACE FUNCTION`
seul, ni via un `UPDATE` qui ne touche pas la colonne SOURCE. Pour forcer un
recalcul complet : `UPDATE table SET colonne_source = colonne_source`
(toucher la vraie colonne source, pas une autre) - ou repartir d'un
TRUNCATE + reload complet si plus sûr.

## Outils de mesure et d'audit (`scripts/`)

- `mesurer_rapprochement.py` : mesure CLI du taux de rapprochement (page
  Recherche de prix) sur un bordereau de test, avec cache de l'extraction
  Gemini (`data/test/*.extraction.json`) - les runs sont comparables entre
  eux car l'extraction ne varie pas d'un run à l'autre. Toute modification
  du pipeline d'arbitrage dans `recherche_de_prix.py` doit être répliquée
  ici (le fichier duplique volontairement le prompt/la config Gemini).
- `auditer_groupes.py` : audit MÉCANIQUE (pas sémantique) des groupes -
  détecte les membres aux nombres différents ou au sens de comparaison
  opposé (`<` vs `>`) qui n'auraient jamais dû fusionner. Un groupe à "0
  suspect" n'est pas forcément sémantiquement juste (voir l'incident
  "Installation de chantier" / "Amené-repli matériel" - deux postes
  distincts fusionnés à tort malgré des nombres/sens cohérents).
- `analyser_rejets.py` : rejoue le préfiltre pour une liste de désignations
  rejetées par Gemini et affiche les candidats qu'il a vus - sert à
  distinguer un rejet légitime (produit absent) d'un rejet trop strict.
- `deployer_vps.sh` : pull + rebuild + `schema.sql` + migrations, en une
  commande, sur le VPS.

## Multi-agences

Un dossier SharePoint de **premier niveau** (racine du drive) = une agence,
peu importe son nom (`_agence_from_item()` dans `pdf_watcher.py` - prend le
1er segment du chemin, pas le dossier parent immédiat, donc robuste à un
sous-dossier sous une agence). Plus de filtre `SHAREPOINT_FOLDER` (retiré) :
tout le drive est scanné. Un fichier déplacé d'une agence à l'autre remonte
via le delta SharePoint normal et se recharge depuis le JSON déjà en cache
(`out/`), **sans re-extraction**. `load_items()` protège `agence` contre un
écrasement par un rechargement local en masse (`agence=None` par défaut) via
un `COALESCE` côté `UPDATE` - seul un appel qui CONNAÎT vraiment l'agence
(le watcher) peut la modifier.

## Pièges Windows / Docker déjà rencontrés

- **Git Bash + `docker exec`/`docker compose exec`** : préfixer
  `MSYS_NO_PATHCONV=1`, sinon Git Bash traduit les chemins Unix du conteneur
  (`/app/...`) en chemins Windows.
- **`docker compose build` ne recrée pas toujours le conteneur** : après un
  build, vérifier `docker compose ps` (colonne CREATED) - utiliser
  `docker compose up -d --force-recreate <service>` si le code ne semble pas
  appliqué.
- **`watcher` et `streamlit` sont des images buildées (`build: .`), pas de
  bind-mount sur `src/`** : `docker compose build` + `up -d` obligatoires
  après CHAQUE changement Python.
- **`ensure_schema()` ne tourne QUE dans le watcher** (à son démarrage), pas
  dans Streamlit - un déploiement doit soit redémarrer le watcher et
  attendre, soit (plus fiable, ce que fait `deployer_vps.sh`) appliquer
  `schema.sql` explicitement via `psql`, sans compter sur le watcher qui
  démarre en asynchrone.
- Chemin du script CLI à passer en ABSOLU dans `docker compose run`
  (`/app/src/db/fusion_designations.py`, pas un chemin relatif) - le
  `working_dir` du service watcher n'est pas la racine du repo.

## Règles de collaboration (établies avec l'utilisateur)

- **Ne jamais `git push` sans permission explicite** - committer localement
  si besoin, mais toujours demander avant de pousser.
- **Tester en local avant de toucher au VPS**, systématiquement - le VPS est
  la référence pour l'équipe métier, jamais un environnement de test.
- Avant un changement de fonction de normalisation ou de règle de fusion :
  mesurer avant/après avec `mesurer_rapprochement.py` sur le bordereau de
  test habituel, chiffrer l'effet, vérifier l'absence de faux rapprochement
  sur des cas volontairement contradictoires (voir les "sentinelles" dans
  l'historique de session) avant de committer.
- Toute base de prix reconstruite "à zéro" (nouvel environnement, nouvelle
  agence) doit repartir des JSON déjà extraits dans `out/` quand ils
  existent - ne jamais refaire payer une extraction Gemini déjà faite.
