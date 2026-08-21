import os
import sys
from pathlib import Path

import pandas as pd
import psycopg
import streamlit as st
from dotenv import load_dotenv
from google import genai
from google.genai import types
from pydantic import BaseModel

load_dotenv()

# gemini_extract.py vit dans src/extraction, pas dans src/restitution : on
# l'ajoute au path pour reutiliser directement extract_pdf_sans_prix, sans
# dupliquer la logique d'appel Gemini ici (meme pattern que
# revue_des_groupes.py pour fusion_designations).
EXTRACTION_DIR = Path(__file__).resolve().parents[2] / "extraction"
if str(EXTRACTION_DIR) not in sys.path:
    sys.path.insert(0, str(EXTRACTION_DIR))
DB_DIR = Path(__file__).resolve().parents[2] / "db"
if str(DB_DIR) not in sys.path:
    sys.path.insert(0, str(DB_DIR))

from gemini_extract import extract_pdf_sans_prix  # noqa: E402
from fusion_designations import memes_nombres  # noqa: E402

MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-pro")
TAILLE_LOT = 20  # lignes de bordereau par appel Gemini (garde le prompt raisonnable)

PROMPT_RAPPROCHEMENT = """Tu reçois des lignes de désignation issues d'un bordereau de prix BTP VIERGE
(sans prix), et pour chacune une courte liste de candidats trouvés dans une
base de prix existante (déjà pré-filtrés par ressemblance de texte et même
unité). Pour CHAQUE ligne, détermine si un des candidats décrit RÉELLEMENT LE
MÊME PRODUIT que la désignation du bordereau (permettant de réutiliser son
prix moyen), ou si aucun candidat ne correspond vraiment.

Sois strict : une différence de nature du produit (matériau fourni vs
matériau réutilisé/existant sur site, classe, diamètre, épaisseur, méthode
de pose) rend deux désignations NON équivalentes, même si le texte se
ressemble beaucoup. Dans le doute, réponds qu'aucun candidat ne correspond
plutôt que de choisir au hasard.

Réponds pour CHAQUE id reçu, dans le même ordre : l'index (0-based) du bon
candidat dans SA liste, ou -1 si aucun candidat ne correspond."""


class ChoixRapprochement(BaseModel):
    id: int
    index_candidat: int  # -1 si aucun candidat ne correspond


class ChoixRapprochements(BaseModel):
    choix: list[ChoixRapprochement]


_client: genai.Client | None = None


def _get_client() -> genai.Client:
    global _client
    if _client is None:
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            st.error("GEMINI_API_KEY manquant dans .env")
            st.stop()
        _client = genai.Client(api_key=api_key)
    return _client


def _arbitrer_par_lot(lot: list[tuple]) -> dict[int, int]:
    """lot = liste de (id, designation_bordereau, [designations_candidates]).
    Renvoie {id: index_candidat_choisi (-1 si aucun)}."""
    lignes = []
    for id_, designation, candidats_txt in lot:
        options = "  ".join(f"[{i}] {c!r}" for i, c in enumerate(candidats_txt))
        lignes.append(f"{id_}. Bordereau: {designation!r}\n   Candidats: {options}")

    response = _get_client().models.generate_content(
        model=MODEL,
        contents=[PROMPT_RAPPROCHEMENT, "\n".join(lignes)],
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=ChoixRapprochements,
        ),
    )
    result = response.parsed if response.parsed is not None else ChoixRapprochements.model_validate_json(response.text)
    return {c.id: c.index_candidat for c in result.choix}


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
    "désignation, présélectionne les candidats les plus proches en base "
    "(texte + unité identique, jamais entre deux nombres différents), puis "
    "Gemini tranche lequel décrit vraiment le même produit avant d'afficher "
    "le prix moyen corrigé correspondant."
)

fichier = st.file_uploader("Bordereau vierge (PDF)", type=["pdf"])

if fichier is not None:
    # Chaque interaction Streamlit (meme cliquer sur "Telecharger en CSV")
    # relance TOUT le script depuis le haut - sans ce garde-fou, cliquer sur
    # le bouton de telechargement relancait l'extraction ET tout le
    # rapprochement Gemini a chaque fois (bug remonte). On ne retraite le
    # fichier que s'il a change (nom+taille), sinon on reutilise le resultat
    # deja calcule, stocke dans la session.
    identifiant_fichier = f"{fichier.name}_{fichier.size}"

    if st.session_state.get("prix_fichier_traite") != identifiant_fichier:
        with st.spinner("Extraction des désignations en cours (peut prendre une minute)..."):
            resultat = extract_pdf_sans_prix(fichier.getvalue(), filename=fichier.name)

        st.success(f"{len(resultat.items)} ligne(s) de désignation extraite(s) du PDF.")

        # Etape 1 : pre-filtre rapide et gratuit (texte + unite + memes nombres)
        # pour chaque ligne, sans encore rien decider - juste raccourcir la liste
        # avant de deranger Gemini (jamais toute la base, seulement le top 5).
        candidats_par_item = []
        n_items = len(resultat.items)
        barre_preselection = st.progress(0.0, text="Présélection des candidats...")
        for idx, item in enumerate(resultat.items):
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
            candidats_par_item.append(candidats.reset_index(drop=True))
            barre_preselection.progress(
                (idx + 1) / n_items, text=f"Présélection des candidats... ({idx + 1}/{n_items})"
            )
        barre_preselection.empty()

        # Etape 2 : score de similarite = 1.0 (texte identique une fois normalise)
        # -> accepte directement, pas la peine de deranger Gemini pour confirmer
        # une evidence. Gemini n'arbitre que les cas ou le texte n'est PAS
        # parfaitement identique mais qu'il reste au moins un candidat pre-filtre
        # (sinon : rien a arbitrer, "non trouve dans la base").
        choix_gemini: dict[int, int] = {}
        a_arbitrer = []
        for i in range(n_items):
            candidats = candidats_par_item[i]
            if candidats.empty:
                continue
            if candidats.iloc[0]["score"] >= 0.999:
                choix_gemini[i] = 0
            else:
                a_arbitrer.append((i, resultat.items[i].designation, candidats["designation"].tolist()))

        if a_arbitrer:
            n_lots = -(-len(a_arbitrer) // TAILLE_LOT)
            barre_arbitrage = st.progress(0.0, text=f"Gemini arbitre {len(a_arbitrer)} rapprochement(s)...")
            for lot_idx, i in enumerate(range(0, len(a_arbitrer), TAILLE_LOT)):
                lot = a_arbitrer[i:i + TAILLE_LOT]
                choix_gemini.update(_arbitrer_par_lot(lot))
                barre_arbitrage.progress(
                    (lot_idx + 1) / n_lots,
                    text=f"Gemini arbitre... (lot {lot_idx + 1}/{n_lots})",
                )
            barre_arbitrage.empty()

        # Etape 3 : assemblage du resultat final
        lignes_resultat = []
        for i, item in enumerate(resultat.items):
            candidats = candidats_par_item[i]
            index_choisi = choix_gemini.get(i, -1)

            if not candidats.empty and 0 <= index_choisi < len(candidats):
                r = candidats.iloc[index_choisi]
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
                lignes_resultat.append({
                    "designation_bordereau": item.designation,
                    "unite": item.unite,
                    "designation_trouvee": None,
                    "sous_famille_trouvee": None,
                    "prix_moyen_corrige": None,
                    "nb_occurrences": None,
                    "score_correspondance": None,
                })

        # Resultat mis en cache dans la session : les prochains re-runs du
        # script (ex. clic sur "Telecharger en CSV") reutilisent ce resultat
        # au lieu de tout recalculer, tant que le meme fichier reste charge.
        st.session_state["prix_fichier_traite"] = identifiant_fichier
        st.session_state["prix_df_resultat"] = pd.DataFrame(lignes_resultat)

    df_resultat = st.session_state["prix_df_resultat"]
    nb_trouves = int(df_resultat["prix_moyen_corrige"].notna().sum())
    st.write(f"**{nb_trouves} / {len(df_resultat)}** désignations rapprochées avec succès.")
    if nb_trouves < len(df_resultat):
        st.caption(
            "Les lignes sans correspondance (aucun candidat présélectionné, "
            "ou Gemini a jugé qu'aucun candidat ne correspondait vraiment) "
            "restent affichées ci-dessous pour que rien ne disparaisse "
            "silencieusement — à compléter manuellement."
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
            "score_correspondance": st.column_config.NumberColumn("Score de similarité", format="%.2f"),
        },
    )

    csv = df_resultat.to_csv(index=False, sep=";", decimal=",").encode("utf-8-sig")
    st.download_button(
        "📥 Télécharger en CSV",
        data=csv,
        file_name=f"prix_{Path(fichier.name).stem}.csv",
        mime="text/csv",
    )
