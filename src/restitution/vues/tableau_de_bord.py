import os

import altair as alt
import pandas as pd
import psycopg
import streamlit as st
from dotenv import load_dotenv

load_dotenv()


def _camembert(donnees: dict[str, int], titre: str):
    """Camembert Altair (mark_arc) - pas de st.pie_chart natif chez
    Streamlit, mais altair est deja une dependance de streamlit (aucun
    ajout necessaire)."""
    df = pd.DataFrame({"categorie": list(donnees.keys()), "valeur": list(donnees.values())})
    chart = (
        alt.Chart(df)
        .mark_arc(outerRadius=110)
        .encode(
            theta=alt.Theta("valeur:Q"),
            color=alt.Color(
                "categorie:N",
                scale=alt.Scale(range=["#003D7C", "#B8C9DC"]),
                legend=alt.Legend(title=None),
            ),
            tooltip=["categorie:N", "valeur:Q"],
        )
        .properties(title=titre, height=280)
    )
    text = chart.mark_text(radius=135, size=13).encode(text="valeur:Q")
    st.altair_chart(chart + text, use_container_width=True)


@st.cache_resource
def get_conn():
    pgurl = os.getenv("PGURL")
    if not pgurl:
        st.error("PGURL manquant dans .env")
        st.stop()
    return psycopg.connect(pgurl, autocommit=True)


conn = get_conn()

st.title("Tableau de bord")
st.caption("Vue d'ensemble de l'état de la base de prix : volume de données, regroupement des désignations, anomalies.")

# Une agence = un dossier SharePoint de premier niveau (Paris, Amiens...),
# meme principe et memes noms que le selecteur de Prix unitaires/Recherche de
# prix - duplique volontairement ici (chaque vue Streamlit est un script
# independant). "Toutes les agences" = comportement inchange (agence=NULL).
# Recalcule les compteurs sur le sous-ensemble choisi (pas un filtre visuel
# apres coup) - SAUF la section "Repartition par agence" plus bas, qui reste
# TOUJOURS une vue globale (elle sert justement a comparer les agences entre
# elles, la filtrer sur une seule la rendrait triviale).
agences_disponibles = pd.read_sql(
    "SELECT DISTINCT agence FROM price_documents WHERE agence IS NOT NULL ORDER BY agence", conn
)
TOUTES_AGENCES = "Toutes les agences"
agence_choisie = st.selectbox("Agence", options=[TOUTES_AGENCES] + agences_disponibles["agence"].tolist())
agence_filtre = None if agence_choisie == TOUTES_AGENCES else agence_choisie

# Une seule requete regroupant tous les compteurs simples (evite plusieurs
# aller-retours) : lignes/documents bruts, designations (vue), groupes,
# anomalies. "designations sans groupe" = lignes de la vue pour lesquelles
# aucun groupe forme ne correspond (soit jamais fusionnees, soit une seule
# orthographe existante - pas forcement un probleme, voir page Revue des
# groupes pour le detail).
# "WITH param" : un SEUL parametre agence, reutilise partout via CROSS JOIN -
# evite de repeter/compter %s a la main pour chaque sous-requete.
# Un groupe n'appartient PAS a une seule agence (ses membres peuvent venir de
# documents d'agences differentes, voir CLAUDE.md) : un groupe compte dans le
# filtre "Amiens" s'il a AU MOINS UN membre dans cette agence (agence_groupes
# ci-dessous) - c'est la comptabilisation "au moins un document concerne",
# pas "exclusivement cette agence".
stats = pd.read_sql(
    """
    WITH param AS (
        SELECT %s::text AS agence
    ),
    agence_lignes AS (
        SELECT pl.*
        FROM price_lines pl
        JOIN price_documents pd ON pd.id = pl.document_id
        CROSS JOIN param
        WHERE param.agence IS NULL OR pd.agence = param.agence
    ),
    agence_designations AS (
        SELECT f.*
        FROM param
        CROSS JOIN LATERAL prix_moyen_par_designation_fn(param.agence) f
    ),
    agence_groupes AS (
        SELECT DISTINCT g.*
        FROM groupes g
        JOIN agence_lignes pl
          ON coalesce(pl.designation_canonique, pl.designation) = g.designation_canonique
         AND pl.sous_famille_canonique IS NOT DISTINCT FROM g.sous_famille
         AND pl.unite_canonique IS NOT DISTINCT FROM g.unite
    )
    SELECT
        (SELECT count(*) FROM price_documents pd CROSS JOIN param
         WHERE param.agence IS NULL OR pd.agence = param.agence)             AS total_documents,
        (SELECT count(*) FROM agence_lignes)                                 AS total_lignes,
        (SELECT count(*) FROM agence_designations)                           AS total_designations,
        (SELECT count(*) FROM agence_groupes)                                AS total_groupes,
        (SELECT count(*) FROM agence_groupes WHERE valide)                   AS groupes_valides,
        -- strpos(...) > 0 <=> contient le separateur "|" (2+ orthographes
        -- distinctes fusionnees, voir fusion_designations.py). Evite LIKE
        -- ici : psycopg3 scanne TOUT le texte de la requete (commentaires
        -- compris) a la recherche de placeholders des qu'elle a par
        -- ailleurs des parametres - le caractere pourcent (seul, ou suivi
        -- de la lettre s/b/t) y est toujours interprete, jamais litteral
        -- dans un commentaire. Piege reel rencontre : ce caractere doit
        -- rester totalement absent de tout texte SQL parametre.
        (SELECT count(*) FROM agence_groupes
         WHERE strpos(membres_signature, '|') > 0)                           AS groupes_avec_gemini,
        (SELECT count(*) FROM agence_groupes
         WHERE strpos(coalesce(membres_signature, ''), '|') = 0)             AS groupes_sans_gemini,
        (SELECT count(*) FROM agence_designations v
         WHERE NOT EXISTS (
             SELECT 1 FROM agence_groupes g
             WHERE g.designation_canonique = v.designation
               AND g.sous_famille IS NOT DISTINCT FROM v.sous_famille
               AND g.unite IS NOT DISTINCT FROM v.unite
         ))                                                                  AS designations_sans_groupe,
        (SELECT count(*) FROM agence_designations WHERE anomalie_detectee)   AS anomalies
    """,
    conn,
    params=(agence_filtre,),
).iloc[0]

st.subheader("Vue d'ensemble")
c1, c2, c3 = st.columns(3)
c1.metric("Désignations (total)", int(stats["total_designations"]))
c2.metric(
    "Groupes formés",
    int(stats["total_groupes"]),
    help="Inclut les groupes formés par Gemini (orthographes différentes) ET les désignations juste répétées à l'identique - voir le détail des deux ci-dessous.",
)
c3.metric("Désignations sans groupe", int(stats["designations_sans_groupe"]))

c4, c5, c6 = st.columns(3)
c4.metric(
    "Groupes validés",
    int(stats["groupes_valides"]),
    delta=f"sur {int(stats['total_groupes'])} au total",
    delta_color="off",
)
c5.metric("Anomalies de prix détectées", int(stats["anomalies"]))
c6.metric("Lignes de prix brutes", int(stats["total_lignes"]), help=f"Extraites de {int(stats['total_documents'])} document(s)")

c7, c8, _ = st.columns(3)
c7.metric(
    "Groupes ayant nécessité Gemini",
    int(stats["groupes_avec_gemini"]),
    help="2+ orthographes différentes réellement fusionnées après un jugement Gemini (ou une normalisation exacte suivie d'une évaluation de cohérence).",
)
c8.metric(
    "Groupes sans Gemini (même orthographe)",
    int(stats["groupes_sans_gemini"]),
    help="Une seule orthographe, répétée sur plusieurs lignes - rien à décider, auto-validé sans appel Gemini.",
)

st.divider()

# Une agence = un dossier SharePoint de premier niveau (voir _agence_from_item
# dans pdf_watcher.py). agence reste NULL tant que le watcher n'a pas
# (re)traite ce document depuis l'ajout de cette colonne - le distinguer
# explicitement plutot que de le compter silencieusement dans une agence,
# pour reperer tout de suite un rescan complet a faire (voir CLAUDE.md).
st.subheader("Répartition par agence")
st.caption(
    "Vue globale de comparaison entre agences, TOUJOURS sur toute la base — "
    "indépendante du filtre « Agence » choisi ci-dessus."
)
agences_stats = pd.read_sql(
    """
    SELECT
        (SELECT count(DISTINCT agence) FROM price_documents WHERE agence IS NOT NULL) AS nb_agences,
        (SELECT count(*) FROM price_documents WHERE agence IS NULL)                    AS docs_sans_agence
    """,
    conn,
).iloc[0]

c_ag1, c_ag2 = st.columns(2)
c_ag1.metric("Agences actives", int(agences_stats["nb_agences"]))
c_ag2.metric(
    "Documents sans agence connue",
    int(agences_stats["docs_sans_agence"]),
    help="Documents jamais retraités par le watcher depuis l'ajout du multi-agences. "
    "Un rescan complet (vider data/state/delta_token.json puis redémarrer le watcher) "
    "les rattache sans ré-extraction, depuis le JSON déjà en cache.",
)

if int(agences_stats["nb_agences"]) > 0:
    # Volume (documents/lignes) directement sur price_lines/price_documents ;
    # nombre de designations DISTINCTES via prix_moyen_par_designation_fn -
    # reutilise le meme calcul que la page Prix unitaires (une ligne "*10cm"
    # et "10cm" comptent pour 1 seule designation apres fusion, pas 2).
    volume_par_agence = pd.read_sql(
        """
        SELECT pd.agence, count(DISTINCT pd.id) AS nb_documents, count(pl.id) AS nb_lignes
        FROM price_documents pd
        LEFT JOIN price_lines pl ON pl.document_id = pd.id
        WHERE pd.agence IS NOT NULL
        GROUP BY pd.agence
        """,
        conn,
    )
    designations_par_agence = pd.read_sql(
        """
        SELECT ag.agence, count(*) AS nb_designations
        FROM (SELECT DISTINCT agence FROM price_documents WHERE agence IS NOT NULL) ag
        CROSS JOIN LATERAL prix_moyen_par_designation_fn(ag.agence) f
        GROUP BY ag.agence
        """,
        conn,
    )
    par_agence_df = volume_par_agence.merge(designations_par_agence, on="agence").sort_values(
        "nb_designations", ascending=False
    )

    st.dataframe(
        par_agence_df,
        use_container_width=True,
        hide_index=True,
        column_config={
            "agence": "Agence",
            "nb_documents": "Documents",
            "nb_lignes": "Lignes brutes",
            "nb_designations": "Désignations distinctes",
        },
    )
    st.bar_chart(par_agence_df.set_index("agence")["nb_designations"])
else:
    st.info("Aucune agence connue pour l'instant — voir « Documents sans agence connue » ci-dessus.")

st.divider()

col_gauche, col_droite = st.columns(2)

with col_gauche:
    st.subheader("Répartition des désignations par nombre d'occurrences")
    st.caption("Combien de fois chaque désignation apparaît dans les documents — plus il y en a, plus la moyenne est fiable.")
    occ_df = pd.read_sql(
        "SELECT nb_occurrences FROM prix_moyen_par_designation_fn(%s)", conn, params=(agence_filtre,)
    )

    def _bucket(n):
        if n == 1:
            return "1"
        if n == 2:
            return "2"
        if 3 <= n <= 5:
            return "3-5"
        if 6 <= n <= 10:
            return "6-10"
        return "10+"

    occ_df["tranche"] = occ_df["nb_occurrences"].apply(_bucket)
    ordre = ["1", "2", "3-5", "6-10", "10+"]
    repartition = occ_df["tranche"].value_counts().reindex(ordre, fill_value=0)
    st.bar_chart(repartition)

with col_droite:
    st.subheader("Groupes : validés vs en attente")
    st.caption("Un groupe 'en attente' n'a pas encore été confirmé par un humain (score de confiance sous le seuil d'auto-validation).")
    _camembert(
        {
            "Validés": int(stats["groupes_valides"]),
            "En attente": int(stats["total_groupes"]) - int(stats["groupes_valides"]),
        },
        "Groupes",
    )

st.divider()

st.subheader("Désignations : avec groupe vs sans groupe")
st.caption(
    f"Sur les {int(stats['total_designations'])} désignations du tableau principal, combien "
    "correspondent à un groupe formé (plusieurs orthographes fusionnées) contre combien "
    "n'ont qu'une seule orthographe existante (rien à fusionner, pas forcément un problème)."
)
designations_avec_groupe = int(stats["total_designations"]) - int(stats["designations_sans_groupe"])

col_pie1, col_pie2, _ = st.columns([1, 1, 1])
with col_pie1:
    _camembert(
        {
            "Avec groupe": designations_avec_groupe,
            "Sans groupe": int(stats["designations_sans_groupe"]),
        },
        "Désignations",
    )

st.divider()

st.subheader("Sous-familles les plus fréquentes")
st.caption("Les 10 sous-familles regroupant le plus de désignations distinctes — donne une idée des grandes catégories de la base.")
top_sf = pd.read_sql(
    """
    SELECT coalesce(sous_famille, '(aucune)') AS sous_famille, count(*) AS nb_designations
    FROM prix_moyen_par_designation_fn(%s)
    GROUP BY sous_famille
    ORDER BY nb_designations DESC
    LIMIT 10
    """,
    conn,
    params=(agence_filtre,),
).set_index("sous_famille")
st.bar_chart(top_sf)

st.divider()

# Journal de tous les fichiers passes par le watcher : succes (price_documents,
# avec le nombre de lignes extraites) UNION echecs non resolus
# (echecs_traitement - voir schema.sql). filename est stocke SANS extension
# cote succes (load_items utilise Path(name).stem) et AVEC cote echec (le nom
# brut du fichier SharePoint) - on retire l'extension des deux pour un
# affichage coherent.
journal_df = pd.read_sql(
    """
    SELECT filename, agence, statut, nb_lignes, date_evenement FROM (
        SELECT pd.filename                                AS filename,
               pd.agence                                   AS agence,
               'Traité'                                    AS statut,
               count(pl.id)                                AS nb_lignes,
               pd.imported_at                               AS date_evenement
        FROM price_documents pd
        LEFT JOIN price_lines pl ON pl.document_id = pd.id
        WHERE %s::text IS NULL OR pd.agence = %s
        GROUP BY pd.id, pd.filename, pd.agence, pd.imported_at

        UNION ALL

        -- Les echecs n'ont pas d'agence en base (echecs_traitement n'a pas
        -- cette colonne) : toujours affiches quel que soit le filtre, pour
        -- ne jamais masquer un fichier perdu par un choix d'agence.
        SELECT regexp_replace(filename, '\\.pdf$', '', 'i')  AS filename,
               NULL                                          AS agence,
               'Échec'                                       AS statut,
               NULL                                          AS nb_lignes,
               survenu_le                                    AS date_evenement
        FROM echecs_traitement
        WHERE NOT resolu
    ) t
    ORDER BY date_evenement DESC
    """,
    conn,
    params=(agence_filtre, agence_filtre),
)

st.subheader("Journal des fichiers traités")
if agence_filtre:
    st.caption(f"Documents traités filtrés sur « {agence_filtre} » — les échecs restent toujours affichés (non attribuables de façon fiable à une agence).")
st.metric("Fichiers traités au total", len(journal_df))
st.dataframe(
    journal_df,
    use_container_width=True,
    hide_index=True,
    column_config={
        "filename": "Fichier",
        "agence": "Agence",
        "statut": "Statut",
        "nb_lignes": "Lignes extraites",
        "date_evenement": st.column_config.DatetimeColumn("Date", format="DD/MM/YYYY HH:mm"),
    },
)
