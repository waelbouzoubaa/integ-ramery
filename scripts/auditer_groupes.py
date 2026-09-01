"""Audit mécanique des groupes de désignations : détecte les groupes dont les
membres ne peuvent PAS être le même produit selon les règles déterministes du
projet (mêmes nombres obligatoires, sens de comparaison opposés interdits).

Les groupes formés AVANT les fixes récents (memes_nombres, normalisation des
<'>) peuvent contenir des membres incompatibles fusionnés à l'époque - ils
moyennent alors les prix de produits différents. Ce script les liste, sans
rien modifier.

Usage :
  docker compose run --rm -v ./scripts:/app/scripts watcher \
      uv run --no-sync python /app/scripts/auditer_groupes.py
"""

import os
import re
import sys
from itertools import combinations

import psycopg
from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, "/app/src/db")
from fusion_designations import memes_nombres  # noqa: E402

# Meme strip de boilerplate que normaliser() : "suivant définition du prix
# N° B52: Bordure 12" et "Bordure 12" sont le meme produit - le numero de
# prix (52) est du bruit de formulaire, pas une spec. Sans ce strip, l'audit
# compte comme suspects des fusions parfaitement legitimes.
_BOILERPLATE = [
    re.compile(r"^(suivant|selon)\s+d[ée]finition\s+du\s+prix.*?:\s*", re.IGNORECASE),
    re.compile(r"^ce prix r[ée]mun[èe]re\s*:?\s*", re.IGNORECASE),
]


def _strip(d: str) -> str:
    for rx in _BOILERPLATE:
        d = rx.sub("", d)
    return d


def main():
    conn = psycopg.connect(os.environ["PGURL"], autocommit=True)
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT designation_canonique, sous_famille_canonique, unite_canonique,
                   array_agg(DISTINCT designation)
            FROM price_lines
            WHERE designation_canonique IS NOT NULL
            GROUP BY designation_canonique, sous_famille_canonique, unite_canonique
            HAVING count(DISTINCT designation) > 1
            """
        )
        groupes = cur.fetchall()

    print(f"{len(groupes)} groupes multi-membres a auditer")
    suspects = 0
    for canonique, sf, unite, membres in groupes:
        # un groupe est suspect des qu'UNE paire de membres viole memes_nombres
        # (nombres differents, ou sens de comparaison opposes)
        paires_ko = [
            (a, b) for a, b in combinations(membres, 2) if not memes_nombres(_strip(a), _strip(b))
        ]
        if paires_ko:
            suspects += 1
            print(f"\n### [{unite or '?'}] {canonique!r} (sf={sf or '-'}, {len(membres)} membres)")
            for a, b in paires_ko[:4]:
                print(f"    KO: {a[:100]!r}")
                print(f"     vs {b[:100]!r}")

    print(f"\n=> {suspects} groupe(s) suspect(s) sur {len(groupes)}")


if __name__ == "__main__":
    main()
