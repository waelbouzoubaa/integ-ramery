"""Pipeline de rapprochement de prix pour un bordereau vierge - extraction,
preselection SQL, arbitrage Gemini. Partage entre le watcher (traitement en
arriere-plan d'une demande deposee dans recherche_prix_jobs, voir
pdf_watcher.py) et scripts/mesurer_rapprochement.py : une seule copie de ce
pipeline, jamais deux (toute divergence entre deux copies fausserait les
mesures - piege deja documente dans CLAUDE.md avant ce refactor).

Volontairement sans aucune dependance a Streamlit : on_etape (optionnel) est
le seul point d'extension pour reporter une progression, laissant a
l'appelant le choix du mecanisme (colonne `etape` d'un job en base, simple
print(), etc.).
"""
import sys
from pathlib import Path

import pandas as pd
from google.genai import types
from pydantic import BaseModel

EXTRACTION_DIR = Path(__file__).resolve().parent.parent / "extraction"
if str(EXTRACTION_DIR) not in sys.path:
    sys.path.insert(0, str(EXTRACTION_DIR))

from gemini_extract import extract_pdf_sans_prix  # noqa: E402
from fusion_designations import (  # noqa: E402
    MODEL,
    _get_client,
    _retry_surcharge,
    _sens_comparaison,
    memes_nombres,
    parse_reponse,
)

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

Nuance importante : un candidat plus DÉTAILLÉ que la ligne du bordereau,
mais sans contradiction avec elle, décrit le MÊME produit — accepte-le.
Exemples réels : "Tête d'aqueduc de sécurité Ø < 400 mm" pour "Tête
d'aqueduc pour Ø < 400 mm" ; "peinture thermoplastique blanche" pour "en
peinture" ; "Grave Non Traitée type B (GNT 2) 0/31.5" pour "GNT 2 (GNT B)
0/31.5" ; "About fonte type T" pour "About en fonte de type T ou A".
Rejette uniquement en cas de contradiction réelle : matériau différent
(fonte vs PVC, peinture vs résine, béton vs résine), classe ou type
différent (CC2 vs CS2, simple vs double gravillonnage), fourni vs
réutilisé, opération différente (dépose vs fourniture, démolition vs
réfection), ou prestation supplémentaire qui change le prix (ex : "y
compris tranchée" quand le bordereau ne la demande pas).

Réponds pour CHAQUE id reçu, dans le même ordre : l'index (0-based) du bon
candidat dans SA liste, ou -1 si aucun candidat ne correspond."""


class ChoixRapprochement(BaseModel):
    id: int
    index_candidat: int  # -1 si aucun candidat ne correspond


class ChoixRapprochements(BaseModel):
    choix: list[ChoixRapprochement]


@_retry_surcharge
def _arbitrer_par_lot(lot: list[tuple]) -> dict[int, int]:
    """lot = liste de (id, designation_bordereau, [designations_candidates]).
    Renvoie {id: index_candidat_choisi (-1 si aucun)}.
    _retry_surcharge (reessai sur 503/coupure reseau) : meme protection que la
    fusion - crash reel constate sur un 503 "high demand" en pleine recherche."""
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
    result = parse_reponse(response, ChoixRapprochements)
    return {c.id: c.index_candidat for c in result.choix}


def _depuis_pandas(valeur):
    """pandas represente un NULL de colonne texte par NaN (float) via
    pd.read_sql, meme quand la colonne reste text - un NaN non converti
    fait planter la serialisation JSON du resultat (json.dumps produit le
    token invalide NaN, rejete par Postgres a l'ecriture dans la colonne
    jsonb : "invalid input syntax for type json... Token NaN is invalid",
    bug reel constate sur sous_famille). A appeler sur toute valeur lue
    d'un DataFrame avant de l'inserer dans le resultat final."""
    return None if pd.isna(valeur) else valeur


def _preselectionner(conn, designation: str, unite: str | None, agence: str | None) -> pd.DataFrame:
    """Top 5 candidats pour une designation de bordereau : egalite stricte des
    nombres d'abord (fonction SQL nombres_texte, AVANT le LIMIT - voir
    schema.sql pour pourquoi l'ordre compte), puis repechage par inclusion
    (nombres_tab, operateur <@) si rien ne matche strictement - cas des
    bornes de quantite/montant absentes des designations en base ("Grave
    0/31.5 recyclee (<250T)")."""
    candidats = pd.read_sql(
        """
        SELECT designation, sous_famille, unite, prix_moyen_corrige, nb_occurrences,
               similarity(normaliser_recherche(designation), normaliser_recherche(%s)) AS score
        FROM prix_moyen_par_designation_fn(%s)
        WHERE unite IS NOT DISTINCT FROM normaliser_unite(%s)
          AND nombres_texte(designation) = nombres_texte(%s)
        ORDER BY score DESC
        LIMIT 20
        """,
        conn, params=(designation, agence, unite, designation),
    )
    # Sur un resultat vide, pd.read_sql + psycopg renvoie un DataFrame sans
    # colonnes -> sort_values("score") leverait un KeyError.
    if not candidats.empty:
        candidats = candidats[candidats["designation"].apply(lambda d: memes_nombres(designation, d))]
        candidats = candidats.sort_values("score", ascending=False).head(5)

    if candidats.empty:
        candidats = pd.read_sql(
            """
            SELECT designation, sous_famille, unite, prix_moyen_corrige, nb_occurrences,
                   similarity(normaliser_recherche(designation), normaliser_recherche(%s)) AS score
            FROM prix_moyen_par_designation_fn(%s)
            WHERE unite IS NOT DISTINCT FROM normaliser_unite(%s)
              AND nombres_tab(designation) <@ nombres_tab(%s)
            ORDER BY score DESC
            LIMIT 5
            """,
            conn, params=(designation, agence, unite, designation),
        )
        if not candidats.empty:
            sens_bordereau = _sens_comparaison(designation)
            candidats = candidats[candidats["designation"].apply(
                lambda d: sens_bordereau is None
                or _sens_comparaison(d) is None
                or _sens_comparaison(d) == sens_bordereau
            )]

    return candidats.reset_index(drop=True)


def extraire(file_bytes: bytes, filename: str):
    """Extraction Gemini seule (designations+unites, sans prix) - isolee du
    reste du pipeline pour permettre de rejouer le rapprochement sur une
    extraction deja faite sans repayer un appel Gemini (voir
    scripts/mesurer_rapprochement.py, qui mesure des runs comparables grace
    a un cache de ce resultat sur disque)."""
    return extract_pdf_sans_prix(file_bytes, filename=filename)


def rapprocher(conn, resultat, agence: str | None, on_etape=None) -> list[dict]:
    """Preselection + arbitrage Gemini pour une extraction DEJA FAITE (voir
    extraire ci-dessus) -> assemblage du resultat final (liste de dict
    JSON-serialisables, consommee telle quelle par recherche_de_prix.py et
    mesurer_rapprochement.py).

    on_etape(str), si fourni, est appele a chaque changement de phase
    ('rapprochement', 'validation')."""
    if on_etape:
        on_etape("rapprochement")
    candidats_par_item = [
        _preselectionner(conn, item.designation, item.unite, agence)
        for item in resultat.items
    ]

    # Score de similarite = 1.0 (texte identique une fois normalise) ->
    # accepte directement, pas la peine de deranger Gemini pour confirmer une
    # evidence. Gemini n'arbitre que les cas ou le texte n'est PAS
    # parfaitement identique mais qu'il reste au moins un candidat pre-filtre.
    choix_gemini: dict[int, int] = {}
    a_arbitrer = []
    for i, candidats in enumerate(candidats_par_item):
        if candidats.empty:
            continue
        if candidats.iloc[0]["score"] >= 0.999:
            choix_gemini[i] = 0
        else:
            a_arbitrer.append((i, resultat.items[i].designation, candidats["designation"].tolist()))

    if a_arbitrer:
        if on_etape:
            on_etape("validation")
        for i in range(0, len(a_arbitrer), TAILLE_LOT):
            lot = a_arbitrer[i:i + TAILLE_LOT]
            choix_gemini.update(_arbitrer_par_lot(lot))

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
                "sous_famille_trouvee": _depuis_pandas(r["sous_famille"]),
                "prix_moyen_corrige": float(r["prix_moyen_corrige"]),
                "nb_occurrences": int(r["nb_occurrences"]),
                "score_correspondance": round(float(r["score"]), 2),
                "raison_echec": None,
                "proposition_a_valider": None,
                "prix_proposition": None,
            })
        else:
            if candidats.empty:
                raison = "Aucun candidat présélectionné (texte, unité ou nombres différents)"
            else:
                raison = f"Gemini a rejeté les {len(candidats)} candidat(s) présélectionné(s)"
            # Sur un rejet, on garde le MEILLEUR candidat et son prix comme
            # PROPOSITION a valider par un humain - jamais comptee comme
            # rapprochee, jamais choisie automatiquement.
            meilleur = candidats.iloc[0] if not candidats.empty else None
            lignes_resultat.append({
                "designation_bordereau": item.designation,
                "unite": item.unite,
                "designation_trouvee": None,
                "sous_famille_trouvee": None,
                "prix_moyen_corrige": None,
                "nb_occurrences": None,
                "score_correspondance": None,
                "raison_echec": raison,
                "proposition_a_valider": None if meilleur is None else meilleur["designation"],
                "prix_proposition": None if meilleur is None else float(meilleur["prix_moyen_corrige"]),
            })

    return lignes_resultat


def traiter_bordereau(
    conn, file_bytes: bytes, filename: str, agence: str | None, on_etape=None
) -> list[dict]:
    """Raccourci extraire+rapprocher pour l'usage courant (une demande de
    recherche_prix_jobs, traitee par le watcher - voir pdf_watcher.py)."""
    if on_etape:
        on_etape("extraction")
    resultat = extraire(file_bytes, filename)
    return rapprocher(conn, resultat, agence, on_etape=on_etape)
