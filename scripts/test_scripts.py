#!/usr/bin/env python3
"""Contrôle de lire-base.py : `python3 scripts/test_scripts.py [--postgres]`.

Sans option : le filtre de requêtes et la lecture SQLite, sur une base temporaire.
Avec `--postgres` : lance un conteneur Postgres jetable (sans port publié, données en mémoire), y vérifie
la lecture, le refus d'écrire en transaction en lecture seule et l'effet du rôle `lecture_seule`, puis le
supprime. Aucune base existante n'est touchée.
"""
import contextlib
import importlib.util
import io
import os
import sqlite3
import subprocess
import sys
import tempfile
import time

ICI: str = os.path.dirname(os.path.abspath(__file__))
SCRIPT: str = os.path.join(ICI, "lire-base.py")
spec = importlib.util.spec_from_file_location("lire_base", SCRIPT)
lire_base = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lire_base)


def acceptee(requete: str, moteur: str = "pg") -> bool:
    try:
        lire_base.controler(requete, moteur)
    except lire_base.Refus:
        return False
    return True


def lancer(*arguments: str) -> tuple:
    resultat = subprocess.run([sys.executable, "-I", SCRIPT] + list(arguments), capture_output=True, text=True)
    return resultat.returncode, resultat.stdout + resultat.stderr


def essais_filtre() -> None:
    lectures: list = [
        "SELECT id, name FROM users WHERE id = 3 LIMIT 5",
        "select count(*) from cards;",
        "WITH r AS (SELECT 1 AS a) SELECT a FROM r",
        "SELECT 'point; virgule', 'l''apostrophe', \"colonne; bizarre\" FROM t",
        'SELECT created_at, "set", "do" FROM cards LIMIT 1',
        "SELECT reset_at, updated_at, do_count FROM stats",
        "SELECT relname FROM pg_class LIMIT 3",
        "SELECT 'DROP TABLE x' AS texte",
    ]
    for requete in lectures:
        assert acceptee(requete), "refus inattendu : %s" % requete
    ecritures: list = [
        "DELETE FROM users",
        "SELECT 1; DROP TABLE users",
        "WITH d AS (DELETE FROM t RETURNING *) SELECT * FROM d",
        "SELECT * INTO copie FROM t",
        "SELECT set FROM cards",
        "SELECT pg_read_file('/etc/passwd')",
        "SELECT lo_import('/etc/passwd')",
        "SELECT nextval('seq')",
        "SELECT query_to_xml('delete from t', true, false, '')",
        "PRAGMA table_info(t)",
        # Ce que l'ancien filtre laissait passer :
        "SELECT E'\\'' AS a) AS lecture; COMMIT; DROP TABLE t; SELECT * FROM (SELECT '1",  # chaîne E'' qui cache un ;
        "SELECT 1 AS \"x'\") AS lecture; COMMIT; DROP TABLE t; SELECT ('",                 # identifiant cité qui cache un ;
        "SELECT 1 \\! touch /tmp/intrus",                                                    # commande \\! de psql
        "SELECT 1 \\gexec",
        "SELECT $$;$$",
        "SELECT 1 -- commentaire",
        "SELECT /* x */ 1",
        "SELECT 'non fermé",
    ]
    for requete in ecritures:
        assert not acceptee(requete), "aurait dû être refusée : %s" % requete
    # Fonctions à effet de bord appelées par un nom cité
    for requete in ('SELECT "pg_terminate_backend"(pid) FROM pg_stat_activity', 'SELECT "pg_reload_conf"()',
                    "SELECT \"pg_read_file\"('/etc/passwd')", "SELECT pg_catalog.\"dblink_exec\" ('dbname=x', 'DELETE FROM t')"):
        assert not acceptee(requete), "aurait dû être refusée : %s" % requete
    assert acceptee('SELECT (ARRAY[1, 2])[1], "pg_class".relname FROM pg_class LIMIT 1')
    assert not acceptee("SELECT `a` FROM t"), "l'accent grave n'existe pas en Postgres"
    # Identifiants cités de SQLite : [..] et `..` ne doivent pas désynchroniser l'analyse
    for requete in ("SELECT 1 AS [a']; DELETE FROM t; SELECT 1 AS [b']",
                    "WITH x AS (SELECT 1 AS [a']) DELETE FROM t WHERE 1 IN (SELECT 1 AS [b'])",
                    "SELECT 1 AS `a'`; DELETE FROM t; SELECT 1 AS `b'`", "SELECT [load_extension]('x')"):
        assert not acceptee(requete, "sqlite"), "aurait dû être refusée (sqlite) : %s" % requete
    assert acceptee("SELECT [ma colonne], `autre` FROM t WHERE nom = 'l''été'", "sqlite")


def essais_sqlite(dossier: str) -> None:
    base: str = os.path.join(dossier, "essai ?# 1.sqlite")  # un nom qui casserait une URI non encodée
    connexion = sqlite3.connect(base)
    connexion.execute("CREATE TABLE t (id INTEGER, nom TEXT)")
    connexion.executemany("INSERT INTO t VALUES (?, ?)", [(1, "un"), (2, None), (3, "trois")])
    connexion.commit()
    connexion.close()
    code, sortie = lancer("sqlite", base, "SELECT id, nom FROM t ORDER BY id")
    assert code == 0 and "1\tun" in sortie and "2\tNULL" in sortie and "# 3 ligne(s)" in sortie, sortie
    code, sortie = lancer("sqlite", base, "SELECT id FROM t ORDER BY id", "--max", "2")
    assert code == 0 and "tronqué à 2 lignes" in sortie, sortie
    code, sortie = lancer("sqlite", base, "DELETE FROM t")
    assert code != 0 and "REFUS" in sortie, sortie
    # Même si le filtre laissait passer une écriture, l'ouverture en lecture seule la refuse
    try:
        lire_base.lire_sqlite(base, "DELETE FROM t", 10)
        raise AssertionError("écriture acceptée sur une base ouverte en lecture seule")
    except sqlite3.OperationalError as erreur:
        assert "readonly" in str(erreur), erreur


def docker(*arguments: str, entree: str = "") -> tuple:
    resultat = subprocess.run(["docker"] + list(arguments), input=entree, capture_output=True, text=True, timeout=120)
    return resultat.returncode, resultat.stdout + resultat.stderr


def essais_postgres() -> None:
    conteneur: str = "vw-essai-pg-%d" % os.getpid()
    image: str = next((i for i in ("postgres:17-alpine", "postgres:16") if docker("image", "inspect", i)[0] == 0), "")
    assert image, "aucune image postgres locale"
    code, sortie = docker("run", "-d", "--rm", "--name", conteneur, "--tmpfs", "/var/lib/postgresql/data",
                          "-e", "POSTGRES_USER=essai", "-e", "POSTGRES_PASSWORD=essai", "-e", "POSTGRES_DB=essai", image)
    assert code == 0, sortie
    try:
        def psql(sql: str) -> str:
            code, sortie = docker("exec", "-i", conteneur, "psql", "-X", "-q", "-A", "-t", "-v", "ON_ERROR_STOP=1", "-U", "essai", "-d", "essai", entree=sql)
            assert code == 0, sortie
            return sortie.strip()

        for _ in range(60):
            if docker("exec", conteneur, "psql", "-U", "essai", "-d", "essai", "-c", "SELECT 1")[0] == 0:
                break
            time.sleep(0.5)
        psql("CREATE TABLE t (id int, nom text); INSERT INTO t VALUES (1, 'un'), (2, NULL), (3, 'trois'); CREATE SEQUENCE compteur;")

        code, sortie = lancer("pg", conteneur, "essai", "SELECT id, nom FROM t ORDER BY id")
        assert code == 0 and "rôle essai" in sortie and "1\tun" in sortie and "# 3 ligne(s)" in sortie, sortie
        code, sortie = lancer("pg", conteneur, "essai", "SELECT id FROM t ORDER BY id", "--max", "2")
        assert code == 0 and "tronqué à 2 lignes" in sortie and "# 2 ligne(s)" in sortie, sortie
        code, sortie = lancer("pg", conteneur, "essai", "SELECT 1 \\! touch /tmp/intrus")
        assert code != 0 and "REFUS" in sortie, sortie

        # Filtre contourné par hypothèse : la transaction en lecture seule refuse l'écriture
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                lire_base.lire_pg(conteneur, "essai", "SELECT nextval('compteur')", 10)
            raise AssertionError("écriture acceptée en transaction READ ONLY")
        except OSError as erreur:
            assert "read-only transaction" in str(erreur), erreur
        # ... et une commande \\ de psql n'est pas interprétée (la requête part par -c)
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                lire_base.lire_pg(conteneur, "essai", "SELECT 1 \\! touch /tmp/intrus", 10)
        except OSError:
            pass
        assert docker("exec", conteneur, "test", "-e", "/tmp/intrus")[0] != 0, "une commande \\! de psql a été exécutée"
        assert psql("SELECT count(*) FROM t") == "3"

        # Plusieurs instructions dans une requête qui aurait trompé le filtre : hors de la transaction
        # ouverte par le script, la session reste en lecture seule par défaut
        injection: str = "SELECT 1) AS lecture; COMMIT; DELETE FROM t WHERE id = 3; SELECT * FROM (SELECT 2"
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                lire_base.lire_pg(conteneur, "essai", injection, 10)
            raise AssertionError("écriture acceptée après un COMMIT injecté")
        except OSError as erreur:
            assert "read-only transaction" in str(erreur), erreur
        assert psql("SELECT count(*) FROM t") == "3"
        # Ce qui reste possible sans rôle dédié : la même requête qui lève elle-même la lecture seule
        forcee: str = "SELECT 1) AS lecture; COMMIT; BEGIN READ WRITE; DELETE FROM t WHERE id = 3; COMMIT; SELECT * FROM (SELECT 2"
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                lire_base.lire_pg(conteneur, "essai", forcee, 10)
        except OSError:
            pass
        sans_role: str = psql("SELECT count(*) FROM t")
        psql("INSERT INTO t SELECT 3, 'trois' WHERE NOT EXISTS (SELECT 1 FROM t WHERE id = 3);")

        # Avec le rôle lecture_seule : la base elle-même refuse, quel que soit le texte reçu
        psql("CREATE ROLE lecture_seule LOGIN; GRANT pg_read_all_data TO lecture_seule;")
        code, sortie = lancer("pg", conteneur, "essai", "SELECT id, nom FROM t ORDER BY id")
        assert code == 0 and "rôle lecture_seule" in sortie and "# 3 ligne(s)" in sortie, sortie
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                lire_base.lire_pg(conteneur, "essai", forcee, 10)
            raise AssertionError("écriture acceptée sous le rôle lecture_seule")
        except OSError as erreur:
            assert "permission denied" in str(erreur), erreur
        assert psql("SELECT count(*) FROM t") == "3"
        print("postgres (%s) : lecture, session en lecture seule et rôle lecture_seule conformes. Sans le rôle, "
              "une requête qui tromperait le filtre ET rouvrirait elle-même une transaction en écriture laisse %s ligne(s) sur 3 ; "
              "avec le rôle, 3 sur 3." % (image, sans_role))
    finally:
        docker("rm", "-f", conteneur)


def demo() -> None:
    essais_filtre()
    with tempfile.TemporaryDirectory() as dossier:
        essais_sqlite(dossier)
    print("lire-base : filtre et SQLite conformes")
    if "--postgres" in sys.argv[1:]:
        essais_postgres()


if __name__ == "__main__":
    demo()
