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

# schema.sql applique en "sandwich" autour des migrations - deux besoins
# opposes selon la migration :
# - migration_unites.sql a besoin du schema A JOUR (nouvelle
#   normaliser_unite()) AVANT de tourner, sinon l'UPDATE recalculerait les
#   colonnes generees avec l'ancienne fonction, sans erreur, silencieusement.
# - migration_agence.sql (ALTER TABLE ADD COLUMN agence) doit au contraire
#   passer AVANT le schema, puisque prix_moyen_par_designation_fn() reference
#   cette colonne des sa definition - schema.sql echouerait sinon
#   ("column pd.agence does not exist").
# Rejouer schema.sql une 2e fois apres les migrations resout les deux cas a
# la fois : il est entierement idempotent (CREATE OR REPLACE FUNCTION,
# CREATE TABLE IF NOT EXISTS, DROP VIEW + CREATE VIEW), le rejouer ne casse
# jamais rien.
echo "==> Application du schema (1er passage, avant les migrations)"
docker compose exec -T postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 -f - < src/db/schema.sql || true

echo "==> Migrations de donnees one-off"
for migration in scripts/migration_*.sql; do
    echo "  -> $migration"
    docker compose exec -T postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 -f - < "$migration"
done

echo "==> Application du schema (2e passage, apres les migrations)"
docker compose exec -T postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 -f - < src/db/schema.sql

echo "==> Deploiement termine."
