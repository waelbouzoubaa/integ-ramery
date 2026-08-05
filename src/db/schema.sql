-- Phase 1 : table de lignes de prix propre, tracable jusqu'au PDF source.
-- Postgres = seule source de verite.

CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE IF NOT EXISTS price_documents (
    id          bigserial PRIMARY KEY,
    filename    text        NOT NULL UNIQUE,
    imported_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS price_lines (
    id             bigserial PRIMARY KEY,
    document_id    bigint      NOT NULL REFERENCES price_documents(id) ON DELETE CASCADE,

    numero         text,                   -- brut, tracabilite uniquement (pas fiable comme cle)
    chapitre       text        NOT NULL,   -- titre de section racine
    sous_famille   text,                   -- dernier header sans prix rencontre (ou NULL)
    designation    text        NOT NULL,   -- brute, telle qu'extraite - jamais modifiee

    unite          text,
    quantite       numeric(14, 3),
    prix_unitaire  numeric(14, 4) NOT NULL,
    montant_ht     numeric(14, 2),

    created_at     timestamptz NOT NULL DEFAULT now()
);

-- CREATE TABLE IF NOT EXISTS n'ajoute pas de colonne a une table existante :
-- toute nouvelle colonne doit passer par un ALTER explicite ici.
ALTER TABLE price_lines ADD COLUMN IF NOT EXISTS designation_canonique text;
-- Regroupement de "quasi-doublons" (pluriel, boilerplate, quasi-synonymes).
-- NULL tant que la fusion n'a pas tourne ; la vue retombe sur designation
-- brute en attendant (voir fusion_designations.py).

CREATE INDEX IF NOT EXISTS idx_price_lines_designation ON price_lines (designation);
CREATE INDEX IF NOT EXISTS idx_price_lines_document    ON price_lines (document_id);
CREATE INDEX IF NOT EXISTS idx_price_lines_desig_trgm
    ON price_lines USING gin (designation gin_trgm_ops);

-- Vue de travail : moyenne de prix par (sous_famille, unite, designation
-- canonique). sous_famille ET unite font partie de la cle :
-- - sous_famille : "avec grille fonte 100 mm" sous "classe 250" vs "classe
--   400" - prix reels differents.
-- - unite : le meme texte facture au ml dans un document et au m2 dans un
--   autre n'est pas le meme prix comparable (base de calcul differente) -
--   ne jamais les moyenner ensemble.
-- Ne jamais retirer l'un ou l'autre de ce GROUP BY.
-- designation_canonique vaut NULL tant que la fusion n'a pas tourne ; la vue
-- utilise designation brute en attendant (COALESCE).
-- DROP necessaire : CREATE OR REPLACE ne permet pas de changer l'ordre des
-- colonnes d'une vue existante (ici on insere sous_famille en 1ere position).
DROP VIEW IF EXISTS prix_moyen_par_designation;
CREATE VIEW prix_moyen_par_designation AS
SELECT
    sous_famille,
    unite,
    coalesce(designation_canonique, designation) AS designation,
    count(*)                       AS nb_occurrences,
    avg(prix_unitaire)             AS prix_moyen,
    stddev_samp(prix_unitaire)     AS ecart_type,
    min(prix_unitaire)             AS prix_min,
    max(prix_unitaire)             AS prix_max
FROM price_lines
GROUP BY sous_famille, unite, coalesce(designation_canonique, designation);
