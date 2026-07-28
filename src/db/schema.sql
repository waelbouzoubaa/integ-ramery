-- Phase 1 : table de lignes de prix propre, tracable jusqu'au PDF source.
-- Postgres = seule source de verite.

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
    designation    text        NOT NULL,

    unite          text,
    quantite       numeric(14, 3),
    prix_unitaire  numeric(14, 4) NOT NULL,
    montant_ht     numeric(14, 2),

    created_at     timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_price_lines_designation ON price_lines (designation);
CREATE INDEX IF NOT EXISTS idx_price_lines_document    ON price_lines (document_id);

-- Vue de travail : moyenne de prix par designation exacte, avec ecart-type
-- pour reperer les cas ou la moyenne est bruitee (grande dispersion).
CREATE OR REPLACE VIEW prix_moyen_par_designation AS
SELECT
    designation,
    count(*)                       AS nb_occurrences,
    avg(prix_unitaire)             AS prix_moyen,
    stddev_samp(prix_unitaire)     AS ecart_type,
    min(prix_unitaire)             AS prix_min,
    max(prix_unitaire)             AS prix_max
FROM price_lines
GROUP BY designation;
