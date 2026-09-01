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
from fusion_designations import _sens_comparaison, memes_nombres  # noqa: E402

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
            temperature=0,
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

# Bandeau d'etapes (stepper) : visualise ou en est le pipeline
# extraction -> rapprochement -> validation Gemini. Meme mecanique que le
# stepper du projet "Agent reunion" (cercles relies par des traits, 3 etats
# fait/actif/a venir), recolore aux couleurs Ramery.
ETAPES = [("extraction", "📄", "Extraction"), ("rapprochement", "🔍", "Rapprochement"), ("validation", "🤖", "Validation Gemini")]

st.markdown(
    """
    <style>
    .stepper-wrap { display: flex; align-items: flex-start; justify-content: center; margin: 0 auto 1.5rem; max-width: 600px; }
    .step-item { display: flex; flex-direction: column; align-items: center; gap: 6px; min-width: 110px; }
    .step-icon { width: 38px; height: 38px; border-radius: 50%; display: flex; align-items: center; justify-content: center; font-size: 1rem; font-weight: 700; flex-shrink: 0; }
    .step-icon-done    { background: #003D7C; color: #fff; }
    .step-icon-active  { background: #003D7C; color: #fff; box-shadow: 0 0 12px rgba(0,61,124,.45); }
    .step-icon-pending { background: #F0F0F0; color: #9CA3AF; border: 2px solid #D1D5DB; }
    .step-label { font-size: .75rem; font-weight: 600; text-align: center; white-space: nowrap; }
    .step-label-done    { color: #003D7C; }
    .step-label-active  { color: #003D7C; }
    .step-label-pending { color: #9CA3AF; }
    .step-connector { flex: 1; height: 2px; margin-top: 18px; min-width: 25px; }
    .step-connector-done    { background: #003D7C; }
    .step-connector-active  { background: linear-gradient(90deg, #003D7C 0%, #D1D5DB 100%); }
    .step-connector-pending { background: #E2E8F0; }
    </style>
    """,
    unsafe_allow_html=True,
)


def _rendre_stepper(placeholder, etape_active: str | None) -> None:
    """etape_active = cle de l'etape en cours ('extraction', 'rapprochement',
    'validation'), ou None pour tout marquer comme termine (pipeline fini)."""
    cur = len(ETAPES) if etape_active is None else next(
        i for i, (cle, _, _) in enumerate(ETAPES) if cle == etape_active
    )
    html = '<div class="stepper-wrap">'
    for i, (_, icone, label) in enumerate(ETAPES):
        if i < cur:
            ic, lc, affichage = "step-icon-done", "step-label-done", "✓"
        elif i == cur:
            ic, lc, affichage = "step-icon-active", "step-label-active", icone
        else:
            ic, lc, affichage = "step-icon-pending", "step-label-pending", icone
        html += f'<div class="step-item"><div class="step-icon {ic}">{affichage}</div><span class="step-label {lc}">{label}</span></div>'
        if i < len(ETAPES) - 1:
            cc = "step-connector-done" if i < cur else ("step-connector-active" if i == cur else "step-connector-pending")
            html += f'<div class="step-connector {cc}"></div>'
    html += "</div>"
    placeholder.markdown(html, unsafe_allow_html=True)

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
    stepper_placeholder = st.empty()

    if st.session_state.get("prix_fichier_traite") != identifiant_fichier:
        _rendre_stepper(stepper_placeholder, "extraction")
        with st.spinner("Extraction des désignations en cours (peut prendre une minute)..."):
            resultat = extract_pdf_sans_prix(fichier.getvalue(), filename=fichier.name)

        st.success(f"{len(resultat.items)} ligne(s) de désignation extraite(s) du PDF.")
        _rendre_stepper(stepper_placeholder, "rapprochement")

        # Etape 1 : pre-filtre rapide et gratuit (texte + unite + memes nombres)
        # pour chaque ligne, sans encore rien decider - juste raccourcir la liste
        # avant de deranger Gemini (jamais toute la base, seulement un top 5
        # final). Le filtre "memes nombres" est applique DANS le SQL
        # (nombres_texte), AVANT le LIMIT : la similarite texte seule ne "sait"
        # pas qu'un nombre different disqualifie un candidat - avec un LIMIT
        # applique avant ce filtre, un bon candidat au texte moins proche se
        # faisait evincer du top-N par des candidats au texte proche mais au
        # mauvais nombre, de toute facon rejetes ensuite. Bugs reels : "bordure
        # ...type T2 <50ml" (bon candidat a 0.38 evince d'un top 5 par des 0.6+
        # sans le "50"), puis "Canalisation PVC CR 8 Ø 125" (bon candidat
        # "tuyaux PVC Ø125 CR8" encore evince d'un top 20 par des canalisations
        # aux autres diametres). memes_nombres reste applique cote Python pour
        # le sens de comparaison ("<2,50" vs ">2,50"), que le SQL ne voit pas.
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
                  AND nombres_texte(designation) = nombres_texte(%s)
                ORDER BY score DESC
                LIMIT 20
                """,
                conn, params=(item.designation, item.unite, item.designation),
            )
            # Sur un resultat vide, pd.read_sql + psycopg renvoie un DataFrame
            # sans colonnes -> sort_values("score") leverait un KeyError.
            if not candidats.empty:
                candidats = candidats[candidats["designation"].apply(lambda d: memes_nombres(item.designation, d))]
                candidats = candidats.sort_values("score", ascending=False).head(5)

            # Rattrapage : si l'egalite stricte des nombres ne donne AUCUN
            # candidat, retenter en INCLUSION (nombres du candidat ⊆ nombres du
            # bordereau). Cas reel : une ligne porteuse d'une borne de quantite
            # ou de montant ("Grave 0/31.5 recyclee (<250T)", "Travaux compris
            # entre 1500 et 5000 €") ne trouvait JAMAIS rien - le seuil
            # tarifaire (250, 1500...) n'existe pas dans les designations en
            # base. Sur le bordereau test, 22 des 36 lignes "aucun candidat"
            # portaient une telle borne. Un candidat qui OMET un nombre du
            # bordereau reste plausible (il n'affirme rien de contradictoire),
            # contrairement a un candidat au nombre DIFFERENT - et Gemini
            # arbitre toujours (jamais d'auto-accept ici : texte jamais
            # identique par construction). Le sens de comparaison reste
            # bloquant ("<2,50" ne matche pas ">2,50").
            if candidats.empty:
                candidats = pd.read_sql(
                    """
                    SELECT designation, sous_famille, unite, prix_moyen_corrige, nb_occurrences,
                           similarity(normaliser_recherche(designation), normaliser_recherche(%s)) AS score
                    FROM prix_moyen_par_designation
                    WHERE unite IS NOT DISTINCT FROM normaliser_unite(%s)
                      AND nombres_tab(designation) <@ nombres_tab(%s)
                    ORDER BY score DESC
                    LIMIT 5
                    """,
                    conn, params=(item.designation, item.unite, item.designation),
                )
                if not candidats.empty:
                    sens_bordereau = _sens_comparaison(item.designation)
                    candidats = candidats[candidats["designation"].apply(
                        lambda d: sens_bordereau is None
                        or _sens_comparaison(d) is None
                        or _sens_comparaison(d) == sens_bordereau
                    )]

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
            _rendre_stepper(stepper_placeholder, "validation")
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
                    "raison_echec": None,
                })
            else:
                if candidats.empty:
                    raison = "Aucun candidat présélectionné (texte, unité ou nombres différents)"
                else:
                    raison = f"Gemini a rejeté les {len(candidats)} candidat(s) présélectionné(s)"
                lignes_resultat.append({
                    "designation_bordereau": item.designation,
                    "unite": item.unite,
                    "designation_trouvee": None,
                    "sous_famille_trouvee": None,
                    "prix_moyen_corrige": None,
                    "nb_occurrences": None,
                    "score_correspondance": None,
                    "raison_echec": raison,
                })

        # Resultat mis en cache dans la session : les prochains re-runs du
        # script (ex. clic sur "Telecharger en CSV") reutilisent ce resultat
        # au lieu de tout recalculer, tant que le meme fichier reste charge.
        st.session_state["prix_fichier_traite"] = identifiant_fichier
        st.session_state["prix_df_resultat"] = pd.DataFrame(lignes_resultat)

    # Etat final (fait ou reutilise du cache) : les 3 etapes passent a "fait".
    _rendre_stepper(stepper_placeholder, None)

    df_resultat = st.session_state["prix_df_resultat"]
    nb_trouves = int(df_resultat["prix_moyen_corrige"].notna().sum())
    st.write(f"**{nb_trouves} / {len(df_resultat)}** désignations rapprochées avec succès.")
    if nb_trouves < len(df_resultat):
        nb_sans_candidat = int(df_resultat["raison_echec"].str.startswith("Aucun candidat", na=False).sum())
        nb_rejetes_gemini = int(df_resultat["raison_echec"].str.startswith("Gemini a rejeté", na=False).sum())
        st.caption(
            f"Sur les {len(df_resultat) - nb_trouves} lignes sans correspondance : "
            f"{nb_sans_candidat} n'avaient aucun candidat présélectionné (texte/unité/nombres), "
            f"{nb_rejetes_gemini} avaient des candidats mais Gemini a jugé qu'aucun ne correspondait vraiment. "
            "Elles restent affichées ci-dessous pour que rien ne disparaisse silencieusement — à compléter manuellement."
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
            "raison_echec": "Raison (si non trouvé)",
        },
    )

    csv = df_resultat.to_csv(index=False, sep=";", decimal=",").encode("utf-8-sig")
    st.download_button(
        "📥 Télécharger en CSV",
        data=csv,
        file_name=f"prix_{Path(fichier.name).stem}.csv",
        mime="text/csv",
    )
