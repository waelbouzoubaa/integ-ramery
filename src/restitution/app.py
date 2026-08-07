import os

import pandas as pd
import psycopg
import streamlit as st
from dotenv import load_dotenv

load_dotenv()

st.set_page_config(page_title="Prix unitaires DQE", page_icon="📊", layout="wide")


@st.cache_resource
def get_conn():
    pgurl = os.getenv("PGURL")
    if not pgurl:
        st.error("PGURL manquant dans .env")
        st.stop()
    # autocommit : lecture seule, on ne veut jamais laisser une transaction
    # ouverte sur cette connexion mise en cache (ca bloque les migrations
    # de schema derriere - deja vu en pratique).
    return psycopg.connect(pgurl, autocommit=True)


conn = get_conn()


def _sql_none(v):
    # pandas represente une valeur NULL de colonne texte par NaN (float),
    # meme quand le dtype de colonne reste "str" - psycopg refuse alors de
    # comparer ce NaN a une colonne text ("text = double precision"). On le
    # reconvertit en None avant de l'utiliser comme parametre SQL.
    return None if pd.isna(v) else v


st.title("Prix unitaires — moyenne par désignation")
st.caption(
    "Moyenne calculee par (sous-famille, unite, designation) : meme texte "
    "mais sous-famille ou unite differente => produits differents (ex: un "
    "diametre decline sur plusieurs classes, ou le meme libelle facture au "
    "ml dans un document et au m2 dans un autre)."
)

recherche = st.text_input("Rechercher une désignation (recherche partielle)")

query = """
    SELECT sous_famille, unite, designation, nb_occurrences, prix_moyen, ecart_type, prix_min, prix_max
    FROM prix_moyen_par_designation
"""
params: tuple = ()
if recherche:
    query += " WHERE designation ILIKE %s"
    params = (f"%{recherche}%",)
query += " ORDER BY nb_occurrences DESC"

df = pd.read_sql(query, conn, params=params)
st.dataframe(
    df,
    use_container_width=True,
    hide_index=True,
    column_config={
        "sous_famille": "Sous-famille",
        "unite": "Unité",
        "designation": "Désignation",
        "nb_occurrences": "Occurrences",
        "prix_moyen": st.column_config.NumberColumn("Prix moyen (€)", format="%.2f"),
        "ecart_type": st.column_config.NumberColumn("Écart-type", format="%.2f"),
        "prix_min": st.column_config.NumberColumn("Min (€)", format="%.2f"),
        "prix_max": st.column_config.NumberColumn("Max (€)", format="%.2f"),
    },
)

st.divider()
st.subheader("Détail d'une désignation")

if not df.empty:
    df["libelle_choix"] = (
        df["sous_famille"].fillna("—") + "  |  " + df["unite"].fillna("—") + "  |  " + df["designation"]
    )
    choix = st.selectbox("Choisir une ligne (sous-famille | unité | désignation)", options=df["libelle_choix"].tolist())
    ligne = df[df["libelle_choix"] == choix].iloc[0]
    sous_famille_choisie = _sql_none(ligne["sous_famille"])
    unite_choisie = _sql_none(ligne["unite"])
    designation_choisie = ligne["designation"]

    detail_query = """
        SELECT pd.filename AS document, pl.chapitre, pl.sous_famille,
               pl.unite, pl.quantite, pl.prix_unitaire, pl.montant_ht
        FROM price_lines pl
        JOIN price_documents pd ON pd.id = pl.document_id
        WHERE coalesce(pl.designation_canonique, pl.designation) = %s
          AND pl.sous_famille IS NOT DISTINCT FROM %s
          AND pl.unite IS NOT DISTINCT FROM %s
        ORDER BY pl.prix_unitaire
    """
    detail_df = pd.read_sql(detail_query, conn, params=(designation_choisie, sous_famille_choisie, unite_choisie))
    st.dataframe(detail_df, use_container_width=True, hide_index=True)
