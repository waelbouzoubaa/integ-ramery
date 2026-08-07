import os

import pandas as pd
import psycopg
import streamlit as st
from dotenv import load_dotenv

load_dotenv()

st.set_page_config(page_title="Revue des groupes", page_icon="🔍", layout="wide")


@st.cache_resource
def get_conn():
    pgurl = os.getenv("PGURL")
    if not pgurl:
        st.error("PGURL manquant dans .env")
        st.stop()
    return psycopg.connect(pgurl, autocommit=True)


conn = get_conn()


def _sql_none(v):
    # pandas represente une valeur NULL de colonne texte par NaN (float),
    # meme quand le dtype de colonne reste "str" - psycopg refuse alors de
    # comparer ce NaN a une colonne text ("text = double precision"). On le
    # reconvertit en None avant de l'utiliser comme parametre SQL.
    return None if pd.isna(v) else v


st.title("Revue des groupes de désignations")
st.caption(
    "Un groupe rassemble plusieurs désignations jugées identiques (normalisation "
    "automatique ou validation Gemini). Valide un groupe pour verrouiller sa "
    "composition, ou retire une ligne si le regroupement te semble faux."
)

groupes_df = pd.read_sql(
    """
    SELECT g.id, g.designation_canonique, g.sous_famille, g.unite,
           g.valide, g.seuil_confiance,
           count(pl.id)                                AS nb_membres,
           count(pl.id) FILTER (WHERE pl.en_attente)    AS nb_en_attente
    FROM groupes g
    JOIN price_lines pl
      ON coalesce(pl.designation_canonique, pl.designation) = g.designation_canonique
     AND pl.sous_famille IS NOT DISTINCT FROM g.sous_famille
     AND pl.unite IS NOT DISTINCT FROM g.unite
    GROUP BY g.id, g.designation_canonique, g.sous_famille, g.unite, g.valide, g.seuil_confiance
    ORDER BY g.valide ASC, nb_en_attente DESC, nb_membres DESC
    """,
    conn,
)

if groupes_df.empty:
    st.info("Aucun groupe pour l'instant — lance fusion_designations.py.")
    st.stop()


def _statut(row):
    if row["valide"]:
        return "✅ Validé"
    if row["nb_en_attente"]:
        return f"🕓 {row['nb_en_attente']} en attente"
    return "⏳ À valider"


groupes_df["statut"] = groupes_df.apply(_statut, axis=1)
groupes_df["libelle"] = (
    groupes_df["statut"] + " — " + groupes_df["designation_canonique"]
    + "  [" + groupes_df["sous_famille"].fillna("—") + " / " + groupes_df["unite"].fillna("—") + "]"
    + "  (" + groupes_df["nb_membres"].astype(str) + " lignes)"
)

choix = st.selectbox("Choisir un groupe", options=groupes_df["libelle"].tolist())
# Extraction colonne par colonne (pas via .iloc[0] sur la ligne complete) :
# une ligne melant texte/bool/Decimal se fait upcaster par pandas et un
# sous_famille NULL devient NaN (float), ce que Postgres refuse de comparer
# a une colonne text (IS NOT DISTINCT FROM $2 -> text = double precision).
ligne_groupe = groupes_df[groupes_df["libelle"] == choix]
groupe_id = int(ligne_groupe["id"].iloc[0])
canon = ligne_groupe["designation_canonique"].iloc[0]
sf = _sql_none(ligne_groupe["sous_famille"].iloc[0])
u = _sql_none(ligne_groupe["unite"].iloc[0])
valide = bool(ligne_groupe["valide"].iloc[0])
nb_membres = int(ligne_groupe["nb_membres"].iloc[0])
seuil_confiance = float(ligne_groupe["seuil_confiance"].iloc[0])

col1, col2, col3 = st.columns(3)
col1.metric("Statut", "Validé" if valide else "À valider")
col2.metric("Membres", nb_membres)
col3.metric("Seuil de confiance", f"{seuil_confiance:.2f}")

membres_df = pd.read_sql(
    """
    SELECT pl.id, pl.designation, pl.sous_famille, pl.unite, pl.chapitre,
           pl.prix_unitaire, pl.montant_ht, pd.filename AS document, pl.en_attente
    FROM price_lines pl
    JOIN price_documents pd ON pd.id = pl.document_id
    WHERE coalesce(pl.designation_canonique, pl.designation) = %s
      AND pl.sous_famille IS NOT DISTINCT FROM %s
      AND pl.unite IS NOT DISTINCT FROM %s
    ORDER BY pl.en_attente DESC, pl.prix_unitaire
    """,
    conn,
    params=(canon, sf, u),
)


def _colorer(row):
    couleur = "background-color: #fff3cd" if row["en_attente"] else ""
    return [couleur] * len(row)


st.dataframe(
    membres_df.drop(columns=["id"]).style.apply(_colorer, axis=1),
    use_container_width=True,
    hide_index=True,
    column_config={
        "designation": "Désignation",
        "sous_famille": "Sous-famille",
        "unite": "Unité",
        "chapitre": "Chapitre",
        "prix_unitaire": st.column_config.NumberColumn("Prix unitaire (€)", format="%.2f"),
        "montant_ht": st.column_config.NumberColumn("Montant HT (€)", format="%.2f"),
        "document": "Document",
        "en_attente": "En attente",
    },
)

st.divider()

col_valider, col_retirer = st.columns(2)

with col_valider:
    st.subheader("Valider ce groupe")
    st.write("Verrouille la composition actuelle : plus aucune fusion automatique ne la modifiera.")
    if st.button("✅ Valider le groupe", type="primary"):
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE groupes SET valide = true, valide_le = now() WHERE id = %s",
                (groupe_id,),
            )
            cur.execute(
                """
                UPDATE price_lines
                SET fusion_manuelle = true, en_attente = false
                WHERE coalesce(designation_canonique, designation) = %s
                  AND sous_famille IS NOT DISTINCT FROM %s
                  AND unite IS NOT DISTINCT FROM %s
                """,
                (canon, sf, u),
            )
        st.success("Groupe validé et verrouillé.")
        st.rerun()

with col_retirer:
    st.subheader("Retirer une ligne")
    membres_df["libelle_ligne"] = membres_df["designation"] + "  —  " + membres_df["document"]
    ligne_choisie = st.selectbox(
        "Ligne à retirer du groupe",
        options=["(aucune)"] + membres_df["libelle_ligne"].tolist(),
    )

    if ligne_choisie != "(aucune)":
        ligne_membre = membres_df[membres_df["libelle_ligne"] == ligne_choisie]
        ligne_id = int(ligne_membre["id"].iloc[0])
        designation_brute = ligne_membre["designation"].iloc[0]

        # Reassignation possible uniquement vers un groupe de meme
        # (sous_famille, unite) : hors de ca, ce ne serait pas un prix
        # comparable (cf. la regle qui a motive la cle de regroupement).
        autres_groupes_df = groupes_df[
            (groupes_df["id"] != groupe_id)
            & (groupes_df["sous_famille"].fillna("") == (sf or ""))
            & (groupes_df["unite"].fillna("") == (u or ""))
        ]

        action = st.radio(
            "Que faire de cette ligne ?",
            ["La laisser seule", "La réassigner à un autre groupe"],
        )

        autre_canon = None
        if action == "La réassigner à un autre groupe":
            if autres_groupes_df.empty:
                st.warning("Aucun autre groupe compatible (même sous-famille/unité) pour l'instant.")
            else:
                choix_destination = st.selectbox(
                    "Groupe de destination", options=autres_groupes_df["libelle"].tolist()
                )
                autre_canon = autres_groupes_df.loc[
                    autres_groupes_df["libelle"] == choix_destination, "designation_canonique"
                ].iloc[0]

        peut_confirmer = action == "La laisser seule" or autre_canon is not None
        if st.button("🗑️ Confirmer le retrait", disabled=not peut_confirmer):
            with conn.cursor() as cur:
                if action == "La laisser seule":
                    # fusion_manuelle reste false ici (volontairement) : la
                    # ligne redevient candidate a un futur regroupement
                    # automatique si d'autres designations similaires
                    # arrivent (cf. besoin exprime : "ca forme un groupe").
                    # Seul son retrait DE CE groupe precis est verrouille,
                    # via le rejet enregistre dans fusion_decisions ci-dessous.
                    cur.execute(
                        """
                        UPDATE price_lines
                        SET designation_canonique = NULL, en_attente = false
                        WHERE id = %s
                        """,
                        (ligne_id,),
                    )
                    # Empeche la refusion automatique immediate avec ce
                    # groupe precis (l'humain vient de dire "non") : le
                    # prochain run de fusion_designations.py respectera ce
                    # rejet via le cache fusion_decisions.
                    a, b = sorted([designation_brute, canon])
                    cur.execute(
                        """
                        INSERT INTO fusion_decisions (sous_famille, unite, designation_a, designation_b, fusionner)
                        VALUES (%s, %s, %s, %s, false)
                        ON CONFLICT (coalesce(sous_famille, ''), coalesce(unite, ''), designation_a, designation_b)
                        DO UPDATE SET fusionner = false, decide_le = now()
                        """,
                        (sf, u, a, b),
                    )
                else:
                    cur.execute(
                        """
                        UPDATE price_lines
                        SET designation_canonique = %s, fusion_manuelle = true, en_attente = false
                        WHERE id = %s
                        """,
                        (autre_canon, ligne_id),
                    )
            st.success("Ligne retirée du groupe.")
            st.rerun()
