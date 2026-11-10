#!/usr/bin/env python3
"""Contrôle des hooks : `python3 hooks/test_hooks.py`.

Chaque cas lance `crochet.py` comme le fait Claude Code, avec des entrées qui reprennent la forme relevée
sur une vraie session (2.1.322) : `agent_id` et `agent_type` pour un appel d'outil fait par un sous-agent,
`last_assistant_message` à la fin d'un agent, `background_tasks` à la fin d'un tour. Le rapport reçu par le
fil principal a la forme réelle d'une notification de tâche (`<task-id>` + `<result>`).
Tout se passe dans un dossier temporaire hors de /tmp (que les hooks ignorent) ; rien n'est écrit ailleurs.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

ICI: str = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ICI)
import garde  # noqa: E402

SESSION: str = "essai-session"
VERIFIER: str = "verified-workflow:verifier"


class Banc:
    """Un projet factice et un dossier d'état vierge."""

    def __init__(self, racine: str) -> None:
        self.racine: str = racine
        self.etat: str = os.path.join(racine, "etat")
        self.ancien: str = os.path.join(racine, "ancien-tmp")
        self.projet: str = os.path.join(racine, "projet")
        os.makedirs(self.projet)
        with open(os.path.join(self.projet, "package.json"), "w", encoding="utf-8"):
            pass
        self.page: str = os.path.join(self.projet, "Page.ts")
        self.transcript: str = os.path.join(racine, "transcript.jsonl")
        self.repartir()

    def regler(self, valeurs: object = None) -> None:
        """Écrit les réglages du banc ; sans argument, revient aux valeurs par défaut."""
        chemin: str = os.path.join(self.racine, "reglages.json")
        if valeurs is None:
            if os.path.exists(chemin):
                os.remove(chemin)
            return
        with open(chemin, "w", encoding="utf-8") as fichier:
            fichier.write(valeurs if isinstance(valeurs, str) else json.dumps(valeurs))

    def repartir(self) -> None:
        """État vierge, fichier d'origine en place."""
        shutil.rmtree(self.etat, ignore_errors=True)
        with open(self.page, "w", encoding="utf-8") as fichier:
            fichier.write("version 1\n")
        with open(self.transcript, "w", encoding="utf-8"):
            pass

    @property
    def session(self) -> str:
        return os.path.join(self.etat, "sessions", SESSION)

    def copie(self, chemin: str) -> str:
        return os.path.join(self.session, "avant", chemin.lstrip(os.sep))

    def journal(self) -> list:
        try:
            with open(os.path.join(self.session, "journal.jsonl"), encoding="utf-8") as fichier:
                return [json.loads(ligne) for ligne in fichier]
        except OSError:
            return []

    def hook(self, evenement: str, session: str = SESSION, **champs: object) -> dict:
        entree: dict = {"session_id": session, "hook_event_name": evenement, "cwd": self.projet,
                        "transcript_path": self.transcript}
        entree.update(champs)
        # Les réglages du banc, pas ceux de l'utilisateur : les tests ne dépendent pas de reglages.json
        environnement: dict = dict(os.environ, VERIFIED_WORKFLOW_ETAT=self.etat, VERIFIED_WORKFLOW_ANCIEN_ETAT=self.ancien,
                                   VERIFIED_WORKFLOW_REGLAGES=os.path.join(self.racine, "reglages.json"))
        resultat = subprocess.run(
            [sys.executable, os.path.join(ICI, "crochet.py")],
            input=json.dumps(entree), capture_output=True, text=True, env=environnement,
        )
        assert resultat.returncode == 0, resultat.stderr
        assert not os.path.exists(os.path.join(self.etat, "erreurs.log")), open(os.path.join(self.etat, "erreurs.log")).read()
        time.sleep(0.002)  # deux événements n'ont jamais le même instant
        return json.loads(resultat.stdout) if resultat.stdout.strip() else {}

    # Raccourcis
    def modifier(self, chemin: str = "", agent: tuple = (), contenu: object = None) -> None:
        """Un Edit réussi : PreToolUse puis PostToolUse. `agent` = (agent_id, agent_type) pour un sous-agent.
        `contenu` : ce que le fichier contient après la modification."""
        chemin = chemin or self.page
        champs: dict = {"tool_name": "Edit", "tool_input": {"file_path": chemin}}
        if agent:
            champs.update(agent_id=agent[0], agent_type=agent[1])
        self.hook("PreToolUse", **champs)
        if contenu is not None:
            with open(chemin, "w", encoding="utf-8") as fichier:
                fichier.write(contenu)
        self.hook("PostToolUse", **champs)

    def lancer(self, agent_id: str, agent_type: str = VERIFIER) -> None:
        self.hook("SubagentStart", agent_id=agent_id, agent_type=agent_type)

    def finir(self, agent_id: str, message: str, agent_type: str = VERIFIER, taches: object = None) -> None:
        champs: dict = {"background_tasks": list(taches)} if taches is not None else {}
        self.hook("SubagentStop", agent_id=agent_id, agent_type=agent_type, last_assistant_message=message, **champs)

    def fin_de_tour(self, taches: object = (), **champs: object) -> str:
        """Raison du blocage, ou ""."""
        if taches is not None:
            champs["background_tasks"] = list(taches)
        return self.hook("Stop", **champs).get("reason", "")

    def notification(self, agent_id: str, resultat: str, note: str = "") -> None:
        """Ce que le fil principal reçoit quand un agent en arrière-plan rend la main."""
        texte: str = (
            "<task-notification>\n<task-id>%s</task-id>\n<output-file>/tmp/x.output</output-file>\n"
            "<status>completed</status>\n<summary>Agent fini</summary>\n%s<result>%s</result>\n</task-notification>"
            % (agent_id, "<note>%s</note>\n" % note if note else "", resultat)
        )
        entree: dict = {
            "type": "user", "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() + 1)) + ".000Z",
            "origin": {"kind": "task-notification"}, "message": {"role": "user", "content": texte},
        }
        with open(self.transcript, "a", encoding="utf-8") as fichier:
            fichier.write(json.dumps(entree, ensure_ascii=False) + "\n")


def rapport(verdict: str, visuel: str) -> str:
    return "VERDICT : %s\nVISUEL : %s\n\n## Verdict\nUne phrase.\n" % (verdict, visuel)


def en_cours(agent_id: str, agent_type: str) -> list:
    return [{"id": agent_id, "type": "subagent", "status": "running", "description": "x", "agent_type": agent_type}]


SHELL: list = [{"id": "s1", "type": "shell", "status": "running", "description": "tests", "command": "bun test"}]
INTERIM: str = ("This agent stopped with background work of its own still running. It may resume on its own when that "
                "work completes or reports, and the same task-id notifies again if it does; the result below may be interim.")


def essais_hooks(b: Banc) -> None:
    # --- Repère et copies d'avant modification
    b.hook("PreToolUse", tool_name="Edit", tool_input={"file_path": b.page})
    assert os.path.exists(os.path.join(b.session, "repere")) and open(b.copie(b.page)).read() == "version 1\n"
    assert os.stat(b.etat).st_mode & 0o777 == 0o700, "le dossier d'état doit être privé"
    assert b.journal() == [], "une modification n'est notée qu'une fois réussie (PostToolUse)"
    with open(b.page, "w", encoding="utf-8") as fichier:
        fichier.write("version 2\n")
    b.hook("PreToolUse", tool_name="Edit", tool_input={"file_path": b.page})
    assert open(b.copie(b.page)).read() == "version 1\n", "une seule copie : celle d'avant la première modification"
    nouveau: str = os.path.join(b.projet, "Nouveau.ts")
    b.hook("PreToolUse", tool_name="Write", tool_input={"file_path": nouveau})
    assert not os.path.exists(b.copie(nouveau)), "un fichier créé n'a pas de copie d'avant"
    b.hook("PreToolUse", tool_name="Edit", tool_input={"file_path": b.page}, session="..")
    assert sorted(os.listdir(os.path.join(b.etat, "sessions"))) == [SESSION], "identifiant de session non simple : ignoré"
    # Un fichier hors projet (scratchpad, note isolée) ne pose pas de repère
    b.repartir()
    b.hook("PreToolUse", tool_name="Write", tool_input={"file_path": "/tmp/brouillon.txt"})
    b.hook("PostToolUse", tool_name="Write", tool_input={"file_path": "/tmp/brouillon.txt"})
    assert not os.path.exists(b.session), "écrire hors d'un projet ne crée aucun état"
    # Une note .md d'un projet est copiée mais n'est pas à vérifier
    note: str = os.path.join(b.projet, "note.md")
    with open(note, "w", encoding="utf-8") as fichier:
        fichier.write("note\n")
    b.modifier(note)
    assert os.path.exists(b.copie(note)) and b.journal()[-1]["projet"] is False
    assert b.fin_de_tour() == ""
    # Les réglages de Claude pour le projet (.claude/) non plus
    os.makedirs(os.path.join(b.projet, ".claude"), exist_ok=True)
    b.modifier(os.path.join(b.projet, ".claude", "settings.json"))
    assert b.journal()[-1]["ev"] == "modif" and b.journal()[-1]["projet"] is False and b.fin_de_tour() == ""

    # --- Fin de tour : modification sans verifier
    b.repartir()
    b.modifier()
    assert b.journal()[-1]["ev"] == "modif" and b.journal()[-1]["projet"] is True
    raison: str = b.fin_de_tour()
    assert "sans vérification" in raison and b.page in raison and os.path.join(b.session, "repere") in raison
    assert "verification.md (absente)" in raison
    assert b.fin_de_tour(stop_hook_active=True) == "", "relancé par le hook, rien de neuf : laisse finir"
    assert b.fin_de_tour() == "", "déjà signalé, rien de neuf : pas de nouveau blocage"
    autre: str = os.path.join(b.projet, "Autre.ts")
    b.modifier(autre)
    raison = b.fin_de_tour()
    assert autre in raison and b.page in raison and "déjà été signalée" in raison, "la dette reportée reste listée"
    assert os.path.exists(b.copie(b.page))

    # --- Taille de la modification : une petite se prouve sans agents, le hook le dit
    b.repartir()
    b.modifier(contenu="version 2\n")
    raison = b.fin_de_tour()
    assert "Taille : 1 fichier(s), environ 2 lignes changées" in raison and "petite modification" in raison, raison
    b.repartir()
    b.modifier(contenu="".join("ligne %d\n" % i for i in range(200)))
    raison = b.fin_de_tour()
    assert "Taille : 1 fichier(s)" in raison and "petite modification" not in raison, "200 lignes : pas une petite modification"
    b.repartir()
    for nom in ("A.ts", "B.ts", "C.ts"):
        b.modifier(os.path.join(b.projet, nom))
    assert "petite modification" not in b.fin_de_tour(), "trois fichiers : pas une petite modification"

    # --- Seuil de fichiers réglable : en dessous, rien n'est exigé mais tout reste noté
    b.regler({"seuil_fichiers": 3})
    b.repartir()
    b.modifier(os.path.join(b.projet, "A.ts"))
    sortie: dict = b.hook("Stop", background_tasks=[])
    assert "reason" not in sortie and "1 fichier(s)" in sortie.get("systemMessage", "") and "seuil de 3" in sortie["systemMessage"], sortie
    assert b.hook("Stop", background_tasks=[]) == {}, "le rappel n'est pas répété sans nouvelle modification"
    b.modifier(os.path.join(b.projet, "B.ts"))
    assert "2 fichier(s)" in b.hook("Stop", background_tasks=[]).get("systemMessage", "")
    b.modifier(os.path.join(b.projet, "C.ts"))
    raison = b.fin_de_tour()
    assert "sans vérification" in raison and all(nom in raison for nom in ("A.ts", "B.ts", "C.ts")), "seuil atteint : tout est à vérifier"
    # ... un fichier qui exige toujours une vérification passe outre le seuil
    b.repartir()
    b.modifier(os.path.join(b.projet, "docker-compose.yml"))
    raison = b.fin_de_tour()
    assert "sans vérification" in raison and "quoi qu'il arrive" in raison and "petite modification" not in raison
    # ... un fichier de réglages illisible laisse les valeurs par défaut (toute modification est vérifiée)
    b.regler("{ pas du json")
    b.repartir()
    b.modifier()
    assert "sans vérification" in b.fin_de_tour()
    b.regler({"seuil_fichiers": "trois", "petite_modification": {"fichiers": -1}})
    b.repartir()
    b.modifier()
    assert "sans vérification" in b.fin_de_tour()
    b.regler()

    # --- Pendant une relance provoquée par le hook (stop_hook_active) : le journal décide, pas l'indicateur
    b.repartir()
    b.modifier()
    assert "sans vérification" in b.fin_de_tour()
    b.lancer("v1")
    b.finir("v1", rapport("VALIDÉ", "OUI"))  # verdict arrivé avant la fin de la relance
    assert "composante visuelle" in b.fin_de_tour(stop_hook_active=True), "verdict reçu pendant la relance"
    assert not os.path.exists(b.copie(b.page)), "... et l'archive est faite"
    assert b.fin_de_tour(stop_hook_active=True) == ""
    b.repartir()
    b.modifier()
    assert "sans vérification" in b.fin_de_tour()
    b.lancer("v1")
    b.finir("v1", rapport("ÉCHEC", "NON"))
    b.modifier()  # correction faite pendant la relance, sans nouveau verifier
    assert "sans vérification" in b.fin_de_tour(stop_hook_active=True), "une correction non vérifiée ne passe pas en silence"
    assert b.fin_de_tour(stop_hook_active=True) == ""
    # Filet de sécurité : jamais plus de trois blocages d'affilée dans une même relance
    b.repartir()
    b.modifier()
    assert b.fin_de_tour() != ""
    for _ in range(2):
        b.modifier()
        assert b.fin_de_tour(stop_hook_active=True) != ""
    b.modifier()
    assert b.fin_de_tour(stop_hook_active=True) == "", "quatrième blocage d'affilée : laisse finir"
    assert "sans vérification" in b.fin_de_tour(), "... et la dette est rappelée à la fin de tour suivante"

    # --- verifier lancé : plus de blocage ; verdicts qui gardent repère et copies
    for verdict in ("ÉCHEC", "NON VÉRIFIABLE", "???"):
        b.repartir()
        b.modifier()
        b.lancer("v1")
        assert b.fin_de_tour(en_cours("v1", VERIFIER)) == "", "verifier en cours"
        b.finir("v1", rapport(verdict, "NON"))
        assert b.fin_de_tour() == "", verdict
        assert os.path.exists(b.copie(b.page)) and os.path.exists(os.path.join(b.session, "repere")), verdict

    # --- VALIDÉ : les copies passent en archive, le repère est retiré
    for verdict in ("VALIDÉ", "VALIDÉ SOUS RÉSERVE", "**VALIDÉ**"):
        b.repartir()
        b.modifier()
        b.lancer("v1")
        b.finir("v1", "J'attends les rapports de mes agents.")  # arrêt intermédiaire, sans verdict
        b.finir("v1", rapport(verdict, "NON"))
        assert b.fin_de_tour() == "", verdict
        assert not os.path.exists(b.copie(b.page)) and not os.path.exists(os.path.join(b.session, "repere")), verdict
        archives: list = os.listdir(os.path.join(b.session, "archives"))
        assert len(archives) == 1 and os.path.exists(os.path.join(b.session, "archives", archives[0], "avant", b.page.lstrip(os.sep)))
        assert b.fin_de_tour() == "" and len(os.listdir(os.path.join(b.session, "archives"))) == 1, "une seule archive par verdict"

    # --- VALIDÉ + VISUEL : OUI sans ouverture : un blocage, une seule fois
    b.repartir()
    b.modifier()
    b.lancer("v1")
    b.finir("v1", rapport("VALIDÉ", "OUI"))
    assert "composante visuelle" in b.fin_de_tour()
    assert not os.path.exists(b.copie(b.page)), "l'archive est faite quand même"
    assert b.fin_de_tour() == "", "pas de second blocage pour le même verdict"
    # ... pas de blocage si ouvrir.sh a été appelé après le verdict
    b.repartir()
    b.modifier()
    b.lancer("v1")
    b.hook("PreToolUse", tool_name="Bash", tool_input={"command": "bash ~/x/scripts/ouvrir.sh 'http://localhost:4100/'"})
    b.finir("v1", rapport("VALIDÉ", "OUI"))
    assert "composante visuelle" in b.fin_de_tour(), "une ouverture d'avant le verdict ne compte pas"
    b.repartir()
    b.modifier()
    b.lancer("v1")
    b.finir("v1", rapport("VALIDÉ", "OUI"))
    for sans_effet in ("bash ~/x/scripts/ouvrir.sh --arreter", "bash ~/x/scripts/ouvrir.sh --liste", "cat ~/x/scripts/ouvrir.sh", "rg ouvrir.sh ~/x"):
        b.hook("PreToolUse", tool_name="Bash", tool_input={"command": sans_effet})
    b.hook("PreToolUse", tool_name="Bash", tool_input={"command": "bash ~/x/scripts/ouvrir.sh /tmp/c.png"},
           agent_id="p1", agent_type="verified-workflow:verif-preuve")
    assert "composante visuelle" in b.fin_de_tour(), "seul un appel sur une cible, hors agents de vérification, compte"
    b.repartir()
    b.modifier()
    b.lancer("v1")
    b.finir("v1", rapport("VALIDÉ", "OUI"))
    b.hook("PreToolUse", tool_name="Bash", tool_input={"command": "cd /tmp && bash ~/x/scripts/ouvrir.sh 'http://localhost:4100/' 2>&1 | tail -3"})
    assert b.fin_de_tour() == ""
    # ... ni si les captures ont été envoyées à l'utilisateur
    b.repartir()
    b.modifier()
    b.lancer("v1")
    b.finir("v1", rapport("VALIDÉ", "OUI"))
    b.hook("PostToolUse", tool_name="SendUserFile", tool_input={"path": "/tmp/capture.png"})
    assert b.fin_de_tour() == ""

    # --- Le verdict tardif d'un ancien verifier ne vaut pas pour le dernier lancé
    b.repartir()
    b.modifier()
    b.lancer("v1")
    b.modifier()
    b.lancer("v2")
    b.finir("v1", rapport("VALIDÉ", "NON"))
    assert b.fin_de_tour(en_cours("v2", VERIFIER)) == "" and os.path.exists(b.copie(b.page))
    # --- Une modification après le lancement du verifier : blocage, rien n'est archivé
    b.repartir()
    b.modifier()
    b.lancer("v1")
    b.finir("v1", rapport("VALIDÉ", "NON"))
    b.modifier()
    assert "sans vérification" in b.fin_de_tour() and os.path.exists(b.copie(b.page))

    # --- Sous-agents : pas de blocage tant qu'ils travaillent, blocage quand tout est fini
    b.repartir()
    b.lancer("g1", "general-purpose")
    b.modifier(agent=("g1", "general-purpose"))
    assert b.fin_de_tour(en_cours("g1", "general-purpose")) == "", "le sous-agent travaille encore"
    b.finir("g1", "fait", "general-purpose")
    raison = b.fin_de_tour()
    assert b.page in raison and "sous-agent general-purpose" in raison
    # Un sous-agent qui a rendu la main en attendant sa propre commande en arrière-plan n'a pas fini
    b.repartir()
    b.lancer("g1", "general-purpose")
    b.modifier(agent=("g1", "general-purpose"))
    b.finir("g1", "Les tests tournent en arrière-plan.", "general-purpose", taches=SHELL)
    assert b.fin_de_tour(SHELL) == "", "sa commande tourne encore : pas de blocage"
    assert b.page in b.fin_de_tour(), "plus rien ne tourne : blocage"
    # Sans `background_tasks` (ancienne version) : le journal dit qui travaille encore
    b.repartir()
    b.lancer("g1", "general-purpose")
    b.modifier(agent=("g1", "general-purpose"))
    assert b.fin_de_tour(taches=None) == ""
    b.finir("g1", "fait", "general-purpose")
    assert b.page in b.fin_de_tour(taches=None)
    # Les écritures des agents de vérification ne sont pas des modifications à vérifier
    b.repartir()
    b.hook("PostToolUse", tool_name="Write", tool_input={"file_path": b.page}, agent_id="p1", agent_type="verified-workflow:verif-preuve")
    assert b.fin_de_tour() == ""

    # --- verifier qui rend la main sans verdict
    b.repartir()
    b.modifier()
    b.lancer("v1")
    b.finir("v1", "J'attends les rapports de mes agents.")
    assert b.fin_de_tour(en_cours("v1", VERIFIER)) == "", "encore en cours : rien à dire"
    assert b.fin_de_tour(en_cours("p1", "verified-workflow:verif-preuve")) == "", "un de ses agents tourne encore"
    assert b.fin_de_tour(SHELL) == "", "une commande en arrière-plan tourne encore"
    raison = b.fin_de_tour()
    assert "sans verdict" in raison and "J'attends les rapports" in raison
    assert b.fin_de_tour() == "", "signalé une seule fois"
    # ... repris par message pour rendre son rapport : le verdict rendu ensuite compte
    b.finir("v1", rapport("VALIDÉ", "NON"))
    assert b.fin_de_tour() == "" and not os.path.exists(b.copie(b.page))
    # Une correction après un ÉCHEC demande un NOUVEAU verifier, même si l'ancien, repris, valide ensuite
    b.repartir()
    b.modifier()
    b.lancer("v1")
    b.finir("v1", rapport("ÉCHEC", "NON"))
    b.modifier()
    b.finir("v1", rapport("VALIDÉ", "NON"))
    assert "sans vérification" in b.fin_de_tour() and os.path.exists(b.copie(b.page))
    # Une notification intermédiaire (l'agent attend son propre travail) n'est pas un rapport sans verdict
    b.repartir()
    b.modifier()
    b.lancer("v1")
    b.notification("v1", "Les tests tournent encore.", note=INTERIM)
    assert b.fin_de_tour() == ""
    # Un verdict qui ne dit pas clairement VALIDÉ ne valide rien
    b.repartir()
    b.modifier()
    b.lancer("v1")
    b.finir("v1", rapport("VALIDATION IMPOSSIBLE", "NON"))
    assert b.fin_de_tour() == "" and os.path.exists(b.copie(b.page))

    # --- Deux verifiers en parallèle : rien n'est validé tant que l'autre tourne ou a rendu un verdict défavorable
    b.repartir()
    b.modifier()
    b.lancer("va")
    b.lancer("vb")
    b.finir("vb", rapport("VALIDÉ", "OUI"))
    assert b.fin_de_tour(en_cours("va", VERIFIER)) == "" and os.path.exists(b.copie(b.page)), "l'autre tourne encore"
    b.finir("va", rapport("ÉCHEC", "NON"))
    assert b.fin_de_tour() == "" and os.path.exists(b.copie(b.page)), "l'autre a rendu ÉCHEC"
    b.modifier()
    b.lancer("vc")
    b.finir("vc", rapport("VALIDÉ", "NON"))
    assert b.fin_de_tour() == "" and not os.path.exists(b.copie(b.page)), "nouveau verifier après correction : validé"

    # --- Secours : SubagentStop n'a rien livré, le verdict est lu dans la notification reçue
    b.repartir()
    b.modifier()
    b.lancer("v1")
    b.notification("v1", rapport("VALIDÉ", "OUI"))
    assert "composante visuelle" in b.fin_de_tour() and not os.path.exists(b.copie(b.page))

    # --- Garde-fous : refus pour les agents du plugin seulement
    commande: dict = {"tool_name": "Bash", "tool_input": {"command": "cd ~/projet && docker compose up -d"}}
    refus: dict = b.hook("PreToolUse", agent_id="p1", agent_type="verified-workflow:verif-preuve", **commande)
    assert refus["hookSpecificOutput"]["permissionDecision"] == "deny" and "production" in refus["hookSpecificOutput"]["permissionDecisionReason"]
    assert b.hook("PreToolUse", **commande) == {}, "le fil principal n'est pas concerné"
    assert b.hook("PreToolUse", agent_id="g1", agent_type="general-purpose", **commande) == {}
    ecriture: dict = {"tool_name": "Write", "tool_input": {"file_path": b.page}}
    assert b.hook("PreToolUse", agent_id="p1", agent_type="verified-workflow:verif-preuve", **ecriture)["hookSpecificOutput"]["permissionDecision"] == "deny"
    ecriture = {"tool_name": "Write", "tool_input": {"file_path": "/tmp/sortie/script.mjs"}}
    assert b.hook("PreToolUse", agent_id="p1", agent_type="verified-workflow:verif-preuve", **ecriture) == {}
    # Un kill ordinaire du fil principal passe ; le refus d'atteindre la production est testé dans essais_garde
    assert b.hook("PreToolUse", tool_name="Bash", tool_input={"command": "kill 999999; echo fait"}) == {}

    # --- Session ouverte sous une version <= 1.4 puis rechargée : repère et copies de /tmp sont repris
    b.repartir()
    os.makedirs(os.path.join(b.ancien, SESSION, "avant", "x"))
    with open(os.path.join(b.ancien, SESSION, "avant", "x", "a.txt"), "w", encoding="utf-8") as fichier:
        fichier.write("avant\n")
    with open(os.path.join(b.ancien, SESSION + ".repere"), "w", encoding="utf-8"):
        pass
    b.hook("PreToolUse", tool_name="Edit", tool_input={"file_path": b.page})
    assert open(os.path.join(b.session, "avant", "x", "a.txt")).read() == "avant\n"
    assert not os.path.exists(os.path.join(b.ancien, SESSION + ".repere")) and os.path.exists(b.copie(b.page))

    # --- Ménage au début d'une session : archives de plus de 14 jours, sessions inactives depuis 30 jours
    b.repartir()
    b.modifier()
    vieille: str = os.path.join(b.session, "archives", "20200101-000000")
    recente: str = os.path.join(b.session, "archives", "recente")
    oubliee: str = os.path.join(b.etat, "sessions", "session-oubliee")
    for dossier in (vieille, recente, oubliee):
        os.makedirs(dossier)
    with open(os.path.join(oubliee, "journal.jsonl"), "w", encoding="utf-8"):
        pass
    for chemin, jours in ((vieille, 20), (oubliee, 40), (os.path.join(oubliee, "journal.jsonl"), 40)):
        os.utime(chemin, (time.time() - jours * 86400,) * 2)
    b.hook("SessionStart", source="startup")
    assert not os.path.exists(vieille) and os.path.exists(recente) and not os.path.exists(oubliee) and os.path.exists(b.copie(b.page))


def essais_garde() -> None:
    maison: str = os.path.expanduser("~")
    releve: dict = {"ports_prod": [9095, 5432], "donnees": ["/srv/data", "/home/x/mon-appli/data", maison + "/mon-appli/data"]}

    def raison(commande: str, cwd: str = "/home/x/projet") -> str:
        return garde.examiner_commande(commande, cwd, lambda: releve)

    permis: list = [
        "docker ps", "docker compose -f x.yml config -q; echo rc=$?", "docker logs app --tail 50", "docker inspect x",
        "timeout 60 docker compose ps", 'rg -n "docker compose up" README.md', 'echo "sudo docker compose down"',
        "git status --short", "git diff -- a.ts", "git stash list | head -3", "git log --oneline -5",
        "git worktree add /tmp/s/wt HEAD", "git -C /tmp/s/wt checkout abc123", "cd /tmp/s/wt && git reset --hard",
        'cd "/tmp/s" && npm install --silent playwright-core', "bun test tests/x.test.ts", "bun run check",
        "uv run --no-project --with-requirements requirements-dev.txt pytest -q", "uvx ruff check a.py", "npm --version",
        'curl -s -i -m 60 "http://127.0.0.1:4110/x"', "curl -s -o /dev/null -w '%{http_code}' --retry 30 http://localhost:4110/",
        "curl -s -X POST -H 'content-type: application/json' -d '{\"u\":\"http://evil.example/\"}' http://127.0.0.1:4110/api",
        "curl -s http://127.0.0.1:9095/", 'curl -s "$BASE/api/x"', 'curl -s "http://127.0.0.1:$PORT/"',
        'python3 -I ~/p/scripts/lire-base.py pg appli-db-1 appli "SELECT 1"',
        "ls -la /srv/data/autre-appli", "stat -c %y /srv/data/autre-appli/db.sqlite", "ls /srv/data > /tmp/s/liste.txt",
        "cat <<'EOF' > /tmp/s/note.sh\ndocker compose up -d\nsudo rm -rf /srv/data\ngit checkout main\nEOF",
        "kill 1234; ss -ltn", "systemctl status docker --no-pager", "journalctl -u docker -n 20",
        "node /tmp/s/essai.mjs 2>&1 | tail -5", "PORT=4110 DATA_DIR=/tmp/s/data node server.js",
        # Redirections, heredoc, options à valeur : ne sont pas des URL
        "curl -s http://127.0.0.1:4110/ 2>&1 | head", "curl -s http://127.0.0.1:4110/ > /tmp/s/a.html 2>/dev/null",
        "curl -s -d @- http://127.0.0.1:4110/api <<'EOF'\n{\"a\": 1}\nEOF", "curl -s --noproxy '*' http://127.0.0.1:4110/",
        "curl --unix-socket /tmp/s/app.sock http://x/health", "curl --help", "wget -qO /tmp/s/out.html http://127.0.0.1:4110/",
        "curl -s -o /dev/null -w %{http_code} http://127.0.0.1:4110/",
        # Lecture seule ou simple consultation
        "command -v sudo", "yarn --version", "git clean -n", "git bisect log", "uv lock --check", "systemctl is-system-running",
        "git branch --show-current", "git tag -l", "gunzip -c /srv/data/x.gz | head", "tar -tf /srv/data/a.tar",
        "rg -n '>> /srv/data/x' notes.txt",
        # Lignes continuées, groupes, variables
        "docker compose \\\n  -f x.yml \\\n  config -q",
        'cd "/home/x/projet" && { bun run check; echo "rc=$?"; } > "/tmp/s/check.log" 2>&1',
        'OUT=/tmp/s; cd "$OUT" && npm install --silent playwright-core', "bun install --cwd /tmp/s/front",
    ]
    for commande in permis:
        assert raison(commande) == "", "refus inattendu : %s -> %s" % (commande, raison(commande))
    refuses: list = [
        "docker compose up -d", "cd ~/autre-appli && docker compose down", "docker-compose restart app",
        "docker exec -it app sh", "docker stop app", "docker run --rm alpine true", "docker compose -f a.yml -p x build",
        "VAR=1 timeout 30 docker compose up", "docker volume rm donnees", "docker system prune -f",
        "sudo ls", "true && sudo systemctl stop x", "systemctl restart docker", "service nginx restart",
        "git checkout main", "cd ~/autre-appli && git stash", "git reset --hard HEAD~1", "git commit -m x", "git -C ~/autre-appli restore .",
        "curl https://app.example.org/", "curl -s https://api.example.net/api/x", "wget http://example.com/x",
        "curl -s http://100.64.0.7:9099/", "curl -X POST http://127.0.0.1:9095/api/x", "curl -d a=1 localhost:9095/x",
        "curl -sS --json '{}' http://127.0.0.1:5432/", "wget --post-data=a=1 http://127.0.0.1:9095/",
        "bun add zod", "npm install", "pip install requests", "cd ~/projet && yarn", "uv pip install ruff",
        "rm -rf /srv/data/autre-appli/logs", "cp /srv/data/autre-appli/db.sqlite ~/copie.sqlite", "echo x > /srv/data/a",
        "sed -i s/a/b/ /home/x/mon-appli/data/state.json", "sqlite3 /srv/data/autre-appli/db.sqlite 'select 1'",
        # Un chemin de /tmp ailleurs dans la commande ne rend pas le projet modifiable
        "cd ~/autre-appli && git stash > /tmp/s/x.log", "git reset --hard HEAD~1 && ls /tmp/",
        'cd "/home/x/projet" && { git checkout -- . ; echo "rc=$?"; } > "/tmp/s/git.log" 2>&1',
        'cd "/home/x/projet" && bun install > /tmp/s/install.log 2>&1',
        # Lignes continuées et substitutions
        "cd ~/autre-appli && \\\n  docker compose up -d --build", 'out="$(docker compose restart app 2>&1)"',
        'echo "$(sudo systemctl restart docker)"', "VAR=1 \\\n docker compose down",
        # Données de production atteintes autrement que par un chemin absolu écrit en clair
        "cd /srv/data/autre-appli && rm -rf logs", "cd ~/mon-appli && echo '{}' > data/state.json",
        "find /srv/data/autre-appli -name '*.log' -delete", "rm -rf $HOME/mon-appli/data/x",
        "dd if=/dev/zero of=/srv/data/a", "echo x >| /srv/data/a", "tar -xf /srv/data/a.tar -C /tmp/s",
        # État du dépôt, installations, hôtes
        "git branch -D vieux", "git tag -d v1", "git update-ref -d refs/heads/x", "python3 -m pip install requests",
        "curl -w %{http_code} https://app.example.org/", "curl --unix-socket /var/run/docker.sock -X POST http://x/containers/app/stop",
    ]
    for commande in refuses:
        assert raison(commande) != "", "aurait dû être refusé : %s" % commande
    assert raison("npm install", cwd="/tmp/s/front") == "", "dans /tmp, l'installation est permise"
    # pkill et killall : refusés aux agents du plugin, quel que soit le motif
    assert raison('pkill -f "node serveur.js"') != "" and raison("killall node") != ""
    assert raison("bash ~/p/scripts/arreter.sh 4110") == "" and raison("kill 12345") == ""
    # Pour tout le monde : un arrêt qui atteindrait un processus de conteneur de production
    prod: list = [(251800, "node", "node server.js"), (328840, "bun", "bun server/index.ts")]
    for commande in ('pkill -f "bun server/index.ts"', "pkill node", "killall bun", "kill 251800", "kill -9 328840",
                     'kill $(pgrep -f "node server.js")', "pgrep -f 'bun server' | xargs kill", "kill -TERM -- -251800"):
        assert garde.examiner_arret(commande, lambda: prod) != "", "aurait dû être refusé : %s" % commande
    for commande in ('pkill -f "node serveur.js"', "pkill -f 'scripts/p1-essai.mjs'", "kill 4242", "kill -9 4242",
                     "killall vite", "echo kill", "pgrep -af 'bun server'", 'pkill -f "PORT=4134"'):
        assert garde.examiner_arret(commande, lambda: prod) == "", "refus inattendu : %s" % commande
    assert raison("rm -rf logs media", cwd="/srv/data/autre-appli") != "", "dossier courant dans les données de production"
    assert garde.examiner_ecriture("/home/x/projet/a.ts", "/home/x/etat") != ""
    assert garde.examiner_ecriture("/tmp/s/a.mjs", "/home/x/etat") == ""


def essais_reglages(dossier: str) -> None:
    """Les réglages de l'utilisateur, dans le dossier d'état, passent par-dessus ceux livrés avec le plugin."""
    import commun
    etat: str = os.path.join(dossier, "etat")
    os.makedirs(etat)
    sauves: dict = {cle: os.environ.pop(cle, None) for cle in ("VERIFIED_WORKFLOW_ETAT", "VERIFIED_WORKFLOW_REGLAGES")}
    os.environ["VERIFIED_WORKFLOW_ETAT"] = etat
    try:
        base: dict = commun.reglages()
        with open(os.path.join(etat, "reglages.json"), "w", encoding="utf-8") as fichier:
            json.dump({"seuil_fichiers": base["seuil_fichiers"] + 2, "petite_modification": {"lignes": 7}}, fichier)
        lus: dict = commun.reglages()
        assert lus["seuil_fichiers"] == base["seuil_fichiers"] + 2, "le réglage de l'utilisateur l'emporte"
        assert lus["petite_modification"] == {"fichiers": base["petite_modification"]["fichiers"], "lignes": 7}
        assert lus["toujours_verifier"] == base["toujours_verifier"], "ce que l'utilisateur ne règle pas reste"
        with open(os.path.join(etat, "reglages.json"), "w", encoding="utf-8") as fichier:
            fichier.write("{pas du json")
        assert commun.reglages() == base, "un fichier illisible est ignoré"
        unique: str = os.path.join(dossier, "unique.json")
        with open(unique, "w", encoding="utf-8") as fichier:
            json.dump({"seuil_fichiers": 9}, fichier)
        os.environ["VERIFIED_WORKFLOW_REGLAGES"] = unique
        assert commun.reglages()["seuil_fichiers"] == 9, "VERIFIED_WORKFLOW_REGLAGES désigne un fichier unique"
    finally:
        for cle, valeur in sauves.items():
            if valeur is None:
                os.environ.pop(cle, None)
            else:
                os.environ[cle] = valeur


def demo() -> None:
    cache: str = os.path.join(os.path.expanduser("~"), ".cache")
    os.makedirs(cache, exist_ok=True)
    racine: str = tempfile.mkdtemp(prefix="essai-hooks-", dir=cache)
    autre: str = tempfile.mkdtemp(prefix="essai-reglages-", dir=cache)
    try:
        essais_garde()
        essais_reglages(autre)
        essais_hooks(Banc(racine))
    finally:
        shutil.rmtree(racine, ignore_errors=True)
        shutil.rmtree(autre, ignore_errors=True)
    print("hooks : tous les cas passent")


if __name__ == "__main__":
    demo()
