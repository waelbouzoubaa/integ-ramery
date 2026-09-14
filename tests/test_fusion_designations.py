"""Tests unitaires des fonctions deterministes de fusion_designations.py -
aucune base, aucun appel Gemini : purs calculs sur du texte/des nombres.
Chaque cas ici correspond a un bug REEL trouve en base au fil du projet
(voir les commentaires du code source et la memoire du projet) - le but
n'est pas la couverture exhaustive, c'est de ne plus jamais regresser sur
un cas deja identifie."""

from fusion_designations import (
    choisir_canonique,
    memes_nombres,
    normaliser,
    _sens_comparaison,
)


class TestNormaliser:
    def test_accents_et_casse(self):
        assert normaliser("Déviation") == normaliser("deviation")

    def test_ponctuation_devient_espace(self):
        assert normaliser("Bordure T2.") == normaliser("Bordure T2")

    def test_espaces_multiples_reduits(self):
        assert normaliser("Bordure   T2") == normaliser("Bordure T2")

    def test_boilerplate_suivant_definition_du_prix_retire(self):
        # Bug reel : "suivant definition du prix N B52: Bordure T2" et
        # "Bordure T2" fusionnaient a tort en 2 groupes AVANT ce fix.
        assert normaliser("suivant définition du prix N° B52: Bordure T2") == normaliser("Bordure T2")

    def test_boilerplate_ce_prix_remunere_retire(self):
        assert normaliser("Ce prix rémunère : Terrassement en déblais") == normaliser("Terrassement en déblais")

    def test_inferieur_superieur_ne_deviennent_pas_identiques(self):
        # LE bug qui a motive ce fix : pg_trgm traite "<" et ">" comme du
        # bruit et les ignore - sans la conversion en mots, ces deux
        # designations OPPOSEES devenaient un texte strictement identique.
        assert normaliser("Fond de forme largeur > 2,50 m") != normaliser("Fond de forme largeur < 2,50 m")

    def test_homoglyphes_cyrilliques_normalises(self):
        # Gemini hallucine parfois des lettres cyrilliques visuellement
        # identiques au latin sur le meme mot BTP (ex: BBME).
        assert normaliser("ВВМЕ") == normaliser("bbme")


class TestSensComparaison:
    def test_signe_inferieur(self):
        assert _sens_comparaison("Largeur < 2,50m") == "inf"

    def test_mot_inferieur(self):
        assert _sens_comparaison("Largeur inférieure à 2,50m") == "inf"

    def test_signe_superieur(self):
        assert _sens_comparaison("Largeur > 2,50m") == "sup"

    def test_aucune_comparaison(self):
        assert _sens_comparaison("Bordure T2") is None

    def test_plage_les_deux_sens_a_la_fois_rend_indetermine(self):
        # Une plage exprimee avec les DEUX symboles ("< 200mm et > 100mm")
        # contient les deux sens a la fois - volontairement laisse a Gemini
        # de trancher, pas un cas bloquant. Attention : "20 < x < 50" (le
        # meme symbole repete deux fois) ne compte PAS comme "les deux sens"
        # pour cette fonction - elle detecte la PRESENCE d'un symbole/mot,
        # pas le nombre d'occurrences.
        assert _sens_comparaison("diamètre < 200mm et > 100mm") is None


class TestMemesNombres:
    def test_memes_nombres_identiques(self):
        assert memes_nombres("Canalisation PVC CR8 Ø160", "Fourniture et pose de tuyaux PVC Ø160 CR8") is True

    def test_diametres_differents_rejetes(self):
        # Bug de fond corrige (fix "filtre nombres") : un candidat au bon
        # diametre ne doit jamais etre ecarte au profit d'un candidat au
        # texte proche mais au MAUVAIS diametre.
        assert memes_nombres("Canalisation PVC CR8 Ø160", "Canalisation PVC CR8 Ø250") is False

    def test_sens_oppose_meme_nombre_rejete(self):
        # Meme nombre (2,50) mais sens de comparaison oppose = produit
        # different (ex: largeur minimale vs maximale).
        assert memes_nombres("Largeur < 2,50m", "Largeur > 2,50m") is False

    def test_meme_sens_meme_nombre_accepte(self):
        assert memes_nombres("Largeur < 2,50m", "Largeur inférieure à 2,50m") is True

    def test_aucun_nombre_des_deux_cotes_accepte(self):
        assert memes_nombres("Installation de chantier", "Installations de chantier") is True


class TestChoisirCanonique:
    def test_plus_frequent_gagne(self):
        membres = [
            ("sf", "u", "Bordure T2"),
            ("sf", "u", "Bordure T2."),
        ]
        occurrences = {("sf", "u", "Bordure T2"): 10, ("sf", "u", "Bordure T2."): 1}
        assert choisir_canonique(membres, occurrences) == "Bordure T2"

    def test_egalite_de_frequence_le_plus_court_gagne(self):
        membres = [
            ("sf", "u", "Installation de chantier"),
            ("sf", "u", "Installation de chantier VRD"),
        ]
        occurrences = {
            ("sf", "u", "Installation de chantier"): 5,
            ("sf", "u", "Installation de chantier VRD"): 5,
        }
        assert choisir_canonique(membres, occurrences) == "Installation de chantier"
