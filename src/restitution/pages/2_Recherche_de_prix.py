import os
import sys
from pathlib import Path

import pandas as pd
import psycopg
import streamlit as st
from dotenv import load_dotenv

load_dotenv()

# gemini_extract.py vit dans src/extraction, pas dans src/restitution : on
# l'ajoute au path pour reutiliser directement extract_pdf_sans_prix, sans
# dupliquer la logique d'appel Gemini ici (meme pattern que
# 1_Revue_des_groupes.py pour fusion_designations).
EXTRACTION_DIR = Path(__file__).resolve().parents[2] / "extraction"
if str(EXTRACTION_DIR) not in sys.path:
    sys.path.insert(0, str(EXTRACTION_DIR))
DB_DIR = Path(__file__).resolve().parents[2] / "db"
if str(DB_DIR) not in sys.path:
    sys.path.insert(0, str(DB_DIR))

from gemini_extract import extract_pdf_sans_prix  # noqa: E402
from fusion_designations import memes_nombres  # noqa: E402

st.set_page_config(page_title="Recherche de prix", page_icon="🔎", layout="wide")


@st.cache_resource
def get_conn():
    pgurl = os.getenv("PGURL")
    if not pgurl:
        st.error("PGURL manquant dans .env")
        st.stop()
    return psycopg.connect(pgurl, autocommit=True)


conn = get_conn()

st.title("Recherche de prix pour un bordereau vierge")
st.caption(
    "Upload un bordereau de prix SANS prix renseignés (DQE/BPU vierge à "
    "compléter, désignations et unités présentes). L'IA extrait chaque "
    "désignation, la rapproche de la base de prix existante par similarité "
    "de texte (jamais entre deux nombres différents — une profondeur, un "
    "diamètre ou une classe qui diffère est presque toujours un produit "
    "différent), et affiche le prix moyen corrigé correspondant."
)

seuil_correspondance = st.slider(
    "Seuil de correspondance (similarité de texte, 0 à 1)",
    min_value=0.0, max_value=1.0, value=0.4, step=0.05,
    help=(
        "En dessous de ce seuil, aucune correspondance n'est proposée pour "
        "la ligne (mieux vaut 'aucune correspondance' qu'un mauvais "
        "rapprochement)."
    ),
)

fichier = st.file_uploader("Bordereau vierge (PDF)", type=["pdf"])

if fichier is not None:
    with st.spinner("Extraction des désignations en cours (peut prendre une minute)..."):
        resultat = extract_pdf_sans_prix(fichier.getvalue(), filename=fichier.name)

    st.success(f"{len(resultat.items)} ligne(s) de désignation extraite(s) du PDF.")

    # Une requete par ligne extraite (volume typique d'un bordereau : quelques
    # dizaines a centaines de lignes, largement gerable en boucle simple sans
    # avoir a batcher en une seule requete geante).
    lignes_resultat = []
    for item in resultat.items:
        # Top 5 (pas juste le meilleur score) : la similarite de texte seule
        # confond des variantes qui ne different que par un nombre (ex.
        # "Fraisage de 6 a 12 cm" vs "Fraisage de 0 a 6 cm" - meme texte a un
        # chiffre pres, score eleve, mais probablement des prix differents).
        # On filtre ensuite avec memes_nombres (meme regle que la fusion des
        # designations) : un nombre qui differe (profondeur, diametre,
        # classe...) est presque toujours un produit different dans ce metier.
        candidats = pd.read_sql(
            """
            SELECT designation, sous_famille, unite, prix_moyen_corrige, nb_occurrences,
                   similarity(normaliser_recherche(designation), normaliser_recherche(%s)) AS score
            FROM prix_moyen_par_designation
            WHERE unite IS NOT DISTINCT FROM normaliser_unite(%s)
            ORDER BY score DESC
            LIMIT 5
            """,
            conn, params=(item.designation, item.unite),
        )
        candidats = candidats[candidats["designation"].apply(lambda d: memes_nombres(item.designation, d))]

        trouve = not candidats.empty and candidats.iloc[0]["score"] >= seuil_correspondance
        if trouve:
            r = candidats.iloc[0]
            lignes_resultat.append({
                "designation_bordereau": item.designation,
                "unite": item.unite,
                "designation_trouvee": r["designation"],
                "sous_famille_trouvee": r["sous_famille"],
                "prix_moyen_corrige": float(r["prix_moyen_corrige"]),
                "nb_occurrences": int(r["nb_occurrences"]),
                "score_correspondance": round(float(r["score"]), 2),
            })
        else:
            score_le_plus_proche = (
                round(float(candidats.iloc[0]["score"]), 2) if not candidats.empty else None
            )
            lignes_resultat.append({
                "designation_bordereau": item.designation,
                "unite": item.unite,
                "designation_trouvee": None,
                "sous_famille_trouvee": None,
                "prix_moyen_corrige": None,
                "nb_occurrences": None,
                "score_correspondance": score_le_plus_proche,
            })

    df_resultat = pd.DataFrame(lignes_resultat)
    nb_trouves = int(df_resultat["prix_moyen_corrige"].notna().sum())
    st.write(f"**{nb_trouves} / {len(df_resultat)}** désignations rapprochées avec succès.")
    if nb_trouves < len(df_resultat):
        st.caption(
            "Les lignes sans correspondance (score sous le seuil, ou aucun "
            "prix de cette unité en base) restent affichées ci-dessous pour "
            "que rien ne disparaisse silencieusement — baisse le seuil si "
            "besoin, ou complète-les manuellement."
        )

    st.dataframe(
        df_resultat,
        use_container_width=True,
        hide_index=True,
        column_config={
            "designation_bordereau": "Désignation (bordereau)",
            "unite": "Unité",
            "designation_trouvee": "Désignation trouvée (base)",
            "sous_famille_trouvee": "Sous-famille (base)",
            "prix_moyen_corrige": st.column_config.NumberColumn("Prix moyen corrigé (€)", format="%.2f"),
            "nb_occurrences": "Occurrences (base)",
            "score_correspondance": st.column_config.NumberColumn("Score de correspondance", format="%.2f"),
        },
    )

    csv = df_resultat.to_csv(index=False, sep=";", decimal=",").encode("utf-8-sig")
    st.download_button(
        "📥 Télécharger en CSV",
        data=csv,
        file_name=f"prix_{Path(fichier.name).stem}.csv",
        mime="text/csv",
    )
