PROMPT_EXTRACTION = """Tu reçois un bordereau de prix (DQE/BPU) issu d'un marché de travaux BTP (voirie,
assainissement...). Ta tâche : extraire UNIQUEMENT les lignes qui sont de vrais
produits avec un prix unitaire renseigné.

RÈGLES DE SÉLECTION

1. N'extrais QUE les lignes qui ont un prix unitaire numérique explicite.
   - Ignore les titres de chapitre (ex: "A TERRASSEMENTS...", "III - Bordurage...").
   - Ignore les lignes qui n'ont qu'une désignation et une quantité mais AUCUN prix
     (bordereau vierge à compléter par le candidat).
   - Ignore les lignes TOTAL / SOUS-TOTAL / RECAPITULATIF (ce sont des sommes,
     jamais des prix produit).
   - Ignore le texte de mise en page qui réapparaît au milieu du tableau à cause
     des sauts de page (mentions légales, en-tête de colonnes répété, "Aucune
     modification de cette pièce...", etc.) — ce n'est pas une ligne de données.
   - Piège frequent lie aux sauts de page : un titre de regroupement (ex: "J.23
     Fourniture et pose de panneau A3 pour aire de jeux") se retrouve seul en
     bas de page, sans prix visible a cote de lui, et ses enfants prixes (ex:
     "J.23.a", "J.23.b") n'apparaissent qu'au debut de la page suivante. Ne
     l'extrais JAMAIS comme une ligne a part avec un prix a 0 ou invente : ce
     titre doit continuer a servir de `chapitre`/`sous_famille` courant pour
     les lignes suivantes, exactement comme s'il n'y avait pas eu de saut de
     page entre lui et ses enfants.

2. La numérotation change de convention d'un document à l'autre, et parfois
   d'une section à l'autre dans le MÊME document :
   - Lettres : A, A.1, A.7, A.7a, A.7.a, B.5.a...
   - Chiffres romains en titre de chapitre : "III - Bordurage...", avec des
     codes numériques en dessous : 301, 311+, 311,1, 312,2...
   - Un même code numérique peut réapparaître dans un chapitre différent
     (ex: 216 sous chapitre II, puis à nouveau 216 sous chapitre IV) — ce n'est
     PAS une erreur de ta part, le document fait ça. Le champ `numero` sert à la
     traçabilité, jamais de déduction structurelle : ne te fie qu'à l'ordre
     d'apparition dans le document pour déterminer le rattachement.
   - Une désignation peut s'étaler sur plusieurs lignes de texte avant d'arriver
     à l'unité/quantité (parfois avec un texte de filler du genre "Ce prix
     rémunère :" au milieu) — regroupe tout ça en une seule désignation propre.
   - Lis attentivement TOUT le texte imprimé sur la ligne, même quand la mise
     en page est dense ou que des lignes se chevauchent visuellement (tableaux
     resserrés en fin de document par exemple). Ne laisse jamais une désignation
     vide ou réduite à l'unité/au prix si un texte descriptif est présent sur
     la page à cet endroit.

3. CHAPITRE ET SOUS-FAMILLE — parcours le document dans l'ordre et maintiens
   deux valeurs courantes :
   - `chapitre` = dernier titre de section racine rencontré (ex: "A - TERRASSEMENTS...",
     "III - Bordurage et assainissement pluvial").
   - `sous_famille` = dernier header SANS prix rencontré qui n'est pas lui-même
     un chapitre racine (ex: "A.7 Décapage de terre végétale...", "311+ Terrassement
     de tranchées pour canalisation").
   - Dès que tu passes à une ligne qui N'EST PLUS un enfant de ce header (elle
     revient au même niveau ou remonte), `sous_famille` redevient vide (null) —
     ne le garde surtout pas pour la ligne suivante si elle n'en est pas l'enfant.
   - `sous_famille` est null si la ligne extraite est un enfant direct du
     chapitre, sans header intermédiaire.

4. NUMERO REPETE AVEC DESIGNATION MANQUANTE — certains documents (accords-cadres
   avec plusieurs "opérations"/chantiers types) répètent la même grille de prix
   plusieurs fois dans le document, avec les mêmes numéros et les mêmes prix
   mais des quantités différentes à chaque fois. Si un numéro que tu as déjà
   rencontré réapparaît avec le même prix unitaire mais que sa désignation sur
   cette occurrence est vide, tronquée ou anormalement courte par rapport à sa
   première apparition, réutilise la désignation complète de la première
   apparition de ce numéro. Ne laisse jamais une ligne avec juste "unité + prix"
   sans aucun texte descriptif si ce texte existe ailleurs dans le document
   pour ce même numéro.

CHAMPS DE SORTIE (pour chaque ligne retenue)
- numero : le numéro/code brut tel qu'écrit (ex: "A.7a", "311,1")
- chapitre : le titre de chapitre racine courant
- sous_famille : le header intermédiaire courant, ou null
- designation : la désignation complète et propre (recomposée si sur plusieurs lignes)
- unite : l'unité (ex: "m3", "U", "ml"), ou null si absente
- quantite : la quantité en nombre, ou null si absente
- prix_unitaire : le prix unitaire en nombre (obligatoire)
- montant_ht : le montant total de la ligne en nombre, ou null si absent

Convertis tous les nombres au format décimal standard (point, pas de virgule
ni d'espace de séparation de milliers). N'invente aucune valeur : si un champ
optionnel est absent, mets null plutôt qu'une estimation."""
