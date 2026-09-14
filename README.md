# integ-ramery2 — base de prix unitaires DQE/BPU

Extraction des bordereaux de prix BTP (PDF) vers PostgreSQL, pour donner aux
équipes métier une estimation de prix moyen par produit, une recherche de
prix par IA sur un bordereau vierge, et une détection d'anomalies de prix.

Voir `CLAUDE.md` pour l'architecture détaillée, le modèle de données et les
règles à respecter pour faire évoluer le projet sans rien casser.

## Démarrage local

```bash
cp .env.example .env   # remplir TENANT_ID / CLIENT_ID / CLIENT_SECRET / SHAREPOINT_HOST / SHAREPOINT_SITE_PATH / GEMINI_API_KEY / POSTGRES_*
docker compose up -d
```

- Streamlit : http://localhost:8502
- Le watcher tourne en continu, scanne tout le drive SharePoint configuré
  (chaque dossier de premier niveau à la racine = une agence).

## Pages Streamlit

- **Tableau de bord** : volume de données, taux de regroupement, anomalies,
  journal des fichiers traités par agence.
- **Prix unitaires** : moyenne de prix par désignation, filtrable par
  agence, avec détail ligne par ligne et correction automatique des valeurs
  aberrantes.
- **Revue des groupes** : validation humaine des regroupements de
  désignations quasi-identiques (fusion Gemini + normalisation).
- **Recherche de prix** : upload d'un bordereau vierge, rapprochement
  automatique avec la base de prix existante.

## Développement

- `scripts/mesurer_rapprochement.py` : mesure le taux de rapprochement sur
  un bordereau de test, hors interface.
- `scripts/auditer_groupes.py` : audit mécanique de cohérence des groupes.
- `scripts/analyser_rejets.py` : inspecte pourquoi une ligne a été rejetée
  en Recherche de prix.
- `scripts/deployer_vps.sh` : déploiement complet (pull + build + schéma +
  migrations), à lancer depuis la racine du repo sur le VPS.
