import msal
import requests
from config import TENANT_ID, CLIENT_ID, CLIENT_SECRET, SHAREPOINT_HOST, SHAREPOINT_SITE_PATH

GRAPH_URL = "https://graph.microsoft.com/v1.0"


def get_token():
    app = msal.ConfidentialClientApplication(
        CLIENT_ID,
        authority=f"https://login.microsoftonline.com/{TENANT_ID}",
        client_credential=CLIENT_SECRET,
    )
    result = app.acquire_token_for_client(scopes=["https://graph.microsoft.com/.default"])
    if "access_token" not in result:
        raise Exception(f"Erreur auth: {result.get('error_description')}")
    return result["access_token"]


def get_headers():
    return {"Authorization": f"Bearer {get_token()}"}


def get_site_id():
    path = SHAREPOINT_SITE_PATH.strip("/")
    url = f"{GRAPH_URL}/sites/{SHAREPOINT_HOST}:/{path}" if path else f"{GRAPH_URL}/sites/{SHAREPOINT_HOST}:/"
    resp = requests.get(url, headers=get_headers())
    resp.raise_for_status()
    return resp.json()["id"]


def get_drive_id(site_id):
    resp = requests.get(
        f"{GRAPH_URL}/sites/{site_id}/drives",
        headers=get_headers()
    )
    resp.raise_for_status()
    drives = resp.json()["value"]
    return drives[0]["id"]
