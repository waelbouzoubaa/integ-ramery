"""Tests unitaires de l'extraction du chemin SharePoint - aucun appel reseau,
juste du parsing de la structure retournee par Graph API (delta)."""

from pdf_watcher import _agence_from_item, _folder_name_from_item


def _item(path: str) -> dict:
    return {"parentReference": {"path": path}}


class TestAgenceFromItem:
    def test_dossier_a_plat(self):
        # root:/Paris/fichier.pdf -> agence = Paris
        assert _agence_from_item(_item("/drives/xxx/root:/Paris")) == "Paris"

    def test_dossier_imbrique_sous_une_agence(self):
        # root:/Amiens/2026/fichier.pdf -> agence = Amiens (premier niveau),
        # PAS "2026" (dossier parent immediat) - c'est toute la raison
        # d'etre de cette fonction, distincte de _folder_name_from_item.
        assert _agence_from_item(_item("/drives/xxx/root:/Amiens/2026")) == "Amiens"

    def test_fichier_a_la_racine_du_drive(self):
        # Ne devrait jamais arriver en usage normal, mais ne doit pas planter.
        assert _agence_from_item(_item("/drives/xxx/root:")) is None

    def test_parent_reference_absente(self):
        assert _agence_from_item({}) is None


class TestFolderNameFromItem:
    def test_dossier_parent_immediat(self):
        assert _folder_name_from_item(_item("/drives/xxx/root:/Paris")) == "Paris"

    def test_prend_le_dernier_segment_pas_le_premier(self):
        # Distinct de _agence_from_item : celle-ci veut le dossier parent
        # IMMEDIAT (sert seulement a l'affichage du chemin dans les logs).
        assert _folder_name_from_item(_item("/drives/xxx/root:/Amiens/2026")) == "2026"
