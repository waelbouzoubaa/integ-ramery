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

# Une seule requete regroupant tous les compteurs simples (evite plusieurs
# aller-retours) : lignes/documents bruts, designations (vue), groupes,
# anomalies. "designations sans groupe" = lignes de la vue pour lesquelles
# aucun groupe forme ne correspond (soit jamais fusionnees, soit une seule
# orthographe existante - pas forcement un probleme, voir page Revue des
# groupes pour le detail).
stats = pd.read_sql(
    """
    SELECT
        (SELECT count(*) FROM price_lines)                                   AS total_lignes,
        (SELECT count(*) FROM price_documents)                               AS total_documents,
        (SELECT count(*) FROM prix_moyen_par_designation)                    AS total_designations,
        (SELECT count(*) FROM groupes)                                       AS total_groupes,
        (SELECT count(*) FROM groupes WHERE valide)                          AS groupes_valides,
        (SELECT count(*) FROM prix_moyen_par_designation v
         WHERE NOT EXISTS (
             SELECT 1 FROM groupes g
             WHERE g.designation_canonique = v.designation
               AND g.sous_famille IS NOT DISTINCT FROM v.sous_famille
               AND g.unite IS NOT DISTINCT FROM v.unite
         ))                                                                  AS designations_sans_groupe,
        (SELECT count(*) FROM prix_moyen_par_designation WHERE anomalie_detectee) AS anomalies
    """,
    conn,
).iloc[0]

st.subheader("Vue d'ensemble")
c1, c2, c3 = st.columns(3)
c1.metric("Désignations (total)", int(stats["total_designations"]))
c2.metric("Groupes formés", int(stats["total_groupes"]))
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

st.divider()

col_gauche, col_droite = st.columns(2)

with col_gauche:
    st.subheader("Répartition des désignations par nombre d'occurrences")
    st.caption("Combien de fois chaque désignation apparaît dans les documents — plus il y en a, plus la moyenne est fiable.")
    occ_df = pd.read_sql("SELECT nb_occurrences FROM prix_moyen_par_designation", conn)

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
col_pie1, col_pie2, _ = st.columns([1, 1, 1])
with col_pie1:
    _camembert(
        {
            "Avec groupe": int(stats["total_groupes"]),
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
    FROM prix_moyen_par_designation
    GROUP BY sous_famille
    ORDER BY nb_designations DESC
    LIMIT 10
    """,
    conn,
).set_index("sous_famille")
st.bar_chart(top_sf)
