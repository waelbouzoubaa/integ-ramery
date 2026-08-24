import base64
from pathlib import Path

import streamlit as st

st.set_page_config(page_title="Prix unitaires DQE", page_icon="📊", layout="wide")

# Charte graphique Ramery (docs/Charte graphique 2023.pdf).
#
# On utilise st.navigation() (liste de pages explicite) plutot que le
# dossier pages/ auto-detecte par Streamlit : c'est le seul moyen d'afficher
# du contenu personnalise (le logo, ici) AU-DESSUS de la navigation dans la
# sidebar - avec le dossier pages/ automatique, tout ce qu'on ajoute
# nous-memes s'affiche forcement APRES la navigation generee par Streamlit.
#
# Le logo est integre en base64 dans une div HTML (comme le fait le projet
# middleware-ramery, streamlit_review/app.py) plutot que via st.logo() :
# st.logo() garantit bien le placement en haut, mais plafonne la hauteur de
# l'image a 32px max (deja teste : trop petit, et forcer une taille plus
# grande en CSS la fait deborder de son conteneur et disparaitre).
LOGO_PATH = Path(__file__).resolve().parent / "assets" / "logo.png"


def _logo_data_uri() -> str:
    try:
        data = base64.b64encode(LOGO_PATH.read_bytes()).decode()
        return f"data:image/png;base64,{data}"
    except OSError:
        return ""


st.markdown(
    """
    <style>
    [data-testid="stSidebar"] { background-color: #003D7C; }
    [data-testid="stSidebar"] * { color: #FFFFFF !important; }

    /* Le logo (ajoute via st.sidebar.markdown, testid stSidebarUserContent)
       s'affichait quand meme APRES la navigation (testid stSidebarNav) :
       Streamlit reserve un emplacement fixe pour la nav dans la sidebar,
       peu importe l'ordre des appels dans le script. On force l'ordre
       visuel en CSS plutot que par l'ordre du code. La fleche pour
       reduire/agrandir la sidebar (stSidebarCollapseButton) est un 3e
       element du meme conteneur - reste tout en haut, au-dessus du logo. */
    [data-testid="stSidebar"] > div:first-child {
        display: flex;
        flex-direction: column;
    }
    [data-testid="stSidebarCollapseButton"] { order: -2; }
    [data-testid="stSidebarUserContent"] { order: -1; }
    [data-testid="stSidebarNav"] { order: 2; }

    .sidebar-header {
        display: flex; justify-content: center; align-items: center;
        padding: 14px 0 18px; margin-bottom: 6px;
        border-bottom: 1px solid rgba(255,255,255,.25);
    }
    .sidebar-header img {
        border-radius: 50%; border: 2px solid rgba(255,255,255,.4);
        height: 70px; width: 70px; object-fit: cover;
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
        f'<div class="sidebar-header"><img src="{_logo}" alt="Ramery"/></div>',
        unsafe_allow_html=True,
    )

pages = [
    st.Page("vues/prix_unitaires.py", title="Prix unitaires", icon="📊", default=True),
    st.Page("vues/revue_des_groupes.py", title="Revue des groupes", icon="🔍"),
    st.Page("vues/recherche_de_prix.py", title="Recherche de prix", icon="🔎"),
]
st.navigation(pages).run()
