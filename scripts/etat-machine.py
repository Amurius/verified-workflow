#!/usr/bin/env python3
"""Relevé de la machine à l'instant présent, pour les agents de vérification et d'enquête.

Remplace les listes figées dans les consignes (projets, ports pris) : ce qui est en production, c'est ce
que Docker fait tourner maintenant. Lecture seule (docker ps/inspect, ss, /proc/meminfo).

Usage : python3 -I etat-machine.py [--json]
"""
import json
import os
import re
import shutil
import subprocess
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "hooks"))
import commun  # noqa: E402

PORTS_DEV: range = range(4100, 4200)
SEUIL_MEMOIRE_MO: int = 2000
OUTILS: tuple = ("git", "python3", "node", "npm", "bun", "uv", "docker", "rg", "pdftotext", "tailscale", "sqlite3", "psql")


def memoire() -> dict:
    valeurs: dict = {}
    try:
        with open("/proc/meminfo", encoding="utf-8") as fichier:
            for ligne in fichier:
                cle, _, reste = ligne.partition(":")
                valeurs[cle] = int(reste.split()[0]) // 1024
    except (OSError, ValueError, IndexError):
        return {}
    return {
        "disponible": valeurs.get("MemAvailable", 0), "total": valeurs.get("MemTotal", 0),
        "echange_utilise": valeurs.get("SwapTotal", 0) - valeurs.get("SwapFree", 0),
    }


def ecoutes() -> dict:
    """Ports TCP en écoute : port -> (adresse, processus)."""
    try:
        sortie: str = subprocess.run(["ss", "-ltnpH"], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    ports: dict = {}
    for ligne in sortie.splitlines():
        champs: list = ligne.split()
        if len(champs) < 4 or ":" not in champs[3]:
            continue
        adresse, _, port = champs[3].rpartition(":")
        processus = re.search(r'\(\("([^"]+)",pid=(\d+)', ligne)
        try:
            ports.setdefault(int(port), (adresse, "%s pid %s" % processus.groups() if processus else ""))
        except ValueError:
            continue
    return ports


def tailscale() -> str:
    try:
        etat: dict = json.loads(subprocess.run(["tailscale", "status", "--self", "--json"], capture_output=True, text=True, timeout=10).stdout)
        moi: dict = etat.get("Self") or {}
        return "%s (%s)" % ((moi.get("DNSName") or "").rstrip("."), ", ".join((moi.get("TailscaleIPs") or [])[:1]))
    except (OSError, ValueError, subprocess.SubprocessError):
        return ""


def chromium() -> str:
    cache: str = os.path.join(commun.HOME, ".cache", "ms-playwright")
    try:
        dossiers: list = sorted(d for d in os.listdir(cache) if d.startswith("chromium"))
    except OSError:
        return ""
    return os.path.join(cache, dossiers[-1]) if dossiers else ""


def main() -> None:
    releve: dict = commun.relever_machine()
    ram: dict = memoire()
    ports: dict = ecoutes()
    prod: set = set(releve["ports_prod"])
    dev_pris: dict = {p: ports[p] for p in ports if p in PORTS_DEV}
    libres: list = [p for p in PORTS_DEV if p not in ports]
    presents: list = [o for o in OUTILS if shutil.which(o)]
    if "--json" in sys.argv[1:]:
        print(json.dumps(dict(releve, memoire=ram, ports_dev_pris=sorted(dev_pris), ports_dev_libres=libres, outils=presents), ensure_ascii=False, indent=1))
        return
    print("# État de la machine, relevé à %s" % time.strftime("%H:%M:%S"))
    if ram:
        print("Mémoire disponible : %d Mo sur %d (échange utilisé : %d Mo)." % (ram["disponible"], ram["total"], ram["echange_utilise"]))
        if ram["disponible"] < SEUIL_MEMOIRE_MO:
            print("  Moins de %d Mo disponibles : une seule tâche lourde à la fois (instance de dev, build, navigateur)." % SEUIL_MEMOIRE_MO)

    print("\n## Production : conteneurs Docker en marche (n'y lance, n'y arrête et n'y écris rien)")
    projets: dict = {}
    for conteneur in releve["conteneurs"]:
        cle: tuple = (conteneur["projet"] or "(hors compose)", conteneur["dossier"] or "-")
        projets.setdefault(cle, {"ports": set(), "noms": []})
        projets[cle]["ports"].update(conteneur["ports"])
        projets[cle]["noms"].append(conteneur["nom"])
    if not projets:
        print("Aucun conteneur en marche (ou Docker injoignable).")
    for (projet, dossier), contenu in sorted(projets.items()):
        print("- %s | dossier %s | ports publiés : %s | conteneurs : %s" % (
            projet, dossier, ", ".join(map(str, sorted(contenu["ports"]))) or "aucun", ", ".join(sorted(contenu["noms"]))))
    print("Ports de production : %s" % (", ".join(map(str, sorted(prod))) or "aucun"))
    print("Dossiers de données de production : %s" % (", ".join(releve["donnees"]) or "aucun"))

    print("\n## Ports")
    autres: list = ["%d (%s)" % (p, ports[p][1] or ports[p][0]) for p in sorted(ports) if p not in prod and p not in PORTS_DEV]
    print("En écoute hors conteneurs : %s" % (", ".join(autres) or "aucun"))
    if dev_pris:
        print("Déjà pris entre 4100 et 4199 : %s. Ce sont les instances d'une autre vérification ou d'une autre session : n'y touche pas."
              % ", ".join("%d (%s)" % (p, dev_pris[p][1] or dev_pris[p][0]) for p in sorted(dev_pris)))
    print("Ports de dev libres : %s%s" % (", ".join(map(str, libres[:8])), " ... (%d libres)" % len(libres) if len(libres) > 8 else ""))

    print("\n## Outils")
    print("Présents : %s" % ", ".join(presents))
    absents: list = [o for o in OUTILS if o not in presents]
    if absents:
        print("Absents : %s%s" % (", ".join(absents), " (lecture de base : scripts/lire-base.py)" if {"sqlite3", "psql"} & set(absents) else ""))
    navigateur: str = chromium()
    print("Chromium headless : %s" % (navigateur or "absent de ~/.cache/ms-playwright (npx playwright install chromium-headless-shell)"))
    reseau: str = tailscale()
    if reseau:
        print("Tailscale : %s" % reseau)


if __name__ == "__main__":
    main()
