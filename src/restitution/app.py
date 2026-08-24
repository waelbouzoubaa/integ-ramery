from pathlib import Path

import streamlit as st

st.set_page_config(page_title="Prix unitaires DQE", page_icon="📊", layout="wide")

# Charte graphique Ramery (docs/Charte graphique 2023.pdf).
#
# st.logo() est utilise (pas de logo en HTML/base64 fait maison) : c'est
# Streamlit qui gere nativement le conteneur "stSidebarHeader" ou vivent
# ENSEMBLE le logo ET la fleche de fermeture de la sidebar
# (stSidebarCollapseButton) - verifie dans le bundle JS. Un logo maison
# vit dans un conteneur DIFFERENT (stSidebarUserContent), donc aucun
# reordonnancement CSS entre les deux n'est fiable (pas le meme parent
# flex) - la fleche se retrouvait mal placee selon les versions.
# Compromis assume : st.logo() plafonne la hauteur a 32px max
# (size="large", le maximum autorise) - plus petit que souhaite, mais la
# fleche reste TOUJOURS correctement positionnee (gere par Streamlit,
# plus par un bricolage CSS).
st.logo(str(Path(__file__).resolve().parent / "assets" / "logo.png"), size="large")

st.markdown(
    """
    <style>
    [data-testid="stSidebar"] { background-color: #003D7C; }
    [data-testid="stSidebar"] * { color: #FFFFFF !important; }

    [data-testid="stSidebarHeader"] {
        display: flex; justify-content: center;
        padding: 10px 0 18px;
        border-bottom: 1px solid rgba(255,255,255,.25);
    }
    [data-testid="stSidebarHeader"] img {
        border-radius: 50%; border: 2px solid rgba(255,255,255,.4);
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

pages = [
    st.Page("vues/prix_unitaires.py", title="Prix unitaires", icon="📊", default=True),
    st.Page("vues/revue_des_groupes.py", title="Revue des groupes", icon="🔍"),
    st.Page("vues/recherche_de_prix.py", title="Recherche de prix", icon="🔎"),
]
st.navigation(pages).run()
