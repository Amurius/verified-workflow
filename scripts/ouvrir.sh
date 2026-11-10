#!/usr/bin/env bash
# Rend un résultat visuel consultable depuis le poste de l'utilisateur.
# La machine est supposée sans écran ou distante : rien ne s'ouvre ici, on donne une URL joignable depuis
# le poste, par le réseau Tailscale s'il est là (et sur lui seul), sinon par un tunnel SSH.
#
#   ouvrir.sh <url locale>          instance de dev. Si elle écoute déjà sur toutes les interfaces ou sur
#                                   l'IP Tailscale : donne l'URL directe. Si elle n'écoute que sur 127.0.0.1 :
#                                   la publie par `tailscale serve` (HTTPS, réseau Tailscale) sans la
#                                   relancer ; à défaut, donne la commande de tunnel SSH.
#   ouvrir.sh <fichier> [fichier...] sert ces fichiers, et eux seuls (IP Tailscale, sinon 127.0.0.1).
#   ouvrir.sh <dossier>             sert ce dossier entier (à ne passer que s'il ne contient rien d'autre
#                                   que ce qu'il faut montrer).
#   ouvrir.sh --liste               ce qui est ouvert en ce moment.
#   ouvrir.sh --arreter [tout]      ferme ce que cette session a ouvert ; `tout` : toutes les sessions.
#
# Tout ce que le script ouvre s'arrête seul au bout de OUVRIR_DUREE (4 h par défaut).
# Les hooks du plugin reconnaissent l'appel à ce script sur une cible comme « visuel rendu consultable ».
set -euo pipefail

ETAT="${VERIFIED_WORKFLOW_ETAT:-$HOME/.claude/plugins/data/verified-workflow-verified-workflow}"
REGISTRE="$ETAT/ouvertures.tsv"   # genre, port, pid, cible, session ; un champ vide s'écrit « - »
DUREE="${OUVRIR_DUREE:-4h}"
SESSION="${CLAUDE_CODE_SESSION_ID:--}"
MOI="$(realpath "$0")"
mkdir -p "$ETAT" && chmod 700 "$ETAT" 2>/dev/null || true
touch "$REGISTRE"

[[ $# -ge 1 ]] || { sed -n '2,17p' "$0" | sed 's/^# \{0,1\}//'; exit 2; }

ip_ts="$(tailscale ip -4 2>/dev/null | head -n1 || true)"
nom_ts="$(tailscale status --self --json 2>/dev/null | python3 -I -c 'import json,sys; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))' 2>/dev/null || true)"
hote="${nom_ts:-${ip_ts:-$(hostname)}}"

en_secondes() {
  python3 -I -c 'import re,sys; m=re.fullmatch(r"(\d+)([smhd]?)", sys.argv[1]); print(int(m.group(1))*{"":1,"s":1,"m":60,"h":3600,"d":86400}[m.group(2)] if m else 14400)' "$1"
}

# Lance une commande détachée et affiche son numéro de processus. Le numéro est écrit par le processus
# lui-même : le chercher par son nom (pgrep) pourrait désigner un autre processus dont la ligne de
# commande contient le même texte.
detacher() {  # detacher <ligne de shell>
  local temoin; temoin="$(mktemp "$ETAT/pid.XXXXXX")"
  setsid -f sh -c 'echo $$ > "$0"; '"$1" "$temoin" >/dev/null 2>&1
  local i; for i in 1 2 3 4 5 6 7 8 9 10; do [[ -s "$temoin" ]] && break; sleep 0.1; done
  cat "$temoin"; rm -f "$temoin"
}

vivant() {  # vivant <pid> <mot attendu dans sa ligne de commande>
  [[ "$1" =~ ^[0-9]+$ ]] && grep -qa -- "$2" "/proc/$1/cmdline" 2>/dev/null
}

# Retire du registre ce qui s'est arrêté seul (délai écoulé, processus tué) : une ligne périmée ne doit
# ni s'afficher, ni faire retirer plus tard une publication qui ne serait plus la nôtre
purger() {
  local g p pid cible session garde=""
  while IFS=$'\t' read -r g p pid cible session; do
    [[ -n "$g" ]] || continue
    if [[ "$g" == fichiers ]] && ! vivant "$pid" "http.server"; then
      [[ "$cible" == "$ETAT/servi/"* ]] && rm -rf "$cible"
      continue
    fi
    if [[ "$g" == publication ]] && ! vivant "$pid" "tailscale serve"; then
      continue
    fi
    garde+="$g"$'\t'"$p"$'\t'"$pid"$'\t'"$cible"$'\t'"${session:--}"$'\n'
  done < "$REGISTRE"
  printf '%s' "$garde" > "$REGISTRE"
}

retirer() {  # retirer <genre> <port> : arrête ce qui est enregistré pour ce port, et l'ôte du registre
  local genre="$1" port="$2" g p pid cible session reste=""
  while IFS=$'\t' read -r g p pid cible session; do
    [[ -n "$g" ]] || continue
    if [[ "$g" == "$genre" && "$p" == "$port" ]]; then
      if [[ "$g" == fichiers ]]; then
        vivant "$pid" "http.server" && kill "$pid" 2>/dev/null || true
        [[ "$cible" == "$ETAT/servi/"* ]] && rm -rf "$cible"
        echo "Serveur de fichiers arrêté (port $p)."
      else
        vivant "$pid" "tailscale serve" && kill "$pid" 2>/dev/null || true
        tailscale serve --https="$p" off >/dev/null 2>&1 || true
        echo "Publication Tailscale retirée (port $p)."
      fi
    else
      reste+="$g"$'\t'"$p"$'\t'"$pid"$'\t'"$cible"$'\t'"${session:--}"$'\n'
    fi
  done < "$REGISTRE"
  printf '%s' "$reste" > "$REGISTRE"
}

purger

if [[ "$1" == "--liste" ]]; then
  if [[ ! -s "$REGISTRE" ]]; then echo "Rien d'ouvert."; exit 0; fi
  while IFS=$'\t' read -r g p pid cible session; do
    [[ -n "$g" ]] || continue
    if [[ "$g" == fichiers ]]; then echo "fichiers     http://$hote:$p/   ($cible)"; else echo "publication  https://$hote:$p/  (-> $cible)"; fi
  done < "$REGISTRE"
  exit 0
fi

if [[ "$1" == "--arreter" ]]; then
  fait=non
  while IFS=$'\t' read -r g p pid cible session; do
    [[ -n "$g" ]] || continue
    if [[ "${2:-}" == tout || "$SESSION" == "-" || "$session" == "$SESSION" ]]; then
      retirer "$g" "$p"
      fait=oui
    fi
  done < <(cat "$REGISTRE")
  [[ "$fait" == oui ]] || echo "Rien à arrêter pour cette session."
  exit 0
fi

# --- URL locale : instance de dev ----------------------------------------------------------------
if [[ "$1" =~ ^(https?)://(localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1\])(:([0-9]+))?(.*)$ ]]; then
  schema="${BASH_REMATCH[1]}"
  port="${BASH_REMATCH[4]}"
  reste="${BASH_REMATCH[5]}"
  if [[ -z "$port" ]]; then
    if [[ "$schema" == https ]]; then port=443; else port=80; fi
  fi
  ecoute="$(ss -ltnH "sport = :$port" 2>/dev/null | awk '{print $4}')"
  if [[ -z "$ecoute" ]]; then
    echo "ERREUR : rien n'écoute sur le port $port."
    exit 1
  fi
  if grep -qE "^(0\.0\.0\.0|\*|\[::\]${ip_ts:+|${ip_ts//./\\.}}):$port$" <<<"$ecoute"; then
    echo "À ouvrir sur le poste : $schema://$hote:$port$reste"
    exit 0
  fi
  # N'écoute que sur 127.0.0.1 : publication sur le réseau Tailscale, sans relancer l'instance
  publie=non
  if [[ -n "$nom_ts" && "$port" -ge 1024 ]]; then
    deja="$(tailscale serve status --json 2>/dev/null | python3 -I -c 'import json,sys; print("oui" if sys.argv[1] in (json.load(sys.stdin).get("TCP") or {}) else "non")' "$port" 2>/dev/null || echo non)"
    a_nous="$(awk -F'\t' -v p="$port" '$1 == "publication" && $2 == p' "$REGISTRE")"
    if [[ "$deja" == oui && -z "$a_nous" ]]; then
      echo "Le port $port est déjà publié par Tailscale pour autre chose : je n'y touche pas."
    else
      [[ -n "$a_nous" ]] && retirer publication "$port" >/dev/null
      cible_locale="http://127.0.0.1:$port"
      [[ "$schema" == https ]] && cible_locale="https+insecure://127.0.0.1:$port"
      if tailscale serve --bg --https="$port" "$cible_locale" >/dev/null 2>&1; then
        minuteur="$(detacher "sleep $(en_secondes "$DUREE"); tailscale serve --https=$port off")"
        printf 'publication\t%s\t%s\t%s\t%s\n' "$port" "${minuteur:--}" "$cible_locale" "$SESSION" >> "$REGISTRE"
        publie=oui
      fi
    fi
  fi
  if [[ "$publie" == oui ]]; then
    echo "À ouvrir sur le poste : https://$hote:$port$reste"
    echo "Publié sur le réseau Tailscale seulement, pour $DUREE. Retrait : bash $MOI --arreter"
  else
    echo "Le port $port n'écoute que sur 127.0.0.1. Sur le poste :"
    echo "  ssh -N -L $port:127.0.0.1:$port $(whoami)@$hote"
    echo "puis ouvrir : $schema://localhost:$port$reste"
  fi
  exit 0
fi

# --- Fichiers ou dossier -------------------------------------------------------------------------
for cible in "$@"; do
  [[ -e "$cible" ]] || { echo "ERREUR : $cible n'existe pas."; exit 1; }
done
# Sans Tailscale : rien n'est exposé sur le réseau local, le poste passe par un tunnel SSH
adresse="${ip_ts:-127.0.0.1}"
noms=()
if [[ $# -eq 1 && -d "$1" ]]; then
  dossier="$(realpath "$1")"
  noms=("")
else
  # Un dossier à part, qui ne contient que des liens vers les fichiers demandés : rien d'autre n'est exposé
  dossier="$ETAT/servi/$(date +%s)-$$"
  mkdir -p "$dossier" && chmod 700 "$ETAT/servi" "$dossier"
  for cible in "$@"; do
    [[ -f "$cible" ]] || { echo "ERREUR : $cible n'est pas un fichier (un seul dossier à la fois, sans autre argument)."; rm -rf "$dossier"; exit 1; }
    nom="$(basename "$cible")"
    n=1
    while [[ -e "$dossier/$nom" ]]; do nom="$n-$(basename "$cible")"; n=$((n + 1)); done
    ln -s "$(realpath "$cible")" "$dossier/$nom"
    noms+=("$nom")
  done
fi
port="$(python3 -I -c 'import socket; s=socket.socket(); s.bind(("",0)); print(s.getsockname()[1])')"
pid="$(detacher "exec timeout $(printf '%q' "$DUREE") python3 -I -m http.server $port --bind $adresse --directory $(printf '%q' "$dossier")")"
sleep 0.4
if ! vivant "$pid" "http.server"; then
  echo "ERREUR : le serveur de fichiers n'a pas démarré."
  exit 1
fi
printf 'fichiers\t%s\t%s\t%s\t%s\n' "$port" "$pid" "$dossier" "$SESSION" >> "$REGISTRE"
if [[ -n "$ip_ts" ]]; then
  for nom in "${noms[@]}"; do
    echo "À ouvrir sur le poste : http://$hote:$port/$(python3 -I -c 'import sys,urllib.parse; print(urllib.parse.quote(sys.argv[1]))' "$nom")"
  done
  echo "Servi sur le réseau Tailscale seulement, pour $DUREE. Arrêt : bash $MOI --arreter"
else
  echo "Tailscale absent : servi sur 127.0.0.1 seulement, pour $DUREE. Sur le poste :"
  echo "  ssh -N -L $port:127.0.0.1:$port $(whoami)@$hote"
  for nom in "${noms[@]}"; do
    echo "puis ouvrir : http://localhost:$port/$(python3 -I -c 'import sys,urllib.parse; print(urllib.parse.quote(sys.argv[1]))' "$nom")"
  done
  echo "Arrêt : bash $MOI --arreter"
fi
