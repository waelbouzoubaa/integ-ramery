"""Regroupe les designations quasi-identiques (boilerplate, casse, pluriel,
quasi-synonymes) sous une designation_canonique commune, sans jamais toucher
a la colonne designation d'origine (tracabilite intacte).

L'unite comparee est le triplet (sous_famille, unite, designation), jamais
designation seule :
- sous_famille : deux lignes avec le meme texte mais un sous_famille
  different sont des produits differents (ex: "avec grille fonte 100 mm"
  sous "classe 250" vs "classe 400").
- unite (U/ml/m2/m3...) : le meme texte facture au ml dans un document et
  au m2 dans un autre n'est pas un prix comparable (base de calcul
  differente) - ne jamais fusionner deux unites differentes.

Deux niveaux :
- normalisation exacte (regex) -> fusion automatique, deterministe, zero risque
- similarite trigramme (pg_trgm), a l'interieur d'un meme (sous_famille,
  unite) -> ce n'est qu'un PRE-FILTRE pour limiter le volume ; la decision
  fusionner/pas est ensuite prise par Gemini, pas par un seuil de score
  (l'experience a montre qu'un score eleve ne suffit pas : "classe 2" vs
  "classe 3", "GNT A" vs "GNT B", "ocre" vs "gris" scorent tout aussi haut
  que de vrais doublons).
"""
import os
import re
from collections import defaultdict

import psycopg
from dotenv import load_dotenv
from google import genai
from google.genai import types
from pydantic import BaseModel

load_dotenv()

SEUIL_PRE_FILTRE = 0.5  # en dessous, on ne derange meme pas Gemini
TAILLE_LOT = 40         # paires par appel Gemini

MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-pro")

PROMPT_VALIDATION = """Tu recois des paires de designations de produits issues de bordereaux de
prix BTP (voirie, assainissement, amenagement...). Chaque paire partage deja
la meme sous-famille et la meme unite de mesure. Pour chaque paire, decide
si les deux designations decrivent REELLEMENT LE MEME PRODUIT (leurs prix
doivent etre moyennes ensemble), ou des VARIANTES DIFFERENTES dont les prix
peuvent legitimement differer.

Fusionne (fusionner=true) UNIQUEMENT si la difference est purement
cosmetique : faute de frappe, pluriel/singulier, texte de preambule
("suivant definition du prix N°...", "ce prix remunere"), reformulation qui
dit exactement la meme chose, ou un prix ecrit en toutes lettres qui differe
(la variation de prix n'est pas un signal de produit different, c'est
justement ce qu'on veut moyenner).

Ne fusionne JAMAIS (fusionner=false) si la difference porte sur une
caracteristique qui peut influencer le prix : classe, diametre, epaisseur,
couleur, dimension, lettre de code/grade (A/B/C...), sens d'une comparaison
(> vs <=), variante technique (mono/bi-couche), methode de pose, espece
(pour des vegetaux), etc. Dans le doute, ne fusionne pas.

Reponds pour CHAQUE id recu, dans le meme ordre."""


class Decision(BaseModel):
    id: int
    fusionner: bool


class Decisions(BaseModel):
    decisions: list[Decision]


_client: genai.Client | None = None


def _get_client() -> genai.Client:
    global _client
    if _client is None:
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY manquant dans .env")
        _client = genai.Client(api_key=api_key)
    return _client


def get_conn():
    pgurl = os.getenv("PGURL")
    if not pgurl:
        raise RuntimeError("PGURL manquant dans .env")
    return psycopg.connect(pgurl)


def normaliser(designation: str) -> str:
    d = designation.lower().strip()
    d = re.sub(r"\s+", " ", d)
    d = re.sub(r"^(suivant|selon)\s+d[ée]finition\s+du\s+prix.*?:\s*", "", d)
    d = re.sub(r"^ce prix r[ée]mun[èe]re\s*:?\s*", "", d)
    return d.strip()


_NUM_RE = re.compile(r"\d+")


def memes_nombres(a: str, b: str) -> bool:
    """Pre-filtre avant Gemini : un nombre different (classe, diametre,
    epaisseur...) est quasiment toujours un produit different dans ce type
    de catalogue BTP - inutile de deranger Gemini pour ces cas, verifie
    empiriquement sur 1037 paires (1031 confirmees produits differents)."""
    return set(_NUM_RE.findall(a)) == set(_NUM_RE.findall(b))


class UnionFind:
    def __init__(self, items):
        self.parent = {x: x for x in items}

    def find(self, x):
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[ra] = rb


def choisir_canonique(membres, occurrences):
    # membres = liste de (sous_famille, unite, designation) ; on choisit la
    # designation la plus frequente, la plus courte en cas d'egalite
    return sorted(membres, key=lambda m: (-occurrences[m], len(m[2])))[0][2]


def valider_par_lot(client: genai.Client, lot: list[tuple]) -> list[bool]:
    """lot = liste de (sous_famille, unite, designation_a, designation_b).
    Renvoie une liste de booleens fusionner/pas, dans le meme ordre."""
    lignes = [
        f"{i}. sous_famille={sf or '(aucune)'}, unite={u or '(aucune)'} | A: {da!r}  <->  B: {db!r}"
        for i, (sf, u, da, db) in enumerate(lot)
    ]
    response = client.models.generate_content(
        model=MODEL,
        contents=[PROMPT_VALIDATION, "\n".join(lignes)],
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=Decisions,
        ),
    )
    result = response.parsed if response.parsed is not None else Decisions.model_validate_json(response.text)
    par_id = {d.id: d.fusionner for d in result.decisions}
    return [par_id.get(i, False) for i in range(len(lot))]


def main():
    conn = get_conn()

    with conn.cursor() as cur:
        cur.execute(
            "SELECT sous_famille, unite, designation, count(*) "
            "FROM price_lines GROUP BY sous_famille, unite, designation"
        )
        occurrences = {(sf, u, d): n for sf, u, d, n in cur.fetchall()}

    cles = list(occurrences.keys())  # (sous_famille, unite, designation)
    print(f"{len(cles)} triplets (sous_famille, unite, designation) distincts")

    uf = UnionFind(cles)

    # 1. Regroupement exact apres normalisation, a l'interieur d'un meme
    #    (sous_famille, unite)
    par_forme: dict[tuple, list] = defaultdict(list)
    for sf, u, d in cles:
        par_forme[(sf, u, normaliser(d))].append((sf, u, d))
    for groupe in par_forme.values():
        for cle in groupe[1:]:
            uf.union(groupe[0], cle)
    print(f"{sum(1 for g in par_forme.values() if len(g) > 1)} groupes fusionnes par normalisation exacte")

    # 2. Pre-filtre trigramme (juste pour limiter le volume envoye a Gemini),
    #    jamais entre deux sous_famille ou deux unites differentes
    with conn.cursor() as cur:
        cur.execute("CREATE TEMP TABLE tmp_formes (sous_famille text, unite text, designation text, forme text)")
        cur.executemany(
            "INSERT INTO tmp_formes (sous_famille, unite, designation, forme) VALUES (%s, %s, %s, %s)",
            [(sf, u, d, normaliser(d)) for sf, u, d in cles],
        )
        cur.execute(
            """
            SELECT a.sous_famille, a.unite, a.designation, b.designation
            FROM tmp_formes a
            JOIN tmp_formes b
              ON coalesce(a.sous_famille, '') = coalesce(b.sous_famille, '')
             AND coalesce(a.unite, '') = coalesce(b.unite, '')
             AND a.designation < b.designation
            WHERE similarity(a.forme, b.forme) > %s
            ORDER BY similarity(a.forme, b.forme) DESC
            """,
            (SEUIL_PRE_FILTRE,),
        )
        candidats = cur.fetchall()

    # Deja fusionnes par l'etape 1, ou nombres differents (jamais un candidat,
    # pas la peine de deranger Gemini) -> filtres avant l'appel IA
    candidats = [
        (sf, u, da, db) for sf, u, da, db in candidats
        if uf.find((sf, u, da)) != uf.find((sf, u, db)) and memes_nombres(da, db)
    ]
    print(f"{len(candidats)} paires candidates envoyees a Gemini pour validation")

    if candidats:
        client = _get_client()
        for i in range(0, len(candidats), TAILLE_LOT):
            lot = candidats[i:i + TAILLE_LOT]
            decisions = valider_par_lot(client, lot)
            for (sf, u, da, db), fusionner in zip(lot, decisions):
                if fusionner:
                    uf.union((sf, u, da), (sf, u, db))
            print(f"  lot {i // TAILLE_LOT + 1}/{-(-len(candidats) // TAILLE_LOT)} traite")

    # 3. Clusters finaux + choix de la designation canonique
    clusters: dict[tuple, list] = defaultdict(list)
    for cle in cles:
        clusters[uf.find(cle)].append(cle)
    a_fusionner = {root: membres for root, membres in clusters.items() if len(membres) > 1}

    print(f"\n{len(a_fusionner)} groupes de quasi-doublons fusionnes au total")

    with conn.cursor() as cur:
        for membres in a_fusionner.values():
            canon = choisir_canonique(membres, occurrences)
            cur.executemany(
                """
                UPDATE price_lines
                SET designation_canonique = %s
                WHERE designation = %s
                  AND sous_famille IS NOT DISTINCT FROM %s
                  AND unite IS NOT DISTINCT FROM %s
                """,
                [(canon, d, sf, u) for sf, u, d in membres],
            )
    conn.commit()
    conn.close()


if __name__ == "__main__":
    main()
