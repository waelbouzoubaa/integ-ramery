from dotenv import load_dotenv
import os

load_dotenv()

TENANT_ID = os.getenv("TENANT_ID")
CLIENT_ID = os.getenv("CLIENT_ID")
CLIENT_SECRET = os.getenv("CLIENT_SECRET")

# Auth par certificat (remplace CLIENT_SECRET si les deux sont renseignés) -
# meme pattern que middleware-ramery (watcher/config.py). CERT_PRIVATE_KEY_PATH
# pointe vers un fichier .key monte dans le conteneur (jamais copie dans
# l'image Docker, jamais commite sur git).
CERT_THUMBPRINT = os.getenv("CERT_THUMBPRINT")
CERT_PRIVATE_KEY_PATH = os.getenv("CERT_PRIVATE_KEY_PATH")

SHAREPOINT_HOST = os.getenv("SHAREPOINT_HOST")
SHAREPOINT_SITE_PATH = os.getenv("SHAREPOINT_SITE_PATH", "/")

# Nom de la BIBLIOTHEQUE DE DOCUMENTS (drive Graph API) a utiliser, pas un
# sous-dossier - un site SharePoint peut heberger plusieurs bibliotheques
# (ex. "Documents" par defaut + "Etudes significatives TP" en onglet separe
# sur le site "Travaux Publics"). Vide = comportement historique (premiere
# bibliotheque renvoyee par l'API, correct quand le site n'en a qu'une).
# Sans valeur ici sur un site a plusieurs bibliotheques, le watcher scanne
# la bibliotheque par defaut ("Documents") au lieu de celle qui contient
# vraiment les bordereaux - incident reel qui a motive cette variable (le
# multi-agences traitait alors une tuile de navigation de "Documents" comme
# une fausse agence).
SHAREPOINT_LIBRARY = os.getenv("SHAREPOINT_LIBRARY", "")

POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "300"))

# Nombre de demandes "Recherche de prix" traitees EN PARALLELE (voir
# _boucle_jobs_recherche_prix dans pdf_watcher.py) - chaque worker est un
# thread independant avec sa propre connexion Postgres, le pickup atomique
# (FOR UPDATE SKIP LOCKED) garantit qu'ils ne traitent jamais deux fois la
# meme demande. Par defaut 2 (prudent) : chaque demande fait plusieurs
# appels Gemini (extraction + arbitrage par lots) - un nombre trop eleve
# pourrait multiplier les 503 "high demande" si le quota API est limite.
# Ajustable sans toucher au code selon le quota reel.
RECHERCHE_PRIX_WORKERS = int(os.getenv("RECHERCHE_PRIX_WORKERS", "2"))
