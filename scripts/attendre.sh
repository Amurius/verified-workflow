#!/usr/bin/env bash
# Attendre un événement précis au lieu d'un `sleep` au jugé. Rend la main dès que l'événement arrive,
# ou après le délai avec un code de retour 1.
#
#   attendre.sh port  <port> [délai]            un port local écoute
#   attendre.sh libre <port> [délai]            un port local est libéré
#   attendre.sh url   <url locale> [délai]      l'URL répond (affiche le statut HTTP)
#   attendre.sh ligne <fichier> <regex> [délai] une ligne du fichier correspond (affiche la ligne)
#   attendre.sh pid   <pid> [délai]             le processus est terminé
#
# Délai en secondes, 60 par défaut. L'outil Bash coupe une commande au bout de 120 s par défaut : pour un
# délai plus long, passe un `timeout` plus grand à l'outil (600 s au plus) ou lance la commande longue en
# arrière-plan et attends sa notification.
set -uo pipefail

mode="${1:-}"
usage() { sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'; exit 2; }
[[ -n "$mode" && $# -ge 2 ]] || usage

fin_dans() {  # délai en secondes ; « 30s » est accepté, toute autre forme vaut 60
  local d="${1:-60}"; d="${d%s}"
  [[ "$d" =~ ^[0-9]+$ ]] || d=60
  echo $(( $(date +%s) + d ))
}
ecoute() { ss -ltnH "sport = :$1" 2>/dev/null | grep -q .; }

case "$mode" in
  port|libre)
    port="$2"; limite="$(fin_dans "${3:-60}")"
    while :; do
      if ecoute "$port"; then etat=port; else etat=libre; fi
      if [[ "$etat" == "$mode" ]]; then
        [[ "$mode" == port ]] && echo "Le port $port écoute." || echo "Le port $port est libre."
        exit 0
      fi
      (( $(date +%s) >= limite )) && { echo "DÉLAI DÉPASSÉ : le port $port n'est pas dans l'état « $mode »."; exit 1; }
      sleep 0.3
    done ;;
  url)
    url="$2"; limite="$(fin_dans "${3:-60}")"
    [[ "$url" =~ ^https?://(localhost|127\.0\.0\.1|\[::1\])(:[0-9]+)?(/.*)?$ ]] || { echo "REFUS : seules les URL locales (localhost, 127.0.0.1) sont permises."; exit 2; }
    while :; do
      statut="$(curl -s -o /dev/null -m 5 -w '%{http_code}' "$url" 2>/dev/null || true)"
      if [[ -n "$statut" && "$statut" != 000 ]]; then echo "Réponse HTTP $statut de $url"; exit 0; fi
      (( $(date +%s) >= limite )) && { echo "DÉLAI DÉPASSÉ : $url ne répond pas."; exit 1; }
      sleep 0.5
    done ;;
  ligne)
    [[ $# -ge 3 ]] || usage
    fichier="$2"; motif="$3"; limite="$(fin_dans "${4:-60}")"
    while :; do
      if [[ -f "$fichier" ]] && trouve="$(grep -m1 -E -- "$motif" "$fichier" 2>/dev/null)"; then echo "$trouve"; exit 0; fi
      if (( $(date +%s) >= limite )); then
        echo "DÉLAI DÉPASSÉ : aucune ligne de $fichier ne correspond à « $motif ». Fin du fichier :"
        tail -n 15 "$fichier" 2>/dev/null
        exit 1
      fi
      sleep 0.5
    done ;;
  pid)
    pid="$2"; limite="$(fin_dans "${3:-60}")"
    while kill -0 "$pid" 2>/dev/null; do
      (( $(date +%s) >= limite )) && { echo "DÉLAI DÉPASSÉ : le processus $pid tourne encore."; exit 1; }
      sleep 0.5
    done
    echo "Le processus $pid est terminé."; exit 0 ;;
  *) usage ;;
esac
