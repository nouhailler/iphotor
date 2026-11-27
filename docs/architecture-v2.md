# Architecture d'iphotor V2 (2.0)

## 1. Principe : modularisation progressive

L'existant reste dans `iphotor.py` : accès aux bibliothèques, index, interface. La **logique
des nouveaux modules** est placée dans un paquet `iphotorlib/`, indépendant de Tkinter et
testable seul :

| Module | Rôle |
|---|---|
| `iphotorlib/storage.py` | Emplacements locaux, ouverture SQLite contrôlée (sources en `mode=ro&immutable=1`), migrations avec sauvegarde, garde-fou d'écriture commun |
| `iphotorlib/metadata.py` | Index des métadonnées techniques (EXIF, dimensions, GPS, taille, empreintes) : incrémental, reprenable, annulable |
| `iphotorlib/search.py` | Critères de recherche multicritère (modèle JSON), compilation en SQL, liste des champs et de leur disponibilité |
| `iphotorlib/timeline.py` | Agrégation chronologique (année, mois, jour, périodes vides, jours les plus riches) |
| `iphotorlib/collections.py` | Albums virtuels, albums intelligents, recherches enregistrées (`collections.sqlite3`) |
| `iphotorlib/duplicates.py` | SHA-256, empreinte perceptuelle (DCT, Pillow seul), regroupement exact et visuel |
| `iphotorlib/stats.py` | Statistiques calculées depuis l'index et l'index des métadonnées |
| `iphotorlib/export.py` | Export CSV et JSON (choix des champs, destination contrôlée) |
| `iphotorlib/geo.py` | Regroupement géographique hors ligne, projection pour la carte locale, lien OpenStreetMap explicite |
| `iphotorlib/applemeta.py` | Lecture en lecture seule des mots-clés, personnes et lieux des bases Apple (schéma détecté) |
| `iphotorlib/folders.py` | Instantanés de dossiers (fichiers ajoutés ou disparus), dossiers récents et favoris |
| `iphotorlib/deps.py` | Diagnostic des dépendances facultatives |

L'interface des nouveaux modules (fenêtres, barre de sélection, frise) reste dans
`iphotor.py`, pour réutiliser le design system 1.5 : `COLORS`, `Theme`, `Icons`, `_Dialog`.

**Installation** : le paquet Debian installe `iphotorlib/` dans `/usr/share/iphotor/`.
`iphotor.py` ajoute à `sys.path` son propre dossier et `/usr/share/iphotor`. L'utilisation
« script seul » reste possible, à condition de garder `iphotorlib/` à côté de `iphotor.py`.

## 2. Séparation des données

| Catégorie | Emplacement | Écriture |
|---|---|---|
| Sources (bibliothèques, dossiers) | là où elles sont | **jamais** |
| Index des photos (existant) | `~/.cache/photoexplorer_iphoto/<bib>/index.sqlite3` | reconstructible |
| **Index des métadonnées** (nouveau) | `…/<bib>/metadata.sqlite3`, `…/dossiers/metadata.sqlite3` | reconstructible (cache) |
| **Résultats de doublons, instantanés de dossiers** (nouveau) | `…/<bib>/doublons.json`, `…/dossiers/instantanes/` | reconstructible (cache) |
| Favoris et tags (existant) | `~/.local/share/iphotor/annotations.sqlite3` | données de l'utilisateur |
| **Albums virtuels, albums intelligents, recherches** (nouveau) | `~/.local/share/iphotor/collections.sqlite3` | données de l'utilisateur, migrations avec sauvegarde |
| Préférences | `~/.config/iphotor/config.json` | nouvelles clés : dossiers récents et favoris, surveillance, diaporama |

**Migrations** : chaque base locale porte un numéro de schéma (`PRAGMA user_version`). Avant
toute migration d'une base de données utilisateur, une copie `*.sqlite3.bak-v<N>` est faite
dans le même dossier. Les bases de cache sont simplement reconstruites si leur schéma change.

## 3. Décisions techniques

**Index des métadonnées séparé de l'index des photos.** L'index des photos se reconstruit en
quelques secondes depuis `AlbumData.xml`. La lecture EXIF de 100 000 fichiers sur un disque
USB prend, elle, de longues minutes. Elle est donc :
- **incrémentale** : signature taille + date par fichier ;
- **reprenable** : validation par lots ;
- **annulable**, et lancée seulement à la demande de l'utilisateur.

Les critères EXIF signalent le nombre de photos non encore analysées, au lieu de les présenter
comme non conformes.

**Recherche en SQL.** Les critères sont compilés en une clause `WHERE` exécutée sur l'index
local, avec l'index des métadonnées attaché par `ATTACH`. Aucun fichier n'est relu. En mode
dossier, une table SQLite en mémoire est construite à partir de la liste des images : la même
compilation sert.

**Résultats affichés dans la grille existante** par de nouvelles vues :
- `View("ids")` : liste d'identifiants ;
- `View("date")` : intervalle de dates ;
- `View("timeline")` : cartes de périodes.

Pagination, visionneuse et copies fonctionnent sans changement.

**Sélection multiple**. Elle est indexée par clé de photo (GUID ou chemin) et conserve les
objets `Photo`. Elle survit au changement de page et de vue dans une même source, et est vidée
au changement de source. Ctrl+clic et Maj+clic seulement : le clic simple ouvre toujours la
visionneuse.

**Albums virtuels multi-sources**. Chaque référence enregistre :
- la portée et la clé (GUID ou chemin) ;
- le chemin absolu du fichier au moment de l'ajout ;
- le nom, la taille et la date.

Une troisième source, « Collections », affiche les albums virtuels avec la mécanique du mode
dossier : des chemins de fichiers. Une référence introuvable est signalée ; « Retrouver » la
cherche par GUID dans la bibliothèque ouverte, ou par nom et taille dans un dossier choisi.

**Albums intelligents** : un nom et des critères de recherche, évalués à l'ouverture sur la
source courante.

**Doublons**. L'empreinte perceptuelle est un pHash DCT 32×32 → 8×8, calculé en Python pur avec
Pillow : pas de dépendance à numpy. Pour éviter les comparaisons deux à deux :
- les quasi-doublons sont cherchés sur toute la source par recherche multi-index (4 segments de
  16 bits, sondage à 1 bit : exhaustif jusqu'à 7 bits d'écart, vérifié contre la force brute) ;
- les photos « proches » sont comparées entre voisines dans le temps (fenêtre de 40 photos).

Une première version (13 segments de 5 bits) prenait 25 s et 1,7 Go pour 10 000 photos ; la
version retenue prend 0,27 s. Trois niveaux :

| Niveau | Critère |
|---|---|
| Identique | SHA-256 égal |
| Quasi-identique | distance ≤ 6 sur 64 bits |
| Proche, même série possible | distance ≤ 12 entre photos voisines dans le temps, jamais présenté comme doublon |

Aucune suppression n'est proposée.

**Géographie hors ligne**. Les photos sont regroupées par grille : environ 100 km, 10 km,
1 km. Les noms de lieux ne sont affichés que s'ils viennent des bases Apple ; il n'y a pas de
géocodage inverse, faute de données locales. La carte est une projection locale des points,
avec une graticule. OpenStreetMap ne s'ouvre que sur clic explicite, avec avertissement.

**Formats** :
- RAW : `rawpy` facultatif, utilisé seulement s'il n'existe pas d'aperçu JPEG intégré ;
- HEIC : `pillow_heif` facultatif, en plus de GdkPixbuf.

L'absence de ces modules est signalée dans le diagnostic.

**Disques externes**. Toutes les 5 s, un `stat` de la racine de la source détecte la
disparition puis le retour du disque. Les vignettes ne sont pas redemandées pendant
l'absence ; au retour, la page courante est rechargée.

**Non retenu, et documenté** :
- lecture vidéo intégrée : GStreamer dans Tk est fragile sous Wayland, on garde le lecteur système ;
- Photos 5 et suivantes : schéma non vérifiable sans bibliothèque réelle, étude dans `rapport-final-v2.md` ;
- tuiles cartographiques locales : volume de données déraisonnable.
