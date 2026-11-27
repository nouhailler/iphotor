"""iphotorlib — logique des fonctionnalités d'iphotor 2 (sans interface graphique).

Chaque module est indépendant de Tkinter et testable seul. Règle commune : aucune écriture
dans les bibliothèques ou dossiers sources ; toutes les données propres à iphotor vont dans
le cache (~/.cache/photoexplorer_iphoto/) ou les données utilisateur (~/.local/share/iphotor/).
"""

__all__ = ["storage", "metadata", "search", "timeline", "collections", "duplicates", "stats",
           "export", "geo", "applemeta", "folders", "deps"]
