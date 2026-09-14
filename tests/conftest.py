import sys
from pathlib import Path

# Meme mecanisme d'import que le code de production (voir pdf_watcher.py,
# recherche_de_prix.py...) : les modules de src/db et src/watcher ne sont pas
# un package installable, ils s'importent via sys.path.insert. On reproduit
# exactement ca ici plutot que d'inventer un mecanisme de test different de
# ce qui tourne reellement en production.
SRC = Path(__file__).resolve().parent.parent / "src"
for sous_dossier in ("db", "watcher", "extraction"):
    chemin = str(SRC / sous_dossier)
    if chemin not in sys.path:
        sys.path.insert(0, chemin)
