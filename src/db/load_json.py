import json
import os
from pathlib import Path

import psycopg
from dotenv import load_dotenv

load_dotenv()

OUT_DIR = Path(__file__).resolve().parent.parent.parent / "out"
SCHEMA_FILE = Path(__file__).resolve().parent / "schema.sql"


def get_conn():
    pgurl = os.getenv("PGURL")
    if not pgurl:
        raise RuntimeError("PGURL manquant dans .env")
    return psycopg.connect(pgurl)


def ensure_schema(conn):
    conn.execute(SCHEMA_FILE.read_text(encoding="utf-8"))
    conn.commit()


def load_items(conn, filename: str, items: list[dict]) -> int:
    """Insere un document (par nom de fichier, sans extension) + ses lignes.
    Idempotent : repart de zero pour ce document a chaque appel, donc
    rejouable sans creer de doublons."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO price_documents (filename)
            VALUES (%s)
            ON CONFLICT (filename) DO UPDATE SET filename = EXCLUDED.filename
            RETURNING id
            """,
            (filename,),
        )
        document_id = cur.fetchone()[0]

        cur.execute("DELETE FROM price_lines WHERE document_id = %s", (document_id,))

        for item in items:
            cur.execute(
                """
                INSERT INTO price_lines
                    (document_id, numero, chapitre, sous_famille, designation,
                     unite, quantite, prix_unitaire, montant_ht)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    document_id,
                    item.get("numero"),
                    item["chapitre"],
                    item.get("sous_famille"),
                    item["designation"],
                    item.get("unite"),
                    item.get("quantite"),
                    item["prix_unitaire"],
                    item.get("montant_ht"),
                ),
            )
    conn.commit()
    return len(items)


def load_file(conn, path: Path) -> int:
    data = json.loads(path.read_text(encoding="utf-8"))
    n = load_items(conn, path.stem, data["items"])
    print(f"  -> {path.name} : {n} lignes chargees")
    return n


def main():
    conn = get_conn()
    ensure_schema(conn)

    json_files = sorted(OUT_DIR.glob("*.json"))
    if not json_files:
        print(f"Aucun fichier JSON dans {OUT_DIR}")
        return

    total_lignes = 0
    for path in json_files:
        total_lignes += load_file(conn, path)

    print(f"\n{len(json_files)} document(s), {total_lignes} lignes au total.")
    conn.close()


if __name__ == "__main__":
    main()
