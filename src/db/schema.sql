-- Phase 1 : table de lignes de prix propre, tracable jusqu'au PDF source.
-- Postgres = seule source de verite.

CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- Normalise une unite pour qu'elle serve de cle de regroupement fiable :
-- minuscules, accents et exposants retires (m2/M2/m² -> "m2"), ponctuation
-- et espaces retires (FT / ft / F.T. -> "ft"). Sans ca, la meme unite
-- ecrite differemment d'un document a l'autre coupe silencieusement des
-- groupes qui devraient etre fusionnes (le meme produit au meme "m2" perd
-- une partie de ses occurrences juste parce qu'un PDF ecrit "M2").
-- Le "/" et le "%" sont volontairement CONSERVES (jamais retires comme le
-- reste de la ponctuation) : ce sont des unites a part entiere dans ce
-- metier, pas de la simple ponctuation decorative.
-- - "/" : unites fractionnaires ("1/2 J" = demi-journee). Le retirer
--   transformerait "1/2 J" en "12j" (douze jours) - bug reel constate.
-- - "%" : unite "pourcentage" a elle seule (ex: "Moins-value pour chantier
--   en route barree", exprimee en % du prix de base). Le retirer laisse une
--   chaine vide -> NULL, ce qui merge silencieusement ces lignes avec
--   toutes celles qui n'ont VRAIMENT aucune unite - bug reel constate.
-- IMMUTABLE : requis pour l'utiliser dans une colonne generee (voir plus bas).
CREATE OR REPLACE FUNCTION normaliser_unite(u text) RETURNS text
LANGUAGE sql IMMUTABLE AS $$
    SELECT NULLIF(
        regexp_replace(
            translate(lower(trim(u)), 'àâäéèêëîïôöùûüç²³', 'aaaeeeeiioouuuc23'),
            '[^a-z0-9/%]', '', 'g'
        ),
        ''
    )
$$;

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

ALTER TABLE price_lines ADD COLUMN IF NOT EXISTS unite_canonique text
    GENERATED ALWAYS AS (normaliser_unite(unite)) STORED;
-- Version normalisee de `unite`, calculee automatiquement (jamais ecrite a
-- la main) : c'est elle qui sert de cle de regroupement partout (vue,
-- fusion_designations.py, table groupes), jamais `unite` brute. `unite`
-- brute reste affichee telle quelle pour la tracabilite.

ALTER TABLE price_lines ADD COLUMN IF NOT EXISTS fusion_manuelle boolean NOT NULL DEFAULT false;
-- Un humain a decide explicitement l'appartenance (groupe) de cette ligne
-- (validation d'un groupe qui la contient, reassignation apres retrait, ou
-- laissee seule apres retrait). Le script de fusion automatique ne doit
-- JAMAIS modifier designation_canonique sur une ligne ou ce flag est true.

ALTER TABLE price_lines ADD COLUMN IF NOT EXISTS en_attente boolean NOT NULL DEFAULT false;
-- true = cette ligne a ete ajoutee automatiquement a un groupe deja valide
-- (matching incremental contre le nom du groupe, au-dessus de son seuil de
-- confiance), mais pas encore reconfirmee par un humain. Sert juste a
-- l'affichage Streamlit (couleur differente) ; repasse a false quand le
-- groupe est revalide.

-- Un groupe = une combinaison (designation_canonique, sous_famille, unite).
-- Porte le statut de validation humaine et le seuil de confiance qui decide
-- si une future designation ressemblante y est ajoutee automatiquement.
CREATE TABLE IF NOT EXISTS groupes (
    id                    bigserial PRIMARY KEY,
    designation_canonique text         NOT NULL,
    sous_famille          text,
    unite                 text,
    valide                boolean      NOT NULL DEFAULT false,
    seuil_confiance       numeric(3,2) NOT NULL DEFAULT 0.75,
    valide_le             timestamptz
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_groupes_cle
    ON groupes (designation_canonique, coalesce(sous_famille, ''), coalesce(unite, ''));

ALTER TABLE groupes ADD COLUMN IF NOT EXISTS membres_signature text;
-- Empreinte de la composition du groupe au moment du dernier calcul de
-- seuil_confiance (designations brutes triees, jointes). Sert de cache :
-- si la composition n'a pas change depuis le dernier run, on ne repaie pas
-- Gemini pour re-evaluer la coherence du groupe (voir fusion_designations.py).

-- Cache des decisions Gemini deja prises pour une paire de designations,
-- pour ne jamais repayer un appel IA sur une paire deja jugee : a chaque
-- lancement de fusion_designations.py, seules les paires absentes d'ici
-- partent chez Gemini. designation_a < designation_b (ordre alphabetique)
-- pour que la paire (a,b) et (b,a) soient toujours la meme ligne.
CREATE TABLE IF NOT EXISTS fusion_decisions (
    id            bigserial PRIMARY KEY,
    sous_famille  text,
    unite         text,
    designation_a text        NOT NULL,
    designation_b text        NOT NULL,
    fusionner     boolean     NOT NULL,
    decide_le     timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_fusion_decisions_cle
    ON fusion_decisions (
        coalesce(sous_famille, ''), coalesce(unite, ''),
        designation_a, designation_b
    );

CREATE INDEX IF NOT EXISTS idx_price_lines_designation ON price_lines (designation);
CREATE INDEX IF NOT EXISTS idx_price_lines_document    ON price_lines (document_id);
CREATE INDEX IF NOT EXISTS idx_price_lines_desig_trgm
    ON price_lines USING gin (designation gin_trgm_ops);

-- Parametre unique et modifiable (table a une seule ligne, id force a 1) :
-- seuil de coefficient de variation (ecart-type / prix moyen, en %) au-dela
-- duquel un groupe de prix est considere comme ayant des valeurs
-- aberrantes. Volontairement en base (pas une constante Python) pour etre
-- ajustable depuis Streamlit sans redeploiement - demande explicite pour
-- pouvoir affiner ce seuil selon les retours clients.
CREATE TABLE IF NOT EXISTS parametres (
    id                int          PRIMARY KEY DEFAULT 1,
    seuil_cv_anomalie numeric(5,2) NOT NULL DEFAULT 5.0,
    CHECK (id = 1)
);
INSERT INTO parametres (id) VALUES (1) ON CONFLICT (id) DO NOTHING;

-- Vue de travail : moyenne de prix par (sous_famille, unite, designation
-- canonique). sous_famille ET unite font partie de la cle :
-- - sous_famille : "avec grille fonte 100 mm" sous "classe 250" vs "classe
--   400" - prix reels differents.
-- - unite : le meme texte facture au ml dans un document et au m2 dans un
--   autre n'est pas le meme prix comparable (base de calcul differente) -
--   ne jamais les moyenner ensemble. On groupe par unite_canonique (voir
--   normaliser_unite) et non par unite brute : "m2"/"M2"/"m²" ou "ft"/"FT"
--   sont la meme unite ecrite differemment, jamais des unites differentes.
-- Ne jamais retirer l'un ou l'autre de ce GROUP BY.
-- designation_canonique vaut NULL tant que la fusion n'a pas tourne ; la vue
-- utilise designation brute en attendant (COALESCE).
--
-- DETECTION ET CORRECTION DES VALEURS ABERRANTES (anomalies de prix) :
-- Methode IQR (interquartile range), en un seul passage (pas d'iteration) :
-- pour chaque groupe, on calcule Q1 (25e percentile) et Q3 (75e percentile)
-- des prix unitaires, puis l'intervalle "normal" = [Q1 - 3*IQR, Q3 + 3*IQR]
-- avec IQR = Q3 - Q1. Le multiplicateur 3 (au lieu du 1.5 statistique "standard")
-- est volontaire : sur de petits echantillons, 1.5*IQR marque comme aberrantes
-- des valeurs qui different seulement de quelques centimes (cas reel observe :
-- 1.06 a 1.43 EUR/ml sur 5 lignes), ce qui n'a pas de sens metier. 3*IQR reste
-- assez large pour ignorer ces petites variations normales entre tranches/lots,
-- tout en detectant toujours les vrais cas extremes (ex. 14757.9 EUR au milieu
-- de valeurs entre 1000 et 3600 EUR). Toute valeur hors de cet intervalle est
-- exclue du calcul de la moyenne corrigee.
-- Une anomalie n'est signalee (anomalie_detectee = true) que si LES TROIS
-- conditions sont vraies :
--   1. au moins 3 occurrences (nb_occurrences >= 3) - en dessous, les quartiles
--      n'ont pas de sens statistique fiable (trop peu de points) ;
--   2. le coefficient de variation brut (ecart-type / moyenne, en %) depasse
--      le seuil configurable de la table parametres (defaut 5%) - le CV est
--      utilise plutot qu'un ecart-type absolu en euros pour rester coherent
--      quelle que soit l'echelle de prix du produit (un ecart de 500 EUR est
--      enorme sur un prix a 10 EUR, negligeable sur un prix a 10 000 EUR) ;
--   3. la methode IQR identifie au moins une valeur reellement hors norme
--      pour l'expliquer (nb_valeurs_aberrantes > 0).
-- prix_moyen_corrige / ecart_type_corrige valent la moyenne/ecart-type
-- recalcules SANS les valeurs aberrantes quand anomalie_detectee est vrai,
-- sinon ils sont identiques a prix_moyen / ecart_type (rien de corrige).
-- Le detail (bornes IQR, valeurs exclues) reste consultable via les
-- colonnes q1/q3/borne_basse/borne_haute et via la table price_lines
-- elle-meme (aucune donnee brute n'est jamais supprimee ou modifiee).
--
-- DROP necessaire : CREATE OR REPLACE ne permet pas de changer l'ordre des
-- colonnes d'une vue existante (ici on insere sous_famille en 1ere position).
DROP VIEW IF EXISTS prix_moyen_par_designation;
CREATE VIEW prix_moyen_par_designation AS
WITH quartiles AS (
    -- percentile_cont est un agregat "ordered-set" : Postgres ne l'autorise
    -- pas avec OVER (fenetre), il faut passer par un GROUP BY classique puis
    -- rejoindre ce resultat aux lignes brutes (etape suivante).
    SELECT
        sous_famille,
        unite_canonique,
        coalesce(designation_canonique, designation) AS designation,
        percentile_cont(0.25) WITHIN GROUP (ORDER BY prix_unitaire) AS q1,
        percentile_cont(0.75) WITHIN GROUP (ORDER BY prix_unitaire) AS q3
    FROM price_lines
    GROUP BY sous_famille, unite_canonique, coalesce(designation_canonique, designation)
),
bornes AS (
    SELECT
        *,
        (q3 - q1)             AS iqr,
        q1 - 3 * (q3 - q1)    AS borne_basse,
        q3 + 3 * (q3 - q1)    AS borne_haute
    FROM quartiles
),
lignes_avec_bornes AS (
    SELECT
        pl.sous_famille,
        pl.unite_canonique,
        coalesce(pl.designation_canonique, pl.designation) AS designation,
        pl.prix_unitaire,
        b.q1, b.q3, b.borne_basse, b.borne_haute
    FROM price_lines pl
    JOIN bornes b
      ON coalesce(pl.designation_canonique, pl.designation) = b.designation
     AND pl.sous_famille IS NOT DISTINCT FROM b.sous_famille
     AND pl.unite_canonique IS NOT DISTINCT FROM b.unite_canonique
),
agrege AS (
    SELECT
        sous_famille,
        unite_canonique AS unite,
        designation,
        count(*)                                                                              AS nb_occurrences,
        avg(prix_unitaire)                                                                     AS prix_moyen,
        stddev_samp(prix_unitaire)                                                             AS ecart_type,
        min(prix_unitaire)                                                                     AS prix_min,
        max(prix_unitaire)                                                                     AS prix_max,
        min(q1)                                                                                AS q1,
        min(q3)                                                                                AS q3,
        min(borne_basse)                                                                       AS borne_basse,
        min(borne_haute)                                                                       AS borne_haute,
        count(*) FILTER (WHERE prix_unitaire < borne_basse OR prix_unitaire > borne_haute)      AS nb_valeurs_aberrantes,
        avg(prix_unitaire) FILTER (WHERE prix_unitaire BETWEEN borne_basse AND borne_haute)     AS prix_moyen_sans_aberrantes,
        stddev_samp(prix_unitaire) FILTER (WHERE prix_unitaire BETWEEN borne_basse AND borne_haute) AS ecart_type_sans_aberrantes
    FROM lignes_avec_bornes
    GROUP BY sous_famille, unite_canonique, designation
)
SELECT
    a.sous_famille,
    a.unite,
    a.designation,
    a.nb_occurrences,
    a.prix_moyen,
    a.ecart_type,
    a.prix_min,
    a.prix_max,
    coalesce(a.ecart_type / nullif(a.prix_moyen, 0) * 100, 0)                        AS coefficient_variation,
    a.q1,
    a.q3,
    a.borne_basse,
    a.borne_haute,
    a.nb_valeurs_aberrantes,
    (a.nb_occurrences >= 3
        AND coalesce(a.ecart_type / nullif(a.prix_moyen, 0) * 100, 0) > p.seuil_cv_anomalie
        AND a.nb_valeurs_aberrantes > 0)                                             AS anomalie_detectee,
    CASE
        WHEN a.nb_occurrences >= 3
             AND coalesce(a.ecart_type / nullif(a.prix_moyen, 0) * 100, 0) > p.seuil_cv_anomalie
             AND a.nb_valeurs_aberrantes > 0
        THEN a.prix_moyen_sans_aberrantes
        ELSE a.prix_moyen
    END AS prix_moyen_corrige,
    CASE
        WHEN a.nb_occurrences >= 3
             AND coalesce(a.ecart_type / nullif(a.prix_moyen, 0) * 100, 0) > p.seuil_cv_anomalie
             AND a.nb_valeurs_aberrantes > 0
        THEN a.ecart_type_sans_aberrantes
        ELSE a.ecart_type
    END AS ecart_type_corrige
FROM agrege a
CROSS JOIN parametres p;
