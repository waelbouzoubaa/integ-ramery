from dotenv import load_dotenv
import os

load_dotenv()

TENANT_ID = os.getenv("TENANT_ID")
CLIENT_ID = os.getenv("CLIENT_ID")
CLIENT_SECRET = os.getenv("CLIENT_SECRET")

SHAREPOINT_HOST = os.getenv("SHAREPOINT_HOST")
SHAREPOINT_SITE_PATH = os.getenv("SHAREPOINT_SITE_PATH", "/")
SHAREPOINT_FOLDER = os.getenv("SHAREPOINT_FOLDER", "")

POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "300"))
