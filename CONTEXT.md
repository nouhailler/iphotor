# CONTEXT — iphotor

Ce document décrit le **contexte, les contraintes et les connaissances techniques** du projet.
Il sert de point de départ à toute personne (ou assistant) qui reprend le développement.

## 1. Pourquoi ce projet

iPhoto et les premières versions de l'app Photos n'existent que sous macOS. Leurs
bibliothèques, souvent conservées sur un disque externe, deviennent difficiles à consulter
depuis Linux. iphotor permet de les parcourir sous Debian en conservant leur organisation
(albums, dossiers, événements, légendes, commentaires, notes), **sans jamais les modifier**.
Depuis la version 1.3, il sert aussi de visionneuse pour n'importe quel dossier d'images
(mode « Explorer un dossier »), avec les mêmes garanties de lecture seule.
La version 1.4 ajoute la copie (presse-papiers, dossier de destination), les favoris, les
tags et l'affichage des informations EXIF.

## 2. Contraintes non négociables

1. **Lecture seule absolue sur la bibliothèque.** Rien n'est modifié, déplacé, renommé,
   supprimé ni créé dans la bibliothèque. Les disques HFS+ sont souvent montés en écriture :
   la protection repose donc sur le code.
   - Les fichiers sont ouverts uniquement en `"rb"`.
   - `LibraryManager.assert_outside()` refuse toute écriture dont la cible se trouverait
     dans la bibliothèque.
   - Une base SQLite de la bibliothèque n'est jamais ouverte sur place. Elle est copiée dans
     le cache (voir § 4), ou ouverte en `mode=ro&immutable=1`.
   La même règle vaut pour les dossiers explorés (`FolderLibrary.assert_outside()`).
   Les copies (`CopyTask`) lisent les sources et refusent toute destination située dans une
   bibliothèque ou un dossier consulté ; elles ne remplacent jamais un fichier existant.
   Les favoris et les tags ne sont jamais écrits dans les photos ni dans la bibliothèque.
2. **Caches hors bibliothèque :** `~/.cache/photoexplorer_iphoto/bibliotheques/<nom>-<empreinte>/`,
   un dossier par bibliothèque, et `…/dossiers/thumbs/` pour les dossiers explorés. Si le
   dossier exploré contient le cache, les vignettes ne sont pas enregistrées. Les préférences
   sont dans `~/.config/iphotor/config.json`. Les favoris et les tags, qui sont des données de
   l'utilisateur et non un cache, sont dans `~/.local/share/iphotor/annotations.sqlite3`.
3. **Pile technique :** Python 3, Tkinter/ttk, Pillow, plistlib, sqlite3. GdkPixbuf est
   facultatif, pour le HEIC et le presse-papiers. Aucun serveur et aucune connexion réseau
   (seul le bouton « Voir sur une carte » ouvre OpenStreetMap dans le navigateur, à la demande).
4. **Un seul fichier** (`iphotor.py`) pour simplifier l'installation, mais organisé en classes.
5. **Interface et messages en français.**
6. **Robustesse :** une photo manquante ou illisible ne doit jamais arrêter l'application.
7. **Confidentialité :** le dépôt ne contient aucune donnée personnelle. Pas de nom de
   bibliothèque réelle, de nom d'album ni de chemin de disque. Les exemples restent génériques.

## 3. Format iPhoto (AlbumData.xml)

Une bibliothèque iPhoto 9.x contient `AlbumData.xml` (plist XML), ainsi que `Masters/`
(originaux), `Previews/` (versions développées), `Thumbnails/` et `Database/` (bases
internes, non utilisées). Le XML peut dépasser 100 Mo. `plistlib` le lit en une dizaine de
secondes, une seule fois grâce à l'index.

### Clés principales

- **`Archive Path`** : chemin macOS de la bibliothèque (`/Volumes/<disque>/<bibliothèque>`),
  utilisé pour convertir les chemins vers Linux.
- **`List of Albums`** : albums de tous types, avec `AlbumId`, `AlbumName`, `Album Type`,
  `Parent`, `KeyList`/`KeyListString`, `PhotoCount` et `GUID`.
- **`List of Rolls`** : événements, avec `RollID`, `ProjectUuid`, `RollName` et `KeyList`.
- **`Master Image List`** : photos, avec `Caption`, `Comment`, `Rating`, `Roll`, `ImagePath`,
  `OriginalPath`, `ThumbPath`, `MediaType` et les dates `…AsTimerInterval`.

### Types d'albums (`Album Type`)

| Type            | Traitement dans iphotor                               |
|-----------------|-------------------------------------------------------|
| `Regular`       | album consultable (`KIND_ALBUM`)                      |
| `Event`         | événement (`KIND_EVENT`), section « Événements »      |
| `Folder`        | dossier (`KIND_FOLDER`), section « Dossiers »         |
| `Smart`         | album intelligent : liste figée telle qu'exportée     |
| `99`            | « Photos » = toutes les photos (`KIND_ALL`)           |
| `Flagged`, `Special Month`, … | albums spéciaux                         |

### Points non évidents

- **Événements et Rolls :** l'`AlbumId` d'un album `Event` est **différent** du `RollID`
  correspondant, et le champ `Roll` des photos pointe sur `RollID`. La correspondance fiable
  est **`Event.GUID` = `Roll.ProjectUuid`**.
- **Dossiers :** un `Folder` a sa propre `KeyList`, qui agrège les photos de ses sous-albums.
  Elle contient souvent des **clés obsolètes** (photos supprimées depuis), qu'il faut ignorer.
- **Parents :** `Parent` pointe vers un `Folder`. Les dossiers peuvent s'imbriquer sur
  plusieurs niveaux. Les événements n'ont pas de parent.
- **Homonymes :** plusieurs albums peuvent porter le même nom, y compris dans le même dossier.
  L'identité d'un album repose donc **toujours sur `AlbumId`**. Dans l'interface, les homonymes
  sont distingués par la date et l'heure de leur première photo.
- **Chemins :**
  - `ImagePath` pointe vers `Masters/`, ou vers `Previews/` quand la photo a été développée
    (elle a alors un `OriginalPath`).
  - Pour les RAW, `ImagePath` est le JPEG de `Previews`, et c'est lui qui est affiché.
  - Les vidéos ont un `ThumbPath` en JPEG.
- **Dates :** `DateAsTimerInterval` est exprimé en secondes depuis le 2001-01-01, en heure
  locale de la prise de vue.
- **Caption :** souvent identique pour toute une série, ou égale au nom de fichier sans
  extension. Dans ce dernier cas, c'est le nom de fichier qui est affiché.
- **Extension :** une bibliothèque iPhoto peut porter l'extension `.photolibrary` tout en
  gardant cette structure.

## 4. Format Photos 1 à 4 (macOS 10.10 à 10.14)

Le schéma n'est pas documenté par Apple ; le projet libre `osxphotos` est une bonne référence.

- **Base :** `database/photos.db` (schémas récents) ou `database/Library.apdb` (premiers
  schémas). Ce sont des tables `RK*` héritées d'Aperture. Photos 5 et suivantes utilisent
  `database/Photos.sqlite` (Core Data), un format entièrement différent et non pris en charge.
- **Journal WAL :** `photos.db-wal` peut contenir des écritures pas encore reportées dans la
  base, que le mode `immutable=1` ignorerait. La base et son journal sont donc **copiés dans
  le cache**, lus depuis la copie, puis la copie est supprimée.
- **Correspondances :**
  - `RKVersion` est la photo telle que vue dans Photos (une ligne par photo). `RKMaster` est
    le fichier original, relié par `RKVersion.masterId`.
  - `RKVersion.name` sert de légende, et `extendedDescription` de commentaire (souvent des
    espaces seuls).
  - `imageDate` est en UTC ; on lui ajoute `imageTimeZoneOffsetSeconds` pour obtenir l'heure locale.
  - Les photos affichées sont celles avec `isInTrash = 0` et `showInLibrary = 1`. `type = 8`
    désigne une vidéo.
- **Albums et dossiers :**
  - Albums utilisateur : `RKAlbum` avec `albumSubclass = 3`, `albumType = 1` et `isMagic = 0`.
    Le type 8 correspond à un diaporama, le type 9 à un projet. Le contenu est dans
    `RKAlbumVersion`, et l'ordre dans `RKCustomSortOrder`.
  - Dossiers : `RKFolder` avec `isMagic = 0` et `folderType = 1`. La hiérarchie passe par
    `parentFolderUuid`. `TopLevelAlbums` est la racine.
  - Les identifiants `RKFolder`, `RKMoment` et `RKAlbum` sont numérotés séparément : dans
    l'index, on leur ajoute 10 000 000 (dossiers), 20 000 000 (moments) ou 30 000 000 (spéciaux).
  - Les photos favorites (`isFavorite`) forment l'album spécial « Favoris ».
- **Moments :** la table `RKMoment` joue le rôle des événements. Le lieu est dans
  `reverseLocationData` (bplist : `compoundNames.sortedNames`), mais il est souvent vide. Les
  moments d'un même jour portant le même nom sont fusionnés.
- **Fichiers générés par Photos** (le chemin dérive de l'identifiant `id`) :
  - Miniatures : `resources/proxies/derivatives/{(id>>8)&255:02x}/{(id>>16)&255:02x}/{id:x}/{adjustmentUuid}_thumb_{id:x}.jpg`.
  - Rendus pleine taille : `resources/media/{version|master}/…/fullsizeoutput_{rid:x}.jpeg`,
    où `rid` est le `modelId` de `RKModelResource` (`resourceType = 4`, `attachedModelType`
    vaut 2 pour une version retouchée, 1 pour un original RAW).
  - Les premiers schémas n'ont ni `derivatives` ni `RKModelResource`. Les miniatures sont
    alors générées depuis les originaux. Pour les RAW, on utilise l'aperçu JPEG intégré.
  - Dans ces premiers schémas, les fichiers `Previews/…/<uuid de version>/…jpg` servent
    notamment d'images de couverture des vidéos.
- **Fichiers référencés :** un original avec `fileIsReference = 1` se trouve hors de la
  bibliothèque, sur le Mac d'origine. On affiche à la place le rendu JPEG ou la miniature.

## 5. Formats d'image

- **HEIC :** décodé par GdkPixbuf avec le chargeur `heif-gdk-pixbuf`, aucun paquet Debian ne
  fournissant le module `pillow-heif`.
- **RAW au format TIFF** (CR2, NEF, ARW, DNG, PEF, SRW) : l'aperçu JPEG pleine taille intégré
  est extrait. Les répertoires TIFF (IFD et SubIFD) sont parcourus, et seul un JPEG décodable
  est retenu (baseline ou progressif ; le JPEG sans perte des données RAW est écarté).
  L'orientation EXIF du RAW est ensuite appliquée.
- **Disques HFS+ sous Linux :** certains dossiers peuvent renvoyer des erreurs d'entrée/sortie
  (limite du pilote `hfsplus`). Toutes les vérifications d'existence tolèrent ces erreurs, et
  un autre fichier de la photo prend le relais.

## 6. Architecture

```
iphotor.py
├── LibraryManager   format de la bibliothèque, chemins macOS → Linux, existence (NFC/NFD), choix du fichier affichable
├── XMLParser        bibliothèques iPhoto : plistlib → dictionnaires normalisés (champs absents tolérés)
├── PhotosDBParser   bibliothèques Photos 1 à 4 (tables RK*) → mêmes dictionnaires que XMLParser
├── CacheManager     construction de l'index SQLite, validité (taille + date des fichiers source), cache des vignettes
│                    (un dossier par bibliothèque : library_cache_dir())
├── Settings         préférences ~/.config/iphotor/config.json (dernière bibliothèque, récentes)
├── find_libraries() détection des bibliothèques sur les disques (profondeur et durée limitées)
├── AlbumManager     albums, dossiers, événements, chemins parents, recherche par nom
├── PhotoManager     requêtes paginées par vue (toutes, album, année, recherche)
├── ThumbnailLoader  4 threads : cache local → miniature de la bibliothèque → image ; résultats via une queue
├── load_pil_image() JPEG/PNG/… (Pillow), HEIC (GdkPixbuf), RAW (aperçu intégré)
├── PhotoViewer      fenêtre de visualisation (chargement dans un thread, redimensionnement)
├── LibraryDialog    fenêtre « Ouvrir une bibliothèque » (récentes, détectées, parcourir)
├── FolderScanner    mode dossier : recherche des images (os.scandir, thread, annulable, erreurs tolérées)
├── FolderBrowser    mode dossier : filtre (sous-dossier, nom), tri ; même interface que PhotoManager
├── FolderLibrary    mode dossier : équivalent de LibraryManager (fichier affichable, garde-fou d'écriture)
├── FolderThumbCache mode dossier : vignettes dans …/dossiers/thumbs/, clé = chemin + taille + date
├── Annotations      favoris et tags (annotations.sqlite3) ; clé = (portée, GUID) pour une bibliothèque,
│                    (« fichiers », chemin absolu) pour un dossier exploré : photo_scope_key()
├── read_exif()      EXIF via Pillow (IFD0, Exif, GPS), repli sur exiftool (HEIC) ; résumé en français
├── copy_png_to_clipboard()  image → presse-papiers : wl-copy, xclip, ou processus GTK auxiliaire
├── CopyTask         copie dans un thread : résolution des fichiers, espace libre, noms uniques, annulation
├── TagDialog, ExifDialog, CopyDialog, CopyProgress   fenêtres des nouvelles actions
├── MainWindow       arborescence + grille sur un Canvas + recherche + pagination + changement de bibliothèque
│                    + barre des modes (set_mode sauvegarde et restaure l'état de chaque mode)
└── verify()         contrôle de cohérence en ligne de commande (--verifier)
```

### Règles de conception

- **Threads :** Tk n'est manipulé **que** dans le thread principal. Les threads de travail
  produisent des images PIL déposées dans une `queue.Queue`, que le thread principal vide via
  `after()`. Chaque page porte un numéro de *génération* : les résultats d'une page qui n'est
  plus affichée sont ignorés.
- **SQLite :** une connexion par thread. L'indexation écrit dans `index.sqlite3.tmp`, puis
  le fichier est renommé de façon atomique (`os.replace`).
- **Dénormalisation :** `photos.event_name` est stocké directement. Une jointure vers
  `albums` sans index rendait l'affichage d'une page plus de mille fois plus lent.
- **Chemins dans l'index :** pour iPhoto, ce sont les chemins macOS d'origine, convertis à
  l'affichage. Pour Photos, ce sont des chemins relatifs à la racine de la bibliothèque.
- **Mémoire :** au plus 100 vignettes et une seule image en grand. Le décodage JPEG est
  réduit avec `Image.draft()`.
- **Mode dossier :** il réutilise la grille, `ThumbnailLoader` et `PhotoViewer` par
  *duck typing* : `FolderBrowser` remplace `PhotoManager`, `FolderLibrary` remplace
  `LibraryManager` et `FolderThumbCache` remplace `CacheManager`. Les images sont des objets
  `Photo` avec `source = "folder"`. Changer de mode échange ces objets (`_MODE_ATTRS`), sans
  rien recharger. Les noms de fichiers qui ne sont pas de l'UTF-8 valide passent par
  `safe_text()` pour l'affichage, et le vrai chemin est conservé pour la lecture.
- **Favoris et tags :** une vue `View("keys", keys=…)` sélectionne les photos par GUID
  (`json_each`) ; en mode dossier, `FolderBrowser.only` filtre par chemin. Les entrées
  « Mes favoris » et « Tags » de l'arborescence sont recréées après chaque modification
  (`_refresh_annotations_tree`), sans recharger la vue courante sauf si c'est l'une d'elles.
- **Presse-papiers :** Tk ne sait copier que du texte. L'image est convertie en PNG, puis
  confiée à `wl-copy` ou `xclip`, sinon à un petit processus Python/GTK (X11, donc aussi
  XWayland) qui garde l'image jusqu'à ce qu'une autre application prenne le presse-papiers.
- **Version du schéma :** `INDEX_SCHEMA_VERSION`. À incrémenter à chaque modification du
  schéma, ce qui force la reconstruction de l'index.

## 7. Ordres de grandeur (Debian 13, disque USB)

| Opération                                          | Durée       |
|----------------------------------------------------|-------------|
| Lecture d'un AlbumData.xml d'environ 100 Mo        | ≈ 10 s      |
| Index d'une bibliothèque d'environ 100 000 photos  | ≈ 15 s      |
| Index d'une bibliothèque de quelques milliers de photos | < 1 s  |
| Page de 100 photos (requête)                       | < 0,1 s     |
| Recherche dans toute la photothèque                | ≈ 0,3 s     |
| 100 vignettes, cache vide                          | quelques s  |
| 100 vignettes depuis le cache local                | ≈ 0,1 s     |

## 8. Pistes pour les versions suivantes

- Lire `Database/` (bases SQLite d'iPhoto, en `mode=ro&immutable=1`) : visages, mots-clés,
  lieux, critères des albums intelligents.
- Exploiter `latitude` / `longitude`, par exemple avec une carte.
- Exploiter `List of Faces` et le champ `Faces` des photos.
- Photos : visages (`RKPerson`, `RKFace`), mots-clés (`RKKeyword`), lieux (`RKPlace`).
- Prendre en charge les bibliothèques Photos 5 et suivantes (`Photos.sqlite`).
- Lire les vidéos (lecteur externe ou intégré).
- Décoder directement les RAW (par exemple avec `rawpy`) quand il n'y a pas d'aperçu intégré.
- Sélection multiple dans la grille (favoris, tags et copie de plusieurs photos à la fois).
- Importer les mots-clés et les favoris d'iPhoto / Photos comme tags iphotor (lecture seule).
- Prévoir un mode de recherche avancé : par date, par note, recherche combinée.
- Mode dossier : HEIC et RAW (déjà décodés pour les bibliothèques), dossiers récents,
  surveillance des changements du dossier.
- Tester le comportement avec le disque débranché, qui n'a pas encore été vérifié en conditions réelles.
