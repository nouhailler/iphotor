# iphotor — Explorateur de bibliothèques iPhoto et Photos

Application de bureau Python/Tkinter qui permet de parcourir sous Debian 13, **en lecture
seule**, une ancienne bibliothèque iPhoto (iPhoto 9.x) ou une bibliothèque
de l'app Photos 1 à 4 (macOS 10.10 à 10.14).

Version actuelle : **1.2.0**. Voir [CHANGELOG.md](CHANGELOG.md) pour l'historique et
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

## 2. Dépendances

| Paquet Debian          | Rôle                                  |
|------------------------|---------------------------------------|
| `python3` (≥ 3.10)     | interpréteur (Debian 13 : 3.13)       |
| `python3-tk`           | interface graphique Tkinter / ttk     |
| `python3-pil`          | Pillow, décodage des images           |
| `python3-pil.imagetk`  | affichage des images Pillow dans Tk   |
| `python3-gi`, `gir1.2-gdkpixbuf-2.0`, `heif-gdk-pixbuf` | facultatifs : photos HEIC (iPhone) |

`plistlib`, `sqlite3`, `threading`… font partie de la bibliothèque standard.

## 3. Installation (Debian 13)

### Option A : paquet .deb (recommandé)

Téléchargez `iphotor_1.2.0_all.deb` depuis la
[page des releases](../../releases), puis :

```bash
sudo apt install ./iphotor_1.2.0_all.deb
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

## 6. Règle de lecture seule

- Tous les fichiers de la bibliothèque sont ouverts en mode `rb` uniquement.
- Rien n'est jamais écrit dans la bibliothèque : un garde-fou (`LibraryManager.assert_outside`)
  refuse toute écriture dont la cible se trouverait dans la bibliothèque.
- Les bases SQLite internes d'iPhoto (`Database/`) ne sont pas utilisées.
- La base d'une bibliothèque Photos n'est jamais ouverte sur place : elle est copiée, avec son
  journal WAL, dans le dossier de cache, lue depuis cette copie, puis la copie est supprimée.
- Le bouton « Ouvrir avec l'application par défaut » de la visionneuse passe le fichier à
  `xdg-open` (utile pour les vidéos). Le fichier est alors ouvert par une autre application.

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
```

Les préférences (dernière bibliothèque, liste des récentes) sont dans
`~/.config/iphotor/config.json`. Le cache unique de la version 1.0.0 est déplacé
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
