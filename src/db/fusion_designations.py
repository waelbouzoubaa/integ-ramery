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


def matcher_contre_groupes_valides(conn) -> int:
    """Phase 0 : les designations pas encore rattachees a un groupe sont
    comparees aux groupes DEJA VALIDES par un humain, chacun avec son propre
    seuil_confiance (colonne groupes.seuil_confiance). Au-dessus du seuil, la
    designation est rattachee automatiquement (designation_canonique) mais
    marquee en_attente=true : elle apparait en Streamlit dans une couleur
    differente tant qu'un humain n'a pas revalide le groupe.

    Un rejet humain anterieur (retrait d'un groupe, enregistre dans
    fusion_decisions avec fusionner=false) est toujours respecte : on ne
    rattache jamais automatiquement ce qu'un humain a explicitement exclu.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT ON (o.sous_famille, o.unite, o.designation)
                   o.sous_famille, o.unite, o.designation, g.designation_canonique
            FROM (
                SELECT DISTINCT sous_famille, unite, designation
                FROM price_lines
                WHERE designation_canonique IS NULL
            ) o
            JOIN groupes g
              ON g.valide = true
             AND o.sous_famille IS NOT DISTINCT FROM g.sous_famille
             AND o.unite IS NOT DISTINCT FROM g.unite
             AND o.designation <> g.designation_canonique
            WHERE similarity(lower(o.designation), lower(g.designation_canonique)) >= g.seuil_confiance
              AND NOT EXISTS (
                  SELECT 1 FROM fusion_decisions fd
                  WHERE fd.sous_famille IS NOT DISTINCT FROM o.sous_famille
                    AND fd.unite IS NOT DISTINCT FROM o.unite
                    AND fd.fusionner = false
                    AND ((fd.designation_a = o.designation AND fd.designation_b = g.designation_canonique)
                      OR (fd.designation_a = g.designation_canonique AND fd.designation_b = o.designation))
              )
            ORDER BY o.sous_famille, o.unite, o.designation,
                     similarity(lower(o.designation), lower(g.designation_canonique)) DESC
            """
        )
        candidats = cur.fetchall()

        nb_ajouts = 0
        for sf, u, d, canon in candidats:
            if not memes_nombres(d, canon):
                continue
            cur.execute(
                """
                UPDATE price_lines
                SET designation_canonique = %s, en_attente = true
                WHERE designation = %s
                  AND sous_famille IS NOT DISTINCT FROM %s
                  AND unite IS NOT DISTINCT FROM %s
                  AND fusion_manuelle = false
                """,
                (canon, d, sf, u),
            )
            nb_ajouts += cur.rowcount
    conn.commit()
    print(f"{nb_ajouts} lignes rattachees automatiquement a un groupe deja valide (en attente de confirmation)")
    return nb_ajouts


def main():
    conn = get_conn()

    matcher_contre_groupes_valides(conn)

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT sous_famille, unite, designation, count(*)
            FROM price_lines pl
            WHERE NOT EXISTS (
                SELECT 1 FROM groupes g
                WHERE g.valide = true
                  AND g.designation_canonique = pl.designation_canonique
                  AND g.sous_famille IS NOT DISTINCT FROM pl.sous_famille
                  AND g.unite IS NOT DISTINCT FROM pl.unite
            )
            GROUP BY sous_famille, unite, designation
            """
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

    # Cache : les paires deja jugees par Gemini lors d'un run precedent ne
    # sont jamais renvoyees a l'IA, meme si le run est relance apres l'ajout
    # de nouveaux documents (sinon on repaye pour re-valider tout l'existant
    # a chaque fois).
    with conn.cursor() as cur:
        cur.execute("SELECT sous_famille, unite, designation_a, designation_b, fusionner FROM fusion_decisions")
        decisions_connues = {(sf, u, da, db): f for sf, u, da, db, f in cur.fetchall()}

    a_juger = []
    for sf, u, da, db in candidats:
        connue = decisions_connues.get((sf, u, da, db))
        if connue is None:
            a_juger.append((sf, u, da, db))
        elif connue:
            uf.union((sf, u, da), (sf, u, db))
    print(f"{len(candidats) - len(a_juger)} paires deja jugees lors d'un run precedent (cache, gratuit)")
    print(f"{len(a_juger)} paires nouvelles envoyees a Gemini pour validation")

    if a_juger:
        client = _get_client()
        nouvelles_decisions = []
        for i in range(0, len(a_juger), TAILLE_LOT):
            lot = a_juger[i:i + TAILLE_LOT]
            decisions = valider_par_lot(client, lot)
            for (sf, u, da, db), fusionner in zip(lot, decisions):
                if fusionner:
                    uf.union((sf, u, da), (sf, u, db))
                nouvelles_decisions.append((sf, u, da, db, fusionner))
            print(f"  lot {i // TAILLE_LOT + 1}/{-(-len(a_juger) // TAILLE_LOT)} traite")

        with conn.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO fusion_decisions (sous_famille, unite, designation_a, designation_b, fusionner)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (coalesce(sous_famille, ''), coalesce(unite, ''), designation_a, designation_b)
                DO UPDATE SET fusionner = EXCLUDED.fusionner, decide_le = now()
                """,
                nouvelles_decisions,
            )
        conn.commit()

    # 3. Clusters finaux + choix de la designation canonique
    clusters: dict[tuple, list] = defaultdict(list)
    for cle in cles:
        clusters[uf.find(cle)].append(cle)
    a_fusionner = {root: membres for root, membres in clusters.items() if len(membres) > 1}

    print(f"\n{len(a_fusionner)} groupes de quasi-doublons fusionnes au total")

    with conn.cursor() as cur:
        for membres in a_fusionner.values():
            canon = choisir_canonique(membres, occurrences)
            sf_groupe, unite_groupe, _ = membres[0]  # meme (sous_famille, unite) pour tous les membres d'un cluster

            # Cree le groupe s'il n'existe pas encore. DO NOTHING (pas
            # d'UPDATE) : si le groupe existe deja et a ete valide par un
            # humain, on ne touche jamais a son statut ici.
            cur.execute(
                """
                INSERT INTO groupes (designation_canonique, sous_famille, unite)
                VALUES (%s, %s, %s)
                ON CONFLICT (designation_canonique, coalesce(sous_famille, ''), coalesce(unite, ''))
                DO NOTHING
                """,
                (canon, sf_groupe, unite_groupe),
            )

            cur.executemany(
                """
                UPDATE price_lines
                SET designation_canonique = %s
                WHERE designation = %s
                  AND sous_famille IS NOT DISTINCT FROM %s
                  AND unite IS NOT DISTINCT FROM %s
                  AND fusion_manuelle = false
                """,
                [(canon, d, sf, u) for sf, u, d in membres],
            )
    conn.commit()
    conn.close()


if __name__ == "__main__":
    main()
