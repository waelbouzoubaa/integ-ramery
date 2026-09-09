-- Ajoute la colonne agence (nom du dossier SharePoint racine, une agence =
-- un dossier a la racine du drive - voir schema.sql et pdf_watcher.py).
-- Idempotent : IF NOT EXISTS. Aucun backfill ici - les documents deja en
-- base recevront leur agence progressivement, au fil des prochains passages
-- du watcher (qui detectera les fichiers deplaces dans les nouveaux dossiers
-- via le delta SharePoint, sans re-extraction grace au cache out/*.json).
ALTER TABLE price_documents ADD COLUMN IF NOT EXISTS agence text;
