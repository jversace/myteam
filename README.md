# myteam

Prototype d'une application de gestion de projets de développement par une équipe d'agents IA.

## État actuel
- SPA autonome (`index.html`), sans dépendance ni build : ouvrir le fichier dans un navigateur.
- Comptes, projets, services IA, rôles, équipe d'agents, tâches arborescentes, git/releases/MR, bugs.
- Données sauvegardées dans le `localStorage` du navigateur (par compte).

## Limites du prototype
- Pas de backend : authentification et stockage uniquement locaux.
- Pas d'appel réel aux API des IA ni à git/GitHub (commits, releases et MR simulés).

## Suite prévue
Backend (comptes, base de données, exécuteur d'agents avec reprise automatique après épuisement des tokens, intégration git), exécutable sous Windows en local ou sur serveur Linux.
