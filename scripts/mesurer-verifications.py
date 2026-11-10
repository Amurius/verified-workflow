#!/usr/bin/env python3
"""Bilan des vérifications passées : ce qu'elles trouvent, ce qu'elles coûtent, et si les hooks suivent.

Lit les transcripts de sous-agents de toutes les sessions (~/.claude/projects/*/*/subagents/, gardés
30 jours par Claude Code) et les journaux des hooks (dossier d'état du plugin). Lecture seule.

- verdicts rendus par le `verifier`, et vérifications terminées sans verdict ;
- durée du `verifier`, de `verif-preuve` et de `verif-regression` (médiane, 9e décile) ;
- tokens par modèle, en moyenne par vérification ;
- temps passé en `sleep` par les agents de vérification (les consignes demandent scripts/attendre.sh) ;
- côté hooks (sessions 1.5 et suivantes) : blocages par motif, refus des garde-fous, archives, et verdicts
  que le hook de fin d'agent n'a pas vus alors que le transcript de l'agent en contient un.

Usage : python3 -I mesurer-verifications.py [--jours 30]
"""
import collections
import datetime
import glob
import json
import os
import re
import statistics
import sys
import time

PROJETS: str = os.path.join(os.path.expanduser("~"), ".claude", "projects")
ETAT: str = os.environ.get("VERIFIED_WORKFLOW_ETAT") or os.path.join(
    os.path.expanduser("~"), ".claude", "plugins", "data", "verified-workflow-verified-workflow")
VERDICT = re.compile(r"^[\s*#`>_]*VERDICT[\s*`_]*:[\s*`_]*(.+?)[\s*`_]*$", re.IGNORECASE | re.MULTILINE)
TYPES: tuple = ("verifier", "verif-preuve", "verif-regression")


def instant(texte: str) -> float:
    return datetime.datetime.fromisoformat(texte.replace("Z", "+00:00")).timestamp()


def lire_agent(transcript: str) -> dict:
    """Début, fin, tokens par modèle, verdict du dernier message et temps de sleep d'un sous-agent."""
    debut = fin = 0.0
    usages: dict = {}        # message.id -> (modèle, usage)
    dernier_texte: str = ""
    appels: dict = {}        # tool_use_id -> (instant, commande)
    sommeil: float = 0.0
    with open(transcript, encoding="utf-8", errors="ignore") as fichier:
        for ligne in fichier:
            try:
                entree: dict = json.loads(ligne)
            except ValueError:
                continue
            if not entree.get("timestamp") or entree.get("type") not in ("assistant", "user"):
                continue
            moment: float = instant(entree["timestamp"])
            debut = debut or moment
            fin = moment
            message: dict = entree.get("message") or {}
            contenu = message.get("content")
            if entree["type"] == "assistant":
                if message.get("id") and message.get("usage"):
                    usages[message["id"]] = (message.get("model") or "?", message["usage"])
                for bloc in contenu if isinstance(contenu, list) else []:
                    if not isinstance(bloc, dict):
                        continue
                    if bloc.get("type") == "text" and bloc.get("text", "").lstrip("*#`>_ \n").upper().startswith("VERDICT"):
                        dernier_texte = bloc["text"]  # le dernier rapport rendu, même si un message l'a suivi
                    elif bloc.get("type") == "tool_use" and bloc.get("name") == "Bash":
                        appels[bloc.get("id")] = (moment, str((bloc.get("input") or {}).get("command") or ""))
            else:
                for bloc in contenu if isinstance(contenu, list) else []:
                    if isinstance(bloc, dict) and bloc.get("type") == "tool_result" and bloc.get("tool_use_id") in appels:
                        depart, commande = appels.pop(bloc["tool_use_id"])
                        if re.search(r"(^|[;&|\n]\s*)sleep\s+\d", commande):
                            sommeil += moment - depart
    tokens: dict = collections.defaultdict(lambda: collections.Counter())
    for modele, usage in usages.values():
        for cle in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
            tokens[modele][cle] += int(usage.get(cle) or 0)
    trouve = VERDICT.search(dernier_texte)
    verdict: str = ""
    if trouve:
        brut: str = trouve.group(1).upper().replace("É", "E")
        verdict = ("VALIDÉ SOUS RÉSERVE" if "RESERVE" in brut else "VALIDÉ") if brut.startswith("VALID") else (
            "ÉCHEC" if brut.startswith("ECHEC") else "NON VÉRIFIABLE" if brut.startswith("NON") else "ILLISIBLE")
    return {"debut": debut, "duree": fin - debut if debut else 0.0, "tokens": tokens, "verdict": verdict, "sommeil": sommeil}


def ligne_durees(nom: str, valeurs: list) -> str:
    if not valeurs:
        return "  %-18s -" % nom
    triees: list = sorted(valeurs)
    return "  %-18s médiane %5.1f min, 9e décile %5.1f min, max %6.1f min (n=%d)" % (
        nom, statistics.median(triees) / 60, triees[min(len(triees) - 1, int(len(triees) * 0.9))] / 60, triees[-1] / 60, len(triees))


def main() -> None:
    jours: int = 30
    if "--jours" in sys.argv[1:]:
        jours = int(sys.argv[sys.argv.index("--jours") + 1])
    depuis: float = time.time() - jours * 86400
    durees: dict = collections.defaultdict(list)
    verdicts: collections.Counter = collections.Counter()
    tokens: dict = collections.defaultdict(collections.Counter)
    sommeil: float = 0.0
    verifications: int = 0
    verdict_par_agent: dict = {}
    for meta in glob.glob(os.path.join(PROJETS, "*", "*", "subagents", "agent-*.meta.json")):
        if "vw-essai" in meta or "sonde-projet" in meta:
            continue  # sessions d'essai du plugin lui-même
        try:
            with open(meta, encoding="utf-8") as fichier:
                genre: str = (json.load(fichier).get("agentType") or "").rsplit(":", 1)[-1]
        except (OSError, ValueError):
            continue
        transcript: str = meta[: -len(".meta.json")] + ".jsonl"
        if genre not in TYPES or not os.path.exists(transcript):
            continue
        agent: dict = lire_agent(transcript)
        if agent["debut"] < depuis:
            continue
        durees[genre].append(agent["duree"])
        sommeil += agent["sommeil"]
        for modele, compte in agent["tokens"].items():
            tokens[modele].update(compte)
        if genre == "verifier":
            verifications += 1
            verdicts[agent["verdict"] or "sans verdict (en cours ou interrompu)"] += 1
            verdict_par_agent[os.path.basename(meta)[len("agent-"):-len(".meta.json")]] = agent["verdict"]

    print("Vérifications des %d derniers jours : %d" % (jours, verifications))
    for verdict, nombre in verdicts.most_common():
        print("  %-38s %3d" % (verdict, nombre))
    print("Durées")
    for genre in TYPES:
        print(ligne_durees(genre, durees[genre]))
    print("Tokens par modèle, en moyenne par vérification")
    for modele, compte in sorted(tokens.items()):
        print("  %-22s produits %9d | relus en cache %11d | écrits en cache %9d | entrée %7d" % (
            modele, compte["output_tokens"] // max(verifications, 1), compte["cache_read_input_tokens"] // max(verifications, 1),
            compte["cache_creation_input_tokens"] // max(verifications, 1), compte["input_tokens"] // max(verifications, 1)))
    print("Temps passé en sleep par les agents de vérification : %.0f min au total" % (sommeil / 60))

    # Côté hooks : journaux des sessions (version 1.5 et suivantes)
    evenements: collections.Counter = collections.Counter()
    vus: dict = {}
    for journal in glob.glob(os.path.join(ETAT, "sessions", "*", "journal.jsonl")):
        with open(journal, encoding="utf-8", errors="ignore") as fichier:
            for ligne in fichier:
                try:
                    e: dict = json.loads(ligne)
                except ValueError:
                    continue
                if e.get("t", 0) < depuis:
                    continue
                if e.get("ev") == "blocage":
                    evenements["blocage : " + str(e.get("motif"))] += 1
                elif e.get("ev") in ("refus", "archive", "reprise"):
                    evenements[e["ev"]] += 1
                elif e.get("ev") == "agent_fin" and e.get("verdict"):
                    vus[e.get("agent_id")] = e["verdict"]
                elif e.get("ev") == "agent_debut" and str(e.get("agent_type") or "").endswith("verifier"):
                    vus.setdefault(e.get("agent_id"), "")
    print("Hooks (journaux du dossier d'état)")
    if not evenements and not vus:
        print("  aucun journal sur la période : aucune session n'a encore tourné avec les hooks 1.5")
        return
    for cle, nombre in sorted(evenements.items()):
        print("  %-28s %3d" % (cle, nombre))
    manques: list = [a for a, verdict in vus.items() if not verdict and verdict_par_agent.get(a)]
    print("  verdicts vus par le hook de fin d'agent : %d sur %d verifiers suivis" % (sum(1 for v in vus.values() if v), len(vus)))
    if manques:
        print("  ATTENTION : %d verdict(s) présents dans le transcript de l'agent mais pas dans le journal (%s). "
              "Le format a peut-être changé : relance hooks/test_hooks.py et regarde erreurs.log." % (len(manques), ", ".join(manques[:5])))


if __name__ == "__main__":
    main()
