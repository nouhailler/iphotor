# Changelog

Toutes les évolutions notables d'iphotor sont consignées ici.
Format inspiré de [Keep a Changelog](https://keepachangelog.com/fr/1.1.0/), versions selon
[Semantic Versioning](https://semver.org/lang/fr/).

## [1.2.0] — 2026-11-23

### Ajouté

- **Bibliothèques Photos 1 à 4** (macOS 10.10 à 10.14, `.photoslibrary` avec `database/photos.db`
  ou `Library.apdb`) :
  - photos, titres, descriptions, notes ;
  - albums et dossiers de l'utilisateur, avec l'ordre personnalisé quand il existe ;
  - « moments » regroupés par jour et par lieu, utilisés comme événements ;
  - album « Favoris ».

  La base est copiée avec son journal WAL dans le cache, puis lue depuis cette copie.
- **HEIC** : affichage des photos d'iPhone via GdkPixbuf (`heif-gdk-pixbuf`).
- **RAW** : affichage de l'aperçu JPEG intégré aux fichiers RAW au format TIFF (CR2, NEF, ARW,
  DNG, PEF, SRW) quand aucune version JPEG n'existe, avec l'orientation de l'appareil.
- Visionneuse : l'origine de l'image affichée est indiquée (version retouchée, rendu du RAW,
  aperçu intégré, miniature).

### Modifié

- La fenêtre « Ouvrir une bibliothèque » accepte les bibliothèques Photos 1 à 4. Seules les
  bibliothèques Photos 5 et suivantes (`Photos.sqlite`) restent signalées comme non prises en charge.
- La validité de l'index dépend désormais de la base Photos et de son journal WAL pour ces
  bibliothèques.
- Paquet .deb : recommande `python3-gi`, `gir1.2-gdkpixbuf-2.0` et `heif-gdk-pixbuf`.

## [1.1.0] — 2026-11-23

### Ajouté

- **Plusieurs bibliothèques** : *Fichier › Ouvrir une bibliothèque…* (Ctrl+O). La fenêtre
  liste les bibliothèques récentes et celles détectées sur les disques montés, avec leur état,
  et propose un bouton *Parcourir…*. Les bibliothèques Photos (`.photoslibrary`) sont
  signalées comme non prises en charge.
- Sous-menu *Fichier › Bibliothèques récentes*. Au démarrage, la dernière bibliothèque
  utilisée est rouverte. Préférences dans `~/.config/iphotor/config.json`.
- **Cache séparé par bibliothèque** : `~/.cache/photoexplorer_iphoto/bibliotheques/<nom>-<empreinte>/`.
  L'ancien cache unique est migré automatiquement.
- Un clic sur le libellé d'une rubrique (Albums, Événements, Dossiers) la déplie ou la replie.
- Paquet Debian `iphotor_<version>_all.deb`, construit avec `packaging/build-deb.sh`.
  Il installe la commande `iphotor`, une entrée de menu et une icône.

### Corrigé

- La rubrique « Albums » (et les autres rubriques) ne pouvait plus être repliée une fois
  sélectionnée : elle était rouverte à chaque clic, et masquait les rubriques suivantes.

### Modifié

- `--bibliotheque` vaut par défaut la dernière bibliothèque ouverte.
- Le message « Bibliothèque inaccessible » ne cite plus un disque en particulier.

## [1.0.0] — 2026-11-23

Première version fonctionnelle.

### Ajouté

- **Lecture d'AlbumData.xml** avec `plistlib` : photos, albums, dossiers, événements (Rolls).
  Prise en charge de `KeyList` et de `KeyListString`, tolérance aux champs absents ou invalides.
- **Index local SQLite** dans `~/.cache/photoexplorer_iphoto/index.sqlite3`. Il est
  reconstruit uniquement si la taille ou la date de modification du XML change. L'écriture
  passe par un fichier temporaire renommé à la fin.
- **Arborescence** avec les rubriques Toutes les photos, Albums (liste à plat avec le chemin
  du dossier), Événements (regroupés par année) et Dossiers (hiérarchie dépliable).
  Le nombre de photos est affiché pour chaque entrée.
- **Grille de vignettes** paginée par 100. Chaque vignette indique la légende ou le nom de
  fichier, la date et la note. Des badges signalent les vidéos et les fichiers introuvables.
- **Cache de vignettes** (200 px) alimenté à la demande, chargé par 4 threads. Les vignettes
  en cache restent visibles quand le disque est débranché.
- **Visionneuse** : image ajustée à la fenêtre sans déformation, légende, commentaire, date,
  note, fichier et événement. Navigation au clavier (←/→, Début/Fin, Échap). Bouton
  d'ouverture avec l'application par défaut.
- **Affichage des RAW CR2** à partir de leur JPEG dans `Previews` (`ImagePath`), avec repli
  sur `OriginalPath` puis `ThumbPath`.
- **Recherche** insensible à la casse et aux accents. Elle porte sur la légende, le
  commentaire, le nom de fichier et l'événement des photos, ainsi que sur les noms d'albums,
  de dossiers et d'événements. Portée : toute la photothèque ou l'album sélectionné.
- **Conversion des chemins** macOS (`/Volumes/…`) vers l'emplacement Linux de la bibliothèque, avec tolérance NFC/NFD (HFS+).
- **Option `--verifier`** : contrôle de cohérence des relations entre albums, dossiers,
  événements et photos, et présence des fichiers sur un échantillon.
- **Options** `--reindexer` et `--bibliotheque` (variable `IPHOTOR_LIBRARY`).
- **Menu Fichier** : recharger, reconstruire l'index, vider le cache des miniatures,
  informations sur le cache.
- **Messages d'erreur en français** : bibliothèque inaccessible, fichier introuvable, image
  illisible, album vide, XML endommagé.
- **Garde-fou de lecture seule** (`LibraryManager.assert_outside`) : toute écriture dans la
  bibliothèque est refusée.

### Notes techniques

- Les événements sont reliés aux Rolls par `GUID` = `ProjectUuid`, car `AlbumId` ≠ `RollID`.
- Les clés obsolètes présentes dans les `KeyList` de dossiers sont ignorées.
- Le nom d'événement est stocké dans la table `photos`, pour garder des requêtes paginées
  sous 0,1 s.

[1.2.0]: ../../releases/tag/v1.2.0
[1.1.0]: ../../releases/tag/v1.1.0
[1.0.0]: ../../releases/tag/v1.0.0
