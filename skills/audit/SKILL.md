---
name: audit
description: "Audit d'un projet ou d'une zone de code pour l'optimiser : performance, duplication, code mort ou superflu, complexité, et changements à plus grande échelle (langage, framework, configuration, dépendances). À utiliser quand l'utilisateur demande un audit, une optimisation globale ou un nettoyage d'un projet, pas pour relire un diff en cours (-> /simplify) ni chercher des bugs (-> /code-review). Produit un plan priorisé à valider, n'applique rien sans accord."
argument-hint: "[chemin ou zone] [perf|duplication|mort|complexite|structure]"
---

# Audit d'optimisation

Arguments : $ARGUMENTS

Sans chemin, le périmètre est le projet du répertoire courant. Sans dimension, audite les cinq : performance, duplication, code mort/superflu, complexité, structurel.

## 1. Cadrer

- Identifie la racine du projet, la stack, les versions et la taille (nombre de fichiers et de lignes par langage, hors dépendances et fichiers générés).
- Si le périmètre est ambigu (plusieurs projets dans le dossier, monorepo), demande à l'utilisateur lequel auditer avant de lancer quoi que ce soit.

## 2. Collecter avec l'agent `auditor`

- Petit périmètre (moins d'environ 15 000 lignes) : un seul agent `auditor` avec toutes les dimensions demandées.
- Plus gros : lance en parallèle plusieurs `auditor` (4 au maximum), découpés par zone du code (ex : backend / frontend, ou par application) plutôt que par dimension, pour que chacun voie la duplication à l'intérieur de sa zone. Ajoute ensuite un agent `auditor` limité aux dimensions duplication et structurel sur tout le projet si des zones risquent de dupliquer du code entre elles.
- Dans chaque brief : le périmètre exact, les dimensions, la stack, et ce qui compte pour l'utilisateur s'il l'a précisé.

## 3. Vérifier chaque constat toi-même

L'auditeur collecte, tu évalues. Pour chaque constat :
- relis le code cité et confirme qu'il est exact (un faux positif présenté à l'utilisateur coûte sa confiance dans tout l'audit) ;
- écarte ce qui est faux, insignifiant ou justifié (code mort qui est en fait une API publique, duplication volontaire entre deux modules qui vont diverger, optimisation sans effet mesurable sur un chemin froid).

## 4. Concevoir les propositions

Pour chaque constat retenu, conçois la modification : quoi changer, gain attendu, effort, risque de régression. Regroupe les constats qui relèvent de la même correction.

**Propositions à plus grande échelle** (langage, framework, configuration, dépendances) : propose-les quand les constats le justifient, pas par principe. Chacune doit indiquer :
- les constats précis qui la motivent ;
- le gain attendu, chiffré ou au moins ordonné ;
- le coût réel : volume de code à migrer, courbe d'apprentissage, écosystème, risques ;
- une alternative plus légère si elle existe (souvent une configuration ou une bibliothèque ciblée plutôt qu'un changement de framework).
Vérifie les versions et l'état actuel des outils proposés (dernières versions stables, maintenance) avant de les recommander.

## 5. Présenter le plan

Présente à l'utilisateur, en français et sans emojis :
1. **Synthèse** : 3 à 5 lignes sur l'état du projet et les principaux leviers.
2. **Propositions priorisées** : un tableau (numéro, dimension, quoi, où, gain, effort, risque), trié par rapport gain/effort, les gains rapides en premier.
3. **Changements à plus grande échelle** : séparés du reste, avec leur analyse coût/bénéfice.
4. **Écarté** : en une ligne chacun, les constats de l'auditeur que tu as rejetés et pourquoi, pour que l'utilisateur sache qu'ils ont été examinés.
5. **Non couvert** : ce que l'audit n'a pas pu examiner.

N'applique rien. Termine en demandant quelles propositions appliquer (par numéro). Lors de l'application, procède proposition par proposition ; l'agent `verifier` contrôle chaque modification comme pour toute autre demande.
