#!/usr/bin/env bash
# Fichiers d'un projet plus récents que le fichier repère de la session : ce qui a changé depuis la
# première modification, quel qu'en soit l'auteur (outil d'édition, commande shell, autre processus).
#
#   fichiers-touches.sh <racine du projet> <fichier repère>
#
# Dans un dépôt git : fichiers suivis ou nouveaux, sans les fichiers ignorés (builds, dépendances, caches).
# Hors git : tout sauf les dossiers de dépendances, de build, de données et de journaux habituels.
# Un chemin par ligne, relatif à la racine. Lecture seule.
set -euo pipefail
racine="${1:?usage : fichiers-touches.sh <racine du projet> <fichier repère>}"
repere="${2:?usage : fichiers-touches.sh <racine du projet> <fichier repère>}"
[[ -d "$racine" ]] || { echo "ERREUR : $racine n'est pas un dossier."; exit 1; }
[[ -f "$repere" ]] || { echo "ERREUR : pas de fichier repère à $repere (aucune modification de projet notée dans cette session)."; exit 1; }
repere="$(realpath "$repere")"
cd "$racine"
if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  git ls-files -z --cached --others --exclude-standard -- . \
    | xargs -0 -r sh -c 'for f; do [ "$f" -nt "$0" ] && printf "%s\n" "$f"; done; true' "$repere" \
    | grep -v '^\.claude/' || true
else
  find . -type f -newer "$repere" \
    -not -path './.claude/*' -not -path '*/node_modules/*' -not -path '*/.next/*' -not -path '*/dist/*' \
    -not -path '*/build/*' -not -path '*/__pycache__/*' -not -path '*/.pytest_cache/*' -not -path '*/.venv/*' \
    -not -path '*/data/*' -not -path '*/logs/*' -not -path '*/.expo/*' | sed 's|^\./||'
fi
