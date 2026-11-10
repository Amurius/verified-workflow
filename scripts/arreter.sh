#!/usr/bin/env bash
# Arrête ce qui écoute sur un port de dev, et rien d'autre.
#
#   arreter.sh <port> [port...]
#   arreter.sh --commune <port>     réservé au verifier : arrête l'instance commune qu'il a lancée
#
# À utiliser à la place de `pkill -f` : un motif comme "bun server/index.ts" ou "node server.js" désigne
# aussi les processus des conteneurs de production, qui tournent sous le même utilisateur et sont visibles
# depuis l'hôte. Ce script ne touche qu'au processus qui tient le port donné, et refuse un port tenu par
# un conteneur ou par un autre utilisateur.
#
# Instance commune : le verifier lance l'instance que ses agents se partagent avec VW_INSTANCE_COMMUNE=1
# dans son environnement. Sans `--commune`, le script refuse de l'arrêter : un agent qui range derrière lui
# ne coupe pas l'instance dont les autres se servent encore.
set -uo pipefail
commune=non
if [[ "${1:-}" == "--commune" ]]; then commune=oui; shift; fi
[[ $# -ge 1 ]] || { sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//'; exit 2; }

code=0
for port in "$@"; do
  if ! [[ "$port" =~ ^[0-9]+$ ]]; then echo "REFUS : « $port » n'est pas un numéro de port."; code=2; continue; fi
  pids="$(ss -ltnpH "sport = :$port" 2>/dev/null | grep -o 'pid=[0-9]*' | cut -d= -f2 | sort -u)"
  if [[ -z "$pids" ]]; then
    if ss -ltnH "sport = :$port" 2>/dev/null | grep -q .; then
      echo "REFUS : le port $port est tenu par un processus qui n'est pas à toi (port publié par Docker : la production)."
      code=2
    else
      echo "Rien n'écoute sur le port $port."
    fi
    continue
  fi
  for pid in $pids; do
    if grep -qE 'docker|containerd|libpod' "/proc/$pid/cgroup" 2>/dev/null; then
      echo "REFUS : le port $port est tenu par un processus de conteneur (pid $pid) : c'est la production."
      code=2
      continue
    fi
    if [[ "$commune" == non ]] && tr '\0' '\n' < "/proc/$pid/environ" 2>/dev/null | grep -qx 'VW_INSTANCE_COMMUNE=1'; then
      echo "REFUS : le port $port est l'instance commune du verifier. D'autres agents s'en servent : c'est lui qui l'arrête."
      code=2
      continue
    fi
    commande="$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null | cut -c1-80)"
    groupe="$(ps -o pgid= -p "$pid" 2>/dev/null | tr -d ' ')"
    # Lancé par `setsid -f` : le processus mène son groupe, on arrête le groupe entier (lui et ses enfants)
    if [[ "$groupe" == "$pid" ]]; then kill -TERM -- "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null; else kill -TERM "$pid" 2>/dev/null; fi
    for _ in $(seq 1 25); do
      ss -ltnH "sport = :$port" 2>/dev/null | grep -q . || break
      sleep 0.2
    done
    if ss -ltnH "sport = :$port" 2>/dev/null | grep -q .; then
      if [[ "$groupe" == "$pid" ]]; then kill -KILL -- "-$pid" 2>/dev/null; else kill -KILL "$pid" 2>/dev/null; fi
      sleep 0.3
    fi
    if ss -ltnH "sport = :$port" 2>/dev/null | grep -q .; then
      echo "ERREUR : le port $port écoute encore après l'arrêt de $commande (pid $pid)."
      code=1
    else
      echo "Arrêté : $commande (pid $pid). Le port $port est libre."
    fi
  done
done
exit $code
