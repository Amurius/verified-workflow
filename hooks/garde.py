#!/usr/bin/env python3
"""Garde-fous techniques pour les agents du plugin (verifier, verif-*, debugger, debug-enqueteur...).

Leurs consignes interdisent déjà ces actions ; ce module les refuse au moment de l'appel d'outil, parce
qu'en mode de permissions permissif rien d'autre ne les arrête. Second rang, pas un bac à sable : il lit
la ligne de commande telle qu'elle est écrite et refuse les formes directes. Une commande cachée dans un
script, une variable non résolue ou un `bash -c "..."` lui échappe.

Refusé à ces agents :
- docker : tout sauf la lecture (ps, logs, inspect, images, compose config/ps/logs...) ;
- sudo, su ; systemctl et service hors lecture ;
- git qui change l'état d'un dépôt (checkout, reset, stash, commit...), sauf dans un dossier de sortie ;
- curl et wget vers un hôte qui n'est pas cette machine, et en écriture vers un port de production ;
- installation de paquets (npm, bun, pip...) hors d'un dossier de sortie ;
- écriture ou copie dans les dossiers de données de la production ;
- pkill et killall (un motif désigne aussi les processus des conteneurs : scripts/arreter.sh <port>) ;
- outils Write et Edit hors d'un dossier de sortie.

Une règle vaut pour tout le monde, fil principal compris (`examiner_arret`) : un kill, pkill ou killall qui
atteindrait un processus de conteneur Docker est refusé. Ces processus tournent sous le même utilisateur
que Claude et sont visibles depuis l'hôte : `pkill -f "bun server/index.ts"` tuerait la production.

« Dossier de sortie » : sous /tmp, ou un dossier `scratchpad`. La commande est suivie segment par segment :
un `cd` change le dossier courant des commandes qui le suivent, une affectation `VAR=...` est retenue.
"""
import os
import re
import shlex

HOME: str = os.path.expanduser("~")
# Le plugin n'est pas installé au même endroit chez tout le monde : le chemin vient de ce fichier
ARRETER: str = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts", "arreter.sh")

DOCKER_LECTURE: frozenset = frozenset({
    "ps", "logs", "inspect", "images", "version", "info", "stats", "top", "port", "history", "save", "diff", "events",
})
DOCKER_GROUPES: dict = {
    "compose": frozenset({"config", "ps", "logs", "ls", "images", "top", "version", "port", "convert", "events"}),
    "container": frozenset({"ls", "list", "ps", "inspect", "logs", "top", "port", "stats", "diff"}),
    "image": frozenset({"ls", "list", "inspect", "history", "save"}),
    "network": frozenset({"ls", "list", "inspect"}),
    "volume": frozenset({"ls", "list", "inspect"}),
    "system": frozenset({"df", "info", "events"}),
    "context": frozenset({"ls", "show", "inspect"}),
}
DOCKER_OPTIONS_A_VALEUR: frozenset = frozenset({
    "-H", "--host", "-c", "--context", "--config", "-l", "--log-level", "--tlscacert", "--tlscert", "--tlskey",
})
COMPOSE_OPTIONS_A_VALEUR: frozenset = frozenset({
    "-f", "--file", "-p", "--project-name", "--project-directory", "--env-file", "--profile", "--ansi",
    "--progress", "--parallel",
})
SYSTEMCTL_LECTURE: frozenset = frozenset({
    "status", "is-active", "is-enabled", "is-failed", "is-system-running", "show", "cat", "list-units",
    "list-unit-files", "list-timers", "list-sockets", "list-dependencies", "list-jobs", "get-default",
})
GIT_OPTIONS_A_VALEUR: frozenset = frozenset({"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path"})
GIT_MODIFIANTS: frozenset = frozenset({
    "checkout", "switch", "reset", "stash", "commit", "merge", "rebase", "pull", "push", "restore", "clean",
    "cherry-pick", "revert", "am", "apply", "add", "rm", "mv", "bisect", "gc", "update-ref", "branch", "tag",
})
INSTALLATIONS: dict = {
    "npm": frozenset({"install", "i", "ci", "add", "uninstall", "remove", "rm", "update", "up", "link", "dedupe", "prune"}),
    "pnpm": frozenset({"install", "i", "add", "remove", "rm", "update", "up", "link", "prune"}),
    "yarn": frozenset({"install", "add", "remove", "up", "upgrade", "link"}),
    "bun": frozenset({"install", "i", "add", "remove", "rm", "update", "link"}),
    "pip": frozenset({"install", "uninstall"}),
    "pip3": frozenset({"install", "uninstall"}),
    "uv": frozenset({"add", "remove", "sync", "lock"}),
}
INSTALLATION_DOSSIER: frozenset = frozenset({"--prefix", "-C", "--cwd", "--dir", "--directory", "--project"})
ECRITURE_FICHIERS: frozenset = frozenset({
    "rm", "mv", "truncate", "chmod", "chown", "chgrp", "touch", "mkdir", "rmdir", "ln", "dd", "tee", "install",
    "shred", "cp", "rsync", "scp", "tar", "zip", "unzip", "gzip", "gunzip", "sqlite3",
})
ENVELOPPES: frozenset = frozenset({
    "time", "nohup", "exec", "builtin", "env", "nice", "ionice", "stdbuf", "setsid", "timeout",
    "xargs", "then", "do", "else", "elif", "if", "while", "until", "!",
})
HOTES_LOCAUX: frozenset = frozenset({"localhost", "127.0.0.1", "::1", "[::1]", "0.0.0.0"})
CURL_COURTES_A_VALEUR: str = "dHFowXmbcAeuTxKrzEDyYCt"
CURL_LONGUES_A_VALEUR: frozenset = frozenset({
    "--data", "--data-raw", "--data-binary", "--data-urlencode", "--data-ascii", "--json", "--header", "--form",
    "--form-string", "--output", "--write-out", "--request", "--max-time", "--cookie", "--cookie-jar",
    "--user-agent", "--referer", "--user", "--retry", "--retry-delay", "--retry-max-time", "--connect-timeout",
    "--upload-file", "--proxy", "--resolve", "--connect-to", "--config", "--cacert", "--cert", "--key",
    "--range", "--time-cond", "--limit-rate", "--max-redirs", "--interface", "--dns-servers", "--unix-socket",
    "--abstract-unix-socket", "--dump-header", "--trace", "--trace-ascii", "--stderr", "--output-dir", "--rate",
    "--speed-time", "--speed-limit", "--continue-at", "--max-filesize", "--proto", "--tls-max", "--ciphers",
    "--url", "--noproxy", "--proxy-user", "--oauth2-bearer", "--aws-sigv4", "--netrc-file", "--local-port",
    "--keepalive-time", "--expect100-timeout", "--request-target", "--socks5", "--socks5-hostname", "--capath",
    "--variable", "--expand-url",
})
CURL_ECRITURE: frozenset = frozenset({
    "--data", "--data-raw", "--data-binary", "--data-urlencode", "--data-ascii", "--json", "--form",
    "--form-string", "--upload-file",
})
WGET_LONGUES_A_VALEUR: frozenset = frozenset({
    "--output-document", "--output-file", "--directory-prefix", "--tries", "--timeout", "--user-agent",
    "--header", "--post-data", "--post-file", "--method", "--body-data", "--body-file", "--input-file",
    "--user", "--password", "--wait", "--quota", "--append-output",
})
WGET_COURTES_A_VALEUR: str = "OoaPtTUeiwQ"
METHODES_LECTURE: frozenset = frozenset({"GET", "HEAD", "OPTIONS"})
AFFECTATION = re.compile(r"^([A-Za-z_]\w*)=(.*)$", re.DOTALL)
SUBSTITUTION = re.compile(r"\$\(([^()]*)\)|`([^`]*)`")
HEREDOC_CITE = re.compile(r"<<-?\s*(['\"])([A-Za-z_][\w-]*)\1.*?\n[ \t]*\2[ \t]*(?=\n|\Z)", re.DOTALL)
VARIABLE = re.compile(r"\$\{(\w+)\}|\$(\w+)")


# --- Découpage de la ligne de shell ---------------------------------------------------------------

def segments(commande: str) -> list:
    """Découpe une ligne de shell en commandes simples : coupe sur ; | & et les sauts de ligne, hors
    guillemets, et saute le corps des heredocs (ce sont des données, pas des commandes)."""
    commande = commande.replace("\\\n", " ")  # une ligne continuée est une seule commande
    morceaux: list = []
    courant: list = []
    heredocs: list = []
    guillemet: str = ""
    i: int = 0
    n: int = len(commande)

    def fin() -> None:
        texte: str = "".join(courant).strip()
        if texte:
            morceaux.append(texte)
        courant.clear()

    def en_tete_de_commande() -> bool:
        return not "".join(courant).strip()

    while i < n:
        c: str = commande[i]
        if guillemet:
            courant.append(c)
            if c == "\\" and guillemet == '"' and i + 1 < n:
                courant.append(commande[i + 1])
                i += 2
                continue
            if c == guillemet:
                guillemet = ""
            i += 1
            continue
        if c == "\\" and i + 1 < n:
            courant.append(c + commande[i + 1])
            i += 2
            continue
        if c in "'\"":
            guillemet = c
            courant.append(c)
            i += 1
            continue
        if c == "$" and commande.startswith("${", i):
            fermeture: int = commande.find("}", i)
            fermeture = n - 1 if fermeture == -1 else fermeture
            courant.append(commande[i:fermeture + 1])
            i = fermeture + 1
            continue
        if commande.startswith("<<", i) and not commande.startswith("<<<", i):
            heredoc = re.match(r"<<(-?)\s*(['\"]?)([A-Za-z_][\w-]*)\2", commande[i:])
            if heredoc:
                heredocs.append((heredoc.group(3), bool(heredoc.group(1))))
                courant.append(heredoc.group(0))
                i += len(heredoc.group(0))
                continue
        if c == "\n":
            fin()
            i += 1
            for delimiteur, tabulations in heredocs:
                while i < n:
                    saut: int = commande.find("\n", i)
                    ligne: str = commande[i:(saut if saut != -1 else n)]
                    i = saut + 1 if saut != -1 else n
                    if (ligne.lstrip("\t") if tabulations else ligne).strip() == delimiteur:
                        break
            heredocs = []
            continue
        precedent: str = commande[i - 1] if i else ""
        suivant: str = commande[i + 1] if i + 1 < n else ""
        if c == "&" and (precedent == ">" or suivant == ">"):
            courant.append(c)  # redirection 2>&1 ou &>
            i += 1
            continue
        if c == "|" and precedent == ">":
            courant.append(c)  # redirection >|
            i += 1
            continue
        if c in ";|&()`":
            fin()
            i += 1
            continue
        if c in "{}":
            # Groupe de commandes `{ ...; }` seulement en tête de commande ; ailleurs c'est un caractère
            # ordinaire (%{http_code}, a.{js,ts})
            if en_tete_de_commande() and (c == "}" or not suivant or suivant.isspace()):
                fin()
            else:
                courant.append(c)
            i += 1
            continue
        if c == "$" and commande.startswith("$(", i):
            fin()
            i += 2
            continue
        if c == "#" and (not courant or courant[-1][-1:].isspace()):
            saut = commande.find("\n", i)
            i = n if saut == -1 else saut
            continue
        courant.append(c)
        i += 1
    fin()
    return morceaux


def analyser(segment: str) -> dict:
    """Décompose une commande simple : affectations en tête, nom, arguments (sans les redirections) et
    cibles des redirections en écriture.

    `VAR=x timeout 60 setsid -f docker ps 2>&1 > f` -> nom `docker`, arguments `["ps"]`, cibles `["f"]`.
    """
    try:
        lexeur = shlex.shlex(segment, posix=True, punctuation_chars=True)
        lexeur.whitespace_split = True
        lexeur.commenters = ""
        jetons: list = list(lexeur)
    except ValueError:
        jetons = segment.split()
    mots: list = []
    cibles: list = []
    i: int = 0
    while i < len(jetons):
        jeton: str = jetons[i]
        if jeton and not jeton.strip("<>&|") and ("<" in jeton or ">" in jeton):
            if mots and mots[-1].isdigit():
                mots.pop()  # descripteur de fichier : 2> ...
            cible: str = jetons[i + 1] if i + 1 < len(jetons) else ""
            if ">" in jeton and cible and not cible.isdigit() and cible != "-":
                cibles.append(cible)
            i += 2
            continue
        mots.append(jeton)
        i += 1
    affectations: dict = {}
    while mots:
        mot: str = mots[0]
        affectation = AFFECTATION.match(mot)
        if affectation:
            affectations[affectation.group(1)] = affectation.group(2)
            mots.pop(0)
        elif mot == "command" and mots[1:2] and mots[1] in ("-v", "-V"):
            return {"affectations": affectations, "nom": "command", "arguments": mots[1:], "cibles": cibles}
        elif mot in ENVELOPPES or mot == "command":
            mots.pop(0)
            while mots and (mots[0].startswith("-") or AFFECTATION.match(mots[0]) or
                            (mot in ("timeout", "nice", "ionice", "stdbuf", "xargs") and re.match(r"^[\d.]+[smhd]?$", mots[0]))):
                mots.pop(0)
        else:
            break
    nom: str = os.path.basename(mots[0]) if mots else ""
    return {"affectations": affectations, "nom": nom, "arguments": mots[1:], "cibles": cibles}


def developper(mot: str, variables: dict) -> object:
    """Le mot avec `~` et les variables connues remplacés, ou None s'il dépend d'une variable inconnue."""
    if mot == "~" or mot.startswith("~/"):
        mot = HOME + mot[1:]
    inconnue: list = []

    def valeur(trouve) -> str:
        nom: str = trouve.group(1) or trouve.group(2)
        if nom in variables:
            return variables[nom]
        inconnue.append(nom)
        return ""

    resultat: str = VARIABLE.sub(valeur, mot)
    return None if inconnue or "$" in resultat or "`" in resultat else resultat


def chemin_absolu(mot: str, repertoire: object, variables: dict) -> object:
    """Chemin absolu normalisé d'un argument, ou None s'il ne peut pas être établi."""
    developpe = developper(mot, variables)
    if not developpe:
        return None
    if not os.path.isabs(developpe):
        if not repertoire:
            return None
        developpe = os.path.join(str(repertoire), developpe)
    return os.path.normpath(developpe)


def dossier_de_sortie(repertoire: object) -> bool:
    """Vrai si ce dossier est un dossier de travail jetable : sous /tmp, ou un scratchpad de session."""
    if not repertoire:
        return False
    chemin: str = os.path.normpath(str(repertoire)) + "/"
    return chemin.startswith("/tmp/") or "/scratchpad/" in chemin


def _premier_argument(arguments: list, a_valeur: frozenset = frozenset()) -> tuple:
    """Premier argument qui n'est pas une option, et ce qui le suit."""
    i: int = 0
    while i < len(arguments):
        argument: str = arguments[i]
        if argument in a_valeur:
            i += 2
            continue
        if argument.startswith("-"):
            i += 1
            continue
        return argument, arguments[i + 1:]
    return "", []


def _valeur_option(arguments: list, options: frozenset) -> str:
    """Valeur de la première option de `options` présente (`--prefix X` ou `--prefix=X`), ou ""."""
    for i, argument in enumerate(arguments):
        cle, egal, valeur = argument.partition("=")
        if cle in options:
            return valeur if egal else (arguments[i + 1] if i + 1 < len(arguments) else "")
    return ""


# --- Règles ---------------------------------------------------------------------------------------

def _docker(nom: str, arguments: list) -> str:
    if nom == "docker-compose":
        groupe: str = "compose"
        reste: list = arguments
    else:
        sous, reste = _premier_argument(arguments, DOCKER_OPTIONS_A_VALEUR)
        if not sous or sous in DOCKER_LECTURE:
            return ""
        if sous not in DOCKER_GROUPES:
            return ("`docker %s` est refusé : les conteneurs en marche sont la production. Lecture seule "
                    "permise : docker ps, logs, inspect, images, compose config/ps/logs." % sous)
        groupe = sous
    sous2, _ = _premier_argument(reste, COMPOSE_OPTIONS_A_VALEUR if groupe == "compose" else frozenset())
    if not sous2 or sous2 in DOCKER_GROUPES[groupe]:
        return ""
    return ("`docker %s %s` est refusé : les conteneurs en marche sont la production, et son redéploiement "
            "revient à l'utilisateur. Permis : docker %s %s." % (groupe, sous2, groupe, ", ".join(sorted(DOCKER_GROUPES[groupe]))))


def _systemctl(nom: str, arguments: list) -> str:
    if nom == "service":
        return "" if arguments[1:2] == ["status"] else "`service` est refusé hors `status` : aucun service système n'est touché."
    sous, _ = _premier_argument(arguments, frozenset({"-n", "--lines", "-o", "--output", "-p", "--property", "-t", "--type", "--state", "-M", "-H"}))
    if not sous or sous in SYSTEMCTL_LECTURE:
        return ""
    return "`systemctl %s` est refusé : aucun service système n'est touché. Lecture permise : status, is-active, show, cat, list-units." % sous


def _git(arguments: list, repertoire: object, variables: dict) -> str:
    sous, reste = _premier_argument(arguments, GIT_OPTIONS_A_VALEUR)
    if sous not in GIT_MODIFIANTS:
        return ""
    ailleurs: str = _valeur_option(arguments[:arguments.index(sous)], frozenset({"-C"}))
    if ailleurs:
        repertoire = chemin_absolu(ailleurs, repertoire, variables)
    if dossier_de_sortie(repertoire):
        return ""
    if sous == "stash" and reste[:1] in (["list"], ["show"]):
        return ""
    if sous == "apply" and any(a in ("--check", "--stat", "--numstat", "--summary") for a in reste):
        return ""
    if sous == "clean" and any(a == "--dry-run" or re.match(r"^-[a-zA-Z]*n", a) for a in reste):
        return ""
    if sous == "bisect" and reste[:1] in (["log"], ["view"], ["visualize"]):
        return ""
    if sous == "branch" and not any(a in ("--delete", "--move", "--force", "--copy") or re.match(r"^-[a-zA-Z]*[dDmMfcC]", a) for a in reste):
        return ""
    if sous == "tag" and not any(a in ("--delete", "--force") or re.match(r"^-[a-zA-Z]*[df]", a) for a in reste):
        return ""
    return ("`git %s` est refusé dans un projet : l'état du dépôt ne doit pas changer. Pour une expérience, "
            "travaille dans un worktree de ton dossier de sortie (`git worktree add <sortie>/wt <commit>`, "
            "puis `git -C <sortie>/wt ...`)." % sous)


def _hote_et_port(url: str) -> tuple:
    """(hôte, port) d'un argument d'URL ; ("", None) si l'hôte dépend d'une variable du shell."""
    autorite = re.match(r"^(?:[a-zA-Z][a-zA-Z0-9+.-]*://)?([^/?#\s]*)", url)
    if not autorite or "$" in autorite.group(1) or "`" in autorite.group(1):
        return "", None  # hôte fourni par une variable ("$BASE/api", "${h[@]}") : indéterminé
    trouve = re.match(r"^(?:[^@]*@)?(\[[^\]]+\]|[^:]+)(?::(.+))?$", autorite.group(1))
    if not trouve:
        return "", None
    hote: str = trouve.group(1)
    port: object = trouve.group(2)
    try:
        port = int(port) if port else None
    except ValueError:
        port = None
    return hote.lower(), port


def _est_local(hote: str) -> bool:
    return hote in HOTES_LOCAUX or bool(re.match(r"^127\.\d+\.\d+\.\d+$", hote))


def _curl(nom: str, arguments: list, machine) -> str:
    urls: list = []
    methode: str = ""
    ecriture: bool = False
    lecture_forcee: bool = False
    socket_unix: str = ""
    i: int = 0
    while i < len(arguments):
        a: str = arguments[i]
        suivant: str = arguments[i + 1] if i + 1 < len(arguments) else ""
        if a in ("--help", "-h", "--version", "-V", "--manual", "-M") and (nom == "curl" or a.startswith("--")):
            return ""  # aucune requête
        if a.startswith("--"):
            cle, egal, valeur = a.partition("=")
            a_valeur: frozenset = CURL_LONGUES_A_VALEUR if nom == "curl" else WGET_LONGUES_A_VALEUR
            if cle in CURL_ECRITURE or cle in ("--post-data", "--post-file", "--body-data", "--body-file"):
                ecriture = True
            if cle == "--get":
                lecture_forcee = True
            if cle == "--head":
                methode = methode or "HEAD"
            if cle in ("--request", "--method"):
                methode = (valeur if egal else suivant).upper()
            if cle == "--url":
                urls.append(valeur if egal else suivant)
            if cle in ("--unix-socket", "--abstract-unix-socket"):
                socket_unix = valeur if egal else suivant
            i += 1 if (egal or cle not in a_valeur) else 2
            continue
        if a.startswith("-") and len(a) > 1:
            courtes: str = CURL_COURTES_A_VALEUR if nom == "curl" else WGET_COURTES_A_VALEUR
            saute: bool = False
            for position, lettre in enumerate(a[1:], 1):
                if nom == "curl":
                    if lettre == "G":
                        lecture_forcee = True
                    if lettre == "I":
                        methode = methode or "HEAD"
                    if lettre in "dFT":
                        ecriture = True
                if lettre in courtes:
                    colle: str = a[position + 1:]
                    if nom == "curl" and lettre == "X":
                        methode = (colle or suivant).upper()
                    saute = not colle
                    break
            i += 2 if saute else 1
            continue
        urls.append(a)
        i += 1
    if not methode:
        methode = "POST" if ecriture and not lecture_forcee else "GET"
    if socket_unix:
        # La requête part sur un socket local : l'hôte de l'URL ne compte pas. Le socket de Docker pilote la production.
        if "docker.sock" in socket_unix and methode not in METHODES_LECTURE:
            return "`%s` en %s sur le socket de Docker est refusé : les conteneurs en marche sont la production." % (nom, methode)
        return ""
    for url in urls:
        hote, port = _hote_et_port(url)
        if not hote:
            continue
        if not _est_local(hote):
            return ("`%s` vers « %s » est refusé : seules les adresses de cette machine (127.0.0.1, localhost) "
                    "sont permises, jamais un domaine public ni la production." % (nom, hote))
        if port is not None and methode not in METHODES_LECTURE and port in (machine().get("ports_prod") or []):
            return ("`%s` en %s vers le port %d est refusé : ce port est publié par un conteneur de production. "
                    "Les écritures se font sur une instance de dev lancée à part (port 4100-4199, données temporaires)."
                    % (nom, methode, port))
    return ""


def _installation(nom: str, arguments: list, repertoire: object, variables: dict) -> str:
    if nom in ("python", "python3") or re.match(r"^python3\.\d+$", nom):
        # python -m pip install ...
        if "-m" not in arguments or arguments[arguments.index("-m") + 1:arguments.index("-m") + 2] != ["pip"]:
            return ""
        nom, arguments = "pip", arguments[arguments.index("-m") + 2:]
    if nom not in INSTALLATIONS:
        return ""
    ailleurs: str = _valeur_option(arguments, INSTALLATION_DOSSIER)
    if ailleurs:
        repertoire = chemin_absolu(ailleurs, repertoire, variables)
    if dossier_de_sortie(repertoire):
        return ""
    sous, reste = _premier_argument(arguments, INSTALLATION_DOSSIER)
    if nom == "yarn" and not arguments:
        sous = "install"  # `yarn` seul installe
    elif nom == "uv" and sous == "pip":
        sous = reste[0] if reste else ""
        if sous not in ("install", "uninstall", "sync"):
            return ""
    elif sous not in INSTALLATIONS[nom]:
        return ""
    if any(a in ("--dry-run", "--check", "--help") for a in arguments):
        return ""
    return ("`%s %s` est refusé dans un projet : aucune dépendance n'y est installée ni retirée. Travaille dans "
            "une copie de ton dossier de sortie, ou lance l'outil sans l'installer (bunx, npx, uvx, `uv run --with`)."
            % (nom, sous))


def _sous_un_dossier(chemin: object, dossiers: list) -> str:
    """Le dossier de données qui contient `chemin`, ou ""."""
    if not chemin:
        return ""
    for dossier in dossiers:
        racine: str = dossier.rstrip("/")
        if chemin == racine or str(chemin).startswith(racine + os.sep):
            return dossier
    return ""


def _lecture_seule(nom: str, arguments: list) -> bool:
    """Formes qui lisent sans écrire, pour des commandes de la liste ECRITURE_FICHIERS."""
    drapeaux: str = "".join(a[1:] for a in arguments if re.match(r"^-[a-zA-Z]+$", a))
    longs: set = {a.partition("=")[0] for a in arguments if a.startswith("--")}
    if nom in ("gzip", "gunzip"):
        return bool(set(drapeaux) & set("clt") or longs & {"--stdout", "--to-stdout", "--list", "--test"})
    if nom == "tar":
        premier: str = arguments[0] if arguments else ""
        mode: str = drapeaux + (premier if premier and not premier.startswith("-") else "")
        return ("t" in mode or "--list" in longs) and not (set(mode) & set("xcru") or longs & {"--extract", "--create", "--append", "--update"})
    if nom == "unzip":
        return bool(set(drapeaux) & set("ltZp"))
    return False


def _donnees(analyse: dict, repertoire: object, variables: dict, machine) -> str:
    nom: str = analyse["nom"]
    arguments: list = analyse["arguments"]
    cibles: list = analyse["cibles"]
    modifie: bool = nom in ECRITURE_FICHIERS and not _lecture_seule(nom, arguments)
    modifie = modifie or (nom in ("sed", "perl") and any(re.match(r"^-[a-zA-Z]*i", a) or a.startswith("--in-place") for a in arguments))
    chemins: list = []
    if nom == "find" and any(a in ("-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fprintf", "-fls") for a in arguments):
        modifie = True
        for argument in arguments:  # les points de départ précèdent la première expression
            if argument.startswith(("-", "(", "!")):
                break
            chemins.append(argument)
    elif modifie:
        chemins = [a.partition("=")[2] if nom == "dd" and a.startswith("of=") else a for a in arguments if not a.startswith("-")]
        if nom == "dd":
            chemins = [a.partition("=")[2] for a in arguments if a.startswith("of=")]
    if not cibles and not (modifie and chemins):
        return ""
    dossiers: list = machine().get("donnees") or []
    if not dossiers:
        return ""
    for cible in cibles:
        touche: str = _sous_un_dossier(chemin_absolu(cible, repertoire, variables), dossiers)
        if touche:
            return "Écriture refusée dans %s : ce sont les données de la production." % touche
    for chemin in chemins if modifie else []:
        touche = _sous_un_dossier(chemin_absolu(chemin, repertoire, variables), dossiers)
        if touche:
            return ("`%s` sur %s est refusé : ce sont les données de la production, qui ne se modifient ni ne se "
                    "copient. Lecture en base par scripts/lire-base.py ; données de test dans ton dossier de sortie."
                    % (nom, touche))
    return ""


def examiner_commande(commande: str, cwd: str, machine, profondeur: int = 0) -> str:
    """Raison du refus d'une commande Bash, ou "". `machine` est une fonction qui renvoie le relevé de la
    production : elle n'est appelée que si une règle en a besoin."""
    commande = commande.replace("\\\n", " ")
    if profondeur < 3:
        # Une substitution $(...) ou `...` s'exécute même entre guillemets doubles ; le corps d'un
        # heredoc à délimiteur cité est du texte, pas du shell
        for interieur in SUBSTITUTION.findall(HEREDOC_CITE.sub(" ", commande)):
            raison: str = examiner_commande(interieur[0] or interieur[1], cwd, machine, profondeur + 1)
            if raison:
                return raison
    variables: dict = {"HOME": HOME}
    repertoire: object = cwd or None
    for segment in segments(commande):
        analyse: dict = analyser(segment)
        nom: str = analyse["nom"]
        arguments: list = analyse["arguments"]
        if not nom:
            for cle, valeur in analyse["affectations"].items():
                developpee = developper(valeur, variables)
                if developpee is None:
                    variables.pop(cle, None)
                else:
                    variables[cle] = developpee
            if analyse["cibles"]:
                raison = _donnees(analyse, repertoire, variables, machine)
                if raison:
                    return raison
            continue
        if nom in ("cd", "pushd"):
            cible, _ = _premier_argument(arguments)
            repertoire = chemin_absolu(cible, repertoire, variables) if cible and cible != "-" else (HOME if not cible else None)
            continue
        raison = ""
        if nom in ("docker", "docker-compose"):
            raison = _docker(nom, arguments)
        elif nom in ("sudo", "su", "doas", "pkexec"):
            raison = "`%s` est refusé : aucune commande avec des droits élevés." % nom
        elif nom in ("pkill", "killall"):
            raison = ("`%s` est refusé : un motif désigne aussi les instances des autres vérifications et les "
                      "processus des conteneurs de production. Arrête ce que tu as lancé par son port, "
                      "`bash %s <port>`, ou par son numéro, `kill <pid>`." % (nom, ARRETER))
        elif nom in ("systemctl", "service"):
            raison = _systemctl(nom, arguments)
        elif nom == "git":
            raison = _git(arguments, repertoire, variables)
        elif nom in ("curl", "wget"):
            raison = _curl(nom, arguments, machine)
        elif nom in INSTALLATIONS or nom.startswith("python"):
            raison = _installation(nom, arguments, repertoire, variables)
        raison = raison or _donnees(analyse, repertoire, variables, machine)
        if raison:
            return raison
    return ""


def processus_de_conteneurs() -> list:
    """(pid, nom court, ligne de commande) des processus de conteneurs que cet utilisateur peut tuer."""
    trouves: list = []
    moi: int = os.getuid()
    try:
        numeros: list = [n for n in os.listdir("/proc") if n.isdigit()]
    except OSError:
        return trouves
    for numero in numeros:
        try:
            if os.stat("/proc/" + numero).st_uid != moi:
                continue
            with open("/proc/%s/cgroup" % numero, encoding="utf-8", errors="replace") as fichier:
                groupe: str = fichier.read()
            if not re.search(r"docker|containerd|libpod", groupe):
                continue
            with open("/proc/%s/comm" % numero, encoding="utf-8", errors="replace") as fichier:
                nom: str = fichier.read().strip()
            with open("/proc/%s/cmdline" % numero, "rb") as fichier:
                ligne: str = fichier.read().replace(b"\0", b" ").decode("utf-8", "replace").strip()
        except OSError:
            continue
        trouves.append((int(numero), nom, ligne))
    return trouves


PKILL_OPTIONS_A_VALEUR: frozenset = frozenset({
    "-u", "-U", "-g", "-G", "-P", "-s", "-t", "--signal", "--euid", "--uid", "--group", "--parent",
    "--pgroup", "--session", "--terminal", "--ns", "--nslist", "-c", "--cgroup", "-r", "--runstates", "-O", "--older",
})


def examiner_arret(commande: str, conteneurs=processus_de_conteneurs) -> str:
    """Raison du refus d'un kill, pkill ou killall qui atteindrait un processus de conteneur, ou "".
    Vaut pour tous les agents et pour le fil principal."""
    if not re.search(r"\b(kill|pkill|killall)\b", commande):
        return ""
    commande = commande.replace("\\\n", " ")
    textes: list = [commande] + [a or b for a, b in SUBSTITUTION.findall(HEREDOC_CITE.sub(" ", commande))]
    processus: object = None
    for texte in textes:
        for segment in segments(texte):
            analyse: dict = analyser(segment)
            nom: str = analyse["nom"]
            arguments: list = analyse["arguments"]
            if nom not in ("kill", "pkill", "killall", "pgrep", "pidof"):
                continue
            if processus is None:
                processus = conteneurs()
            if not processus:
                return ""
            if nom == "kill":
                numeros: set = {abs(int(a)) for a in arguments if re.fullmatch(r"-?\d+", a) and not (a.startswith("-") and len(a) <= 3)}
                touches: list = [p for p in processus if p[0] in numeros]
            else:
                motif, _ = _premier_argument(arguments, PKILL_OPTIONS_A_VALEUR)
                if not motif or "$" in motif:
                    continue
                ligne_entiere: bool = any(a == "--full" or re.match(r"^-[a-zA-Z]*f", a) for a in arguments)
                exact: bool = nom in ("killall", "pidof") or any(a == "--exact" or re.match(r"^-[a-zA-Z]*x", a) for a in arguments)

                def touche(un: tuple) -> bool:
                    cible: str = un[2] if ligne_entiere else un[1]
                    if exact:
                        return cible == motif or (not ligne_entiere and cible == motif[:15])
                    try:
                        return re.search(motif, cible) is not None
                    except re.error:
                        return motif in cible

                touches = [p for p in processus if touche(p)]
            if touches:
                exemples: str = " ; ".join("%d %s" % (p[0], p[2][:50]) for p in touches[:3])
                return ("`%s` atteindrait %d processus de conteneur Docker, c'est-à-dire la production (%s). Ces "
                        "processus tournent sous le même utilisateur et sont visibles d'ici. Pour arrêter une "
                        "instance de dev : `bash %s <port>`, "
                        "ou `kill <pid>` avec le numéro du processus que tu as lancé. Un conteneur s'arrête avec "
                        "docker, à la main de l'utilisateur." % (nom, len(touches), exemples, ARRETER))
    return ""


def examiner_ecriture(chemin: str, dossier_etat: str) -> str:
    """Raison du refus d'un Write/Edit d'un agent du plugin, ou ""."""
    if not chemin:
        return ""
    reel: str = os.path.realpath(os.path.expanduser(chemin))
    if dossier_de_sortie(reel) or reel.startswith(os.path.realpath(dossier_etat) + os.sep):
        return ""
    return ("Écriture refusée dans %s : un agent de vérification ou d'enquête n'écrit que dans son dossier "
            "de sortie (sous /tmp), jamais dans un projet." % chemin)
