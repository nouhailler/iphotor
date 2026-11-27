# Rapport final — iphotor 2.0.0

## A. Fonctionnalités existantes préservées

Toutes les fonctions 1.x restent disponibles :
- lecture d'iPhoto 9.x, de Photos 1 à 4 et des dossiers ;
- albums, événements, dossiers, recherche simple, pagination ;
- visionneuse, favoris, tags, EXIF, presse-papiers, copies ;
- thèmes ;
- `--verifier`, `--reindexer`, `--bibliotheque`.

Elles sont vérifiées par 47 contrôles de non-régression. Les raccourcis existants sont
inchangés ; Ctrl+A, Échap, Ctrl+Maj+F, Ctrl+clic et Maj+clic ont été ajoutés.

## B. Nouvelles fonctionnalités

La logique est dans `iphotorlib/` (12 modules testables sans interface). L'interface est dans
`iphotor.py`. Le détail d'utilisation est dans [guide-fonctionnalites.md](guide-fonctionnalites.md).

| Module | État | Tests |
|---|---|---|
| 1. Sélection multiple et actions groupées | ✔ terminé | parcours A |
| 2. Recherche avancée, recherches enregistrées | ✔ terminé : plus de 35 critères, ET/OU, champs indisponibles signalés | unitaires et parcours B |
| 3. Chronologie | ✔ terminé : années, mois, jours, périodes vides, plus riches, dates inconnues | unitaires et parcours |
| 4. Albums virtuels et albums intelligents | ✔ terminé : multi-sources, réorganisation, retrouver les références | unitaires et parcours A |
| 5. Géographie | ✔ carte locale hors ligne, regroupements, OpenStreetMap explicite. Noms pays / région / ville seulement si fournis par Apple : pas de géocodage inverse local | unitaires et parcours |
| 6. Comparaison | ✔ côte à côte synchronisé, rideau, tableau des métadonnées | parcours |
| 7. Doublons | ✔ SHA-256 et pHash, 3 niveaux, aucune suppression, copie des versions retenues, résultats enregistrés | unitaires et parcours C |
| 8. Statistiques | ✔ terminé, avec cache | unitaires et parcours |
| 9. Mots-clés, personnes, lieux Apple | ✔ **expérimental** : schéma détecté, testé sur bases synthétiques uniquement | unitaires et parcours |
| 10. Photos 5 et suivantes | ✘ **non implémenté** : étude ci-dessous, message explicite conservé | — |
| 11. RAW / HEIC | ✔ rawpy et pillow-heif facultatifs. RAW et HEIC affichés en mode dossier, formats non décodables signalés | repli testé, modules absents |
| 12. Dossiers | ✔ récents, favoris, changements depuis la dernière visite, surveillance passive facultative | unitaires et parcours |
| 13. Diaporama | ✔ terminé | parcours |
| 14. Export CSV / JSON | ✔ terminé, choix des champs | unitaires et parcours E |
| 15. Lecture vidéo intégrée | ✘ **non implémenté** : GStreamer intégré à Tk jugé trop fragile sous Wayland. Le lecteur système (bouton *Ouvrir*) reste la solution | — |

**Transversal** :
- index des métadonnées incrémental, reprenable et annulable ;
- surveillance des disques externes, avec reconnexion automatique (parcours D) ;
- diagnostic (menu Aide et `--diagnostic`) ;
- nettoyage des caches obsolètes ;
- migrations des bases locales avec sauvegarde.

**Étude Photos 5 et suivantes** :
- Le schéma de `Photos.sqlite` (Core Data : `ZASSET` ou `ZGENERICASSET`, tables de jointure
  `Z_xxASSETS` numérotées selon la version) change d'une version de macOS à l'autre.
- Les originaux se trouvent dans `originals/` (Photos 5 et suivantes), avec les dérivés dans
  `resources/derivatives`.
- Sans bibliothèque réelle de test, une prise en charge présenterait des données non vérifiées.
  Le message actuel est donc conservé.
- La voie proposée : une couche d'accès qui détecte les noms de tables par introspection, à
  valider sur des bibliothèques réelles de chaque génération.

## C. Sécurité et intégrité des sources

Voir [guide-securite-donnees.md](guide-securite-donnees.md). Le contrôle par empreintes porte sur
toutes les sources, dans tous les parcours : **aucune modification**.

## D. Performances

Voir [rapport-tests.md](rapport-tests.md). Avec 100 000 photos :
- index en 9 s ;
- page en 5 ms ;
- recherche avancée en 0,3 s ;
- statistiques en 1,1 s ;
- regroupement des doublons en 10 s.

La première page s'affiche en 2 s, quelle que soit la taille. La grille reste paginée par 100,
ce qui borne la mémoire.

## E. Compatibilité testée

- Bibliothèques iPhoto au format AlbumData.xml, synthétiques.
- Dossiers JPEG.
- Bases Apple synthétiques au format Photos 1 à 4 et iPhoto (`Library.apdb`, `Faces.db`).
- Debian 13 et Tk 8.6.16.
- **Non testé** sur des bibliothèques réelles pendant cette mission.

## F. Tests

| Total | Réussis | Échoués | Non exécutés |
|---|---|---|---|
| 139 (25 + 47 + 67) et `--verifier` | 139 | 0 | rawpy, pillow-heif, bibliothèques réelles (voir rapport-tests) |

## G. Fonctionnalités non terminées

- Photos 5 et suivantes.
- Lecture vidéo intégrée.
- Géocodage hors ligne (noms de lieux sans données Apple).
- Choix de l'ordre aléatoire du diaporama dans l'interface : seulement par `config.json`.
- Indexation incrémentale de l'index des photos lui-même : il est reconstruit en entier, 9 s pour 100 000 photos.

## H. Installation et utilisation

```
sudo apt install ./iphotor_2.0.0_all.deb
```

Le paquet installe `iphotorlib` dans `/usr/share/iphotor/`. Aucune nouvelle dépendance
obligatoire. Facultatifs : `python3-pillow-heif`, `rawpy` (pip), `libimage-exiftool-perl`.

En utilisation sans paquet, garder le dossier `iphotorlib/` à côté de `iphotor.py`.

Tests : `./tests/run_tests.sh`.

## I. Conclusion

La version 2.0.0 est **stable sur les données de démonstration** : tous les tests passent et la
lecture seule est vérifiée. Elle est **prête à être testée en conditions réelles**. La priorité
est d'essayer la bibliothèque réelle de l'utilisateur, en particulier l'import des informations
Apple, qui est expérimental.
