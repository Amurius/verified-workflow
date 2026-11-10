#!/usr/bin/env python3
"""Point d'entrée unique des hooks de verified-workflow 1.5 (voir hooks.json).

Chaque hook écrit ce qu'il voit dans le journal de la session ; le hook de fin de tour décide à partir
de ce journal, sans analyser le transcript (dont le format n'est pas documenté et a déjà changé).

  SessionStart  ménage des archives et des sessions anciennes.
  PreToolUse    Bash, Edit, Write... : refus, pour tout le monde, d'un kill qui atteindrait un conteneur de
                production ; garde-fous pour les agents du plugin ; repère et copie d'avant modification
                pour un fichier de projet ; note l'appel à scripts/ouvrir.sh.
  PostToolUse   Edit, Write... : note la modification réussie (auteur, fichier de projet ou non).
  SubagentStart note le lancement d'un agent (dont le verifier).
  SubagentStop  note la fin d'un agent ; pour le verifier, lit le verdict dans son dernier message.
  Stop          bloque si des modifications de projet plus récentes que le dernier signalement n'ont pas
                été suivies d'un verifier et que plus aucun sous-agent ne travaille ; une fois si le
                verifier s'est terminé sans verdict ; une fois si un verdict validé avec composante
                visuelle n'a pas été rendu consultable. Archive les copies d'avant quand le dernier
                verifier a validé.

Une erreur interne ne bloque jamais un outil : elle part dans erreurs.log.
"""
import datetime
import difflib
import json
import os
import re
import shutil
import sys
import time
import traceback

import commun
import garde

# Tolère la mise en forme Markdown autour du mot-clé ou de la valeur (**VERDICT : VALIDÉ**, VERDICT : **VALIDÉ**)
VERDICT_LIGNE = re.compile(r"^[\s*#`>_]*VERDICT[\s*`_]*:[\s*`_]*(.+?)[\s*`_]*$", re.IGNORECASE)
VISUEL_LIGNE = re.compile(r"^[\s*#`>_]*VISUEL[\s*`_]*:[\s*`_]*(OUI|NON)\b", re.IGNORECASE)
RESULTAT = re.compile(r"<result>(.*?)(?:</result>|\Z)", re.DOTALL)
NOTE = re.compile(r"<note>(.*?)</note>", re.DOTALL)
MAX_FICHIERS_LISTES: int = 25
# Filet de sécurité : pas plus de blocages que ça entre deux fins de tour normales
MAX_BLOCAGES_PAR_RELANCE: int = 3
# Un sous-agent qui a rendu la main en laissant une commande en arrière-plan est tenu pour « encore au
# travail » pendant ce délai, tant qu'une commande en arrière-plan tourne
ATTENTE_ARRIERE_PLAN_S: int = 1200
DEFAVORABLES: tuple = ("ÉCHEC", "NON VÉRIFIABLE", "ILLISIBLE")
PLUGIN: str = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def lire_verdict(texte: object) -> dict:
    """Verdict d'un rapport du verifier, ou {} : sa première ligne non vide doit être `VERDICT : ...`."""
    lignes: list = [ligne for ligne in str(texte or "").splitlines() if ligne.strip()]
    if not lignes:
        return {}
    trouve = VERDICT_LIGNE.match(lignes[0])
    if not trouve:
        return {}
    brut: str = " ".join(trouve.group(1).strip().upper().replace("É", "E").replace("È", "E").split())
    if brut in ("VALIDE", "VALIDEE"):
        verdict: str = "VALIDÉ"
    elif brut.startswith("VALIDE SOUS RESERVE") or brut.startswith("VALIDEE SOUS RESERVE"):
        verdict = "VALIDÉ SOUS RÉSERVE"
    elif brut == "ECHEC":
        verdict = "ÉCHEC"
    elif brut.startswith("NON VERIFIABLE"):
        verdict = "NON VÉRIFIABLE"
    else:
        verdict = "ILLISIBLE"  # jamais lu comme une validation
    visuel: object = None
    for ligne in lignes[1:4]:
        vu = VISUEL_LIGNE.match(ligne)
        if vu:
            visuel = vu.group(1).upper() == "OUI"
            break
    return {"verdict": verdict, "visuel": visuel}


def rapports_du_transcript(transcript: object, agent_id: str) -> list:
    """Ce que le fil principal a reçu de cet agent : [(instant, texte du résultat)].

    Secours quand SubagentStop n'a pas livré de verdict : le rapport d'un agent en arrière-plan revient
    dans une notification qui porte `<task-id>` et `<result>`, comme message ou par la file d'attente
    quand elle arrive en plein tour. Une notification intermédiaire (l'agent attend son propre travail en
    arrière-plan et reprendra seul) n'est pas un rapport. Le fichier est lu ligne à ligne et seules les
    lignes qui nomment l'agent sont décodées.
    """
    rapports: list = []
    if not transcript or not agent_id or not os.path.isfile(str(transcript)):
        return rapports
    balise: str = "<task-id>%s</task-id>" % agent_id
    with open(str(transcript), encoding="utf-8", errors="replace") as fichier:
        for ligne in fichier:
            if agent_id not in ligne:
                continue
            try:
                entree = json.loads(ligne)
            except ValueError:
                continue
            if not isinstance(entree, dict) or entree.get("isSidechain"):
                continue
            if entree.get("type") == "user":
                contenu = (entree.get("message") or {}).get("content")
                if isinstance(contenu, list):
                    contenu = "\n".join(b.get("text", "") for b in contenu if isinstance(b, dict) and b.get("type") == "text")
            elif entree.get("type") == "queue-operation":
                contenu = entree.get("content")
            else:
                continue
            if not isinstance(contenu, str) or balise not in contenu:
                continue
            note = NOTE.search(contenu)
            if note and ("interim" in note.group(1) or "still running" in note.group(1)):
                continue
            resultat = RESULTAT.search(contenu)
            try:
                instant: float = datetime.datetime.fromisoformat(str(entree.get("timestamp")).replace("Z", "+00:00")).timestamp()
            except ValueError:
                instant = 0.0
            rapport: tuple = (instant, resultat.group(1).strip() if resultat else "")
            if not rapports or rapports[-1][1] != rapport[1]:  # la file note la même notification plusieurs fois
                rapports.append(rapport)
    return rapports


def shells_en_cours(entree: dict) -> int:
    """Commandes en arrière-plan encore en cours (0 si Claude Code ne les donne pas)."""
    taches = entree.get("background_tasks")
    if not isinstance(taches, list):
        return 0
    return sum(1 for t in taches if isinstance(t, dict) and t.get("type") == "shell" and t.get("status") == "running")


def agents_en_cours(entree: dict, journal: list, maintenant: float) -> list:
    """Sous-agents qui travaillent encore : [(identifiant, type)]."""
    taches = entree.get("background_tasks")
    if isinstance(taches, list):
        return [
            (tache.get("id") or "", tache.get("agent_type") or "")
            for tache in taches
            if isinstance(tache, dict) and tache.get("type") == "subagent" and tache.get("status") == "running"
        ]
    # Version de Claude Code sans `background_tasks` : démarré ou actif après sa dernière fin, depuis moins d'une heure
    etat: dict = {}
    for evenement in journal:
        identifiant = evenement.get("agent_id")
        if not identifiant:
            continue
        if evenement["ev"] in ("agent_debut", "modif"):
            etat[identifiant] = (True, evenement["t"], evenement.get("agent_type") or "")
        elif evenement["ev"] == "agent_fin":
            etat[identifiant] = (False, evenement["t"], evenement.get("agent_type") or "")
    return [(i, genre) for i, (actif, instant, genre) in etat.items() if actif and maintenant - instant < 3600]


def auteur_en_attente(journal: list, modifs: list, shells: int, maintenant: float) -> bool:
    """Un sous-agent auteur de ces modifications a rendu la main en laissant une commande en arrière-plan,
    et une commande en arrière-plan tourne encore : il reprendra seul, son travail n'est pas fini.
    (Un agent dans cet état n'apparaît plus parmi les sous-agents en cours.)"""
    if not shells:
        return False
    auteurs: set = {m.get("agent_id") for m in modifs if m.get("agent_id")}
    dernieres_fins: dict = {}
    for evenement in journal:
        if evenement["ev"] == "agent_fin" and evenement.get("agent_id") in auteurs:
            dernieres_fins[evenement["agent_id"]] = evenement
    return any(fin.get("shells") and maintenant - fin["t"] < ATTENTE_ARRIERE_PLAN_S for fin in dernieres_fins.values())


def appelle_ouvrir(commande: str) -> bool:
    """La commande lance scripts/ouvrir.sh sur une cible (pas `--arreter`, `--liste`, ni un simple `cat`)."""
    for segment in garde.segments(commande):
        analyse: dict = garde.analyser(segment)
        mots: list = [analyse["nom"]] + analyse["arguments"]
        if mots[0] in ("bash", "sh"):
            mots = mots[1:]
        if mots and os.path.basename(mots[0]) == "ouvrir.sh" and mots[1:] and not mots[1].startswith("-"):
            return True
    return False


def decider(journal: list, entree: dict, maintenant: float, rapports, reglages: object = None) -> dict:
    """Ce que le hook de fin de tour doit faire. Fonction pure : `rapports(agent_id)` donne ce que le fil
    principal a reçu de l'agent (lu dans le transcript seulement si le journal n'a pas de verdict).

    Renvoie {"archiver": bool, "verifier": id du dernier verifier, "blocage": (motif, détails) ou None,
    "note": texte à montrer à l'utilisateur sans bloquer, ou ""}.

    Une modification est couverte par un verifier LANCÉ après elle. Une correction faite après un ÉCHEC
    demande donc un nouveau verifier : reprendre l'ancien par message ne la couvre pas.
    """
    decision: dict = {"archiver": False, "verifier": "", "blocage": None, "note": ""}
    reglages = reglages if isinstance(reglages, dict) else commun.REGLAGES_PAR_DEFAUT
    lancements: list = [e for e in journal if e["ev"] == "agent_debut" and commun.est_verifier(e.get("agent_type"))]
    dernier: dict = lancements[-1] if lancements else {}
    identifiant: str = dernier.get("agent_id") or ""
    instant_verifier: float = dernier.get("t", 0.0)
    decision["verifier"] = identifiant
    non_verifiees: list = [
        e for e in journal
        if e["ev"] == "modif" and e.get("projet") and not commun.de_la_verification(e.get("agent_type"))
        and e["t"] > instant_verifier
    ]
    en_cours: list = agents_en_cours(entree, journal, maintenant)
    actifs: set = {i for i, _ in en_cours}
    shells: int = shells_en_cours(entree)
    blocages: list = [e for e in journal if e["ev"] == "blocage"]

    def verdict_de(agent_id: str, depuis: float) -> dict:
        rendus: list = [
            e for e in journal
            if e["ev"] == "agent_fin" and e.get("agent_id") == agent_id and e.get("verdict") and e["t"] >= depuis
        ]
        return {"verdict": rendus[-1]["verdict"], "visuel": rendus[-1].get("visuel"), "t": rendus[-1]["t"]} if rendus else {}

    verdict: dict = {}
    dernier_message: object = None  # le verifier a rendu la main sans verdict et n'attend plus rien : ce qu'il a dit
    if dernier:
        verdict = verdict_de(identifiant, instant_verifier)
        if not verdict and identifiant not in actifs:
            recus: list = [(t, texte) for t, texte in rapports(identifiant) if t >= instant_verifier]
            for instant, texte in recus:
                lu: dict = lire_verdict(texte)
                if lu:
                    verdict = dict(lu, t=instant)
            if not verdict and not en_cours and not shells:
                fins: list = [e for e in journal if e["ev"] == "agent_fin" and e.get("agent_id") == identifiant and e["t"] >= instant_verifier]
                if fins:
                    dernier_message = fins[-1].get("apercu") or ""
                elif recus:
                    dernier_message = recus[-1][1]

    # Autres verifiers de la même génération (lancés depuis la dernière archive) : tant que l'un d'eux
    # tourne encore, ou a rendu un verdict défavorable pendant ou après le dernier lancé, rien n'est validé
    generation: float = max((e["t"] for e in journal if e["ev"] == "archive"), default=0.0)
    contredit: bool = False
    for autre in {e.get("agent_id"): e for e in lancements if e["t"] > generation and e.get("agent_id") != identifiant}.values():
        rendu: dict = verdict_de(autre.get("agent_id") or "", autre["t"])
        if autre.get("agent_id") in actifs or (rendu and rendu["verdict"] in DEFAVORABLES and rendu["t"] > instant_verifier):
            contredit = True
    valide: bool = bool(verdict) and verdict["verdict"].startswith("VALIDÉ") and not contredit

    if valide and not non_verifiees and not any(e["ev"] == "archive" and e["t"] >= verdict["t"] for e in journal):
        decision["archiver"] = True

    if non_verifiees:
        dernier_signalement: float = max((b["t"] for b in blocages if b.get("motif") == "dette"), default=0.0)
        nouvelles: list = [m for m in non_verifiees if m["t"] > dernier_signalement]
        au_travail: bool = any(not commun.de_la_verification(genre) for _, genre in en_cours)
        # Seuil réglable : en dessous, la modification reste notée et sera vérifiée avec les suivantes,
        # sauf si elle touche un fichier qui exige toujours une vérification
        fichiers: list = sorted({m.get("chemin") or "" for m in non_verifiees})
        sensibles: list = [f for f in fichiers if commun.est_sensible(f, reglages.get("toujours_verifier") or [])]
        due: bool = len(fichiers) >= int(reglages.get("seuil_fichiers") or 1) or bool(sensibles)
        if not nouvelles or au_travail or auteur_en_attente(journal, non_verifiees, shells, maintenant):
            return decision
        if due:
            decision["blocage"] = ("dette", {"modifs": non_verifiees, "anciennes": len(non_verifiees) - len(nouvelles), "sensibles": sensibles})
        else:
            dernier_rappel: float = max((e["t"] for e in journal if e["ev"] == "note"), default=0.0)
            if any(m["t"] > dernier_rappel for m in non_verifiees):
                decision["note"] = (
                    "verified-workflow : %d fichier(s) modifié(s) sans vérification, sous le seuil de %d "
                    "(%s). Ils seront vérifiés avec les modifications suivantes." % (
                        len(fichiers), int(reglages.get("seuil_fichiers") or 1), ", ".join(os.path.basename(f) for f in fichiers[:5])))
        return decision

    def deja_bloque(motif: str) -> bool:
        depuis: float = verdict["t"] if verdict else instant_verifier
        return any(b.get("motif") == motif and b.get("agent_id") == identifiant and b["t"] >= depuis for b in blocages)

    if dernier and not verdict and dernier_message is not None and not deja_bloque("sans_verdict"):
        decision["blocage"] = ("sans_verdict", {"message": str(dernier_message)})
        return decision
    if valide and verdict.get("visuel") and not deja_bloque("visuel"):
        if not any(e["ev"] == "ouverture" and e["t"] > verdict["t"] for e in journal):
            decision["blocage"] = ("visuel", {})
    return decision


# --- Messages -------------------------------------------------------------------------------------

def lignes_changees(session_id: str, chemins: list) -> object:
    """Nombre de lignes ajoutées ou retirées par rapport aux copies d'avant, ou None si ce n'est pas
    mesurable simplement (trop de fichiers, fichier volumineux ou binaire)."""
    if len(chemins) > 20:
        return None
    total: int = 0
    for chemin in chemins:
        copie: str = os.path.join(commun.dossier_copies(session_id), chemin.lstrip(os.sep))
        try:
            if os.path.getsize(chemin) > 400_000:
                return None
            with open(chemin, encoding="utf-8") as fichier:
                apres: list = fichier.readlines()
            avant: list = []
            if os.path.isfile(copie):
                with open(copie, encoding="utf-8") as fichier:
                    avant = fichier.readlines()
        except (OSError, UnicodeDecodeError):
            return None
        total += sum(1 for ligne in difflib.ndiff(avant, apres) if ligne[:1] in "+-") if avant else len(apres)
    return total


def message_dette(session_id: str, details: dict, reglages: object = None) -> str:
    reglages = reglages if isinstance(reglages, dict) else commun.REGLAGES_PAR_DEFAUT
    chemins: list = []
    auteurs: dict = {}
    for modif in details["modifs"]:
        chemin: str = modif.get("chemin") or ""
        if chemin not in chemins:
            chemins.append(chemin)
        if modif.get("agent_type"):
            auteurs[chemin] = modif["agent_type"]
    lignes: list = [
        "- %s%s" % (chemin, " (modifié par un sous-agent %s)" % auteurs[chemin] if chemin in auteurs else "")
        for chemin in chemins[:MAX_FICHIERS_LISTES]
    ]
    if len(chemins) > MAX_FICHIERS_LISTES:
        lignes.append("- ... et %d autres fichiers" % (len(chemins) - MAX_FICHIERS_LISTES))
    racines: list = []
    for chemin in chemins:
        racine: str = commun.racine_projet(chemin)
        if racine and racine not in racines:
            racines.append(racine)
    fiches: list = []
    for racine in racines[:3]:
        candidats: list = commun.fiches_possibles(racine)
        presente: list = [c for c in candidats if os.path.isfile(c)]
        fiches.append("%s (%s)" % (presente[0], "présente") if presente else "%s (absente)" % candidats[0])
    rappel: str = ""
    if details.get("anciennes"):
        rappel = ("\nUne partie de ces fichiers avait déjà été signalée à un tour précédent et n'a toujours pas "
                  "été vérifiée : la vérification doit les couvrir aussi.\n")
    # Taille : une petite modification se prouve sans agents (c'est la part des vérifications qui coûte du temps)
    bornes: dict = reglages.get("petite_modification") or {}
    changees = lignes_changees(session_id, [c for c in chemins if os.path.isfile(c)])
    taille: str = "Taille : %d fichier(s)%s." % (len(chemins), ", environ %d lignes changées" % changees if changees is not None else "")
    if (not details.get("sensibles") and changees is not None
            and len(chemins) <= int(bornes.get("fichiers", 2)) and changees <= int(bornes.get("lignes", 60))):
        taille += (" C'est une petite modification : écris « petite modification » en tête du brief, le verifier "
                   "la prouvera lui-même, sans lancer d'agents.")
    elif details.get("sensibles"):
        taille += " Fichier(s) à vérifier quoi qu'il arrive : %s." % ", ".join(os.path.basename(f) for f in details["sensibles"][:5])
    return (
        "Des fichiers de projet ont été modifiés sans vérification indépendante depuis :\n"
        + "\n".join(lignes) + "\n" + rappel + "\n" + taille + "\n"
        "Fichier repère de la session : %s\n"
        "Copies d'avant modification : %s\n"
        "Fiche de vérification du projet : %s\n\n"
        "Lance un nouvel agent `verifier` avec dans son brief la demande exacte de l'utilisateur, la liste des "
        "fichiers modifiés, ces chemins, et comment lancer le projet si la fiche ne le dit pas. S'il rend ÉCHEC, "
        "corrige puis lance un nouveau verifier (reprendre l'ancien par message ne couvre pas la correction). "
        "Si une vérification n'a pas lieu d'être maintenant (l'utilisateur l'a refusée, modification purement "
        "cosmétique d'un commentaire, travail encore en cours qui sera vérifié d'un bloc), explique pourquoi à "
        "l'utilisateur dans ta réponse : ces fichiers resteront signalés à la prochaine modification."
        % (commun.chemin_repere(session_id), commun.dossier_copies(session_id), " ; ".join(fiches) or "aucune racine de projet reconnue")
    )


def message_sans_verdict(identifiant: str, details: dict) -> str:
    apercu: str = " ".join(details.get("message", "").split())[:300]
    return (
        "Le verifier lancé (agent %s) a rendu la main sans verdict : son message ne commence pas par "
        "`VERDICT : ...`. Ce qu'il a dit : « %s »\n\n"
        "La modification n'est donc pas vérifiée. Reprends cet agent avec SendMessage en lui demandant son "
        "rapport final (les deux premières lignes sont `VERDICT : ...` et `VISUEL : ...`), ou lance un nouveau "
        "`verifier`. Si tu ne fais ni l'un ni l'autre, dis clairement à l'utilisateur que la vérification n'a pas abouti."
        % (identifiant, apercu or "(vide)")
    )


def message_visuel() -> str:
    ouvrir: str = os.path.join(PLUGIN, "scripts", "ouvrir.sh")
    return (
        "Le verifier a validé une modification qui a une composante visuelle (VISUEL : OUI), mais le visuel n'a "
        "pas été rendu consultable par l'utilisateur. Il regarde depuis son poste, pas forcément sur l'écran de "
        "cette machine. À partir de la section « Composante visuelle » du rapport : lance si besoin une "
        "instance de dev détachée (`setsid -f`, port libre 4100-4199, données temporaires, jamais la base de "
        "prod) ; elle peut rester en écoute sur 127.0.0.1. Puis `bash %s '<url locale>'` donne l'URL à ouvrir sur "
        "le poste : il la publie sur le réseau Tailscale quand la machine en fait partie, sinon il donne la "
        "commande de tunnel SSH à transmettre. Sans instance à montrer, passe au même script les "
        "captures du verifier (un ou plusieurs fichiers). Ne redémarre ni ne reconstruis jamais un conteneur de "
        "production pour ça. Dis ensuite à l'utilisateur quoi regarder et comment atteindre l'état à voir, et "
        "demande-lui de valider le rendu. Quand il a répondu : `bash %s --arreter` (ne ferme que ce que cette "
        "session a ouvert)." % (ouvrir, ouvrir)
    )


# --- Traitements ----------------------------------------------------------------------------------

def chemin_modifie(entree_outil: dict) -> str:
    return str(entree_outil.get("file_path") or entree_outil.get("notebook_path") or "")


def refuser(raison: str) -> None:
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": raison,
    }}, ensure_ascii=True))


def pre_outil(entree: dict) -> None:
    session_id: str = entree.get("session_id") or ""
    outil: str = entree.get("tool_name") or ""
    entree_outil: dict = entree.get("tool_input") or {}
    agent_type: str = entree.get("agent_type") or ""
    chemin: str = chemin_modifie(entree_outil)
    if outil == "Bash":
        # Pour tout le monde, fil principal compris : ne jamais tuer un processus de conteneur de production
        arret: str = garde.examiner_arret(str(entree_outil.get("command") or ""))
        if arret:
            refuser(arret)
            sys.stdout.flush()
            commun.noter(session_id, "refus", outil=outil, agent_id=entree.get("agent_id"), agent_type=agent_type, raison=arret[:160])
            return
    if commun.du_plugin(agent_type):
        raison: str = ""
        if outil == "Bash":
            raison = garde.examiner_commande(str(entree_outil.get("command") or ""), entree.get("cwd") or "", commun.etat_machine)
        elif outil in commun.OUTILS_EDITION:
            raison = garde.examiner_ecriture(chemin, commun.dossier_etat())
        if raison:
            refuser(raison)  # d'abord le refus : il ne dépend pas de l'écriture du journal
            sys.stdout.flush()
            commun.noter(session_id, "refus", outil=outil, agent_id=entree.get("agent_id"), agent_type=agent_type, raison=raison[:160])
            return
    if outil == "Bash":
        if not commun.de_la_verification(agent_type) and appelle_ouvrir(str(entree_outil.get("command") or "")):
            commun.noter(session_id, "ouverture", par="ouvrir.sh", agent_type=agent_type)
        return
    if outil not in commun.OUTILS_EDITION or not chemin or not commun.racine_projet(chemin):
        return
    dossier: str = commun.dossier_session(session_id, creation=True)
    if not dossier:
        return
    repere: str = os.path.join(dossier, "repere")
    if not os.path.exists(repere):
        with open(repere, "a", encoding="utf-8"):
            pass
    absolu: str = os.path.abspath(os.path.expanduser(chemin))
    if os.path.isfile(absolu):
        copie: str = os.path.join(dossier, "avant", absolu.lstrip(os.sep))
        if not os.path.exists(copie):
            commun.creer(os.path.dirname(copie))
            shutil.copy2(absolu, copie)


def post_outil(entree: dict) -> None:
    session_id: str = entree.get("session_id") or ""
    outil: str = entree.get("tool_name") or ""
    if outil == "SendUserFile":
        commun.noter(session_id, "ouverture", par="SendUserFile")
        return
    chemin: str = chemin_modifie(entree.get("tool_input") or {})
    if outil not in commun.OUTILS_EDITION or not chemin or not commun.racine_projet(chemin):
        return
    commun.noter(
        session_id, "modif",
        chemin=os.path.abspath(os.path.expanduser(chemin)),
        projet=commun.est_fichier_de_projet(chemin),
        agent_id=entree.get("agent_id"), agent_type=entree.get("agent_type"),
    )


def agent_debut(entree: dict) -> None:
    commun.noter(entree.get("session_id") or "", "agent_debut", agent_id=entree.get("agent_id"), agent_type=entree.get("agent_type"))


def agent_fin(entree: dict) -> None:
    champs: dict = {
        "agent_id": entree.get("agent_id"), "agent_type": entree.get("agent_type"),
        "shells": shells_en_cours(entree) or None,
    }
    if commun.est_verifier(entree.get("agent_type")):
        lu: dict = lire_verdict(entree.get("last_assistant_message"))
        champs.update(lu)
        if not lu:
            champs["apercu"] = " ".join(str(entree.get("last_assistant_message") or "").split())[:240]
    commun.noter(entree.get("session_id") or "", "agent_fin", **champs)


def debut_session(entree: dict) -> None:
    commun.menage()


def blocages_de_la_relance(journal: list) -> int:
    """Blocages posés depuis la dernière fin de tour qui n'était pas elle-même une relance par un hook."""
    nombre: int = 0
    for evenement in reversed(journal):
        if evenement["ev"] == "fin_de_tour" and not evenement.get("relance"):
            break
        if evenement["ev"] == "blocage":
            nombre += 1
    return nombre


def fin_de_tour(entree: dict) -> None:
    """`stop_hook_active` (Claude continue déjà parce qu'un hook l'a relancé) ne suffit pas à se taire : un
    verdict peut arriver pendant cette relance, et il faut alors archiver et signaler le visuel. Ce qui
    empêche de bloquer en boucle, c'est le journal : une dette n'est signalée que s'il y a une modification
    plus récente que le dernier signalement, un verdict qu'une fois."""
    session_id: str = entree.get("session_id") or ""
    if not commun.dossier_session(session_id):
        return
    journal: list = commun.lire_journal(session_id)
    if not journal:
        return
    relance: bool = bool(entree.get("stop_hook_active"))
    reglages: dict = commun.reglages()
    decision: dict = decider(
        journal, entree, time.time(),
        lambda agent_id: rapports_du_transcript(entree.get("transcript_path"), agent_id),
        reglages,
    )
    commun.noter(session_id, "fin_de_tour", relance=relance or None)
    if decision["archiver"]:
        commun.archiver(session_id)
        commun.noter(session_id, "archive", agent_id=decision["verifier"])
    if decision["note"] and not decision["blocage"]:
        # Sous le seuil : rien n'est exigé, mais l'utilisateur voit ce qui reste à vérifier
        commun.noter(session_id, "note")
        print(json.dumps({"systemMessage": decision["note"]}, ensure_ascii=True))
        return
    if not decision["blocage"]:
        return
    if relance and blocages_de_la_relance(journal) >= MAX_BLOCAGES_PAR_RELANCE:
        return
    motif, details = decision["blocage"]
    if motif == "dette":
        message: str = message_dette(session_id, details, reglages)
    elif motif == "sans_verdict":
        message = message_sans_verdict(decision["verifier"], details)
    else:
        message = message_visuel()
    commun.noter(session_id, "blocage", motif=motif, agent_id=decision["verifier"])
    # ensure_ascii : la sortie reste lisible quelle que soit la locale du processus
    print(json.dumps({"decision": "block", "reason": message}, ensure_ascii=True))


TRAITEMENTS: dict = {
    "SessionStart": debut_session,
    "PreToolUse": pre_outil,
    "PostToolUse": post_outil,
    "SubagentStart": agent_debut,
    "SubagentStop": agent_fin,
    "Stop": fin_de_tour,
}


def main() -> None:
    try:
        entree = json.load(sys.stdin)
    except ValueError:
        return
    if not isinstance(entree, dict):
        return
    evenement = entree.get("hook_event_name")
    traitement = TRAITEMENTS.get(evenement) if isinstance(evenement, str) else None
    if not traitement:
        return
    try:
        traitement(entree)
    except Exception:
        commun.noter_erreur(evenement, traceback.format_exc())
        if evenement == "Stop":
            print(json.dumps({"systemMessage": "verified-workflow : erreur interne du hook de fin de tour, "
                              "contrôle ignoré (voir erreurs.log dans le dossier d'état du plugin)."}, ensure_ascii=True))


if __name__ == "__main__":
    main()
