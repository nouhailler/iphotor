# Changelog

Toutes les évolutions notables d'iphotor sont consignées ici.
Format inspiré de [Keep a Changelog](https://keepachangelog.com/fr/1.1.0/), versions selon
[Semantic Versioning](https://semver.org/lang/fr/).

## [1.4.0] — 2026-11-23

### Ajouté

- **Copier une photo dans le presse-papiers** (Ctrl+C) : l'image se colle dans une autre
  application. Utilise `wl-copy` ou `xclip` s'ils sont installés, sinon GTK.
  *Copier le chemin du fichier* (Ctrl+Maj+C) copie le chemin sous forme de texte.
- **Copier vers un dossier** : une photo (Ctrl+S), un album, un événement, une année
  d'événements, un dossier iPhoto (avec sa structure d'albums), un dossier exploré (avec ses
  sous-dossiers), les favoris, un tag ou les résultats d'une recherche. Clic droit dans le
  panneau gauche, ou *Fichier › Copier l'élément sélectionné vers un dossier…* (Ctrl+Maj+S).
  - Choix de la version affichée (retouchée) ou du fichier original.
  - Aucun fichier existant n'est remplacé ; la date de modification est conservée ; noms
    compatibles avec les clés USB.
  - Destination refusée si elle se trouve dans une bibliothèque ou dans le dossier consulté.
  - Copie en arrière-plan avec progression, annulation et bilan (fichiers introuvables).
- **Favoris** (touche F) : entrée « ♥ Mes favoris » dans le panneau gauche, cœur sur les
  vignettes.
- **Tags** (touche T) : tags libres sur chaque photo, entrée « 🏷 Tags » dans le panneau
  gauche, retrait d'un tag de toutes les photos.
- Favoris et tags enregistrés dans `~/.local/share/iphotor/annotations.sqlite3`, jamais dans
  la bibliothèque ni dans les fichiers.
- **Informations EXIF** (touche I) : résumé en français (appareil, objectif, date, exposition,
  ouverture, ISO, focale, flash, GPS…) et toutes les balises par groupe, copie du texte,
  position GPS sur OpenStreetMap. Fichiers HEIC lus avec `exiftool` s'il est installé.
- Menu contextuel (clic droit) sur les vignettes et dans la visionneuse ; nouveaux boutons dans
  la visionneuse (Favori, Tags, EXIF, Copier, Copier vers…).

### Modifié

- Le bouton « Ouvrir avec l'application par défaut » de la visionneuse devient « Ouvrir ↗ ».
- La fenêtre des raccourcis clavier liste les nouvelles actions.
- Paquet .deb : recommande `gir1.2-gtk-3.0` et suggère `wl-clipboard`, `xclip` et
  `libimage-exiftool-perl`.

## [1.3.0] — 2026-11-23

### Ajouté

- **Mode « Explorer un dossier »** : ouverture de n'importe quel dossier de l'ordinateur ou
  d'un disque externe (bouton *Ouvrir un dossier…*, *Fichier › Ouvrir un dossier…*,
  Ctrl+Maj+O), avec le sélecteur de dossiers de Tk.
  - Images JPG, JPEG, PNG, GIF, BMP et TIFF, en grille paginée (100 par page), avec le nom
    du fichier, la date de modification et la taille sous chaque miniature.
  - Option *Inclure les sous-dossiers*, avec l'arborescence des sous-dossiers dans le panneau
    gauche.
  - Tri par nom (ordre naturel), date de modification ou taille, dans les deux sens.
  - Recherche par nom de fichier, insensible à la casse et aux accents.
  - Visionneuse partagée avec les bibliothèques : navigation précédente / suivante,
    dimensions et chemin d'accès de l'image.
  - Analyse en arrière-plan, annulée si un autre dossier est choisi. Les dossiers
    inaccessibles et les fichiers illisibles sont signalés sans interrompre l'application.
  - Miniatures en cache dans `~/.cache/photoexplorer_iphoto/dossiers/`, jamais dans le
    dossier consulté.
- Barre des modes **Bibliothèque iPhoto** / **Explorer un dossier** : chaque mode conserve son
  état de navigation (sélection, page, recherche, position dans la grille).
- La visionneuse affiche aussi les dimensions des photos des bibliothèques.
- Raccourci F5 : recharger la bibliothèque ou le dossier.

### Modifié

- *Fichier › Recharger la bibliothèque* devient *Fichier › Recharger* (selon le mode).
- Un long chemin dans le sous-titre ne réduit plus le champ de recherche.
- Message plus clair pour une image dont le format n'est pas reconnu.

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

[1.4.0]: ../../releases/tag/v1.4.0
[1.3.0]: ../../releases/tag/v1.3.0
[1.2.0]: ../../releases/tag/v1.2.0
[1.1.0]: ../../releases/tag/v1.1.0
[1.0.0]: ../../releases/tag/v1.0.0
