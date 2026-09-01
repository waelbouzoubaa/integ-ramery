"""Retraite un ou plusieurs fichiers precis par leur chemin SharePoint, sans
toucher au delta token du watcher normal (pdf_watcher.py) - utile apres un
echec ponctuel (troncature Gemini deja corrigee, coupure reseau...) qu'on ne
veut pas attendre un cycle complet de watcher pour reprendre, et sans
re-extraire au passage tous les autres fichiers deja traites ailleurs.

Usage : uv run python retraiter_echecs.py "Sous-dossier/fichier1.pdf" "fichier2.pdf"
Les chemins sont relatifs a SHAREPOINT_FOLDER (voir .env).
"""
import sys
import urllib.parse
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "extraction"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "db"))

from sharepoint_client import get_headers, get_site_id, get_drive_id
from config import SHAREPOINT_FOLDER
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
    noms_fichiers = sys.argv[1:]
    if not noms_fichiers:
        print("Usage : uv run python retraiter_echecs.py \"fichier1.pdf\" \"fichier2.pdf\" ...")
        sys.exit(1)

    site_id = get_site_id()
    drive_id = get_drive_id(site_id)
    conn = get_conn()
    ensure_schema(conn)

    for nom in noms_fichiers:
        chemin_complet = f"{SHAREPOINT_FOLDER}/{nom}" if SHAREPOINT_FOLDER else nom
        print(f"[RETRAITEMENT] {chemin_complet}")
        try:
            file_bytes = _telecharger_par_chemin(drive_id, chemin_complet)
        except Exception as exc:
            print(f"  -> Erreur telechargement : {exc}")
            continue
        handle_pdf(nom, file_bytes, {}, conn, chemin=chemin_complet)

    conn.close()


if __name__ == "__main__":
    main()
