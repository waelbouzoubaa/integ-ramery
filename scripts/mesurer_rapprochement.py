"""Mesure le taux de rapprochement de "Recherche de prix" en CLI, hors Streamlit.

Appelle directement src/db/recherche_prix.py (meme pipeline que la
production, voir pdf_watcher.py) - plus de copie dupliquee du prompt ou de
l'arbitrage ici (ancien risque documente dans CLAUDE.md : toute divergence
entre deux copies du pipeline fausse les mesures). Cache l'extraction sur
disque : le PDF n'est extrait qu'une fois, les runs suivants reutilisent le
meme JSON - les ecarts de taux entre deux runs mesurent alors les
changements de la base/du matching, pas la variance Gemini d'extraction.

Usage (depuis la racine du repo, via le service watcher) :
  docker compose run --rm -v ./scripts:/app/scripts watcher \
      uv run --no-sync python /app/scripts/mesurer_rapprochement.py \
      /app/data/test/DQE_Lot_1.pdf [--agence=NOM]

Écrit à côté du PDF : <nom>.extraction.json (cache) et <nom>.resultat.csv.
"""

import os
import sys
import warnings
from pathlib import Path

import pandas as pd
import psycopg
from dotenv import load_dotenv

warnings.filterwarnings("ignore", message="pandas only supports SQLAlchemy")
load_dotenv()

sys.path.insert(0, "/app/src/extraction")
sys.path.insert(0, "/app/src/db")

from recherche_prix import extraire, rapprocher  # noqa: E402
from schema import ExtractionSansPrix  # noqa: E402


def main():
    pdf_path = Path(sys.argv[1])
    agence = next((a.split("=", 1)[1] for a in sys.argv[2:] if a.startswith("--agence=")), None)
    cache_path = pdf_path.with_suffix(".extraction.json")
    csv_path = pdf_path.with_suffix(".resultat.csv")

    if cache_path.exists():
        resultat = ExtractionSansPrix.model_validate_json(cache_path.read_text(encoding="utf-8"))
        print(f"Extraction relue du cache : {len(resultat.items)} lignes ({cache_path.name})")
    else:
        print("Extraction Gemini du PDF...", flush=True)
        resultat = extraire(pdf_path.read_bytes(), pdf_path.name)
        cache_path.write_text(resultat.model_dump_json(indent=1), encoding="utf-8")
        print(f"{len(resultat.items)} lignes extraites, cache écrit : {cache_path.name}")

    conn = psycopg.connect(os.environ["PGURL"], autocommit=True)

    print("Présélection + arbitrage Gemini...", flush=True)
    lignes = rapprocher(
        conn, resultat, agence,
        on_etape=lambda e: print(f"  étape : {e}", flush=True),
    )

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
