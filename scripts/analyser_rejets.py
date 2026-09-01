"""Pour chaque ligne rejetée par Gemini, rejoue le préfiltre (strict puis
rattrapage, comme la vue) et affiche les candidats vus par Gemini - pour
classer à la main : rejet légitime (produit absent de la base) vs rejet trop
strict (bon candidat refusé).

Usage :
  docker compose run --rm -v ./scripts:/app/scripts watcher \
      uv run --no-sync python /app/scripts/analyser_rejets.py /app/data/test/rejets_vps.csv
"""

import os
import sys
import warnings

import pandas as pd
import psycopg
from dotenv import load_dotenv

warnings.filterwarnings("ignore", message="pandas only supports SQLAlchemy")
load_dotenv()

sys.path.insert(0, "/app/src/db")
from fusion_designations import _sens_comparaison, memes_nombres  # noqa: E402


def main():
    rejets = pd.read_csv(sys.argv[1], sep=";")
    conn = psycopg.connect(os.environ["PGURL"], autocommit=True)

    for _, ligne in rejets.iterrows():
        des, unite = ligne["designation_bordereau"], ligne["unite"]
        if pd.isna(unite):
            unite = None
        candidats = pd.read_sql(
            """
            SELECT designation, round(similarity(normaliser_recherche(designation), normaliser_recherche(%s))::numeric, 2) AS score
            FROM prix_moyen_par_designation
            WHERE unite IS NOT DISTINCT FROM normaliser_unite(%s)
              AND nombres_texte(designation) = nombres_texte(%s)
            ORDER BY score DESC LIMIT 20
            """,
            conn, params=(des, unite, des),
        )
        mode = "strict"
        if not candidats.empty:
            candidats = candidats[candidats["designation"].apply(lambda d: memes_nombres(des, d))].head(5)
        if candidats.empty:
            mode = "rattrapage"
            candidats = pd.read_sql(
                """
                SELECT designation, round(similarity(normaliser_recherche(designation), normaliser_recherche(%s))::numeric, 2) AS score
                FROM prix_moyen_par_designation
                WHERE unite IS NOT DISTINCT FROM normaliser_unite(%s)
                  AND nombres_tab(designation) <@ nombres_tab(%s)
                ORDER BY score DESC LIMIT 5
                """,
                conn, params=(des, unite, des),
            )
            if not candidats.empty:
                sens = _sens_comparaison(des)
                candidats = candidats[candidats["designation"].apply(
                    lambda d: sens is None or _sens_comparaison(d) is None or _sens_comparaison(d) == sens
                )]

        print(f"\n### [{unite or '?'}] {des}  ({mode})")
        if candidats.empty:
            print("    (aucun candidat)")
        else:
            for _, c in candidats.iterrows():
                print(f"    {c['score']:.2f}  {c['designation'][:130]}")


if __name__ == "__main__":
    main()
