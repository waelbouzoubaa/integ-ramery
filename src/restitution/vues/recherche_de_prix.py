import os
import sys
from pathlib import Path

import pandas as pd
import psycopg
import streamlit as st
from dotenv import load_dotenv

load_dotenv()

# recherche_prix.py (le pipeline extraction+rapprochement+arbitrage) vit
# dans src/db, pas dans src/restitution - mais cette page ne l'appelle plus
# directement : la demande est deposee en base (recherche_prix_jobs) et
# traitee en ARRIERE-PLAN par le watcher (voir pdf_watcher.py). Meme
# principe que la file d'attente du projet file-diff-ramery (lib/db.py) :
# la page affiche TOUTES les demandes (liste chronologique), pas une seule
# suivie via un lien a conserver - fermer/rouvrir la page, ou revenir
# n'importe quand, montre simplement la liste a jour.
DB_DIR = Path(__file__).resolve().parents[2] / "db"
if str(DB_DIR) not in sys.path:
    sys.path.insert(0, str(DB_DIR))

STATUT_LABELS = {
    "en_attente": "⏳ En attente",
    "en_cours": "⚙️ En cours de traitement",
    "termine": "✅ Terminé",
    "erreur": "❌ Erreur",
}

# Bandeau d'etapes (stepper) : visualise ou en est le traitement d'une
# demande EN COURS precisement. Repris a l'identique de l'ancienne version
# synchrone (meme CSS, meme decoupage en 3 etapes) - seul le niveau de
# detail change : avant, mis a jour ligne par ligne pendant le calcul ;
# maintenant, lu depuis la colonne `etape` d'un job traite ailleurs, donc
# par phase grossiere uniquement.
ETAPES = [("extraction", "📄", "Extraction"), ("rapprochement", "🔍", "Rapprochement"), ("validation", "🤖", "Validation Gemini")]

st.markdown(
    """
    <style>
    .stepper-wrap { display: flex; align-items: flex-start; justify-content: center; margin: 0 auto 1rem; max-width: 500px; }
    .step-item { display: flex; flex-direction: column; align-items: center; gap: 4px; min-width: 90px; }
    .step-icon { width: 30px; height: 30px; border-radius: 50%; display: flex; align-items: center; justify-content: center; font-size: .8rem; font-weight: 700; flex-shrink: 0; }
    .step-icon-done    { background: #003D7C; color: #fff; }
    .step-icon-active  { background: #003D7C; color: #fff; box-shadow: 0 0 10px rgba(0,61,124,.45); }
    .step-icon-pending { background: #F0F0F0; color: #9CA3AF; border: 2px solid #D1D5DB; }
    .step-label { font-size: .68rem; font-weight: 600; text-align: center; white-space: nowrap; }
    .step-label-done    { color: #003D7C; }
    .step-label-active  { color: #003D7C; }
    .step-label-pending { color: #9CA3AF; }
    .step-connector { flex: 1; height: 2px; margin-top: 15px; min-width: 20px; }
    .step-connector-done    { background: #003D7C; }
    .step-connector-active  { background: linear-gradient(90deg, #003D7C 0%, #D1D5DB 100%); }
    .step-connector-pending { background: #E2E8F0; }
    </style>
    """,
    unsafe_allow_html=True,
)


def _rendre_stepper(placeholder, etape_active: str | None) -> None:
    """etape_active = cle de l'etape en cours ('extraction', 'rapprochement',
    'validation'), ou None pour tout marquer comme en attente (rien demarre)."""
    cur = -1 if etape_active is None else next(
        (i for i, (cle, _, _) in enumerate(ETAPES) if cle == etape_active), 0
    )
    html = '<div class="stepper-wrap">'
    for i, (_, icone, label) in enumerate(ETAPES):
        if i < cur:
            ic, lc, affichage = "step-icon-done", "step-label-done", "✓"
        elif i == cur:
            ic, lc, affichage = "step-icon-active", "step-label-active", icone
        else:
            ic, lc, affichage = "step-icon-pending", "step-label-pending", icone
        html += f'<div class="step-item"><div class="step-icon {ic}">{affichage}</div><span class="step-label {lc}">{label}</span></div>'
        if i < len(ETAPES) - 1:
            cc = "step-connector-done" if i < cur else ("step-connector-active" if i == cur else "step-connector-pending")
            html += f'<div class="step-connector {cc}"></div>'
    html += "</div>"
    placeholder.markdown(html, unsafe_allow_html=True)


@st.cache_resource
def get_conn():
    pgurl = os.getenv("PGURL")
    if not pgurl:
        st.error("PGURL manquant dans .env")
        st.stop()
    return psycopg.connect(pgurl, autocommit=True)


conn = get_conn()

st.title("Recherche de prix pour un bordereau vierge")
st.caption(
    "Upload un bordereau de prix SANS prix renseignés (DQE/BPU vierge à "
    "compléter, désignations et unités présentes). La demande est traitée "
    "en arrière-plan (extraction, présélection, arbitrage Gemini) — inutile "
    "de garder cette page ouverte, reviens consulter son statut plus tard "
    "dans la liste ci-dessous."
)

# ── Dépôt d'une demande ──────────────────────────────────────────────────
st.subheader("Déposer une demande")

agences_df = pd.read_sql(
    "SELECT DISTINCT agence FROM price_documents WHERE agence IS NOT NULL ORDER BY agence", conn
)
TOUTES_AGENCES = "Toutes les agences"
agence_choisie = st.selectbox(
    "Comparer aux prix de", options=[TOUTES_AGENCES] + agences_df["agence"].tolist()
)
agence_filtre = None if agence_choisie == TOUTES_AGENCES else agence_choisie

label_input = st.text_input("Nom de la demande (optionnel)", placeholder="ex : DQE Lot 3 - révision")
fichier = st.file_uploader("Bordereau vierge (PDF)", type=["pdf"])

if fichier is not None and st.button("🔍 Déposer la demande", type="primary"):
    label = label_input.strip() or Path(fichier.name).stem
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO recherche_prix_jobs (label, fichier_nom, fichier_octets, agence) VALUES (%s, %s, %s, %s)",
            (label, fichier.name, fichier.getvalue(), agence_filtre),
        )
    st.success(f"Demande « {label} » déposée — suis son avancement ci-dessous.")
    st.rerun()

st.divider()

# ── Suivi des demandes ───────────────────────────────────────────────────
col_titre, col_rafraichir = st.columns([5, 1])
with col_titre:
    st.subheader("Suivi des demandes")
with col_rafraichir:
    if st.button("🔄 Rafraîchir", use_container_width=True):
        st.rerun()

demandes = pd.read_sql(
    "SELECT id, label, fichier_nom, agence, statut, etape, resultat, erreur, cree_le "
    "FROM recherche_prix_jobs ORDER BY cree_le DESC",
    conn,
)

if demandes.empty:
    st.info("Aucune demande déposée pour l'instant.")

for _, demande in demandes.iterrows():
    titre = demande["label"] or demande["fichier_nom"]
    meta = f"Déposée le {demande['cree_le'].strftime('%d/%m/%Y à %H:%M')} — {demande['fichier_nom']}"
    if demande["agence"]:
        meta += f" — {demande['agence']}"

    with st.container(border=True):
        col_label, col_statut = st.columns([3, 1])
        with col_label:
            st.markdown(f"**{titre}**")
            st.caption(meta)
        with col_statut:
            st.markdown(f"<div style='text-align:right'>{STATUT_LABELS.get(demande['statut'], demande['statut'])}</div>", unsafe_allow_html=True)

        if demande["statut"] == "erreur":
            st.error(demande["erreur"] or "Erreur inconnue lors du traitement.")

        elif demande["statut"] in ("en_attente", "en_cours"):
            placeholder = st.empty()
            _rendre_stepper(placeholder, demande["etape"] if demande["statut"] == "en_cours" else None)

        elif demande["statut"] == "termine":
            df_resultat = pd.DataFrame(demande["resultat"])
            nb_trouves = int(df_resultat["prix_moyen_corrige"].notna().sum())
            st.write(f"**{nb_trouves} / {len(df_resultat)}** désignations rapprochées avec succès.")

            with st.expander("Voir le détail"):
                if nb_trouves < len(df_resultat):
                    nb_sans_candidat = int(df_resultat["raison_echec"].str.startswith("Aucun candidat", na=False).sum())
                    nb_rejetes_gemini = int(df_resultat["raison_echec"].str.startswith("Gemini a rejeté", na=False).sum())
                    st.caption(
                        f"Sur les {len(df_resultat) - nb_trouves} lignes sans correspondance : "
                        f"{nb_sans_candidat} n'avaient aucun candidat présélectionné (texte/unité/nombres), "
                        f"{nb_rejetes_gemini} avaient des candidats mais Gemini a jugé qu'aucun ne correspondait vraiment. "
                        "Pour chacune, la colonne « Proposition (à valider) » montre le meilleur candidat écarté et son prix."
                    )
                st.dataframe(
                    df_resultat,
                    use_container_width=True,
                    hide_index=True,
                    column_config={
                        "designation_bordereau": "Désignation (bordereau)",
                        "unite": "Unité",
                        "designation_trouvee": "Désignation trouvée (base)",
                        "sous_famille_trouvee": "Sous-famille (base)",
                        "prix_moyen_corrige": st.column_config.NumberColumn("Prix moyen corrigé (€)", format="%.2f"),
                        "nb_occurrences": "Occurrences (base)",
                        "score_correspondance": st.column_config.NumberColumn("Score de similarité", format="%.2f"),
                        "raison_echec": "Raison (si non trouvé)",
                        "proposition_a_valider": "Proposition (à valider)",
                        "prix_proposition": st.column_config.NumberColumn("Prix proposition (€)", format="%.2f"),
                    },
                )

            csv = df_resultat.to_csv(index=False, sep=";", decimal=",").encode("utf-8-sig")
            st.download_button(
                "📥 Télécharger en CSV",
                data=csv,
                file_name=f"prix_{titre}.csv",
                mime="text/csv",
                key=f"csv_{demande['id']}",
            )
