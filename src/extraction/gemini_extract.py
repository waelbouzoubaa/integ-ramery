import os

from dotenv import load_dotenv
from google import genai
from google.genai import types

from prompt import PROMPT_EXTRACTION
from schema import ExtractionResult

load_dotenv()

MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-pro")

_client: genai.Client | None = None


def _get_client() -> genai.Client:
    global _client
    if _client is None:
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY manquant dans .env")
        _client = genai.Client(api_key=api_key)
    return _client


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
        ),
    )

    if response.parsed is not None:
        return response.parsed
    # Filet de securite si le parsing automatique echoue
    return ExtractionResult.model_validate_json(response.text)


if __name__ == "__main__":
    import sys

    path = sys.argv[1]
    with open(path, "rb") as f:
        data = f.read()
    result = extract_pdf(data, filename=path)
    print(f"{len(result.items)} lignes extraites")
    for item in result.items[:5]:
        print(item)
