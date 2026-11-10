# verified-workflow

Plugin Claude Code qui apporte des agents spécialisés et une règle simple : **aucune modification de projet n'est rendue sans preuve qu'elle fonctionne**.

> **In English.** A Claude Code plugin: specialised agents (exhaustive search, code analysis, debugging, audit) and a mandatory independent verification after every code change, enforced by hooks. The agents' instructions, the hook messages and this documentation are written in French. Linux only.

- Les agents collectent et prouvent des faits ; c'est l'agent principal qui raisonne, décide et écrit les modifications.
- Après chaque modification de code, un vérificateur indépendant prouve que la demande fonctionne (tests, instance de dev sur données temporaires, captures) et que rien d'autre n'est cassé.
- Si le travail a une partie visuelle, le résultat est rendu consultable depuis le poste de l'utilisateur, pour qu'il valide le rendu.

Le plugin est pensé pour une machine où l'on travaille à distance, souvent un serveur sans écran, et où la production peut tourner dans des conteneurs Docker lancés depuis les dossiers de projet eux-mêmes. Il fonctionne aussi sans Docker : il n'y a alors simplement pas de production à protéger.

## Installation

```
claude plugin marketplace add Amurius/verified-workflow
claude plugin install verified-workflow@verified-workflow
```

Mise à jour : `claude plugin marketplace update verified-workflow`, puis `claude plugin update verified-workflow@verified-workflow`.

Prérequis :

- **Linux** (ou WSL). Les scripts s'appuient sur `/proc`, `ss` et `setsid` : macOS et Windows natif ne sont pas pris en charge.
- `python3`, `bash`, `git`, `curl`.
- Pour les captures et les parcours dans une page : `node` et `npm`, le Chromium headless de Playwright (`npx playwright install chromium-headless-shell`, cherché dans `~/.cache/ms-playwright`), et `playwright-core` installé une fois dans le dossier d'état du plugin (le script donne la commande quand il manque).
- Selon les projets et la machine, sans que rien ne soit obligatoire : `docker` (relevé de la production), `tailscale` (publication du visuel), `rg`, `pdftotext`, `bun`, `uv`.

`python3 -I scripts/etat-machine.py` dit ce qui est présent sur la machine.

## Contenu

| Élément | Type | Rôle | Modèle |
|---|---|---|---|
| `searcher` | Agent | Recherche **exhaustive** dans une codebase ou le système de fichiers. | Haiku 5.5 |
| `code-analyst` | Agent | Faits vérifiés pour comprendre un fonctionnement ou mesurer l'impact d'une modification. | Sonnet 5.5 |
| `debugger` | Agent | Enquête sur un bug : hypothèses, `debug-enqueteur` en parallèle, cause racine prouvée. Ne corrige rien. | Opus 5.5 |
| `debug-enqueteur` | Agent | Lancé par `debugger` seulement. Reproduit le symptôme ou teste une hypothèse. | Sonnet 5.5 |
| `verifier` | Agent | Vérification indépendante. Prouve lui-même une petite modification, sinon lance `verif-preuve` et `verif-regression`. Rend VALIDÉ, VALIDÉ SOUS RÉSERVE, ÉCHEC ou NON VÉRIFIABLE. | Opus 5.5 |
| `verif-preuve` | Agent | Lancé par `verifier` seulement. Tests ciblés, instance de dev, captures. | Haiku 5.5 |
| `verif-regression` | Agent | Lancé par `verifier` seulement. Périmètre, dépendants, contrôle statique, tests, build. | Haiku 5.5 |
| `auditor` | Agent | Performance, duplication, code mort, complexité, limites structurelles. | Sonnet 5.5 |
| `/verified-workflow:audit` | Skill | Audit d'optimisation complet, plan priorisé, rien n'est appliqué sans accord. | |

Scripts (`scripts/`) :

| Script | Rôle |
|---|---|
| `etat-machine.py` | Relevé de l'instant : projets en production et leurs dossiers, ports et dossiers de données de production, ports de dev libres ou pris, mémoire, outils. Remplace toute liste figée. |
| `attendre.sh` | Attend un port, une URL locale, une ligne de journal ou la fin d'un processus, au lieu d'un `sleep`. |
| `fichiers-touches.sh` | Fichiers d'un projet plus récents que le repère, sans les fichiers ignorés par git. |
| `capture.js` | Capture Chromium headless d'une URL locale. |
| `scenario.js` | Parcours dans une page (aller, remplir, cliquer, vérifier un texte, capture) décrit en JSON : remplace les scripts Playwright écrits à la main. |
| `navigateur.js` | Module commun aux deux précédents : blocage de tout hôte non local et des requêtes non-GET, 15 s par action, arrêt automatique du script au bout de 100 s. |
| `arreter.sh` | Arrête ce qui écoute sur un port de dev, et rien d'autre ; refuse un port de production. À la place de `pkill`. |
| `lire-base.py` | Lecture seule : `sqlite <fichier>` ou `pg <conteneur> <base>`. Voir « Lecture de base ». |
| `ouvrir.sh` | Rend un visuel consultable depuis le poste. Voir « Visuel ». |
| `mesurer-verifications.py` | Bilan : verdicts, durées, tokens par modèle, et ce que les hooks ont vu. |
| `cout.py` | Ce que consomment les agents du plugin, par agent et par jour, et leur part dans tout l'usage de Claude Code (équivalent au tarif de l'API, tarifs datés dans le script). |

## Fonctionnement de la vérification

1. Claude modifie un **fichier de projet** : fichier d'un dépôt git, ou sous un dossier (autre que `~`) qui contient un `package.json`, `docker-compose.yml`, `compose.yml`, `Dockerfile`, `pyproject.toml` ou `manage.py`. Les `.md` ne comptent pas, ni `~/.claude` et `/tmp`.
2. Avant de rendre la main, le hook de fin de tour regarde si un `verifier` a été lancé après la dernière modification. Sinon il bloque, une fois, en donnant la liste des fichiers, le repère, les copies d'avant et la fiche du projet.
   - Tant qu'un sous-agent travaille encore, ou attend sa propre commande en arrière-plan, il ne bloque pas : la vérification se fait quand le travail est fini.
   - Si Claude passe outre en expliquant pourquoi, les fichiers restent notés : ils sont rappelés au blocage suivant, déclenché par la prochaine modification.
3. `verifier` relève la machine, lit la fiche du projet, puis prouve lui-même une petite modification ou lance `verif-preuve` et `verif-regression`.
4. Le rapport commence par `VERDICT : ...` et `VISUEL : OUI/NON`.
   - **VALIDÉ** : tout est prouvé en exécution.
   - **VALIDÉ SOUS RÉSERVE** : tout ce qui peut s'exécuter ici est prouvé ; le reste (appareil, redéploiement) est listé pour l'utilisateur.
   - **ÉCHEC** : Claude corrige et lance un nouveau verifier. Une modification n'est couverte que par un verifier lancé après elle : reprendre l'ancien par message ne suffit pas.
   - **NON VÉRIFIABLE** : le comportement demandé n'a pas pu être observé.
   - Un verifier qui rend la main sans verdict, alors que plus rien ne tourne, est signalé : le hook demande de le reprendre.
   - Avec plusieurs verifiers lancés en parallèle, rien n'est validé tant que l'un d'eux tourne encore ou a rendu un verdict défavorable.
5. Sur un verdict validé avec `VISUEL : OUI`, le hook bloque une fois tant que le visuel n'a pas été rendu consultable (`scripts/ouvrir.sh`, ou envoi des captures à l'utilisateur).
6. Sur un verdict validé, le repère est retiré et les copies d'avant passent en archive (gardées 14 jours) : la version vérifiée devient la base de la suite. ÉCHEC et NON VÉRIFIABLE gardent tout en place.

**Garde-fou production.** Est tenu pour la production tout ce que Docker fait tourner au moment du relevé : les conteneurs, les ports qu'ils publient, les dossiers de l'hôte qu'ils montent en écriture (et `/srv/data` s'il existe). Quand ces conteneurs tournent sous le même utilisateur que Claude, leurs processus sont visibles depuis l'hôte : un `kill`, `pkill` ou `killall` qui atteindrait l'un d'eux est refusé à tout le monde, fil principal compris. Pour les agents du plugin : aucun `docker compose up/down/restart/build/run`, aucune écriture dans les données de production, aucune requête ailleurs que sur `127.0.0.1`. Les preuves se font par les tests et sur une instance de dev (port 4100-4199, données temporaires). Ces interdits sont dans les consignes des agents **et** refusés au moment de l'appel d'outil par `hooks/garde.py`. Ce garde-fou lit la commande telle qu'elle est écrite, en suivant les `cd`, les affectations et les substitutions `$(...)` : ce qui est caché dans un script ou une variable qu'il ne peut pas résoudre lui échappe. Un dossier de sortie (sous `/tmp`, ou un `scratchpad`) n'est pas un projet : git et les installations y sont libres.

## Réglages

Deux fichiers `reglages.json`, relus à chaque fin de tour (pas besoin de recharger) : celui livré à la racine du plugin, puis le vôtre dans le dossier d'état, `~/.claude/plugins/data/verified-workflow-verified-workflow/reglages.json`. Le vôtre l'emporte, clé par clé, et une mise à jour du plugin ne le remplace pas.

| Réglage | Défaut | Effet |
|---|---|---|
| `seuil_fichiers` | `1` | Nombre de fichiers de projet modifiés à partir duquel le hook exige une vérification. Avec `3`, une modification d'un ou deux fichiers n'est pas vérifiée tout de suite : elle reste notée, l'utilisateur en est averti à la fin du tour, et elle est vérifiée avec les suivantes dès que trois fichiers sont en attente. |
| `petite_modification` | 2 fichiers, 60 lignes | Sous ces deux bornes, le hook annonce une « petite modification » : le verifier la prouve seul, sans lancer d'agents. |
| `toujours_verifier` | migrations, SQL, compose, Dockerfile, dépendances, `.env`, `auth/` | Fichiers dont la modification exige une vérification immédiate et complète, quel que soit le seuil. |

Exemple de fichier personnel : `{ "seuil_fichiers": 3 }`.

Sur un mois de vérifications de l'auteur, celles d'un ou deux fichiers ont échoué aussi souvent que les autres (4 sur 13) : un seuil plus haut fait gagner du temps mais retarde la découverte de ces défauts. `python3 -I scripts/mesurer-verifications.py` permet de suivre l'effet d'un changement.

## Fiche de vérification d'un projet

`<racine du projet>/.claude/verification.md` dit aux agents comment vérifier ce projet : ils la lisent au lieu de retrouver le lancement à tâtons. Quand elle manque ou qu'elle est fausse, le rapport du `verifier` contient le texte à enregistrer et Claude l'écrit. Modèle :

```
# Vérification de <projet>

## Projet
Pile technique, gestionnaire de paquets, ce qui fait foi.

## Contrôle statique et tests
Commandes exactes (suite complète, test ciblé).

## Build
Quand le lancer, où il écrit.

## Instance de dev sur données temporaires
Commande complète avec <port> et <sortie>, variables qui détournent la base et les fichiers.

## Compte de test
Comment obtenir une session, rôles à prouver.

## Production : ne pas toucher
Projet compose, base, journaux.
```

C'est le seul endroit où décrire un projet : rien n'est à reporter dans les consignes des agents. Ce qui décrit votre machine (où sont vos projets, vos sauvegardes, vos données) va dans votre `~/.claude/CLAUDE.md`, que les agents reçoivent aussi.

## Visuel

`ouvrir.sh <url locale>` donne l'adresse à ouvrir depuis le poste. Si l'instance de dev n'écoute que sur `127.0.0.1` et que la machine fait partie d'un réseau Tailscale, elle est publiée par `tailscale serve` (HTTPS, réseau Tailscale seulement) sans être relancée, et jamais sur un port que Tailscale publie déjà pour autre chose. `ouvrir.sh <fichier>...` sert ces fichiers et eux seuls. Tout s'arrête seul au bout de 4 h (`OUVRIR_DUREE`), ou par `ouvrir.sh --arreter`, qui ne ferme que ce que la session a ouvert (`--arreter tout` pour toutes) ; `--liste` montre ce qui est ouvert.

La publication d'une instance demande que l'utilisateur soit opérateur Tailscale (`sudo tailscale set --operator=$USER`) ; sinon le script donne la commande de tunnel SSH à lancer sur le poste. Sans Tailscale du tout, rien n'est exposé sur le réseau : les fichiers sont servis sur `127.0.0.1` et le script donne la même commande de tunnel.

## Lecture de base

`lire-base.py` refuse tout ce qui n'est pas une lecture en une instruction (analyse caractère par caractère : un point-virgule ne peut pas se cacher dans une chaîne `E'...'` ou un identifiant cité ; barre oblique inverse, `$` et commentaires refusés), ouvre SQLite en `mode=ro`, et lit Postgres dans une session en lecture seule. Pour que la base elle-même interdise l'écriture, quel que soit le texte reçu, créer une fois dans chaque Postgres de production :

```
CREATE ROLE lecture_seule LOGIN;
GRANT pg_read_all_data TO lecture_seule;
```

Le script l'utilise dès qu'il existe.

## État du plugin

Tout est sous `~/.claude/plugins/data/verified-workflow-verified-workflow/` (mode 700), hors de `/tmp` :

- `sessions/<session>/journal.jsonl` : ce que les hooks ont vu (modifications, lancements et fins d'agents, verdicts, blocages, refus) ;
- `sessions/<session>/repere`, `avant/` : repère et copies d'avant de la génération en cours ; `archives/` : générations validées, 14 jours ;
- `machine.json`, `ouvertures.tsv`, `erreurs.log` ;
- `reglages.json` : vos réglages, si vous en avez ;
- `outils/` : `playwright-core`, installé une fois pour tous les agents (`npm install --prefix <ce dossier>/outils playwright-core`).

Les sessions sans activité depuis 30 jours sont retirées au démarrage d'une session.

Les hooks décident à partir de ce journal, pas du transcript : `SubagentStop` donne le dernier message du verifier, `Stop` donne les sous-agents encore en cours. Seul secours lu dans le transcript : le rapport reçu par le fil principal, quand le journal n'a pas de verdict.

## Travailler sur le plugin

Depuis un clone, `claude plugin marketplace add <dossier du clone>` : Claude Code lit alors ce dossier **en place**, une modification s'applique à la session suivante ou tout de suite avec `/reload-plugins`. Les scripts de `scripts/` et de `hooks/` sont relus à chaque appel, y compris par les sessions déjà ouvertes : n'y laissez jamais un fichier à moitié écrit.

```
python3 hooks/test_hooks.py                 # « hooks : tous les cas passent »
python3 scripts/test_scripts.py             # filtre de lire-base.py et SQLite
python3 scripts/test_scripts.py --postgres  # en plus : Postgres dans un conteneur jetable (demande docker)
```

Dans les consignes des agents, `${CLAUDE_PLUGIN_ROOT}` est remplacé par Claude Code par le dossier où le plugin est installé. Cette variable n'existe pas dans le shell des agents : c'est pourquoi les consignes portent le chemin, et pourquoi le dossier d'état a un chemin fixe.

`mesurer-verifications.py` et `cout.py` laissent de côté les sessions lancées depuis un dossier dont le nom contient `vw-essai` ou `sonde-projet` : c'est là que se font les essais du plugin lui-même.

## Limites

- Les modifications faites par une commande shell (`sed -i`, redirections) ne sont ni copiées ni comptées. `fichiers-touches.sh` les retrouve par le repère, s'il a été posé par une modification précédente.
- Le garde-fou refuse les formes directes, pas une commande construite à l'exécution.
- Une vérification a un coût, partagé à peu près à égalité entre le `verifier` (Opus 5.5) et ses agents Haiku, qui consomment beaucoup plus de tokens mais bien moins chers (`scripts/cout.py`). La fiche du projet et l'instance de dev commune sont ce qui réduit la part des agents.
- Éprouvé sur Debian. Les captures cherchent le Chromium de Playwright pour Linux x86-64.

## Licence

MIT, voir `LICENSE`.
