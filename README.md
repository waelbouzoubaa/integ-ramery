# integ-ramery2 — base de prix unitaires DQE/BPU

Extraction des bordereaux de prix PDF vers PostgreSQL, pour donner aux
métiers une estimation de prix moyen par produit.

## Workflow

```
SharePoint (scan) -> Extraction IA (PDF natif + JSON force) -> Nettoyage technique
   -> PostgreSQL (source de verite) -> Streamlit -> puis text-to-SQL (OpenWebUI)
```

## État

- [x] Watcher SharePoint (détection + téléchargement des PDF)
- [ ] Extraction IA (Gemini, PDF natif + structured output)
- [ ] Nettoyage / normalisation
- [ ] Schéma PostgreSQL + chargement
- [ ] Restitution (Streamlit puis text-to-SQL)

## Watcher SharePoint

```bash
uv sync
cp .env.example .env   # remplir TENANT_ID / CLIENT_ID / CLIENT_SECRET / SHAREPOINT_HOST / SHAREPOINT_SITE_PATH
cd src/watcher
uv run python pdf_watcher.py
```

Détecte les PDF ajoutés/modifiés dans le dossier SharePoint configuré
(`SHAREPOINT_FOLDER`) via une requête delta Microsoft Graph, et les
télécharge dans `data/incoming/`. État de polling conservé dans
`data/state/` (delta token + cache des fichiers vus).

Règle d'extraction retenue (voir conversation) : ne garder que les lignes
avec un prix réel ; `chapitre` = racine, `sous_famille` = dernier header
sans prix rencontré (réinitialisé dès qu'on quitte ses enfants).
