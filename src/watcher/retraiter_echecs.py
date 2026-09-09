"""Retraite un ou plusieurs fichiers precis par leur chemin SharePoint, sans
toucher au delta token du watcher normal (pdf_watcher.py) - utile apres un
echec ponctuel (troncature Gemini deja corrigee, coupure reseau...) qu'on ne
veut pas attendre un cycle complet de watcher pour reprendre, et sans
re-extraire au passage tous les autres fichiers deja traites ailleurs.

Usage : uv run python retraiter_echecs.py "Paris/fichier1.pdf" "Amiens/sous-dossier/fichier2.pdf"
Les chemins sont relatifs a la RACINE du drive (depuis le passage multi-
agences : chaque dossier de premier niveau, ex. "Paris", est une agence -
inclure ce dossier dans le chemin, il n'y a plus de dossier par defaut).
"""
import sys
import urllib.parse
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "extraction"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "db"))

from sharepoint_client import get_headers, get_site_id, get_drive_id
from load_json import get_conn, ensure_schema
from pdf_watcher import GRAPH_URL, handle_pdf


def _telecharger_par_chemin(drive_id: str, chemin_complet: str) -> bytes:
    chemin_encode = urllib.parse.quote(chemin_complet)
    resp = requests.get(
        f"{GRAPH_URL}/drives/{drive_id}/root:/{chemin_encode}:/content",
        headers=get_headers(),
        allow_redirects=True,
        timeout=60,
    )
    resp.raise_for_status()
    return resp.content


def main():
    chemins = sys.argv[1:]
    if not chemins:
        print("Usage : uv run python retraiter_echecs.py \"Paris/fichier1.pdf\" \"Amiens/fichier2.pdf\" ...")
        sys.exit(1)

    site_id = get_site_id()
    drive_id = get_drive_id(site_id)
    conn = get_conn()
    ensure_schema(conn)

    for chemin_complet in chemins:
        nom = Path(chemin_complet).name
        # 1er segment du chemin = agence (meme convention que le watcher
        # normal, voir _agence_from_item dans pdf_watcher.py).
        agence = chemin_complet.strip("/").split("/")[0] if "/" in chemin_complet.strip("/") else None
        print(f"[RETRAITEMENT] {chemin_complet}  (agence: {agence or '?'})")
        try:
            file_bytes = _telecharger_par_chemin(drive_id, chemin_complet)
        except Exception as exc:
            print(f"  -> Erreur telechargement : {exc}")
            continue
        handle_pdf(nom, file_bytes, {}, conn, chemin=chemin_complet, agence=agence)

    conn.close()


if __name__ == "__main__":
    main()
