-- Phase 1 : table de lignes de prix propre, tracable jusqu'au PDF source.
-- Postgres = seule source de verite.

CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- Normalise un texte pour la recherche (pas pour le regroupement) : accents
-- et casse retires, espaces multiples/en trop reduits a un seul. Sert a
-- comparer le texte tape par l'utilisateur a la designation en base sans
-- rater un resultat a cause d'un accent absent ou d'un espace en trop
-- (bug remonte : chercher "deviation" ne trouvait pas "déviation").
-- Volontairement distincte de normaliser_unite : ici on garde la ponctuation
-- (utile pour retrouver un texte tel quel), on ne fait que lisser accents/
-- espaces/casse.
-- Exception : "<" et ">" sont convertis en mots ("inferieur"/"superieur")
-- AVANT tout le reste. pg_trgm (similarity(), utilise pour le rapprochement
-- automatique de "Recherche de prix") traite ces symboles comme de simples
-- separateurs de mots et les ignore completement (verifie via show_trgm) :
-- "largeur < 2,50 m" et "largeur > 2,50 m" - deux produits opposes, prix
-- differents - obtenaient un score de similarite de 1.0, parfaitement
-- identiques pour Postgres. En mots, les trigrammes redeviennent distincts.
CREATE OR REPLACE FUNCTION normaliser_recherche(t text) RETURNS text
LANGUAGE sql IMMUTABLE AS $$
    SELECT regexp_replace(
        regexp_replace(
            regexp_replace(
                translate(lower(trim(t)), 'àâäéèêëîïôöùûüç', 'aaaeeeeiioouuuc'),
                '<', ' inferieur ', 'g'
            ),
            '>', ' superieur ', 'g'
        ),
        '\s+', ' ', 'g'
    )
$$;

-- Ensemble des nombres d'un texte, sous forme canonique comparable ("0,125,8"
-- pour "PVC CR 8 Ø 125") : suites de chiffres, dedupliquees et triees.
-- Equivalent SQL exact de set(_NUM_RE.findall(...)) de fusion_designations.py
-- (comparaison de CHAINES : "05" et "5" restent distincts, comme en Python).
-- Sert au prefiltre de "Recherche de prix" a filtrer les candidats sur les
-- memes nombres AVANT le LIMIT du top-N par similarite texte : la similarite
-- texte seule ne "sait" pas qu'un nombre different disqualifie un candidat -
-- avec un LIMIT applique avant ce filtre, les bons candidats (memes nombres,
-- texte moins proche) se faisaient evincer du top-N par des candidats au
-- texte proche mais au mauvais nombre, de toute facon rejetes ensuite.
CREATE OR REPLACE FUNCTION nombres_texte(t text) RETURNS text
LANGUAGE sql IMMUTABLE AS $$
    SELECT coalesce(
        (SELECT string_agg(DISTINCT m[1], ',' ORDER BY m[1])
         FROM regexp_matches(t, '\d+', 'g') m),
        ''
    )
$$;

-- Meme extraction, en tableau, pour les comparaisons d'INCLUSION (operateur
-- <@) et pas seulement d'egalite. Sert au "rattrapage" de la Recherche de
-- prix : une ligne de bordereau porteuse d'une borne de quantite/montant
-- ("Grave 0/31.5 recyclee (<250T)", "Travaux compris entre 1500 et 5000 €")
-- ne trouve JAMAIS de candidat en egalite stricte des nombres - le seuil
-- tarifaire (250, 1500...) n'existe pas dans la designation en base
-- ("Grave Non Traitee de recyclage 0/31.5"). Un candidat dont les nombres
-- sont un SOUS-ENSEMBLE de ceux du bordereau reste un rapprochement
-- plausible (il n'affirme rien de contradictoire), que Gemini arbitre.
CREATE OR REPLACE FUNCTION nombres_tab(t text) RETURNS text[]
LANGUAGE sql IMMUTABLE AS $$
    SELECT coalesce(
        (SELECT array_agg(DISTINCT m[1]) FROM regexp_matches(t, '\d+', 'g') m),
        '{}'::text[]
    )
$$;

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
-- "ml" (metre lineaire) fusionne avec "m" (metre) : en BTP, les deux
-- designent la meme unite physique pour un article lineaire (bordure,
-- canalisation...) - simple convention d'ecriture differente, jamais deux
-- unites reellement differentes (contrairement a m2/m3 qui restent
-- distincts, l'exposant est deja preserve). Incident reel constate sur
-- "Recherche de prix" : la MEME ligne de bordereau ("type T2 <50ml")
-- extraite en "M" par Gemini au lieu de "ML" (contrairement a la ligne A2
-- juste au-dessus, extraite en "ML") bloquait un candidat par ailleurs
-- parfaitement identique en base, uniquement a cause de cette variation
-- d'ecriture entre deux lignes du MEME document.
-- IMMUTABLE : requis pour l'utiliser dans une colonne generee (voir plus bas).
-- Apres le nettoyage, les SYNONYMES d'ecriture d'une meme unite sont ramenes
-- a une forme canonique unique. Meme logique que ml -> m (metre lineaire =
-- metre pour un article lineaire, deux conventions d'ecriture) : en base
-- reelle, la famille "forfait" etait eclatee en 7 ecritures (ft 563
-- designations, f 114, forfait 37, fft, for, forf, ff) et "unite" en 6 (u
-- 2045, unitaire 45, lunite, unite, un, piece) - le meme produit au meme
-- forfait ne se retrouvait JAMAIS si un document ecrivait "F" et l'autre
-- "FT". Les libelles verbeux ("Le metre carre" -> "lemetrecarre") viennent
-- de PDF qui ecrivent l'unite en toutes lettres.
-- "ens"/"ensemble" sont unifies entre eux mais PAS avec "forfait" :
-- volontairement conservateur, "l'ensemble" peut porter un sens metier
-- distinct (l'ensemble des unites d'un lot) qu'on ne tranche pas ici.
CREATE OR REPLACE FUNCTION normaliser_unite(u text) RETURNS text
LANGUAGE sql IMMUTABLE AS $$
    SELECT NULLIF(
        CASE nettoye
            WHEN 'ml'           THEN 'm'
            WHEN 'lemetre'      THEN 'm'
            WHEN 'lemetrecarre' THEN 'm2'
            WHEN 'lemetrecube'  THEN 'm3'
            WHEN 'latonne'      THEN 't'
            WHEN 'tonne'        THEN 't'
            WHEN 'f'            THEN 'forfait'
            WHEN 'ft'           THEN 'forfait'
            WHEN 'fft'          THEN 'forfait'
            WHEN 'ff'           THEN 'forfait'
            WHEN 'for'          THEN 'forfait'
            WHEN 'forf'         THEN 'forfait'
            WHEN 'unitaire'     THEN 'u'
            WHEN 'unite'        THEN 'u'
            WHEN 'lunite'       THEN 'u'
            WHEN 'un'           THEN 'u'
            WHEN 'piece'        THEN 'u'
            WHEN 'pieces'       THEN 'u'
            WHEN 'ensemble'     THEN 'ens'
            WHEN 'heure'        THEN 'h'
            ELSE nettoye
        END,
        ''
    )
    FROM (
        SELECT regexp_replace(
            translate(lower(trim(u)), 'àâäéèêëîïôöùûüç²³', 'aaaeeeeiioouuuc23'),
            '[^a-z0-9/%]', '', 'g'
        ) AS nettoye
    ) s
$$;

-- Normalise une sous_famille pour qu'elle serve de cle de regroupement fiable,
-- meme principe que normaliser_unite : accents/casse/ponctuation retires, un
-- seul pluriel final strippe ("niveaux" -> "niveau", et pluriels irreguliers
-- en "-x" du francais - "niveaux", "travaux" - d'ou le [sx]$, pas juste s$).
-- Bug reel constate : "B16 Mises à niveau" / "B16 Mises à niveaux" / "B16
-- Mises à niveaux." / "B41 Mises à niveau." ne fusionnaient JAMAIS entre
-- elles (sous_famille brut fait partie de la cle de regroupement partout -
-- vue, table groupes, fusion_decisions - mais n'a jamais reçu la moindre
-- normalisation, contrairement a designation et unite).
-- "<"/">"/"≤"/"≥" convertis en mots avant le strip generique de ponctuation,
-- meme raison que normaliser_recherche : sinon "S > 1,20 m²" et "S < 1,20 m²"
-- (deux sous-familles reellement opposees) deviendraient un texte identique
-- une fois la ponctuation retiree.
-- Volontairement CONSERVATEUR par ailleurs : ne touche jamais au prefixe de
-- code eventuel (B16 vs B41 restent distincts) - la ou designation/unite ne
-- traitent que du bruit de formatage objectif, un prefixe de code peut porter
-- un sens metier reel (chapitre/lot different) qui n'est pas a cette
-- fonction de trancher.
-- IMMUTABLE : requis pour l'utiliser dans une colonne generee (voir plus bas).
CREATE OR REPLACE FUNCTION normaliser_sous_famille(sf text) RETURNS text
LANGUAGE sql IMMUTABLE AS $$
    SELECT NULLIF(
        regexp_replace(
            trim(
                regexp_replace(
                    regexp_replace(
                        regexp_replace(
                            regexp_replace(
                                regexp_replace(
                                    translate(lower(trim(sf)), 'àâäéèêëîïôöùûüç', 'aaaeeeeiioouuuc'),
                                    '<=|≤', ' inferieurouegal ', 'g'
                                ),
                                '>=|≥', ' superieurouegal ', 'g'
                            ),
                            '<', ' inferieur ', 'g'
                        ),
                        '>', ' superieur ', 'g'
                    ),
                    '[^a-z0-9]+', ' ', 'g'
                )
            ),
            '[sx]$', ''
        ),
        ''
    )
$$;

CREATE TABLE IF NOT EXISTS price_documents (
    id          bigserial PRIMARY KEY,
    filename    text        NOT NULL UNIQUE,
    imported_at timestamptz NOT NULL DEFAULT now()
);

-- Trace durable de tout fichier qui echoue (telechargement SharePoint ou
-- extraction Gemini) dans le watcher, pour ne plus jamais avoir a grep les
-- logs docker pour savoir quels fichiers ont ete perdus (incident reel :
-- "CANAPLES...pdf" perdu silencieusement sur une coupure reseau pendant le
-- telechargement - le watcher avance son delta token SharePoint a chaque
-- cycle QUE le fichier ait reussi ou non, donc un fichier en echec n'est
-- JAMAIS retente automatiquement). Sert de liste "a retraiter manuellement"
-- (voir tableau de bord). resolu=true une fois le fichier reintegre avec
-- succes (ou le probleme juge non pertinent) - jamais supprime, pour garder
-- l'historique.
CREATE TABLE IF NOT EXISTS echecs_traitement (
    id           bigserial PRIMARY KEY,
    filename     text        NOT NULL,
    chemin       text,
    etape        text        NOT NULL,  -- 'telechargement' ou 'extraction'
    erreur       text        NOT NULL,
    survenu_le   timestamptz NOT NULL DEFAULT now(),
    resolu       boolean     NOT NULL DEFAULT false
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

ALTER TABLE price_lines ADD COLUMN IF NOT EXISTS sous_famille_canonique text
    GENERATED ALWAYS AS (normaliser_sous_famille(sous_famille)) STORED;
-- Meme principe que unite_canonique, pour sous_famille (voir
-- normaliser_sous_famille ci-dessus). Sert de cle de regroupement partout a
-- la place de `sous_famille` brute (vue, fusion_designations.py, table
-- groupes) - meme convention deja en place pour `unite`/`unite_canonique`,
-- ou la valeur canonique (normalisee) est ce qui s'affiche partout dans
-- l'app, jamais la valeur brute.

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

ALTER TABLE parametres ADD COLUMN IF NOT EXISTS seuil_auto_validation numeric(3,2) NOT NULL DEFAULT 0.85;
-- Seuil de confiance (score Gemini sur le groupe, voir evaluer_confiance_groupe
-- dans fusion_designations.py) au-dela duquel un groupe de designations est
-- valide automatiquement (valide=true), sans attendre un clic humain dans
-- "Revue des groupes". Demande explicite : la revue manuelle systematique
-- de groupes evidents (score 1.0, simple faute de frappe ou point final en
-- trop) n'apporte rien et decourage l'usage - seuls les groupes ambigus
-- (score bas) doivent remonter a un humain. Ajustable comme seuil_cv_anomalie.

-- Correction ITERATIVE des valeurs aberrantes (demande client explicite,
-- transcript reunion : "tu dois te retrouver sur un ecart-type qui est
-- coherent... apres correction qui soit inferieur ou egal a [seuil]").
-- Contrairement a un simple passage IQR (qui exclut une fois puis s'arrete
-- meme si le resultat reste disperse), cette fonction repete la detection
-- IQR (bornes = [Q1 - 3*IQR, Q3 + 3*IQR], multiplicateur 3 choisi pour ne
-- pas exclure des ecarts de quelques centimes sur petit echantillon - voir
-- historique) sur les valeurs RESTANTES, jusqu'a ce que :
--   1. le coefficient de variation repasse sous le seuil demande (objectif
--      atteint) ; ou
--   2. un passage n'exclue plus rien (le groupe reste disperse mais aucune
--      valeur n'est plus statistiquement hors norme - on arrete, continuer
--      viderait le groupe sans justification statistique) ; ou
--   3. il reste moins de 3 valeurs (quartiles plus fiables en dessous).
-- Retourne toujours q1/q3/bornes du DERNIER passage evalue, meme quand rien
-- n'est exclu (0 lignes valides -> pas d'anomalie mais bornes affichables).
-- valeurs_retenues = les prix effectivement gardes a la fin (utile pour
-- determiner ligne par ligne qui a ete exclu : comparer une valeur aux
-- bornes du DERNIER passage uniquement serait faux, une valeur exclue tot
-- peut tomber dans l'intervalle final une fois les extremes retires).
CREATE OR REPLACE FUNCTION corriger_valeurs_aberrantes(prix numeric[], seuil_cv numeric)
RETURNS TABLE(
    moyenne_corrigee   numeric,
    ecart_type_corrige numeric,
    nb_exclues         int,
    q1                 numeric,
    q3                 numeric,
    borne_basse        numeric,
    borne_haute        numeric,
    valeurs_retenues   numeric[]
)
LANGUAGE plpgsql IMMUTABLE AS $$
DECLARE
    restants          numeric[] := prix;
    nouveaux_restants numeric[];
    v_q1 numeric; v_q3 numeric; v_iqr numeric; v_bb numeric; v_bh numeric;
    v_moy numeric; v_ect numeric; v_cv numeric;
    v_nb_exclu int := 0;
BEGIN
    LOOP
        SELECT avg(x), stddev_samp(x) INTO v_moy, v_ect FROM unnest(restants) x;
        v_cv := coalesce(v_ect / nullif(v_moy, 0) * 100, 0);

        SELECT percentile_cont(0.25) WITHIN GROUP (ORDER BY x),
               percentile_cont(0.75) WITHIN GROUP (ORDER BY x)
          INTO v_q1, v_q3
          FROM unnest(restants) x;
        v_iqr := v_q3 - v_q1;
        v_bb := v_q1 - 3 * v_iqr;
        v_bh := v_q3 + 3 * v_iqr;

        EXIT WHEN v_cv <= seuil_cv OR array_length(restants, 1) < 3;

        SELECT array_agg(x) INTO nouveaux_restants FROM unnest(restants) x WHERE x BETWEEN v_bb AND v_bh;

        EXIT WHEN array_length(nouveaux_restants, 1) = array_length(restants, 1);

        v_nb_exclu := v_nb_exclu + (array_length(restants, 1) - array_length(nouveaux_restants, 1));
        restants := nouveaux_restants;
    END LOOP;

    RETURN QUERY SELECT v_moy, v_ect, v_nb_exclu, v_q1, v_q3, v_bb, v_bh, restants;
END;
$$;

-- Vue de travail : moyenne de prix par (sous_famille, unite, designation
-- canonique). sous_famille ET unite font partie de la cle :
-- - sous_famille : "avec grille fonte 100 mm" sous "classe 250" vs "classe
--   400" - prix reels differents. On groupe par sous_famille_canonique (voir
--   normaliser_sous_famille) et non par sous_famille brute, meme logique que
--   unite ci-dessous : "B16 Mises a niveau" / "B16 Mises a niveaux." sont la
--   meme sous-famille ecrite differemment, jamais fusionnee avec une AUTRE
--   sous-famille pour autant (le prefixe de code, ex. B16 vs B41, reste
--   distinctif - voir normaliser_sous_famille).
-- - unite : le meme texte facture au ml dans un document et au m2 dans un
--   autre n'est pas le meme prix comparable (base de calcul differente) -
--   ne jamais les moyenner ensemble. On groupe par unite_canonique (voir
--   normaliser_unite) et non par unite brute : "m2"/"M2"/"m²" ou "ft"/"FT"
--   sont la meme unite ecrite differemment, jamais des unites differentes.
-- Ne jamais retirer l'un ou l'autre de ce GROUP BY.
-- designation_canonique vaut NULL tant que la fusion n'a pas tourne ; la vue
-- utilise designation brute en attendant (COALESCE).
--
-- DETECTION ET CORRECTION DES VALEURS ABERRANTES : voir corriger_valeurs_
-- aberrantes ci-dessus pour l'algorithme iteratif. anomalie_detectee est
-- vrai des que la fonction a exclu au moins une valeur (nb_valeurs_
-- aberrantes > 0) - la garde "nb_occurrences >= 3" est deja assuree en
-- interne par la fonction (elle n'exclut jamais rien en dessous de 3
-- valeurs restantes), pas besoin de la repeter ici.
-- prix_moyen_corrige / ecart_type_corrige valent la moyenne/ecart-type
-- APRES la correction iterative complete quand anomalie_detectee est vrai,
-- sinon ils sont identiques a prix_moyen / ecart_type (rien de corrige).
-- Le detail (bornes du dernier passage, valeurs exclues) reste consultable
-- via q1/q3/borne_basse/borne_haute et via price_lines elle-meme (aucune
-- donnee brute n'est jamais supprimee ou modifiee).
--
-- DROP necessaire : CREATE OR REPLACE ne permet pas de changer l'ordre des
-- colonnes d'une vue existante (ici on insere sous_famille en 1ere position).
DROP VIEW IF EXISTS prix_moyen_par_designation;
CREATE VIEW prix_moyen_par_designation AS
WITH agrege AS (
    SELECT
        sous_famille_canonique AS sous_famille,
        unite_canonique AS unite,
        coalesce(designation_canonique, designation) AS designation,
        count(*)                    AS nb_occurrences,
        avg(prix_unitaire)          AS prix_moyen,
        stddev_samp(prix_unitaire)  AS ecart_type,
        min(prix_unitaire)          AS prix_min,
        max(prix_unitaire)          AS prix_max,
        array_agg(prix_unitaire)    AS prix_liste
    FROM price_lines
    GROUP BY sous_famille_canonique, unite_canonique, coalesce(designation_canonique, designation)
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
    coalesce(a.ecart_type / nullif(a.prix_moyen, 0) * 100, 0) AS coefficient_variation,
    c.q1,
    c.q3,
    c.borne_basse,
    c.borne_haute,
    c.nb_exclues                                              AS nb_valeurs_aberrantes,
    (c.nb_exclues > 0)                                        AS anomalie_detectee,
    CASE WHEN c.nb_exclues > 0 THEN c.moyenne_corrigee   ELSE a.prix_moyen END AS prix_moyen_corrige,
    CASE WHEN c.nb_exclues > 0 THEN c.ecart_type_corrige ELSE a.ecart_type END AS ecart_type_corrige,
    c.valeurs_retenues
FROM agrege a
CROSS JOIN parametres p
CROSS JOIN LATERAL corriger_valeurs_aberrantes(a.prix_liste, p.seuil_cv_anomalie) c;
