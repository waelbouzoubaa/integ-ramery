-- Migration : unification des unites synonymes (f/ft/fft/ff/for/forf/forfait,
-- u/unitaire/unite/lunite/un/piece(s), t/latonne/tonne, lemetre(carre/cube),
-- ml, ensemble, heure) - voir normaliser_unite dans schema.sql.
--
-- ORDRE IMPORTANT (pieges deja rencontres, voir memoire du projet) :
-- 1. La colonne generee STORED price_lines.unite_canonique ne se recalcule
--    QUE si la colonne source est touchee (SET unite = unite).
-- 2. groupes.unite et fusion_decisions.unite sont des valeurs STOCKEES (pas
--    des colonnes generees) : a resynchroniser a la main.
-- 3. Les index uniques idx_groupes_cle et idx_fusion_decisions_cle peuvent
--    entrer en collision lors du renommage en masse -> dedoublonner AVANT.
--
-- Prerequis : la nouvelle version de normaliser_unite est deja en place
-- (CREATE OR REPLACE depuis schema.sql, ou ensure_schema()).

BEGIN;

-- Recalcul de la colonne generee unite_canonique
UPDATE price_lines SET unite = unite;

-- Groupes qui deviennent identiques sous la nouvelle normalisation
-- (ex: "Constat d'huissier" existait en 'f' ET 'ft') : on garde un seul id
-- par cle (le plus petit), en conservant la validation si au moins un des
-- deux etait valide. membres_signature sera recalculee au prochain run de
-- fusion_designations.py.
CREATE TEMP TABLE _collisions ON COMMIT DROP AS
SELECT designation_canonique AS dc, coalesce(sous_famille,'') AS sf,
       normaliser_unite(unite) AS u2,
       min(id) AS garder, bool_or(valide) AS un_valide,
       min(valide_le) AS premier_valide_le
FROM groupes
GROUP BY 1, 2, 3
HAVING count(*) > 1;

UPDATE groupes g
SET valide = c.un_valide,
    valide_le = coalesce(g.valide_le, c.premier_valide_le)
FROM _collisions c
WHERE g.id = c.garder;

DELETE FROM groupes g
USING _collisions c
WHERE g.designation_canonique = c.dc
  AND coalesce(g.sous_famille,'') = c.sf
  AND normaliser_unite(g.unite) = c.u2
  AND g.id <> c.garder;

UPDATE groupes SET unite = normaliser_unite(unite)
WHERE unite IS DISTINCT FROM normaliser_unite(unite);

-- fusion_decisions (cache Gemini) : meme dedoublonnage (garde la decision la
-- plus recente = id max), puis normalisation.
DELETE FROM fusion_decisions fd
USING fusion_decisions fd2
WHERE fd.id < fd2.id
  AND coalesce(fd.sous_famille,'') = coalesce(fd2.sous_famille,'')
  AND normaliser_unite(fd.unite) IS NOT DISTINCT FROM normaliser_unite(fd2.unite)
  AND fd.designation_a = fd2.designation_a
  AND fd.designation_b = fd2.designation_b;

UPDATE fusion_decisions SET unite = normaliser_unite(unite)
WHERE unite IS DISTINCT FROM normaliser_unite(unite);

COMMIT;

-- Controle : plus aucune ecriture synonyme ne doit rester
SELECT unite, count(*) AS designations
FROM prix_moyen_par_designation
WHERE unite IN ('f','ft','fft','ff','for','forf','unitaire','unite','lunite',
                'un','piece','pieces','latonne','tonne','lemetre',
                'lemetrecarre','lemetrecube','ensemble','heure','ml')
GROUP BY 1;
