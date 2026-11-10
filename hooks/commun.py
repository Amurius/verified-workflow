#!/usr/bin/env python3
"""Socle commun des hooks de verified-workflow (1.5) : dossier d'état, journal de session,
reconnaissance des projets, relevé de la production.

L'état vit hors de /tmp (mémoire vive, vidé au redémarrage, nettoyé après 10 jours) :

  ~/.claude/plugins/data/verified-workflow-verified-workflow/        (mode 700)
    sessions/<session_id>/journal.jsonl   une ligne par événement de hook
    sessions/<session_id>/repere          fichier vide posé avant la première modification de projet
    sessions/<session_id>/avant/<chemin>  copie de chaque fichier avant sa première modification
    sessions/<session_id>/archives/<date>/avant/...   générations déjà validées (gardées 14 jours)
    machine.json                          dernier relevé de la production (ports, dossiers de données)
    erreurs.log                           erreurs internes des hooks

Le chemin est fixe : les hooks, les scripts et les consignes des agents doivent désigner le même dossier,
et les commandes Bash ne reçoivent pas CLAUDE_PLUGIN_DATA. `VERIFIED_WORKFLOW_ETAT` le détourne (tests).
"""
import fnmatch
import json
import os
import re
import shutil
import subprocess
import time

HOME: str = os.path.expanduser("~")
ID_PLUGIN: str = "verified-workflow-verified-workflow"
# Emplacement des versions <= 1.4 (la variable ne sert qu'aux tests)
ANCIEN_ETAT: str = os.environ.get("VERIFIED_WORKFLOW_ANCIEN_ETAT") or "/tmp/claude-verified-workflow"
OUTILS_EDITION: frozenset = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit"})
AGENTS_DU_PLUGIN: frozenset = frozenset({
    "verifier", "verif-preuve", "verif-regression", "debugger", "debug-enqueteur",
    "searcher", "code-analyst", "auditor",
})
AGENTS_DE_VERIFICATION: frozenset = frozenset({"verifier", "verif-preuve", "verif-regression"})
# Projets hors git : un dossier parent contient un de ces fichiers
MARQUEURS: frozenset = frozenset({
    "package.json", "docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml",
    "dockerfile", "pyproject.toml", "manage.py",
})
# Documentation : rien à prouver en exécution (les fiches de vérification sont aussi des .md)
SUFFIXES_SANS_PREUVE: tuple = (".md",)
# Réglages par défaut, surchargés par `reglages.json` à la racine du plugin, puis par celui du dossier d'état
REGLAGES_PAR_DEFAUT: dict = {
    # Nombre de fichiers de projet modifiés à partir duquel le hook exige une vérification.
    # 1 : toute modification. 3 : seulement à partir de trois fichiers ; en dessous, les modifications
    # restent notées et sont vérifiées avec les suivantes, dès que le seuil est atteint.
    "seuil_fichiers": 1,
    # Sous ces deux bornes, la modification est dite « petite » : le verifier la prouve seul, sans agents.
    "petite_modification": {"fichiers": 2, "lignes": 60},
    # Fichiers dont la modification exige toujours une vérification, quel que soit le seuil
    "toujours_verifier": [
        "*.sql", "*/migrations/*", "*/drizzle/*", "schema.ts", "models.py",
        "docker-compose*.y*ml", "compose*.y*ml", "Dockerfile*",
        "package.json", "pyproject.toml", "requirements*.txt", ".env*", "*/auth/*",
    ],
}
JOURS_ARCHIVES: int = 14
JOURS_SESSIONS: int = 30
VALIDITE_RELEVE_S: int = 600


# --- Dossier d'état -------------------------------------------------------------------------------

def dossier_etat() -> str:
    return os.environ.get("VERIFIED_WORKFLOW_ETAT") or os.path.join(HOME, ".claude", "plugins", "data", ID_PLUGIN)


def creer(dossier: str) -> None:
    os.makedirs(dossier, mode=0o700, exist_ok=True)


def preparer_etat() -> str:
    """Crée le dossier d'état s'il manque et le garde privé (il contient des copies de fichiers de projet)."""
    racine: str = dossier_etat()
    creer(racine)
    try:
        if os.stat(racine).st_mode & 0o077:
            os.chmod(racine, 0o700)
    except OSError:
        pass
    return racine


def dossier_session(session_id: str, creation: bool = False) -> str:
    """Dossier de la session, ou "" si l'identifiant n'est pas un nom simple."""
    if not session_id or not re.fullmatch(r"[\w-]+", session_id):
        return ""
    racine: str = dossier_etat()
    dossier: str = os.path.join(racine, "sessions", session_id)
    if creation and not os.path.isdir(dossier):
        preparer_etat()
        creer(os.path.join(racine, "sessions"))
        creer(dossier)
        reprendre_ancien_etat(session_id, dossier)
    return dossier


def reprendre_ancien_etat(session_id: str, dossier: str) -> None:
    """Session ouverte sous une version <= 1.4 puis rechargée : reprend son repère et ses copies de /tmp."""
    ancien_repere: str = os.path.join(ANCIEN_ETAT, session_id + ".repere")
    anciennes_copies: str = os.path.join(ANCIEN_ETAT, session_id, "avant")
    try:
        if os.path.isfile(ancien_repere) and not os.path.exists(os.path.join(dossier, "repere")):
            shutil.move(ancien_repere, os.path.join(dossier, "repere"))
        if os.path.isdir(anciennes_copies) and not os.path.exists(os.path.join(dossier, "avant")):
            shutil.move(anciennes_copies, os.path.join(dossier, "avant"))
            shutil.rmtree(os.path.join(ANCIEN_ETAT, session_id), ignore_errors=True)
    except OSError:
        pass


def chemin_repere(session_id: str) -> str:
    return os.path.join(dossier_session(session_id), "repere")


def dossier_copies(session_id: str) -> str:
    return os.path.join(dossier_session(session_id), "avant")


# --- Journal --------------------------------------------------------------------------------------

def noter(session_id: str, evenement: str, **champs: object) -> None:
    """Ajoute une ligne au journal de la session. Une seule écriture O_APPEND : pas de ligne mêlée
    quand plusieurs hooks (sous-agents en parallèle) écrivent en même temps."""
    dossier: str = dossier_session(session_id, creation=True)
    if not dossier:
        return
    ligne: dict = {"t": round(time.time(), 3), "ev": evenement}
    ligne.update({k: v for k, v in champs.items() if v not in (None, "")})
    donnees: bytes = (json.dumps(ligne, ensure_ascii=False) + "\n").encode("utf-8")
    descripteur: int = os.open(os.path.join(dossier, "journal.jsonl"), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(descripteur, donnees)
    finally:
        os.close(descripteur)


def lire_journal(session_id: str) -> list:
    dossier: str = dossier_session(session_id)
    evenements: list = []
    if not dossier:
        return evenements
    try:
        with open(os.path.join(dossier, "journal.jsonl"), encoding="utf-8", errors="replace") as fichier:
            for ligne in fichier:
                try:
                    evenement = json.loads(ligne)
                except ValueError:
                    continue
                if isinstance(evenement, dict) and isinstance(evenement.get("t"), (int, float)) and evenement.get("ev"):
                    evenements.append(evenement)
    except OSError:
        pass
    return evenements


def noter_erreur(contexte: str, trace: str) -> None:
    """Une erreur interne ne doit jamais bloquer un outil : elle est consignée, le hook rend la main."""
    try:
        preparer_etat()
        with open(os.path.join(dossier_etat(), "erreurs.log"), "a", encoding="utf-8") as fichier:
            fichier.write("%s %s\n%s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), contexte, trace))
    except OSError:
        pass


# --- Réglages -------------------------------------------------------------------------------------

def fichiers_de_reglages() -> list:
    """Du moins prioritaire au plus prioritaire : le fichier livré avec le plugin, puis celui de l'utilisateur
    dans le dossier d'état, qu'une mise à jour du plugin ne remplace pas. `VERIFIED_WORKFLOW_REGLAGES`
    désigne un fichier unique (tests)."""
    force: str = os.environ.get("VERIFIED_WORKFLOW_REGLAGES") or ""
    if force:
        return [force]
    return [
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "reglages.json"),
        os.path.join(dossier_etat(), "reglages.json"),
    ]


def reglages() -> dict:
    """Réglages du plugin : valeurs par défaut, puis chaque fichier de `fichiers_de_reglages`.
    Un fichier absent, illisible ou une valeur du mauvais type laisse la valeur déjà retenue."""
    valeurs: dict = json.loads(json.dumps(REGLAGES_PAR_DEFAUT))
    for chemin in fichiers_de_reglages():
        try:
            with open(chemin, encoding="utf-8") as fichier:
                lus = json.load(fichier)
        except (OSError, ValueError):
            continue
        if isinstance(lus, dict):
            _appliquer_reglages(valeurs, lus)
    return valeurs


def _appliquer_reglages(valeurs: dict, lus: dict) -> None:
    if isinstance(lus.get("seuil_fichiers"), int) and not isinstance(lus.get("seuil_fichiers"), bool) and lus["seuil_fichiers"] >= 1:
        valeurs["seuil_fichiers"] = lus["seuil_fichiers"]
    petite = lus.get("petite_modification")
    if isinstance(petite, dict):
        for cle in ("fichiers", "lignes"):
            if isinstance(petite.get(cle), int) and not isinstance(petite.get(cle), bool) and petite[cle] >= 0:
                valeurs["petite_modification"][cle] = petite[cle]
    motifs = lus.get("toujours_verifier")
    if isinstance(motifs, list) and all(isinstance(m, str) for m in motifs):
        valeurs["toujours_verifier"] = motifs


def est_sensible(chemin: str, motifs: list) -> bool:
    """Le fichier fait partie de ceux qui exigent toujours une vérification (migrations, compose, dépendances...)."""
    nom: str = os.path.basename(chemin)
    return any(fnmatch.fnmatch(nom, motif) or fnmatch.fnmatch(chemin, motif) for motif in motifs)


# --- Agents ---------------------------------------------------------------------------------------

def nom_agent(agent_type: object) -> str:
    """`verified-workflow:verifier` -> `verifier` ; un agent installé hors plugin garde son nom."""
    return str(agent_type or "").rsplit(":", 1)[-1]


def est_verifier(agent_type: object) -> bool:
    return nom_agent(agent_type) == "verifier"


def de_la_verification(agent_type: object) -> bool:
    """Le verifier et les agents qu'il lance : leurs écritures ne sont pas des modifications à vérifier."""
    return nom_agent(agent_type) in AGENTS_DE_VERIFICATION


def du_plugin(agent_type: object) -> bool:
    texte: str = str(agent_type or "")
    if texte.startswith("verified-workflow:"):
        return True
    return ":" not in texte and texte in AGENTS_DU_PLUGIN


# --- Projets --------------------------------------------------------------------------------------

def _hors_champ(reel: str) -> bool:
    """Fichiers de Claude et fichiers temporaires : ni copie ni vérification."""
    for exclu in (os.path.join(HOME, ".claude"), "/tmp", dossier_etat()):
        prefixe: str = os.path.realpath(exclu) + os.sep
        if reel.startswith(prefixe):
            return True
    return False


def racine_projet(chemin: str) -> str:
    """Racine du projet qui contient `chemin`, ou "".

    Le dossier parent le plus proche qui a un `.git` (dépôt ou worktree) ; à défaut, le dossier parent
    le plus haut qui contient un marqueur de projet. Le dossier personnel n'est jamais une racine.
    """
    if not chemin:
        return ""
    reel: str = os.path.realpath(os.path.expanduser(chemin))
    if _hors_champ(reel):
        return ""
    dossier: str = os.path.dirname(reel)
    while not os.path.isdir(dossier):
        parent: str = os.path.dirname(dossier)
        if parent == dossier:
            return ""
        dossier = parent
    par_marqueur: str = ""
    while True:
        if os.path.exists(os.path.join(dossier, ".git")):
            return dossier
        if dossier != HOME:
            try:
                noms: list = os.listdir(dossier)
            except OSError:
                noms = []
            if any(nom.lower() in MARQUEURS for nom in noms):
                par_marqueur = dossier
        parent = os.path.dirname(dossier)
        if parent == dossier:
            return par_marqueur
        dossier = parent


def est_fichier_de_projet(chemin: str) -> bool:
    """Fichier dont la modification doit être prouvée : dans un projet, sans être de la documentation ni un
    réglage de Claude pour ce projet (`<racine>/.claude/` : fiche de vérification, settings, agents).
    Le code d'un worktree de `.claude/worktrees/` compte : sa racine est le worktree lui-même."""
    if not chemin or str(chemin).lower().endswith(SUFFIXES_SANS_PREUVE):
        return False
    racine: str = racine_projet(chemin)
    if not racine:
        return False
    relatif: str = os.path.relpath(os.path.realpath(os.path.expanduser(chemin)), racine)
    return not (relatif == ".claude" or relatif.startswith(".claude" + os.sep))


def fiches_possibles(racine: str) -> list:
    """Emplacements de la fiche de vérification : la racine du projet, puis le dépôt d'origine si la
    racine est un worktree git (une fiche non commitée n'existe que dans le dépôt d'origine)."""
    candidats: list = [os.path.join(racine, ".claude", "verification.md")]
    git: str = os.path.join(racine, ".git")
    if os.path.isfile(git):
        try:
            with open(git, encoding="utf-8") as fichier:
                contenu: str = fichier.read()
        except OSError:
            contenu = ""
        trouve = re.search(r"gitdir:\s*(.+?)/\.git/worktrees/", contenu)
        if trouve:
            candidats.append(os.path.join(trouve.group(1), ".claude", "verification.md"))
    return candidats


# --- Générations de copies ------------------------------------------------------------------------

def archiver(session_id: str) -> bool:
    """La version vérifiée devient la base : le repère est retiré, les copies d'avant passent en archive
    (récupérables pendant JOURS_ARCHIVES jours, seule version antérieure d'un projet sans git)."""
    dossier: str = dossier_session(session_id)
    if not dossier:
        return False
    fait: bool = False
    copies: str = os.path.join(dossier, "avant")
    if os.path.isdir(copies):
        cible: str = os.path.join(dossier, "archives", time.strftime("%Y%m%d-%H%M%S"))
        suffixe: int = 0
        while os.path.exists(cible + ("-%d" % suffixe if suffixe else "")):
            suffixe += 1
        cible += "-%d" % suffixe if suffixe else ""
        creer(cible)
        shutil.move(copies, os.path.join(cible, "avant"))
        fait = True
    try:
        os.remove(os.path.join(dossier, "repere"))
        fait = True
    except OSError:
        pass
    return fait


def menage() -> None:
    """Retire les archives de plus de JOURS_ARCHIVES jours et les sessions inactives depuis JOURS_SESSIONS
    jours. Au plus une fois par jour."""
    racine: str = dossier_etat()
    temoin: str = os.path.join(racine, "menage")
    maintenant: float = time.time()
    try:
        if maintenant - os.path.getmtime(temoin) < 86400:
            return
    except OSError:
        pass
    sessions: str = os.path.join(racine, "sessions")
    try:
        noms: list = os.listdir(sessions)
    except OSError:
        return
    for nom in noms:
        dossier: str = os.path.join(sessions, nom)
        try:
            dernier: float = max(
                os.path.getmtime(os.path.join(dossier, f)) for f in os.listdir(dossier)
            ) if os.listdir(dossier) else os.path.getmtime(dossier)
        except OSError:
            continue
        if maintenant - dernier > JOURS_SESSIONS * 86400:
            shutil.rmtree(dossier, ignore_errors=True)
            continue
        archives: str = os.path.join(dossier, "archives")
        try:
            for archive in os.listdir(archives):
                chemin: str = os.path.join(archives, archive)
                if maintenant - os.path.getmtime(chemin) > JOURS_ARCHIVES * 86400:
                    shutil.rmtree(chemin, ignore_errors=True)
        except OSError:
            pass
    try:
        with open(temoin, "w", encoding="utf-8"):
            pass
    except OSError:
        pass


# --- Production -----------------------------------------------------------------------------------

def _docker(arguments: list) -> str:
    try:
        # Deux appels au plus par relevé, dans un hook limité à 10 s
        resultat = subprocess.run(["docker"] + arguments, capture_output=True, text=True, timeout=4)
    except (OSError, subprocess.SubprocessError):
        return ""
    return resultat.stdout if resultat.returncode == 0 else ""


def relever_machine() -> dict:
    """Ce qui tourne en production : conteneurs Docker en marche, ports qu'ils publient sur l'hôte,
    dossiers de l'hôte qu'ils montent en écriture. Écrit le relevé dans machine.json."""
    conteneurs: list = []
    ports: set = set()
    montages: set = set()
    dossiers_compose: set = set()
    identifiants: list = _docker(["ps", "-q"]).split()
    details: list = []
    if identifiants:
        try:
            details = json.loads(_docker(["inspect"] + identifiants) or "[]")
        except ValueError:
            details = []
    for detail in details:
        etiquettes: dict = (detail.get("Config") or {}).get("Labels") or {}
        publies: list = []
        for liaisons in ((detail.get("NetworkSettings") or {}).get("Ports") or {}).values():
            for liaison in liaisons or []:
                try:
                    publies.append(int(liaison.get("HostPort")))
                except (TypeError, ValueError):
                    continue
        ports.update(publies)
        dossier_compose: str = etiquettes.get("com.docker.compose.project.working_dir") or ""
        if dossier_compose:
            dossiers_compose.add(dossier_compose)
        for montage in detail.get("Mounts") or []:
            source: str = montage.get("Source") or ""
            if montage.get("Type") == "bind" and montage.get("RW") and source and os.path.isdir(source):
                montages.add(source.rstrip("/"))
        conteneurs.append({
            "nom": (detail.get("Name") or "").lstrip("/"),
            "projet": etiquettes.get("com.docker.compose.project") or "",
            "dossier": dossier_compose,
            "ports": sorted(set(publies)),
        })
    # Dossiers de données : montés en écriture, hors système, et qui ne sont pas le code d'un projet
    donnees: set = set()
    if os.path.isdir("/srv/data"):
        donnees.add("/srv/data")
    for source in montages:
        if source.startswith(("/var/run", "/run", "/proc", "/sys", "/dev", "/etc", "/var/lib/docker")):
            continue
        if any(d == source or d.startswith(source + os.sep) for d in dossiers_compose):
            continue
        donnees.add(source)
    racines: list = sorted(d for d in donnees if not any(d != p and d.startswith(p + os.sep) for p in donnees))
    releve: dict = {
        "t": round(time.time(), 3),
        "ports_prod": sorted(ports),
        "donnees": racines,
        "dossiers_compose": sorted(dossiers_compose),
        "conteneurs": sorted(conteneurs, key=lambda c: (c["projet"], c["nom"])),
    }
    try:
        preparer_etat()
        provisoire: str = os.path.join(dossier_etat(), "machine.json.%d" % os.getpid())
        with open(provisoire, "w", encoding="utf-8") as fichier:
            json.dump(releve, fichier, ensure_ascii=False)
        os.replace(provisoire, os.path.join(dossier_etat(), "machine.json"))
    except OSError:
        pass
    return releve


def etat_machine() -> dict:
    """Dernier relevé s'il a moins de VALIDITE_RELEVE_S secondes, sinon un relevé neuf."""
    try:
        with open(os.path.join(dossier_etat(), "machine.json"), encoding="utf-8") as fichier:
            releve: dict = json.load(fichier)
        if time.time() - float(releve.get("t") or 0) < VALIDITE_RELEVE_S:
            return releve
    except (OSError, ValueError, TypeError):
        pass
    return relever_machine()
