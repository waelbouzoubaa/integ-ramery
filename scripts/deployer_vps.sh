#!/usr/bin/env bash
# Deploiement complet sur le VPS : pull le code, rebuild les images, redemarre
# les services, puis rejoue les migrations de donnees one-off (celles qui ne
# sont PAS dans schema.sql, car ensure_schema() ne les rejoue jamais - elles
# recalculent/dedoublonnent des donnees deja en base, pas la structure).
#
# Usage (depuis la racine du repo, sur le VPS) :
#   bash scripts/deployer_vps.sh
#
# Idempotent : chaque migration listee ci-dessous peut etre rejouee sans
# risque (verifie pour migration_unites.sql - ne fait que des UPDATE
# conditionnels et des fusions de doublons deja resolus au 2e passage).

set -euo pipefail

# POSTGRES_USER/POSTGRES_DB sont dans .env (lu par docker-compose.yml pour les
# conteneurs, mais pas automatiquement expose a ce script bash) - on les
# source explicitement.
set -a
source .env
set +a

echo "==> git pull"
git pull

echo "==> Rebuild des images (watcher + streamlit)"
docker compose build watcher streamlit

echo "==> Redemarrage streamlit (watcher redemarre seul si le service tourne en continu)"
docker compose up -d streamlit watcher

# schema.sql AVANT les migrations, toujours. Convention du projet (voir
# CLAUDE.md) : toute nouvelle colonne vit en ALTER TABLE ... IF NOT EXISTS
# DANS schema.sql (jamais dans un script de migration a part) - donc
# schema.sql est TOUJOURS suffisant pour amener la structure a jour avant
# qu'une migration de DONNEES (scripts/migration_*.sql) ne tourne dessus.
# C'est l'inverse qui casserait : migration_unites.sql a besoin de la
# NOUVELLE normaliser_unite() avant de recalculer les colonnes generees,
# sinon l'UPDATE les recalculerait avec l'ancienne fonction, sans erreur,
# silencieusement.
echo "==> Application du schema (fonctions/vues/colonnes a jour)"
docker compose exec -T postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 -f - < src/db/schema.sql

echo "==> Migrations de donnees one-off"
for migration in scripts/migration_*.sql; do
    echo "  -> $migration"
    docker compose exec -T postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 -f - < "$migration"
done

echo "==> Deploiement termine."
