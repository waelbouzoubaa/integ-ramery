import os

import httpx
from dotenv import load_dotenv
from google import genai
from google.genai import types
from google.genai.errors import ServerError
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from prompt import PROMPT_EXTRACTION, PROMPT_EXTRACTION_SANS_PRIX
from schema import ExtractionResult, ExtractionSansPrix

load_dotenv()

# Meme protection que dans fusion_designations : Gemini renvoie regulierement
# des 503 UNAVAILABLE ("high demand") transitoires - sans retry, un pic de
# charge cote Google faisait planter l'extraction entiere (incident reel :
# crash de la page Recherche de prix en pleine utilisation). Le SDK a bien un
# retry interne mais il abandonne trop vite sur les 503 persistants.
# Reponse VIDE (ni .parsed ni .text) retentee au meme titre que le 503 :
# constate en pratique sur la fusion (voir fusion_designations.py) - passer
# response.text (None) a pydantic fait planter tout le traitement.
class ReponseVideError(Exception):
    pass


_retry_surcharge = retry(
    retry=retry_if_exception_type((ServerError, httpx.TransportError, ReponseVideError)),
    wait=wait_exponential(multiplier=2, min=4, max=60),
    stop=stop_after_attempt(6),
    reraise=True,
)


def _parse_reponse(response, schema):
    if response.parsed is not None:
        return response.parsed
    if response.text:
        return schema.model_validate_json(response.text)
    finish = "?"
    if getattr(response, "candidates", None):
        finish = getattr(response.candidates[0], "finish_reason", "?")
    raise ReponseVideError(f"Reponse Gemini vide (finish_reason={finish})")

MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-pro")
# Plafond de sortie de gemini-2.5-pro (65536) : sans ce reglage explicite, le
# SDK utilise une valeur par defaut plus basse et les gros DQE (une centaine
# de lignes ou plus, 20+ pages) se retrouvent tronques en plein milieu du
# JSON -> ExtractionResult invalide, document entier perdu (voir incident
# "2A-CORBIE... DPGF BASE", 21 pages, EOF a la ligne 4580 de la reponse).
MAX_OUTPUT_TOKENS = 65536
# gemini-2.5-pro consacre par defaut une partie du budget de sortie au
# "thinking" (raisonnement interne) AVANT de generer la reponse - ce budget
# est preleve sur le meme MAX_OUTPUT_TOKENS. Sur "2A-CORBIE" (21 pages),
# augmenter MAX_OUTPUT_TOKENS n'a pas suffi : la troncature est arrivee
# encore PLUS TOT (ligne 4364 au lieu de 4580) car le modele a pense plus
# longtemps sur ce document dense, laissant moins de place au JSON final.
# Ce modele n'accepte pas de desactiver totalement le thinking (0), 128 est
# le minimum autorise - une tache d'extraction/transcription n'a pas besoin
# de raisonnement pousse, mieux vaut maximiser la place pour le JSON.
THINKING_BUDGET = 128
# Extraction structuree (transcription fidele), pas de generation creative :
# temperature=0 minimise la variabilite d'un run a l'autre sur les decisions
# limites (une designation etalee sur 2 lignes fusionnee ou pas, un total
# ambigu inclus ou non...) - incident reel : le meme PDF extrait tantot 173,
# tantot 174 lignes selon le run.
TEMPERATURE = 0

_client: genai.Client | None = None


def _get_client() -> genai.Client:
    global _client
    if _client is None:
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY manquant dans .env")
        _client = genai.Client(api_key=api_key)
    return _client


@_retry_surcharge
def extract_pdf(file_bytes: bytes, filename: str = "") -> ExtractionResult:
    """Envoie le PDF natif a Gemini et recupere les lignes de prix structurees.

    Pas d'OCR ni de pre-extraction texte : le modele lit directement la mise
    en page du PDF (voir prompt.py pour les regles chapitre/sous_famille)."""
    client = _get_client()
    response = client.models.generate_content(
        model=MODEL,
        contents=[
            types.Part.from_bytes(data=file_bytes, mime_type="application/pdf"),
            PROMPT_EXTRACTION,
        ],
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=ExtractionResult,
            max_output_tokens=MAX_OUTPUT_TOKENS,
            thinking_config=types.ThinkingConfig(thinking_budget=THINKING_BUDGET),
            temperature=TEMPERATURE,
        ),
    )

    return _parse_reponse(response, ExtractionResult)


@_retry_surcharge
def extract_pdf_sans_prix(file_bytes: bytes, filename: str = "") -> ExtractionSansPrix:
    """Meme principe que extract_pdf, pour un bordereau VIERGE (designations
    et unites presentes, pas de prix) - voir 2_Recherche_de_prix.py."""
    client = _get_client()
    response = client.models.generate_content(
        model=MODEL,
        contents=[
            types.Part.from_bytes(data=file_bytes, mime_type="application/pdf"),
            PROMPT_EXTRACTION_SANS_PRIX,
        ],
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=ExtractionSansPrix,
            max_output_tokens=MAX_OUTPUT_TOKENS,
            thinking_config=types.ThinkingConfig(thinking_budget=THINKING_BUDGET),
            temperature=TEMPERATURE,
        ),
    )

    return _parse_reponse(response, ExtractionSansPrix)


if __name__ == "__main__":
    import sys

    path = sys.argv[1]
    with open(path, "rb") as f:
        data = f.read()
    result = extract_pdf(data, filename=path)
    print(f"{len(result.items)} lignes extraites")
    for item in result.items[:5]:
        print(item)
