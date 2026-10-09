#!/usr/bin/env bash
# Garde pré-commit légère : aucune donnée privée ne doit être suivie par Git (INV-11).
#
# Échoue (code 1) si les fichiers suivis (git ls-files) contiennent :
#   - un fichier *.csv hors backend/tests/fixtures/
#   - un fichier *.sqlite* (sqlite, sqlite3, sqlite-wal, sqlite-shm...)
#   - un fichier .env (ou .env.*), sauf .env.example
#   - tmp.zip
#   - un chemin sous csv_references/
#   - un IBAN français réel (motif FR76 complet, 27 caractères) dans un fichier texte suivi
#   - une suite de 8 chiffres consécutifs ou plus dans un fichier .csv suivi
#
# Les fixtures synthétiques ne doivent donc contenir ni IBAN FR76 complet ni numéro de 8+ chiffres.
# Le script n'affiche JAMAIS le contenu des lignes en cause : uniquement fichier et numéro de ligne.
#
# Usage : scripts/check_no_private_data.sh [--with-untracked]
#   --with-untracked : inspecte aussi les fichiers non suivis et non ignorés (avant `git add`).
# Codes de sortie : 0 = OK, 1 = violation, 2 = erreur d'usage / hors dépôt Git.

set -u
set -o pipefail

with_untracked=0
for arg in "$@"; do
  case "$arg" in
    --with-untracked) with_untracked=1 ;;
    -h|--help) sed -n '2,17p' "$0"; exit 0 ;;
    *) echo "Option inconnue : $arg" >&2; exit 2 ;;
  esac
done

root="$(git rev-parse --show-toplevel 2>/dev/null)" || { echo "ERREUR : hors d'un dépôt Git." >&2; exit 2; }
cd "$root" || exit 2

# Motif IBAN FR76 : FR76 + 23 caractères alphanumériques, espaces facultatifs entre groupes de 4.
iban_re='FR76 ?([0-9A-Z]{4} ?){5}[0-9A-Z]{3}'
digits_re='[0-9]{8,}'

violations=0
report() {
  echo "VIOLATION : $1" >&2
  violations=$((violations + 1))
}

list_files() {
  git ls-files -z
  if [ "$with_untracked" -eq 1 ]; then
    git ls-files -z --others --exclude-standard
  fi
}

checked=0
while IFS= read -r -d '' f; do
  [ -e "$f" ] || continue   # fichier suivi mais supprimé de l'arbre de travail
  checked=$((checked + 1))
  lower="${f,,}"
  base="${lower##*/}"

  # --- Contrôles sur les chemins ---
  case "$lower" in
    csv_references/*|*/csv_references/*) report "chemin csv_references/ suivi : $f" ;;
  esac
  case "$lower" in
    *.csv)
      case "$f" in
        backend/tests/fixtures/*) ;;
        *) report "fichier .csv hors backend/tests/fixtures/ : $f" ;;
      esac
      ;;
  esac
  case "$base" in
    *.sqlite|*.sqlite3|*.sqlite-wal|*.sqlite-shm|*.sqlite-journal|*.sqlite[0-9]*|*.sqlite-*)
      report "base SQLite suivie : $f" ;;
  esac
  case "$base" in
    .env.example) ;;
    .env|.env.*) report "fichier d'environnement suivi : $f" ;;
  esac
  [ "$base" = "tmp.zip" ] && report "archive tmp.zip suivie : $f"

  # --- Contrôles sur le contenu (fichiers texte uniquement ; -I ignore les binaires) ---
  [ "$f" = "scripts/check_no_private_data.sh" ] && continue
  if hits="$(grep -InE -e "$iban_re" -- "$f" 2>/dev/null | cut -d: -f1)" && [ -n "$hits" ]; then
    while IFS= read -r ln; do
      report "motif IBAN FR76 dans $f (ligne $ln)"
    done <<< "$hits"
  fi
  case "$lower" in
    *.csv)
      if hits="$(grep -InE -e "$digits_re" -- "$f" 2>/dev/null | cut -d: -f1)" && [ -n "$hits" ]; then
        while IFS= read -r ln; do
          report "suite de 8+ chiffres consécutifs dans $f (ligne $ln)"
        done <<< "$hits"
      fi
      ;;
  esac
done < <(list_files)

if [ "$violations" -gt 0 ]; then
  echo "ECHEC : $violations violation(s) sur $checked fichier(s) inspecté(s). Aucun contenu affiché par prudence." >&2
  exit 1
fi

echo "OK : aucune donnée privée détectée ($checked fichier(s) inspecté(s))."
exit 0
