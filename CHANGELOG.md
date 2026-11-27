# Historique des versions — myTeam

Numérotation `MAJEUR.MINEUR.CORRECTIF` : **correctif** (3e chiffre) = bug ; **mineur** (2e chiffre) = évolution ;
**majeur** (1er chiffre) = refonte complète du projet uniquement.
Le numéro courant est défini dans `version.py` ; l'entrée la plus récente ci-dessous doit toujours lui correspondre.

## [1.0.0] — 2026-11-27
Version initiale figée (tag `v1.0.0` sur `main`). Les évolutions et corrections suivantes se font sur la branche `DEV`.
- Mise en place du versionnement : `version.py`, route publique `GET /api/version`, version affichée dans le titre de l'application (onglet du navigateur, barre de menu, écran de connexion).
- Menu « À propos » : numéro de version et lien vers le dépôt GitHub.
