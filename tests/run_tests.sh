#!/bin/sh
# Suite complète des tests d'iphotor : ./tests/run_tests.sh
# 1. compilation  2. tests unitaires (iphotorlib)  3. non-régression 1.x  4. parcours V2
# Les tests d'interface nécessitent un affichage (ils sont ignorés sinon).
set -e
cd "$(dirname "$0")/.."
echo "== Compilation"
python3 -m py_compile iphotor.py iphotorlib/*.py tests/*.py
echo "== Tests unitaires"
python3 -m unittest discover -s tests -p "test_lib.py" -v 2>&1 | tail -n 4
echo "== Non-régression (interface 1.x)"
python3 tests/regression_gui.py | grep -E "ÉCHEC|RÉSULTAT"
echo "== Parcours utilisateurs V2"
python3 tests/test_gui_v2.py 2>&1 | grep -E "ÉCHEC|RÉSULTAT|^OK$|FAILED"
echo "== Cohérence de l'index (démo)"
TMP=$(mktemp -d)
python3 tests/fixtures.py "$TMP/src" > /dev/null
XDG_CACHE_HOME="$TMP/cache" python3 iphotor.py --verifier -b "$TMP/src/iPhoto Library" | tail -n 1
rm -rf "$TMP"
