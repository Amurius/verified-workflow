#!/usr/bin/env python3
"""Lecture seule d'une base pour les agents de vérification et de débogage.

Trois protections :
1. la requête est analysée caractère par caractère : une seule instruction, qui commence par SELECT ou
   WITH, sans mot-clé d'écriture, sans barre oblique inverse, sans `$`, sans commentaire. Les littéraux
   ('...') et les identifiants cités ("...") sont reconnus avec leur doublement ('' et ""), si bien
   qu'un point-virgule ou un mot-clé ne peut pas se cacher dedans ;
2. la base est ouverte en lecture seule : SQLite `mode=ro` ; Postgres `BEGIN READ ONLY` puis ROLLBACK,
   dans une session où toute transaction est en lecture seule par défaut, la requête étant passée à psql
   par `-c` (aucune commande `\\...` de psql n'est interprétée) ;
3. si le conteneur Postgres a un rôle `lecture_seule`, la connexion se fait sous ce rôle : les droits
   de la base interdisent alors toute écriture, quel que soit le texte de la requête.

Usage :
  python3 -I lire-base.py sqlite <fichier.sqlite> "SELECT ..." [--max 200]
  python3 -I lire-base.py pg <conteneur> <base> "SELECT ..." [--max 200]
Postgres passe par `docker exec` et le `POSTGRES_USER` du conteneur : aucun mot de passe à fournir.
"""
import argparse
import os
import re
import sqlite3
import subprocess
import sys
import urllib.parse

ROLE_LECTURE: str = "lecture_seule"

INTERDITS = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|TRUNCATE|MERGE|ALTER|CREATE|UPSERT|GRANT|REVOKE|VACUUM|ATTACH|DETACH|"
    r"PRAGMA|REINDEX|ANALYZE|COPY|CALL|DO|LOCK|LISTEN|NOTIFY|SET|RESET|INTO|EXECUTE|PREPARE|DEALLOCATE|"
    r"REFRESH|CLUSTER|DISCARD|CHECKPOINT|LOAD|COMMIT|ROLLBACK|SAVEPOINT|RELEASE|ABORT)\b"
    r"|\b(pg_\w+|dblink\w*|lo_\w+|set_config|nextval|setval|\w+_to_xml\w*|load_extension)\s*\(",
    re.IGNORECASE,
)


# Fonctions à effet de bord : refusées aussi quand leur nom est écrit entre guillemets ("pg_read_file"(...))
FONCTION_INTERDITE = re.compile(r"^(pg_\w+|dblink\w*|lo_\w+|set_config|nextval|setval|\w+_to_xml\w*|load_extension)$", re.IGNORECASE)


class Refus(Exception):
    """La requête n'est pas une lecture simple."""


def _refuser_fonction_citee(nom: str, suite: str) -> None:
    if suite.lstrip().startswith("(") and FONCTION_INTERDITE.match(nom.strip()):
        raise Refus("fonction interdite « %s ». Ce script ne fait que lire." % nom.strip())


def sans_litteraux(requete: str, moteur: str = "pg") -> str:
    """Renvoie la requête dont les littéraux et les identifiants cités sont vidés.

    Refuse ce qui permettrait de faire lire autre chose au serveur qu'à ce contrôle : barre oblique
    inverse (chaînes E'...', commandes de psql), `$` (chaînes $$...$$), commentaires, guillemet non fermé.
    Les identifiants cités suivent le moteur : "..." partout, et pour SQLite aussi [...] et `...`.
    """
    if "\x00" in requete:
        raise Refus("caractère nul.")
    if "\\" in requete:
        raise Refus("barre oblique inverse interdite (chaînes E'...', commandes \\ de psql).")
    sortie: list = []
    i: int = 0
    n: int = len(requete)
    while i < n:
        c: str = requete[i]
        if c in "'\"" or (c == "`" and moteur == "sqlite"):
            # Littéral ou identifiant cité : le guillemet se double pour s'écrire lui-même
            j: int = i + 1
            while True:
                if j >= n:
                    raise Refus("guillemet %s non fermé." % c)
                if requete[j] == c:
                    if j + 1 < n and requete[j + 1] == c:
                        j += 2
                        continue
                    break
                j += 1
            if c != "'":
                _refuser_fonction_citee(requete[i + 1:j].replace(c + c, c), requete[j + 1:])
            sortie.append("''" if c == "'" else '""')
            i = j + 1
            continue
        if c == "[" and moteur == "sqlite":
            # Identifiant cité à la manière de SQLite : jusqu'au premier crochet fermant
            j = requete.find("]", i + 1)
            if j == -1:
                raise Refus("crochet [ non fermé.")
            _refuser_fonction_citee(requete[i + 1:j], requete[j + 1:])
            sortie.append('""')
            i = j + 1
            continue
        if c == "`":
            raise Refus("accent grave interdit.")
        if c == "$":
            raise Refus("caractère $ interdit hors d'un littéral (chaînes $$...$$, paramètres).")
        if requete.startswith("--", i) or requete.startswith("/*", i):
            raise Refus("commentaire interdit : retire-le de la requête.")
        sortie.append(c)
        i += 1
    return "".join(sortie)


def controler(requete: str, moteur: str = "pg") -> None:
    """Lève Refus si la requête n'est pas une lecture simple en une seule instruction."""
    nettoyee: str = sans_litteraux(requete, moteur).strip()
    if ";" in nettoyee.rstrip(";").rstrip():
        raise Refus("une seule requête.")
    if not re.match(r"(SELECT|WITH)\b", nettoyee, re.IGNORECASE):
        raise Refus("la requête doit commencer par SELECT ou WITH.")
    trouve = INTERDITS.search(nettoyee)
    if trouve:
        raise Refus(
            "mot-clé interdit « %s ». Ce script ne fait que lire. Si c'est un nom de colonne, "
            "écris-le entre guillemets doubles." % trouve.group(0).rstrip("( ")
        )


def lire_sqlite(fichier: str, requete: str, maximum: int) -> None:
    # Le chemin est encodé : un « ? » ou un « # » dans un nom de fichier ne doit pas faire perdre mode=ro
    connexion = sqlite3.connect("file:%s?mode=ro" % urllib.parse.quote(os.path.abspath(fichier)), uri=True, timeout=5)
    try:
        curseur = connexion.execute(requete)
        print("# sqlite " + fichier)
        print("\t".join(c[0] for c in curseur.description or []))
        n: int = 0
        for ligne in curseur:
            if n >= maximum:
                print("# ... tronqué à %d lignes" % maximum)
                break
            print("\t".join("NULL" if v is None else str(v) for v in ligne))
            n += 1
        print("# %d ligne(s)" % n)
    finally:
        connexion.close()


# Exécuté dans le conteneur. $1 = base, $2 = requête déjà enveloppée, $3 = rôle de lecture.
# Chaque -c part au serveur tel quel : psql n'y interprète ni commande \ ni variable.
SCRIPT_PG: str = (
    'u="$POSTGRES_USER"; '
    'if psql -X -q -A -t -U "$POSTGRES_USER" -d "$1" '
    "-c \"SELECT 1 FROM pg_roles WHERE rolname = '$3' AND rolcanlogin\" 2>/dev/null | grep -qx 1; "
    'then u="$3"; fi; '
    'echo "# role $u"; '
    'PGOPTIONS="-c default_transaction_read_only=on" '
    'psql -v ON_ERROR_STOP=1 -X -q -A -F "$(printf \'\\t\')" -P footer=off -U "$u" -d "$1" '
    '-c "BEGIN READ ONLY" -c "$2" -c "ROLLBACK"'
)


def lire_pg(conteneur: str, base: str, requete: str, maximum: int) -> None:
    enveloppee: str = "SELECT * FROM (%s) AS lecture LIMIT %d" % (requete.strip().rstrip(";").rstrip(), maximum + 1)
    commande: list = ["docker", "exec", "-i", conteneur, "sh", "-c", SCRIPT_PG, "sh", base, enveloppee, ROLE_LECTURE]
    resultat = subprocess.run(commande, capture_output=True, text=True, timeout=90)
    if resultat.returncode != 0:
        raise OSError(resultat.stderr.strip() or "psql rc=%d" % resultat.returncode)
    lignes: list = [l for l in resultat.stdout.splitlines() if l not in ("BEGIN", "ROLLBACK")]
    role: str = ""
    if lignes and lignes[0].startswith("# role "):
        role = lignes.pop(0)[len("# role "):]
    print("# postgres %s / %s (rôle %s)" % (conteneur, base, role or "?"))
    if lignes:
        print(lignes[0])
    donnees: list = lignes[1:]
    for ligne in donnees[:maximum]:
        print(ligne)
    if len(donnees) > maximum:
        print("# ... tronqué à %d lignes" % maximum)
    print("# %d ligne(s)" % min(len(donnees), maximum))


def main() -> None:
    parseur = argparse.ArgumentParser()
    sous = parseur.add_subparsers(dest="moteur", required=True)
    s = sous.add_parser("sqlite")
    s.add_argument("fichier")
    s.add_argument("requete")
    p = sous.add_parser("pg")
    p.add_argument("conteneur")
    p.add_argument("base")
    p.add_argument("requete")
    for sp in (s, p):
        sp.add_argument("--max", type=int, default=200)
    args = parseur.parse_args()
    try:
        controler(args.requete, args.moteur)
    except Refus as refus:
        sys.exit("REFUS : %s" % refus)
    try:
        if args.moteur == "sqlite":
            lire_sqlite(args.fichier, args.requete, args.max)
        else:
            lire_pg(args.conteneur, args.base, args.requete, args.max)
    except (sqlite3.Error, OSError, subprocess.SubprocessError) as erreur:
        sys.exit("ERREUR : %s" % erreur)


if __name__ == "__main__":
    main()
