#!/usr/bin/env python3
"""Ce que consomment les agents du plugin, comparé au reste de l'usage de Claude Code.

Lit les transcripts de toutes les sessions (~/.claude/projects, gardés 30 jours) et chiffre chaque requête
au tarif public de l'API. Avec un forfait, ces dollars ne sont pas facturés : ils servent d'unité commune
pour comparer des modèles qui ne pèsent pas pareil, et c'est la PART du plugin dans le total qui compte.
Lecture seule.

Les tarifs ci-dessous sont ceux du 2026-11-10 (dollars par million de tokens). Ils ont été contrôlés
contre le relevé que Claude Code tient lui-même par session (entrées `cost-state` des transcripts) :
à corriger ici quand les tarifs changent.

Usage : python3 -I cout.py [début d'identifiant de session] [--jours]
"""
import collections, glob, json, os, sys, datetime

# $ par million de tokens : entrée, sortie, lecture de cache, écriture de cache 5 min, écriture 1 h
TARIFS = {
    "claude-opus-5-5":   (4.0, 20.0, 0.20, 5.0, 8.0),
    "claude-sonnet-5-5": (2.0, 10.0, 0.10, 2.5, 4.0),
    "claude-haiku-5-5":  (0.10, 0.50, 0.01, 0.125, 0.20),   # x5 au-delà de 100 000 tokens d'entrée
    "claude-fable-5-2":  (8.0, 40.0, 0.20, 10.0, 16.0),
    "claude-opus-4-8":   (5.0, 25.0, 0.50, 6.25, 10.0),
}

def cout_requete(modele, u):
    t = TARIFS.get(modele)
    if not t: return None
    entree = int(u.get("input_tokens") or 0); sortie = int(u.get("output_tokens") or 0)
    lu = int(u.get("cache_read_input_tokens") or 0); ecrit = int(u.get("cache_creation_input_tokens") or 0)
    detail = u.get("cache_creation") or {}
    e1h = int(detail.get("ephemeral_1h_input_tokens") or 0); e5m = ecrit - e1h
    facteur = 5.0 if modele == "claude-haiku-5-5" and entree + lu + ecrit > 100_000 else 1.0
    return facteur * (entree * t[0] + sortie * t[1] + lu * t[2] + e5m * t[3] + e1h * t[4]) / 1e6

def lire(transcript, depuis=0.0):
    """{message.id: (modèle, usage, instant)} d'un transcript (une réponse API = plusieurs lignes)."""
    vus = {}
    try:
        with open(transcript, encoding="utf-8", errors="ignore") as f:
            for l in f:
                if '"usage"' not in l: continue
                try: e = json.loads(l)
                except ValueError: continue
                if e.get("type") != "assistant" or e.get("isSidechain") and "subagents" not in transcript: continue
                m = e.get("message") or {}
                if m.get("id") and m.get("usage") and m.get("model") and not str(m.get("model")).startswith("<"):
                    vus[m["id"]] = (m["model"], m["usage"], e.get("timestamp") or "")
    except OSError:
        pass
    return vus

def main():
    par_cat = collections.defaultdict(lambda: collections.Counter()); par_agent = collections.Counter(); par_modele = collections.defaultdict(collections.Counter)
    inconnus = collections.Counter(); premier = "9"; dernier = ""; par_session = collections.Counter(); par_jour = collections.defaultdict(collections.Counter)
    seulement = sys.argv[1] if len(sys.argv) > 1 else ""
    for principal in glob.glob(os.path.expanduser("~/.claude/projects/*/*.jsonl")):
        session = os.path.basename(principal)[:-6]
        if seulement and not session.startswith(seulement): continue
        if not seulement and ("vw-essai" in principal or "sonde-projet" in principal): continue  # essais du plugin lui-même
        sources = [("fil principal", "", principal)]
        for meta in glob.glob(principal[:-6] + "/subagents/agent-*.meta.json"):
            try: genre = json.load(open(meta)).get("agentType") or "?"
            except (OSError, ValueError): genre = "?"
            cat = "agents du plugin" if genre.startswith("verified-workflow:") else "autres sous-agents"
            sources.append((cat, genre, meta[:-len(".meta.json")] + ".jsonl"))
        for cat, genre, chemin in sources:
            for modele, usage, instant in lire(chemin).values():
                c = cout_requete(modele, usage)
                if c is None: inconnus[modele] += 1; continue
                premier = min(premier, instant or premier); dernier = max(dernier, instant)
                par_cat[cat]["cout"] += c; par_cat[cat]["requetes"] += 1
                par_cat[cat]["sortie"] += int(usage.get("output_tokens") or 0); par_cat[cat]["lu"] += int(usage.get("cache_read_input_tokens") or 0)
                par_modele[cat][modele] += c
                par_session[session] += c
                if cat == "agents du plugin": par_agent[genre.split(":", 1)[1]] += c; par_jour[instant[:10]]["plugin"] += c
                par_jour[instant[:10]]["total"] += c
    total = sum(v["cout"] for v in par_cat.values()) or 1
    print("Période couverte par les transcripts : du %s au %s" % (premier[:10], dernier[:10]))
    print("\n%-20s %12s %7s %10s %14s %16s" % ("", "équiv. API $", "part", "requêtes", "tokens produits", "relus en cache"))
    for cat in ("agents du plugin", "autres sous-agents", "fil principal"):
        v = par_cat[cat]
        print("%-20s %12.2f %6.1f%% %10d %14d %16d" % (cat, v["cout"], 100 * v["cout"] / total, v["requetes"], v["sortie"], v["lu"]))
    print("%-20s %12.2f" % ("total", total))
    print("\nAgents du plugin, par agent :")
    for agent, c in par_agent.most_common(): print("  %-18s %9.2f $" % (agent, c))
    print("\nPar modèle :")
    for cat in ("agents du plugin", "autres sous-agents", "fil principal"):
        print("  %-20s %s" % (cat, ", ".join("%s %.2f $" % (m.replace("claude-", ""), c) for m, c in par_modele[cat].most_common())))
    if inconnus: print("\nRequêtes sur un modèle sans tarif connu (non comptées) :", dict(inconnus))
    if "--jours" in sys.argv:
        print("\nPar jour (plugin / total) :")
        for jour in sorted(par_jour): print("  %s  %8.2f / %8.2f $" % (jour, par_jour[jour]["plugin"], par_jour[jour]["total"]))
    return par_session

if __name__ == "__main__":
    main()
