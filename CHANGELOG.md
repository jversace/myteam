# Historique des versions — myTeam

Numérotation `MAJEUR.MINEUR.CORRECTIF` : **correctif** (3e chiffre) = bug ; **mineur** (2e chiffre) = évolution ;
**majeur** (1er chiffre) = refonte complète du projet uniquement.
Le numéro courant est défini dans `version.py` ; l'entrée la plus récente ci-dessous doit toujours lui correspondre.

## [1.1.0] — 2026-11-28
Outils tiers pilotés par les agents (Lot 1 : type CLI). Évolution : voir « Outils tiers » dans `CLAUDE.md`.
- Nouveau menu **Outils** (niveau application) : registre d'outils (nom, description, programme, arguments avec `{paramètres}` typés, plateforme, délai, niveau libre / avec accord / interdit, activation) avec contrôle statique affiché (erreurs, avertissements, vigilance, aperçu de la commande).
- Nouveau droit **par rôle** : outils autorisés (cases dans la vue « Rôles »). Aucun outil par défaut.
- Nouvelles actions d'agent `tools_list` et `tool_run` (proposées seulement aux agents ayant au moins un outil, en répertoire local ou mode git) ; l'application construit la commande **sans shell**, valide chaque paramètre (type, motif, choix, chemin confiné, pas d'option déguisée) et exécute dans l'espace de travail de l'agent.
- Niveau « avec accord » : carte « 🛠️ Outil demandé » dans les Décisions (commande exacte) ; approbation → exécution par l'application puis résultat remis à l'agent (relance de sa tâche ou boîte de notifications, sans appel IA) ; la demande est annulée si l'outil, les droits ou la commande ont changé entre-temps.
- Exécuteur confiné (`tools.py`) : programme jamais dans l'espace de travail des agents, shells et élévation de privilèges refusés d'office, délai maximal, sortie limitée, environnement nettoyé (ni jeton ni clé), un lancement à la fois par outil, interruption à l'arrêt/annulation d'un agent.
- Journal : `p.tool_runs` (carte dans l'onglet Équipe) et ligne dans l'historique du projet.
- Route `POST /api/tools/check` ; 42 tests (`tests/test_tools.py`).

## [1.0.0] — 2026-11-27
Version initiale figée (tag `v1.0.0` sur `main`). Les évolutions et corrections suivantes se font sur la branche `DEV`.
- Mise en place du versionnement : `version.py`, route publique `GET /api/version`, version affichée dans le titre de l'application (onglet du navigateur, barre de menu, écran de connexion).
- Menu « À propos » : numéro de version et lien vers le dépôt GitHub.
