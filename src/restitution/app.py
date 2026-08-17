import os
from collections import Counter

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

seuil_actuel = float(pd.read_sql("SELECT seuil_cv_anomalie FROM parametres WHERE id = 1", conn)["seuil_cv_anomalie"].iloc[0])
with st.expander("⚙️ Paramètres de détection des anomalies de prix"):
    st.caption(
        "Si le coefficient de variation (écart-type / prix moyen) d'une désignation "
        "dépasse ce seuil, les valeurs les plus hors norme (méthode IQR) sont "
        "exclues, puis le calcul est refait — et recommence tant que le résultat "
        "dépasse encore le seuil, jusqu'à repasser en dessous ou ne plus rien "
        "trouver à exclure. La moyenne affichée est alors corrigée automatiquement "
        "et marquée d'un astérisque *."
    )
    nouveau_seuil = st.number_input(
        "Seuil de coefficient de variation (%)",
        min_value=0.0, max_value=100.0, value=seuil_actuel, step=0.5,
    )
    if st.button("Enregistrer le seuil"):
        with conn.cursor() as cur:
            cur.execute("UPDATE parametres SET seuil_cv_anomalie = %s WHERE id = 1", (nouveau_seuil,))
        st.success("Seuil mis à jour.")
        st.rerun()

recherche = st.text_input("Rechercher une désignation (recherche partielle) ou un prix")

query = """
    SELECT sous_famille, unite, designation, nb_occurrences,
           prix_moyen, ecart_type, prix_min, prix_max, coefficient_variation,
           anomalie_detectee, prix_moyen_corrige, ecart_type_corrige,
           nb_valeurs_aberrantes, q1, q3, borne_basse, borne_haute, valeurs_retenues
    FROM prix_moyen_par_designation
"""
params: tuple = ()
if recherche:
    # Si le texte tape est un nombre, on cherche un PRIX (utile pour
    # retrouver a quelle designation appartient une valeur vue dans un PDF).
    # Sinon, recherche textuelle sur la designation, normalisee (accents/
    # espaces/casse) des deux cotes pour ne pas rater un resultat a cause
    # d'un accent absent (bug remonte : "deviation" ne trouvait pas
    # "déviation").
    prix_recherche = None
    try:
        prix_recherche = float(recherche.strip().replace(",", ".").replace(" ", ""))
    except ValueError:
        pass
    if prix_recherche is not None:
        query += " WHERE %s BETWEEN prix_min AND prix_max"
        params = (prix_recherche,)
    else:
        query += " WHERE normaliser_recherche(designation) ILIKE '%%' || normaliser_recherche(%s) || '%%'"
        params = (recherche,)
query += " ORDER BY nb_occurrences DESC"

df = pd.read_sql(query, conn, params=params)
df["designation_affichee"] = df["designation"] + df["anomalie_detectee"].apply(lambda a: " *" if a else "")

st.dataframe(
    df[[
        "designation_affichee", "sous_famille", "unite", "nb_occurrences",
        "prix_moyen", "prix_moyen_corrige", "ecart_type_corrige",
        "prix_min", "prix_max",
    ]],
    use_container_width=True,
    hide_index=True,
    column_config={
        "sous_famille": "Sous-famille",
        "unite": "Unité",
        "designation_affichee": "Désignation",
        "nb_occurrences": "Occurrences",
        "prix_moyen": st.column_config.NumberColumn("Moyenne brute (€)", format="%.2f"),
        "prix_moyen_corrige": st.column_config.NumberColumn("Moyenne corrigée (€)", format="%.2f"),
        "ecart_type_corrige": st.column_config.NumberColumn("Écart-type", format="%.2f"),
        "prix_min": st.column_config.NumberColumn("Min (€)", format="%.2f"),
        "prix_max": st.column_config.NumberColumn("Max (€)", format="%.2f"),
    },
)
if df["anomalie_detectee"].any():
    st.caption("* = moyenne corrigée automatiquement (valeur(s) aberrante(s) exclue(s) — détail ci-dessous). Sans anomalie, les 2 colonnes sont identiques.")

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
    anomalie = bool(ligne["anomalie_detectee"])

    detail_query = """
        SELECT pd.filename AS document, pl.chapitre, pl.sous_famille,
               pl.unite, pl.quantite, pl.prix_unitaire, pl.montant_ht,
               pl.designation AS designation_brute
        FROM price_lines pl
        JOIN price_documents pd ON pd.id = pl.document_id
        WHERE coalesce(pl.designation_canonique, pl.designation) = %s
          AND pl.sous_famille IS NOT DISTINCT FROM %s
          AND pl.unite_canonique IS NOT DISTINCT FROM %s
        ORDER BY pl.prix_unitaire
    """
    detail_df = pd.read_sql(detail_query, conn, params=(designation_choisie, sous_famille_choisie, unite_choisie))

    if anomalie:
        borne_basse = float(ligne["borne_basse"])
        borne_haute = float(ligne["borne_haute"])
        st.warning(
            f"⚠️ Moyenne corrigée automatiquement : {int(ligne['nb_valeurs_aberrantes'])} valeur(s) "
            f"jugée(s) aberrante(s) (méthode IQR, correction itérative) exclue(s) du calcul.\n\n"
            f"Moyenne brute (toutes les lignes) : **{ligne['prix_moyen']:.2f} €** "
            f"→ moyenne corrigée : **{ligne['prix_moyen_corrige']:.2f} €**"
        )
        with st.expander("Détail du calcul de correction (méthode IQR)"):
            st.write(
                f"Intervalle de prix considéré comme normal au dernier passage "
                f"(la correction répète l'exclusion tant que l'écart-type reste "
                f"trop élevé) : **[{borne_basse:.2f} € ; {borne_haute:.2f} €]**."
            )
            # Comparer chaque prix aux bornes du DERNIER passage serait faux :
            # une valeur exclue a un passage precedent peut retomber dans cet
            # intervalle une fois les extremes retires. On compare plutot a la
            # liste reelle des valeurs retenues a la fin (Counter pour gerer
            # les doublons : peu importe QUELLE ligne exacte parmi des prix
            # identiques est marquee exclue, seul le compte final compte).
            valeurs_retenues = Counter(float(v) for v in ligne["valeurs_retenues"])

            def _statut(p):
                p = float(p)
                if valeurs_retenues[p] > 0:
                    valeurs_retenues[p] -= 1
                    return "✅ Retenue"
                return "❌ Exclue (aberrante)"

            detail_df["statut"] = detail_df["prix_unitaire"].apply(_statut)

    st.dataframe(
        detail_df,
        use_container_width=True,
        hide_index=True,
        column_config={
            "document": "Document",
            "designation_brute": "Désignation (telle qu'écrite dans le PDF)",
            "chapitre": "Chapitre",
            "sous_famille": "Sous-famille",
            "unite": "Unité",
            "quantite": st.column_config.NumberColumn("Quantité", format="%.2f"),
            "prix_unitaire": st.column_config.NumberColumn("Prix unitaire (€)", format="%.2f"),
            "montant_ht": st.column_config.NumberColumn("Montant HT (€)", format="%.2f"),
            "statut": "Statut",
        },
    )
