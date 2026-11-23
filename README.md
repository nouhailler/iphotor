# iphotor — Explorateur de bibliothèques iPhoto et Photos

Application de bureau Python/Tkinter qui permet de parcourir sous Debian 13, **en lecture
seule**, une ancienne bibliothèque iPhoto (iPhoto 9.x) ou une bibliothèque
de l'app Photos 1 à 4 (macOS 10.10 à 10.14), ainsi que **n'importe quel dossier d'images**
de l'ordinateur ou d'un disque externe. Les photos peuvent être copiées (presse-papiers ou
dossier de destination), marquées comme favorites, classées par tags, et leurs informations
EXIF affichées.

Version actuelle : **1.4.0**. Voir [CHANGELOG.md](CHANGELOG.md) pour l'historique et
[CONTEXT.md](CONTEXT.md) pour le contexte, les contraintes et la structure des bibliothèques
(AlbumData.xml d'iPhoto, base SQLite de Photos).

## 1. Formats pris en charge

| Élément                        | Prise en charge                                              |
|--------------------------------|--------------------------------------------------------------|
| Bibliothèque iPhoto (9.x)      | oui, via `AlbumData.xml`                                     |
| Bibliothèque Photos 1 à 4 (macOS 10.10 à 10.14) | oui, via `database/photos.db` ou `Library.apdb` |
| Bibliothèque Photos 5 et suivantes (macOS 10.15+) | non (`Photos.sqlite`)                     |
| JPEG, PNG, BMP, GIF, TIFF      | oui                                                          |
| HEIC (iPhone)                  | oui, si les paquets HEIC facultatifs sont installés          |
| RAW (CR2, NEF, ARW, DNG, PEF, SRW) | oui : JPEG généré par iPhoto/Photos, sinon aperçu intégré au fichier |
| Vidéos (MOV, MP4, AVI, MPG…)   | vignette seulement ; lecture avec l'application par défaut   |
| Dossier quelconque             | oui (mode « Explorer un dossier ») : JPG, JPEG, PNG, GIF, BMP, TIFF |

## 2. Dépendances

| Paquet Debian          | Rôle                                  |
|------------------------|---------------------------------------|
| `python3` (≥ 3.10)     | interpréteur (Debian 13 : 3.13)       |
| `python3-tk`           | interface graphique Tkinter / ttk     |
| `python3-pil`          | Pillow, décodage des images           |
| `python3-pil.imagetk`  | affichage des images Pillow dans Tk   |
| `python3-gi`, `gir1.2-gdkpixbuf-2.0`, `heif-gdk-pixbuf` | facultatifs : photos HEIC (iPhone) |
| `gir1.2-gtk-3.0`, ou `wl-clipboard` / `xclip` | facultatifs : copier une image dans le presse-papiers |
| `libimage-exiftool-perl` | facultatif : informations EXIF des fichiers HEIC                |

`plistlib`, `sqlite3`, `threading`… font partie de la bibliothèque standard.

## 3. Installation (Debian 13)

### Option A : paquet .deb (recommandé)

Téléchargez `iphotor_1.4.0_all.deb` depuis la
[page des releases](../../releases), puis :

```bash
sudo apt install ./iphotor_1.4.0_all.deb
```

`apt` installe automatiquement les dépendances. L'application apparaît dans le menu
(« iphotor ») et se lance aussi avec la commande `iphotor`. Pour la désinstaller :

```bash
sudo apt remove iphotor
```

Pour reconstruire le paquet depuis les sources : `./packaging/build-deb.sh`
(résultat dans `dist/`).

### Option B : script seul

```bash
sudo apt install python3-tk python3-pil python3-pil.imagetk
```

Facultatif, pour les photos HEIC :

```bash
sudo apt install python3-gi gir1.2-gdkpixbuf-2.0 heif-gdk-pixbuf
```

```bash
chmod +x iphotor.py
```

Aucune connexion Internet ni serveur n'est nécessaire.

## 4. Lancement

```bash
./iphotor.py
```

Options :

| Option                          | Effet                                                        |
|---------------------------------|--------------------------------------------------------------|
| `--bibliotheque CHEMIN` (`-b`)  | bibliothèque à ouvrir (défaut : la dernière ouverte, sinon `~/Pictures/iPhoto Library`) |
| `--reindexer`                   | force la reconstruction de l'index au démarrage              |
| `--verifier`                    | contrôle de cohérence albums/dossiers/événements/photos, sans interface |

La variable d'environnement `IPHOTOR_LIBRARY` peut aussi indiquer le chemin.

À la **première ouverture** d'une bibliothèque, son index est construit : de moins d'une
seconde pour quelques milliers de photos à une quinzaine de secondes pour une bibliothèque
d'environ 100 000 photos. Les ouvertures suivantes sont immédiates. Si aucune bibliothèque
n'est trouvée au démarrage, la fenêtre « Ouvrir une bibliothèque » s'affiche.

## 5. Utilisation

La barre du haut propose deux modes : **Bibliothèque iPhoto** et **Explorer un dossier**.
Chaque mode garde son état (sélection, page, recherche, position dans la grille) quand on
passe de l'un à l'autre.

### Mode « Bibliothèque iPhoto »

- **Plusieurs bibliothèques** : *Fichier › Ouvrir une bibliothèque…* (Ctrl+O) affiche les
  bibliothèques récentes et celles détectées sur les disques montés (`/media/<utilisateur>`,
  `/mnt`, `~/Images`…), avec leur état (indexée ou non). Le bouton *Parcourir…* permet de
  choisir un dossier à la main. *Fichier › Bibliothèques récentes* donne un accès direct.
  Chaque bibliothèque a son propre index et son propre cache de miniatures, et l'application
  rouvre la dernière bibliothèque utilisée.
- **Bibliothèques Photos** (`.photoslibrary`, Photos 1 à 4) : elles s'ouvrent comme les
  bibliothèques iPhoto. Les albums et dossiers créés par l'utilisateur sont repris, les
  « moments » de Photos deviennent les événements (un par jour et par lieu), et les photos
  marquées favorites forment l'album « Favoris ». Les bibliothèques Photos 5 et suivantes
  (macOS 10.15+, base `Photos.sqlite`) ne sont pas prises en charge.
- **Panneau gauche** (un clic sur une rubrique la déplie ou la replie)
  - *Toutes les photos* : toute la photothèque, par date de prise de vue.
  - *Albums* : tous les albums, triés par nom. Le chemin du dossier parent est affiché entre
    parenthèses ; quand plusieurs albums ont le même nom au même endroit, la date de leur
    première photo est ajoutée pour les distinguer.
  - *Événements* : regroupés par année (cliquer sur une année affiche toutes ses photos).
  - *Dossiers* : l'arborescence d'origine (iPhoto ou Photos), dossiers et sous-albums dépliables.
- **Grille** : 100 vignettes par page maximum, boutons *Précédent / Suivant* (ou touches
  Page préc. / Page suiv.). Un bandeau rouge « Fichier introuvable » signale une photo
  absente du disque ; un badge orange signale une vidéo.
- **Visionneuse** (clic sur une vignette) : photo ajustée à la fenêtre sans déformation,
  légende, commentaire, date, note, nom de fichier et événement. Une mention indique l'origine
  de l'image (version retouchée, rendu du RAW, aperçu intégré, miniature). Navigation ← / →,
  Début / Fin, Échap pour fermer. Elle parcourt toute la vue courante, pas seulement la page.
- **Recherche** (Ctrl+F, puis Entrée) : insensible à la casse **et aux accents**
  (`ete` trouve `Été`). Elle porte sur la légende, le commentaire, le nom de fichier
  et le nom d'événement des photos, et sur les noms d'albums, de dossiers et d'événements,
  qui apparaissent sous le nœud « Résultats » de l'arborescence. Portée : toute la
  photothèque ou l'album sélectionné.

### Mode « Explorer un dossier »

- **Ouvrir un dossier** : bouton *Ouvrir un dossier…* de la barre du haut, *Fichier › Ouvrir
  un dossier…* ou Ctrl+Maj+O. Le sélecteur de dossiers de Tk s'ouvre ; le dossier choisi est
  analysé en arrière-plan, puis ses images s'affichent en grille. Le dernier dossier ouvert
  est rouvert au retour dans ce mode.
- **Formats** : JPG, JPEG, PNG, GIF, BMP et TIFF. Les fichiers et dossiers cachés (dont les
  fichiers `._*` laissés par macOS sur les disques externes) sont ignorés, ainsi que les liens
  symboliques vers des dossiers.
- **Inclure les sous-dossiers** : case à cocher de la barre du haut. Le panneau gauche affiche
  alors l'arborescence des sous-dossiers qui contiennent des images ; un clic sur un
  sous-dossier n'affiche que ses images (et celles de ses propres sous-dossiers).
- **Tri** : par nom (ordre naturel : `IMG_2` avant `IMG_10`), par date de modification ou par
  taille, dans les deux sens. Le tri choisi est mémorisé.
- **Recherche** (Ctrl+F, puis Entrée) : sur le nom de fichier, insensible à la casse et aux
  accents, dans le dossier ou le sous-dossier sélectionné.
- **Grille** : 100 images par page, avec le nom du fichier, la date de modification et la
  taille sous chaque miniature. Le survol affiche le chemin complet dans la barre d'état.
- **Visionneuse** (clic sur une miniature) : la même que pour les bibliothèques, avec
  navigation précédente / suivante sur toutes les images de la vue. Elle affiche le dossier,
  la date de modification, la taille, les **dimensions** et le **chemin d'accès** de l'image.
- **F5** (ou *Fichier › Recharger*) relance l'analyse du dossier.

### Copier, favoris, tags et EXIF (dans les deux modes)

Un **clic droit** sur une vignette, ou les boutons de la visionneuse, donnent accès aux actions
suivantes. Les raccourcis s'appliquent à la photo affichée dans la visionneuse, ou à la
vignette survolée par le pointeur dans la fenêtre principale.

| Action | Raccourci | Effet |
|--------|-----------|-------|
| Copier l'image | Ctrl+C | place l'image dans le presse-papiers ; elle se colle (Ctrl+V) dans un traitement de texte, un logiciel de retouche, une messagerie… |
| Copier le chemin du fichier | Ctrl+Maj+C | copie le chemin d'accès du fichier, sous forme de texte |
| Copier vers un dossier… | Ctrl+S | copie le fichier de la photo dans le dossier choisi |
| Ajouter aux favoris / retirer | F | la photo apparaît dans « ♥ Mes favoris » ; un cœur s'affiche sur sa vignette |
| Tags… | T | cocher des tags existants ou en créer (séparés par des virgules) |
| Informations EXIF… | I | appareil, objectif, date de prise de vue, temps de pose, ouverture, ISO, focale, flash, orientation, coordonnées GPS… puis toutes les balises par groupe |

- **Copier un album, un événement ou un dossier** : clic droit sur l'élément dans le panneau
  gauche › *Copier « … » vers un dossier…*, ou *Fichier › Copier l'élément sélectionné vers un
  dossier…* (Ctrl+Maj+S). Fonctionne aussi pour une année d'événements, *Toutes les photos*,
  les résultats d'une recherche, *Mes favoris* et un tag.
  - Un sous-dossier portant le nom de l'album est créé (option décochable).
  - Un **dossier iPhoto** est copié avec sa structure : un sous-dossier par album ou sous-dossier.
  - Une **année d'événements** donne un sous-dossier par événement.
  - En mode « Explorer un dossier », un dossier est copié avec ses sous-dossiers (seules les
    images affichées par iphotor sont copiées).
- **Fichier copié** (bibliothèques) : la *version affichée* (retouchée si elle existe) ou le
  *fichier original* (tel que sorti de l'appareil, RAW compris).
- **Sécurité des copies** : un fichier existant n'est jamais remplacé (« IMG_0001 (2).jpg »),
  la date de modification d'origine est conservée, et les noms incompatibles avec les clés
  USB (FAT, exFAT) sont corrigés. Une copie vers une bibliothèque ou vers le dossier consulté
  est refusée. L'espace libre est vérifié avant de commencer. La copie se fait en arrière-plan,
  avec une barre de progression et un bouton *Annuler*. Les photos introuvables (disque
  débranché) sont signalées à la fin.
- **Mes favoris et Tags** : deux entrées du panneau gauche, sous *Toutes les photos* (ou sous le
  dossier ouvert). Un clic affiche les photos correspondantes ; un clic droit sur un tag permet
  de le retirer de toutes les photos. Les favoris et les tags sont propres à chaque
  bibliothèque ; ceux des dossiers explorés sont liés au chemin des fichiers. Ils sont
  enregistrés par iphotor (voir § 7) : **la bibliothèque n'est pas modifiée**, et les favoris ou
  mots-clés définis autrefois dans iPhoto ne sont pas touchés.
- **EXIF** : lus dans le fichier original. Le bouton *Copier le texte* copie toutes les
  informations ; *Voir sur une carte* ouvre la position GPS dans OpenStreetMap (navigateur).
  Pour les fichiers HEIC, installez `exiftool` (paquet `libimage-exiftool-perl`).

## 6. Règle de lecture seule

- Tous les fichiers de la bibliothèque sont ouverts en mode `rb` uniquement.
- Rien n'est jamais écrit dans la bibliothèque : un garde-fou (`LibraryManager.assert_outside`)
  refuse toute écriture dont la cible se trouverait dans la bibliothèque.
- Les bases SQLite internes d'iPhoto (`Database/`) ne sont pas utilisées.
- La base d'une bibliothèque Photos n'est jamais ouverte sur place : elle est copiée, avec son
  journal WAL, dans le dossier de cache, lue depuis cette copie, puis la copie est supprimée.
- Les dossiers explorés sont traités de la même façon : seules la liste des fichiers
  (`os.scandir`) et leur lecture sont utilisées, et un garde-fou (`FolderLibrary.assert_outside`)
  refuse toute écriture dans le dossier consulté. Rien n'y est jamais déplacé ni supprimé.
- Le bouton « Ouvrir ↗ » de la visionneuse passe le fichier à `xdg-open` (utile pour les
  vidéos). Le fichier est alors ouvert par une autre application.
- Les copies lisent les fichiers sources et écrivent uniquement dans le dossier de destination
  choisi. Celui-ci ne peut pas se trouver dans une bibliothèque ou dans le dossier consulté.
- Les favoris et les tags ne sont pas écrits dans les photos ni dans la bibliothèque.

## 7. Organisation du cache

Tout est stocké dans `~/.cache/photoexplorer_iphoto/` (ou `$XDG_CACHE_HOME/photoexplorer_iphoto/`),
avec **un dossier par bibliothèque** :

```
~/.cache/photoexplorer_iphoto/
└── bibliotheques/
    ├── iPhoto_Library-0123456789/      <nom>-<empreinte du chemin>
    │   ├── index.sqlite3               index des photos, albums, dossiers, événements (≈ 120 Mo)
    │   └── thumbs/                     vignettes 200 px générées à la demande
    │       ├── 00/ … ff/               répartition par identifiant de photo
    │       └── <id>_<horodatage>.jpg
    └── Autre_bibliotheque-9876543210/
        └── …
└── dossiers/
    └── thumbs/                         vignettes des dossiers explorés (mode « Explorer un dossier »)
        ├── 00/ … ff/
        └── <empreinte du chemin, de la taille et de la date>.jpg
```

Les vignettes des dossiers explorés sont repérées par le chemin, la taille et la date de
modification du fichier : une image modifiée obtient une nouvelle vignette. Si le dossier
exploré contient lui-même le cache (par exemple tout le dossier personnel), les vignettes ne
sont pas enregistrées, pour ne rien écrire dans le dossier consulté.

Les préférences (dernière bibliothèque, liste des récentes, dernier dossier exploré, tri et
option des sous-dossiers) sont dans
`~/.config/iphotor/config.json`, avec le dernier dossier de destination des copies.

Les **favoris et tags** sont dans `~/.local/share/iphotor/annotations.sqlite3` (ou
`$XDG_DATA_HOME/iphotor/`) : ils ne font pas partie du cache et ne sont pas effacés quand on le
vide. Pour les sauvegarder, copiez ce fichier. Le cache unique de la version 1.0.0 est déplacé
automatiquement dans le dossier de sa bibliothèque au premier lancement.

- **index.sqlite3** contient les tables :
  - `photos` : métadonnées et chemins macOS d'origine,
  - `albums` : albums, dossiers, événements, avec `AlbumId`, `Parent` et le type d'origine,
  - `album_photos` : contenu ordonné de chaque album,
  - `meta` : taille et date des fichiers source, version du schéma.
- **Validité** : à chaque ouverture, la taille et la date de modification des fichiers source
  (`AlbumData.xml` pour iPhoto, base et journal WAL pour Photos) sont comparées à celles
  enregistrées. L'index n'est reconstruit qu'en cas de différence
  (ou via *Fichier › Reconstruire l'index*). La reconstruction se fait dans un fichier
  temporaire, remplacé à la fin : l'ancien index reste valable en cas d'échec.
- **Vignettes** : le cache est rempli au fil de la navigation à partir des miniatures de la
  bibliothèque (`ThumbPath` pour iPhoto, `resources/proxies/derivatives` pour Photos), ou à
  défaut de l'image elle-même. Le nom de fichier inclut la date de modification de la photo. Les vignettes
  déjà en cache restent visibles même quand le disque est débranché.
  *Fichier › Vider le cache des miniatures* les supprime sans toucher à la bibliothèque.
- Supprimer tout le dossier `~/.cache/photoexplorer_iphoto/` est sans risque : il sera recréé.
  *Fichier › Vider le cache des miniatures* ne concerne que la bibliothèque ouverte.

## 8. Choix techniques

- **Chemins** : les chemins macOS du XML (`/Volumes/<disque>/<bibliothèque>/…`, valeur
  « Archive Path ») sont convertis vers l'emplacement réel de la bibliothèque sous Linux.
  L'existence de chaque fichier est vérifiée, avec tolérance aux formes Unicode NFC/NFD
  (disques HFS+).
- **Fichier affiché (iPhoto)** : `ImagePath` en priorité (pour les RAW CR2, c'est le JPEG de
  `Previews`), puis `OriginalPath`, puis `ThumbPath`.
- **Fichier affiché (Photos)** : rendu de la version retouchée (`resources/media/version`),
  sinon rendu JPEG du RAW (`resources/media/master`), sinon l'original (`Masters/`). Les
  miniatures viennent de `resources/proxies/derivatives`. La base (`database/photos.db` ou
  `Library.apdb`) est **copiée dans le cache avec son journal WAL** avant d'être lue : elle
  n'est jamais ouverte dans la bibliothèque.
- **RAW** : quand aucun JPEG n'existe, iphotor affiche l'aperçu JPEG pleine taille intégré au
  fichier RAW (CR2, NEF, ARW, DNG, PEF, SRW). Les données RAW elles-mêmes ne sont pas décodées.
- **HEIC** : décodé par GdkPixbuf (paquets `python3-gi`, `gir1.2-gdkpixbuf-2.0`,
  `heif-gdk-pixbuf`, recommandés par le paquet .deb).
- **Événements** : les albums de type `Event` sont reliés aux `List of Rolls` par
  `GUID` = `ProjectUuid`. Le champ `Roll` des photos pointe sur `RollID`, qui est différent de
  l'`AlbumId` de l'événement.
- **Identité des albums** : toujours par `AlbumId`, jamais par nom. Les doublons de clés et
  les clés de photos inexistantes (fréquentes dans les `KeyList` de dossiers) sont ignorés.
  `KeyListString` est pris en charge comme alternative à `KeyList`.
- **Performances** : pagination SQL (`LIMIT/OFFSET`), vignettes chargées par 4 threads,
  décodage JPEG réduit (`draft`), images de la page précédente libérées à chaque changement
  de page, une seule image en grand en mémoire.

## 9. Messages d'erreur

| Situation                          | Comportement                                               |
|------------------------------------|------------------------------------------------------------|
| Disque externe débranché           | message « Bibliothèque inaccessible » ; si un index existe, navigation possible dans les albums (vignettes en cache) |
| Photo déplacée ou supprimée        | bandeau « Fichier introuvable » sur la vignette, message dans la visionneuse |
| Miniature absente                  | repli sur `ImagePath`, sinon placeholder « Aucun aperçu »   |
| Image illisible ou corrompue       | placeholder « Image illisible », message dans la visionneuse |
| Entrée XML incomplète              | valeurs par défaut, entrée ignorée si l'identifiant manque  |
| Album vide                         | « Cet album ne contient aucune photo. »                     |
| AlbumData.xml ou base Photos endommagé | message d'erreur ; l'index précédent est conservé       |
| Bibliothèque Photos 5+             | signalée « non prise en charge » dans la fenêtre d'ouverture |
| Photo HEIC sans paquets HEIC       | miniature de la bibliothèque si elle existe, sinon vignette « Image introuvable » |
| RAW sans JPEG ni aperçu intégré    | « Format RAW sans aperçu JPEG exploitable »                 |
| Dossier illisible sur le disque (erreur d'entrée/sortie) | repli sur un autre fichier de la photo, sans interrompre l'application |
| Dossier exploré inexistant ou sans droit de lecture | message « Dossier inaccessible » |
| Sous-dossier inaccessible pendant l'analyse | ignoré ; listé sous « ⚠ Dossiers inaccessibles » dans le panneau gauche, avec la raison |
| Dossier exploré sans image         | « Aucune image (JPG, PNG, GIF, BMP, TIFF) dans ce dossier. » |
| Copie vers une bibliothèque ou le dossier consulté | refusée, avec un message           |
| Destination sans droit d'écriture ou trop petite | refusée avant le début de la copie   |
| Fichier à copier introuvable       | ignoré ; listé dans le bilan de fin de copie               |
| Presse-papiers indisponible        | message proposant d'installer `wl-clipboard` ou `xclip`, ou de copier le chemin |
| EXIF illisible (HEIC sans exiftool, format inconnu) | message dans la fenêtre « Informations » |

## 10. Vérification de cohérence

```bash
./iphotor.py --verifier
```

```bash
./iphotor.py --verifier -b "/chemin/vers/Ma photothèque.photoslibrary"
```

Le contrôle porte sur :
- les parents des albums, qui doivent être des dossiers existants ;
- le contenu des dossiers ;
- la correspondance entre chaque photo et son événement ;
- les références à des photos inexistantes ;
- la présence des fichiers, sur un échantillon de photos.

Il affiche aussi les noms partagés par plusieurs albums, pour vérifier que chacun garde son
propre contenu. Le code de sortie vaut 0 si tout est cohérent, 1 sinon.
