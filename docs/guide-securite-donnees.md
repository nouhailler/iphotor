# Garanties de lecture seule et protection des données — iphotor 2

## 1. Principe

**Les bibliothèques iPhoto et Photos, et les dossiers explorés, ne sont jamais modifiés.**
iphotor ne fait que lire les fichiers. Tout ce qu'il produit (index, miniatures, métadonnées,
favoris, tags, albums virtuels, résultats) est écrit **ailleurs**, dans l'espace de l'utilisateur.

## 2. Où sont écrites les données d'iphotor

| Données | Emplacement | Nature |
|---|---|---|
| Index des photos, miniatures | `~/.cache/photoexplorer_iphoto/<bibliothèque>/` | cache, reconstructible |
| Index des métadonnées (EXIF, GPS, empreintes) | `…/<bibliothèque>/metadata.sqlite3`, `…/dossiers/metadata.sqlite3` | cache, reconstructible |
| Résultats de doublons | `…/<bibliothèque>/doublons.json`, `…/dossiers/doublons-*.json` | cache |
| Instantanés des dossiers (changements) | `…/dossiers/instantanes/` | cache |
| Favoris et tags | `~/.local/share/iphotor/annotations.sqlite3` | données de l'utilisateur |
| Albums virtuels, intelligents, recherches | `~/.local/share/iphotor/collections.sqlite3` | données de l'utilisateur |
| Préférences | `~/.config/iphotor/config.json` | préférences |

Les variables `XDG_CACHE_HOME`, `XDG_DATA_HOME` et `XDG_CONFIG_HOME` sont respectées.

## 3. Mécanismes de protection

| Mécanisme | Où |
|---|---|
| **Garde-fous d'écriture** : toute écriture est refusée si sa destination se trouve dans une source ouverte. Liens symboliques résolus. | `LibraryManager.assert_outside`, `FolderLibrary.assert_outside`, `CollectionLibrary.assert_outside`, `storage.assert_outside` (index des métadonnées, collections, exports, instantanés, résultats) |
| **Bases Apple jamais ouvertes sur place** : `photos.db`, `Library.apdb`, `Faces.db`… sont copiées avec leur journal WAL dans le cache, lues, puis la copie est supprimée | `PhotosDBParser`, `storage.SourceDBCopy`, `applemeta` |
| Ouverture SQLite strictement en lecture (`mode=ro&immutable=1`, `query_only`) quand une copie n'est pas nécessaire | `storage.open_source_db` |
| **Fichiers lus en lecture seule** : `"rb"`, en-têtes Pillow, `os.scandir`, `stat` | tout le code |
| **Copies sans risque** : destination refusée dans une source, aucun écrasement (« nom (2).jpg »), fichier `.iphotor-part` puis renommage, espace libre vérifié, annulation sans résidu | `CopyTask` |
| **Exports sans risque** : destination choisie par l'utilisateur, refusée dans une source, écriture atomique | `export.write` |
| **Aucune suppression de photo** : les doublons ne proposent que la copie ; supprimer un album virtuel ne supprime que des références | `DuplicatesWindow`, `Collections.delete` |
| **Surveillance passive** des dossiers et des disques : simple relecture de la liste des fichiers ou `stat` | `_watch_folder`, `_watch_disk` |
| **Migrations sûres** des bases de l'utilisateur : numéro de schéma, sauvegarde `*.bak-v<N>` avant migration, refus d'ouvrir une base créée par une version plus récente | `storage.open_user_db` |

## 4. Confidentialité et réseau

- **Aucune connexion Internet** n'est nécessaire, et aucune n'est faite automatiquement.
- **Deux ouvertures de navigateur possibles**, toujours sur action explicite :
  - *Voir sur une carte* (fenêtre EXIF) ;
  - *Ouvrir dans OpenStreetMap…* (fenêtre Carte), après une confirmation indiquant que la position sera envoyée au site.
- La carte intégrée est **locale** : points et graticule, sans fond téléchargé.
- **Aucun compte, aucun service distant, aucune télémétrie.**

## 5. Vérifications réalisées

Les tests automatiques (`tests/`) calculent, **avant et après** chaque scénario, l'empreinte
SHA-256, la taille et la date de **chaque fichier et de chaque dossier** des sources. Ils
vérifient l'égalité stricte.

Scénarios couverts :
- favoris, tags, sélection et actions groupées ;
- albums virtuels, recherche avancée et albums intelligents ;
- analyse des métadonnées ;
- doublons, comparaison, copie, export ;
- reconstruction de l'index ;
- déconnexion puis reconnexion d'un disque ;
- import des informations Apple (base source comprise).

Les bases SQLite sources font partie des fichiers contrôlés.

Résultat : **aucune modification** des sources dans aucun scénario. Le détail figure dans
[rapport-tests.md](rapport-tests.md).
