import base64
import runpy
from pathlib import Path

import streamlit as st

st.set_page_config(page_title="Prix unitaires DQE", page_icon="📊", layout="wide")

# Charte graphique Ramery (docs/Charte graphique 2023.pdf), structure de
# sidebar reprise du projet middleware-ramery (streamlit_review/app.py) :
# logo + wordmark en haut, puis menu st.radio() - au lieu de
# st.navigation()/st.Page() qu'on utilisait avant. Avantage : plus besoin de
# forcer l'ordre en CSS (pas de navigation auto-injectee par Streamlit a
# contourner). Le radio garde son apparence normale (pas de surbrillance/
# barre laterale personnalisee) - demande explicite, contrairement au rendu
# stylise de middleware-ramery.
LOGO_PATH = Path(__file__).resolve().parent / "assets" / "logo.png"
VUES_DIR = Path(__file__).resolve().parent / "vues"


def _logo_data_uri() -> str:
    try:
        data = base64.b64encode(LOGO_PATH.read_bytes()).decode()
        return f"data:image/png;base64,{data}"
    except OSError:
        return ""


st.markdown(
    """
    <style>
    .stApp, [data-testid="stAppViewContainer"], [data-testid="stHeader"] { background: #FFFFFF; }

    [data-testid="stSidebar"] { background: linear-gradient(180deg, #003D7C 0%, #00295C 100%); }
    [data-testid="stSidebar"] * { color: #FFFFFF !important; }
    [data-testid="stSidebar"] hr { border-color: rgba(255,255,255,.22); }

    .sidebar-header {
        display: flex; flex-direction: column; align-items: center; gap: 10px;
        padding: 6px 0 18px; margin-bottom: 10px;
        border-bottom: 1px solid rgba(255,255,255,.25);
    }
    .sidebar-header img {
        border-radius: 50%; border: 2px solid rgba(255,255,255,.4);
        height: 60px; width: 60px; object-fit: cover;
    }
    .sidebar-header .wordmark {
        font-size: 16px; font-weight: 700; color: #FFFFFF !important;
        text-align: center; letter-spacing: .3px; line-height: 1.3;
    }

    /* Champs de saisie et listes deroulantes : sans cadre, blanc sur blanc
       se confondait avec le fond de page - bordure bleue + coins arrondis
       pour qu'on les distingue clairement comme des zones cliquables. */
    .stTextInput input {
        border: 1px solid #003D7C !important;
        border-radius: 6px !important;
    }
    [data-testid="stSelectbox"] > div > div,
    [data-testid="stSelectbox"] div[data-baseweb="select"] > div {
        border: 1px solid #003D7C !important;
        border-radius: 6px !important;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

_logo = _logo_data_uri()
if _logo:
    st.sidebar.markdown(
        f"""
        <div class="sidebar-header">
          <img src="{_logo}" alt="Ramery"/>
          <div class="wordmark">Base de prix Ramery</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

PAGES = {
    "📊 Prix unitaires": "prix_unitaires.py",
    "🔍 Revue des groupes": "revue_des_groupes.py",
    "🔎 Recherche de prix": "recherche_de_prix.py",
}

vue = st.sidebar.radio("Vue", list(PAGES.keys()), label_visibility="collapsed")
runpy.run_path(str(VUES_DIR / PAGES[vue]), run_name="__main__")
