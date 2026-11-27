# Rapport de tests — iphotor 2.0.0

Environnement : Debian 13, Python 3.13, Tk 8.6.16, Pillow 11.1, session GNOME/Wayland (XWayland).
Lancement de la suite complète : `./tests/run_tests.sh`.

## Résultats

| Suite | Contenu | Résultat |
|---|---|---|
| Compilation | `py_compile` de `iphotor.py`, `iphotorlib/*.py`, `tests/*.py` | ✔ |
| `tests/test_lib.py` | 25 tests unitaires : stockage et garde-fous, migrations, copie des bases sources, extraction EXIF/GPS, index incrémental, reprise, recherche multicritère (texte, dates, EXIF, ET/OU, dialecte dossier, erreurs), chronologie, albums virtuels (ordre, relocalisation, recherche par nom et taille), doublons (exacts, quasi-doublons, exhaustivité contre force brute, séries), statistiques, export CSV/JSON, géographie, instantanés de dossiers, dépendances, mots-clés Apple (Photos et iPhoto Faces.db) | **25/25** |
| `tests/regression_gui.py` | 47 vérifications des fonctions 1.x dans l'interface : grille, pagination, raccourcis, recherche, visionneuse, favoris, tags, EXIF, copies, annulation, thèmes, redimensionnement, mode dossier, lecture seule | **47/47** |
| `tests/test_gui_v2.py` | 67 vérifications : parcours A à E du cahier des charges, plus la chronologie, les albums intelligents, les statistiques, la carte, le diaporama, la comparaison, le diagnostic, les doublons, les dossiers et les mots-clés Apple | **67/67** |
| `--verifier` sur la démo | Cohérence de l'index | « cohérent ✔ » |

Aucun test en échec. Aucun test désactivé.

## Lecture seule

Dans chaque parcours, l'empreinte SHA-256, la taille et la date de chaque fichier et dossier des
sources sont relevées avant et après. Les sources contrôlées sont la bibliothèque de démonstration,
le dossier d'images, la bibliothèque copiée sur un disque externe simulé et la base Apple
synthétique. Résultat : **identiques**.

## Performances mesurées (`tests/perf.py`, `tests/perf_gui.py`)

**Opérations sans interface** sur bibliothèques synthétiques, avec un index des métadonnées
synthétique :

| Photos | Index | Page | Recherche simple | Recherche avancée (4 critères) | Chronologie | Statistiques | Groupes de doublons |
|---|---|---|---|---|---|---|---|
| 1 000 | 0,16 s | 1 ms | 0,5 ms | < 1 ms | 4 ms | 11 ms | 0,02 s |
| 10 000 | 0,94 s | 1 ms | 5 ms | < 2 ms | 38 ms | 0,11 s | 0,27 s |
| 50 000 | 4,4 s | 3 ms | 24 ms | 0,18 s | 0,19 s | 0,56 s | 3,0 s |
| 100 000 | 8,9 s | 5 ms | 51 ms | 0,29 s | 0,38 s | 1,1 s | 10,1 s |

**Interface réelle** (index déjà construit, cache des miniatures vide) :

| Photos | Première page avec ses 100 vignettes | Page suivante | Chronologie | Mémoire |
|---|---|---|---|---|
| 1 000 | 2,3 s | 0,3 s | 0,04 s | 87 Mo |
| 10 000 | 2,3 s | 0,3 s | 0,11 s | 101 Mo |
| 50 000 | 2,1 s | 0,3 s | 0,51 s | 172 Mo |

**Coûts par fichier** (JPEG de 48 Ko, disque local) :

| Opération | Durée |
|---|---|
| Lecture EXIF | 0,31 ms |
| SHA-256 | 0,08 ms |
| pHash | 3,9 ms |

Sur un disque USB, la lecture des fichiers domine : ces chiffres sont des minimums.

**Goulot corrigé pendant les tests** : le regroupement des doublons prenait 25 s et 1,7 Go pour
10 000 photos. Il a été réécrit (recherche multi-index et fenêtre temporelle) et descend à 0,27 s.

## Non testé

- Bibliothèques réelles : le disque de l'utilisateur n'était pas branché.
- Bibliothèques Photos 1 à 4 réelles.
- Module `rawpy` et module `pillow-heif` : absents de la machine. Les chemins de repli ont été vérifiés.
- Collage d'image dans une application Wayland.
