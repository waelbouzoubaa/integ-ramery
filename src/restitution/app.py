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
    return psycopg.connect(pgurl)


conn = get_conn()

st.title("Prix unitaires — moyenne par désignation")
st.caption(
    "Moyenne calculee sur un match exact de designation (pas de rapprochement "
    "entre libelles proches pour l'instant)."
)

recherche = st.text_input("Rechercher une désignation (recherche partielle)")

query = """
    SELECT designation, nb_occurrences, prix_moyen, ecart_type, prix_min, prix_max
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

designation_choisie = st.selectbox(
    "Choisir une désignation pour voir les lignes source",
    options=df["designation"].tolist() if not df.empty else [],
)

if designation_choisie:
    detail_query = """
        SELECT pd.filename AS document, pl.chapitre, pl.sous_famille,
               pl.unite, pl.quantite, pl.prix_unitaire, pl.montant_ht
        FROM price_lines pl
        JOIN price_documents pd ON pd.id = pl.document_id
        WHERE pl.designation = %s
        ORDER BY pl.prix_unitaire
    """
    detail_df = pd.read_sql(detail_query, conn, params=(designation_choisie,))
    st.dataframe(detail_df, use_container_width=True, hide_index=True)
