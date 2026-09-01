"""Mesure le taux de rapprochement de "Recherche de prix" en CLI, hors Streamlit.

Reproduit EXACTEMENT le pipeline de src/restitution/vues/recherche_de_prix.py
(préfiltre SQL + memes_nombres + auto-accept score 1.0 + arbitrage Gemini),
mais avec un cache d'extraction sur disque : le PDF n'est extrait qu'une fois,
les runs suivants réutilisent le même JSON — les écarts de taux entre deux runs
mesurent alors les changements de la base/du matching, pas la variance Gemini
d'extraction.

Usage (depuis la racine du repo, via le service watcher) :
  docker compose run --rm -v ./scripts:/app/scripts watcher \
      uv run --no-sync python /app/scripts/mesurer_rapprochement.py \
      /app/data/test/DQE_Lot_1.pdf

Écrit à côté du PDF : <nom>.extraction.json (cache) et <nom>.resultat.csv.
"""

import os
import sys
import warnings
from pathlib import Path

import pandas as pd
import psycopg
from dotenv import load_dotenv
from google import genai
from google.genai import types
from pydantic import BaseModel

warnings.filterwarnings("ignore", message="pandas only supports SQLAlchemy")
load_dotenv()

sys.path.insert(0, "/app/src/extraction")
sys.path.insert(0, "/app/src/db")

from fusion_designations import _sens_comparaison, memes_nombres  # noqa: E402
from gemini_extract import extract_pdf_sans_prix  # noqa: E402
from schema import ExtractionSansPrix  # noqa: E402

# --- Copie de l'arbitrage de recherche_de_prix.py (prompt + config identiques).
# recherche_de_prix.py exécute toute l'UI Streamlit au niveau module, on ne
# peut pas l'importer proprement ici - toute divergence entre les deux doit
# être reportée des deux côtés.
MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-pro")
TAILLE_LOT = 20

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
    index_candidat: int


class ChoixRapprochements(BaseModel):
    choix: list[ChoixRapprochement]


_client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

from fusion_designations import _retry_surcharge  # noqa: E402


@_retry_surcharge
def _arbitrer_par_lot(lot):
    lignes = []
    for id_, designation, candidats_txt in lot:
        options = "  ".join(f"[{i}] {c!r}" for i, c in enumerate(candidats_txt))
        lignes.append(f"{id_}. Bordereau: {designation!r}\n   Candidats: {options}")
    response = _client.models.generate_content(
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


def _arbitrer(a_arbitrer):
    choix = {}
    n_lots = -(-len(a_arbitrer) // TAILLE_LOT)
    for lot_idx, i in enumerate(range(0, len(a_arbitrer), TAILLE_LOT)):
        lot = a_arbitrer[i:i + TAILLE_LOT]
        choix.update(_arbitrer_par_lot(lot))
        print(f"  arbitrage Gemini lot {lot_idx + 1}/{n_lots}", flush=True)
    return choix


def main():
    pdf_path = Path(sys.argv[1])
    rattrapage = "--rattrapage" in sys.argv[2:]
    filtre_sql = "--filtre-sql" in sys.argv[2:] or rattrapage  # rattrapage = filtre-sql + repechage inclusion
    cache_path = pdf_path.with_suffix(".extraction.json")
    if rattrapage:
        suffixe = ".resultat_rattrapage.csv"
    elif filtre_sql:
        suffixe = ".resultat_filtre_sql.csv"
    else:
        suffixe = ".resultat.csv"
    csv_path = pdf_path.with_suffix(suffixe)

    if cache_path.exists():
        resultat = ExtractionSansPrix.model_validate_json(cache_path.read_text(encoding="utf-8"))
        print(f"Extraction relue du cache : {len(resultat.items)} lignes ({cache_path.name})")
    else:
        print("Extraction Gemini du PDF...", flush=True)
        resultat = extract_pdf_sans_prix(pdf_path.read_bytes(), filename=pdf_path.name)
        cache_path.write_text(resultat.model_dump_json(indent=1), encoding="utf-8")
        print(f"{len(resultat.items)} lignes extraites, cache écrit : {cache_path.name}")

    conn = psycopg.connect(os.environ["PGURL"], autocommit=True)

    # --- Préfiltre : copie conforme de la vue ---
    if filtre_sql:
        # Variante testée : le filtre "mêmes nombres" est appliqué DANS le SQL,
        # avant le LIMIT - le top 20 ne contient que des candidats aux mêmes
        # nombres, un bon candidat au texte moins proche ne peut plus être
        # évincé par des candidats au mauvais nombre.
        requete = """
            SELECT designation, sous_famille, unite, prix_moyen_corrige, nb_occurrences,
                   similarity(normaliser_recherche(designation), normaliser_recherche(%s)) AS score
            FROM prix_moyen_par_designation
            WHERE unite IS NOT DISTINCT FROM normaliser_unite(%s)
              AND nombres_texte(designation) = nombres_texte(%s)
            ORDER BY score DESC
            LIMIT 20
            """
    else:
        requete = """
            SELECT designation, sous_famille, unite, prix_moyen_corrige, nb_occurrences,
                   similarity(normaliser_recherche(designation), normaliser_recherche(%s)) AS score
            FROM prix_moyen_par_designation
            WHERE unite IS NOT DISTINCT FROM normaliser_unite(%s)
            ORDER BY score DESC
            LIMIT 20
            """
    candidats_par_item = []
    for item in resultat.items:
        params = (item.designation, item.unite, item.designation) if filtre_sql else (item.designation, item.unite)
        candidats = pd.read_sql(requete, conn, params=params)
        # Sur un resultat vide, pd.read_sql + psycopg renvoie un DataFrame
        # SANS colonnes -> sort_values("score") leverait un KeyError.
        if not candidats.empty:
            candidats = candidats[candidats["designation"].apply(lambda d: memes_nombres(item.designation, d))]
            candidats = candidats.sort_values("score", ascending=False).head(5)

        # Mode rattrapage (teste par-dessus --filtre-sql) : si l'egalite
        # stricte des nombres ne donne AUCUN candidat, retenter en inclusion
        # (nombres du candidat ⊆ nombres du bordereau) - cas des bornes de
        # quantite/montant du bordereau ("(<250T)", "compris entre 1500 et
        # 5000 €") absentes des designations en base. Gemini arbitre toujours
        # (jamais d'auto-accept : score < 1.0 par construction). Le sens de
        # comparaison reste bloquant ("<2,50" ne matche pas ">2,50").
        if rattrapage and candidats.empty:
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
    print(f"Préfiltre terminé ({len(resultat.items)} lignes).", flush=True)

    choix_gemini = {}
    a_arbitrer = []
    for i in range(len(resultat.items)):
        candidats = candidats_par_item[i]
        if candidats.empty:
            continue
        if candidats.iloc[0]["score"] >= 0.999:
            choix_gemini[i] = 0
        else:
            a_arbitrer.append((i, resultat.items[i].designation, candidats["designation"].tolist()))

    if a_arbitrer:
        choix_gemini.update(_arbitrer(a_arbitrer))

    lignes = []
    for i, item in enumerate(resultat.items):
        candidats = candidats_par_item[i]
        index_choisi = choix_gemini.get(i, -1)
        if not candidats.empty and 0 <= index_choisi < len(candidats):
            r = candidats.iloc[index_choisi]
            lignes.append({
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
            raison = (
                "Aucun candidat présélectionné (texte, unité ou nombres différents)"
                if candidats.empty
                else f"Gemini a rejeté les {len(candidats)} candidat(s) présélectionné(s)"
            )
            lignes.append({
                "designation_bordereau": item.designation,
                "unite": item.unite,
                "designation_trouvee": None,
                "sous_famille_trouvee": None,
                "prix_moyen_corrige": None,
                "nb_occurrences": None,
                "score_correspondance": None,
                "raison_echec": raison,
            })

    df = pd.DataFrame(lignes)
    df.to_csv(csv_path, index=False, sep=";", decimal=",", encoding="utf-8-sig")

    nb_ok = int(df["prix_moyen_corrige"].notna().sum())
    nb_sans = int(df["raison_echec"].str.startswith("Aucun candidat", na=False).sum())
    nb_rejet = int(df["raison_echec"].str.startswith("Gemini a rejeté", na=False).sum())
    print(f"\nRESULTAT : {nb_ok}/{len(df)} rapprochées ({100 * nb_ok / len(df):.0f}%)")
    print(f"  - aucun candidat présélectionné : {nb_sans}")
    print(f"  - rejetées par Gemini           : {nb_rejet}")
    print(f"CSV : {csv_path}")


if __name__ == "__main__":
    main()
