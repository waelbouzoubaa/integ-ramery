import sys
import json
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "extraction"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "db"))

from sharepoint_client import get_headers, get_site_id, get_drive_id
from config import POLL_INTERVAL, SHAREPOINT_FOLDER
from gemini_extract import extract_pdf
from load_json import get_conn, ensure_schema, load_items

sys.stdout.reconfigure(encoding="utf-8")

GRAPH_URL = "https://graph.microsoft.com/v1.0"
STATE_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "state"
OUT_DIR = Path(__file__).resolve().parent.parent.parent / "out"
DELTA_TOKEN_FILE = STATE_DIR / "delta_token.json"
FILE_CACHE_FILE = STATE_DIR / "file_cache.json"


def _folder_name_from_item(item) -> str:
    """Nom du dossier SharePoint parent d'un item Graph (delta)."""
    parent_path = item.get("parentReference", {}).get("path", "")
    if "root:" in parent_path:
        return parent_path.split("root:")[-1].strip("/").split("/")[-1]
    return parent_path.strip("/").split("/")[-1]


def load_state(drive_id):
    delta_link = None
    file_cache = {}
    if DELTA_TOKEN_FILE.exists():
        data = json.loads(DELTA_TOKEN_FILE.read_text())
        delta_link = data.get(drive_id)
    if FILE_CACHE_FILE.exists():
        file_cache = json.loads(FILE_CACHE_FILE.read_text())
    return delta_link, file_cache


def save_state(drive_id, delta_link, file_cache):
    data = {}
    if DELTA_TOKEN_FILE.exists():
        data = json.loads(DELTA_TOKEN_FILE.read_text())
    data[drive_id] = delta_link
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    DELTA_TOKEN_FILE.write_text(json.dumps(data, indent=2))
    FILE_CACHE_FILE.write_text(json.dumps(file_cache, indent=2))


def fetch_delta(url):
    items = []
    next_link = url
    delta_link = None
    while next_link:
        resp = requests.get(next_link, headers=get_headers())
        resp.raise_for_status()
        body = resp.json()
        items.extend(body.get("value", []))
        next_link = body.get("@odata.nextLink")
        delta_link = body.get("@odata.deltaLink")
    return items, delta_link


def _download_file(item) -> bytes:
    """Telecharge via l'endpoint Graph authentifie (token frais a chaque
    appel). On evite volontairement l'URL pre-signee @microsoft.graph.downloadUrl
    de la reponse delta : sur un batch sequentiel de plusieurs minutes, elle a
    le temps d'expirer avant d'arriver aux derniers fichiers (404 constate)."""
    item_id = item["id"]
    drive_id = item.get("parentReference", {}).get("driveId")

    if drive_id:
        resp = requests.get(
            f"{GRAPH_URL}/drives/{drive_id}/items/{item_id}/content",
            headers=get_headers(),
            allow_redirects=True,
            timeout=60,
        )
        resp.raise_for_status()
        return resp.content

    # Repli si driveId absent (ne devrait pas arriver sur une reponse delta)
    download_url = item["@microsoft.graph.downloadUrl"]
    resp = requests.get(download_url, timeout=60)
    resp.raise_for_status()
    return resp.content


def handle_pdf(name: str, file_bytes: bytes, item: dict, conn) -> None:
    """Recoit les octets telecharges directement depuis SharePoint (rien
    n'est ecrit sur disque avant extraction), lance l'extraction Gemini,
    dump en JSON (cache/audit) puis charge en Postgres. Ne doit jamais
    lever d'exception : un fichier en echec ne doit pas interrompre le
    traitement des suivants."""
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    dest = OUT_DIR / f"{Path(name).stem}.json"

    if dest.exists():
        print(f"  -> Deja extrait ({dest.name}), chargement Postgres...")
        try:
            data = json.loads(dest.read_text(encoding="utf-8"))
            n = load_items(conn, Path(name).stem, data["items"])
            print(f"  -> {n} lignes chargees en base")
        except Exception as exc:
            print(f"  -> Erreur chargement Postgres : {exc!r}")
        return

    print(f"  -> Extraction en cours : {name} ({len(file_bytes)} octets)")
    try:
        result = extract_pdf(file_bytes, filename=name)
        print(f"  -> {len(result.items)} lignes de prix extraites")
        dest.write_text(result.model_dump_json(indent=2), encoding="utf-8")
        print(f"  -> Dump : {dest}")
        n = load_items(conn, Path(name).stem, result.model_dump()["items"])
        print(f"  -> {n} lignes chargees en base")
    except Exception as exc:
        print(f"  -> Erreur extraction : {exc!r}")


def process_changes(items, file_cache, conn):
    for item in items:
        item_id = item["id"]

        if "folder" in item:
            if "deleted" not in item:
                file_cache[item_id] = {"name": item["name"], "path": ""}
            continue

        if "deleted" in item:
            cached = file_cache.pop(item_id, {})
            label = cached.get("path") or cached.get("name") or item_id
            print(f"[SUPPRIME]  {label}")
            continue

        name = item.get("name", "inconnu")
        folder = _folder_name_from_item(item)
        full_path = f"{folder}/{name}" if folder else name
        file_cache[item_id] = {"name": name, "path": full_path}

        if not name.lower().endswith(".pdf"):
            continue

        if SHAREPOINT_FOLDER and folder.lower() != SHAREPOINT_FOLDER.lower():
            continue

        is_new = item.get("lastModifiedDateTime") == item.get("createdDateTime")
        tag = "AJOUTE" if is_new else "MODIFIE"
        print(f"[{tag}]    {full_path}")

        try:
            file_bytes = _download_file(item)
        except Exception as exc:
            print(f"  -> Erreur telechargement : {exc}")
            continue

        handle_pdf(name, file_bytes, item, conn)


def run(once: bool = False):
    print("Watcher SharePoint (PDF) demarre")
    site_id = get_site_id()
    drive_id = get_drive_id(site_id)
    print(f"Drive ID : {drive_id}")

    conn = get_conn()
    ensure_schema(conn)

    delta_link, file_cache = load_state(drive_id)
    if not delta_link:
        print("Premier scan complet...")
        delta_link = f"{GRAPH_URL}/drives/{drive_id}/root/delta"

    while True:
        print("\nPolling delta...")
        items, new_delta_link = fetch_delta(delta_link)

        fichiers = [i for i in items if "folder" not in i]
        if fichiers:
            print(f"{len(fichiers)} changement(s) detecte(s)")
        else:
            print("Aucun changement.")
        process_changes(items, file_cache, conn)

        save_state(drive_id, new_delta_link, file_cache)
        delta_link = new_delta_link

        if once:
            print("\nCycle unique termine.")
            return

        interval_str = f"{POLL_INTERVAL}s" if POLL_INTERVAL < 60 else f"{POLL_INTERVAL // 60} min"
        print(f"Prochain check dans {interval_str}...")
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    run(once="--once" in sys.argv)
