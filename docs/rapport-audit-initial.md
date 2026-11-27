# Rapport d'audit initial — iphotor 1.5.0 (avant la V2)

Audit réalisé avant toute modification, sur la version 1.5.0 (commit `69c29fc`).

## 1. Contenu du dépôt

| Élément | Rôle |
|---|---|
| `iphotor.py` (6 497 lignes) | Toute l'application : accès aux bibliothèques, index, cache, interface Tkinter, CLI |
| `packaging/build-deb.sh`, `iphotor.desktop`, `iphotor.svg` | Paquet Debian (le script est installé en `/usr/bin/iphotor`) |
| `README.md`, `CHANGELOG.md`, `CONTEXT.md` | Documentation utilisateur, historique, contexte technique |
| `docs/rapport-modernisation-design.md`, `docs/captures/` | Rapport de la refonte graphique 1.5 |

Il n'y a **aucun test automatisé dans le dépôt**. Les tests des versions précédentes étaient
des scripts ponctuels, non versionnés.

## 2. Fonctionnalités existantes (à préserver)

**Sources lues** :
- bibliothèques iPhoto 9.x (`AlbumData.xml`) ;
- bibliothèques Photos 1 à 4 (`database/photos.db` ou `Library.apdb`, copiées dans le cache avant lecture) ;
- dossiers d'images quelconques (mode « Explorer un dossier »).

Les bibliothèques Photos 5 et suivantes (`Photos.sqlite`) sont détectées et refusées avec un message.

**Navigation** :
- Toutes les photos, albums à plat (homonymes distingués) ;
- événements groupés par année ;
- hiérarchie des dossiers iPhoto ;
- recherche simple (légende, commentaire, nom, événement) dans toute la photothèque ou dans l'album sélectionné ;
- pagination à 100 photos.

**Grille** :
- vignettes chargées par 4 threads, avec cache local ;
- badges vidéo, note, favori et fichier introuvable ;
- survol renseignant la barre d'état ;
- menu contextuel.

**Visionneuse** :
- navigation au clavier ;
- affichage des RAW (aperçu intégré) et des HEIC (GdkPixbuf) ;
- note sur la version affichée ;
- ouverture avec l'application par défaut.

**Actions** (v1.4) :
- copie de l'image dans le presse-papiers et copie du chemin ;
- copie vers un dossier d'une photo, d'un album, d'un événement, d'une année, d'un dossier iPhoto ou exploré, des favoris, d'un tag ou d'une recherche ;
- favoris et tags ;
- EXIF (Pillow, ou exiftool en repli) et ouverture facultative dans OpenStreetMap.

**Mode dossier** :
- sous-dossiers facultatifs ;
- tri par nom, date ou taille ;
- recherche par nom ;
- dernier dossier mémorisé.

**Maintenance** :
- Recharger (F5), reconstruire l'index ;
- vider le cache des miniatures, informations sur le cache ;
- `--verifier`, `--reindexer`, `--bibliotheque`.

**Interface 1.5** : design system centralisé (`PALETTES`, `Theme`, `Icons`), thèmes clair et
sombre, barre supérieure adaptative.

**Raccourcis** : Ctrl+O, Ctrl+Maj+O, Ctrl+F, F5, Ctrl+Q, Page préc./suiv., Ctrl+Maj+S, puis
Ctrl+C, Ctrl+Maj+C, Ctrl+S, F, T et I sur la vignette survolée ou dans la visionneuse.

## 3. Architecture (classes principales)

`LibraryManager`, `XMLParser`, `PhotosDBParser` et `CacheManager` (index SQLite par
bibliothèque) forment le cœur. `AlbumManager` et `PhotoManager` exposent des requêtes paginées
par « vue » (`View`). `ThumbnailLoader` gère les vignettes en threads.

Le mode dossier passe par `FolderScanner`, `FolderBrowser`, `FolderLibrary` et
`FolderThumbCache`, qui offrent la même interface que leurs équivalents bibliothèque (*duck typing*).

Les autres composants sont :
- `Annotations` : favoris et tags ;
- `read_exif()` ;
- `CopyTask` ;
- `PhotoViewer` ;
- les fenêtres secondaires ;
- `MainWindow`, environ 1 900 lignes, dont l'état par mode est sauvegardé dans `_MODE_ATTRS`.

**Règle de threads respectée partout** : les threads produisent des résultats dans une
`queue.Queue`, que le thread Tk lit via `after()`.

## 4. Protections de lecture seule existantes

- **Fichiers sources** : ils ne sont ouverts qu'en lecture (`"rb"`, Pillow, `os.scandir`, `stat`).
- **Garde-fous d'écriture** : `LibraryManager.assert_outside()` et
  `FolderLibrary.assert_outside()` refusent toute écriture dans la source. Ils sont appelés
  avant d'écrire l'index, les miniatures et la copie de la base Photos.
- **Base Photos** : elle est copiée dans le cache, avec son journal WAL, et jamais ouverte sur place.
- **Copies** : `CopyTask.check_destination()` refuse toute destination située dans une source
  ouverte, n'écrase jamais un fichier et écrit dans un fichier `.iphotor-part`.
- **Index** : il est écrit dans un fichier `.tmp`, puis renommé de façon atomique.

## 5. Stockage local

| Données | Emplacement |
|---|---|
| Index et miniatures | `~/.cache/photoexplorer_iphoto/<bibliothèque>/` |
| Miniatures des dossiers | `~/.cache/photoexplorer_iphoto/dossiers/` |
| Préférences | `~/.config/iphotor/config.json` |
| Favoris et tags | `~/.local/share/iphotor/annotations.sqlite3` |

## 6. Dépendances

- **Obligatoires** : `python3` 3.10 ou plus, `python3-tk`, `python3-pil`, `python3-pil.imagetk`.
- **Facultatives** :
  - GdkPixbuf avec `heif-gdk-pixbuf`, pour les HEIC ;
  - `wl-clipboard` ou `xclip`, ou à défaut GTK, pour le presse-papiers d'images ;
  - `exiftool`, pour l'EXIF des HEIC ;
  - `xdg-utils`, pour ouvrir les fichiers.
- **Absentes sur la machine de test** : `rawpy`, `pillow_heif`, `numpy`.

## 7. Lacunes et risques identifiés

| Constat | Conséquence pour la V2 |
|---|---|
| Fichier unique de 6 500 lignes | La V2 ajouterait plusieurs milliers de lignes. **Modularisation progressive** : la logique des nouveaux modules va dans un paquet `iphotorlib/`, testable sans interface. L'existant reste dans `iphotor.py`. |
| Pas de tests dans le dépôt | Créer `tests/` : tests unitaires (stdlib `unittest`), tests d'intégration de l'interface et tests de lecture seule par empreintes. |
| L'index ne contient aucune donnée EXIF | La recherche EXIF, les statistiques, la géographie et les doublons demandent un **index de métadonnées** séparé, incrémental et reprenable, stocké dans le cache. |
| Clic simple sur une vignette = ouvrir la visionneuse | La sélection multiple doit passer par Ctrl+clic et Maj+clic, pour ne pas changer le comportement existant. |
| Visionneuse et grille liées à une seule source à la fois | Les albums virtuels multi-sources demandent une source « Collections » dédiée, sur le modèle du mode dossier. |
| Photos 5 et suivantes : schéma interne non documenté et variable | Sans bibliothèque réelle de test, une prise en charge serait non vérifiable. Étude documentée et message explicite conservé. |
| Disques externes : vérification de disponibilité seulement à l'ouverture | Ajouter une surveillance périodique légère (`stat`), avec reconnexion. |
| Bibliothèques réelles de l'utilisateur non accessibles pendant le développement (disque débranché) | Tests sur bibliothèques et bases Apple **synthétiques**. Les limites seront documentées. |

## 8. Découpage technique proposé

Voir [architecture-v2.md](architecture-v2.md).
