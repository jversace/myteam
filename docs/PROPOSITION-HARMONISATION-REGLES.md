# Proposition d'harmonisation des règles (à valider — rien n'est implémenté)

> **État d'avancement (24 nov. 2026, option 1 retenue)** — **réalisé** (357 → 381 tests OK) : étape 1 en partie (modèle « sujet + portée » appliqué aux instructions, priorité agent > rôle/module > global > défaut, plus récent de même portée remplace) ; étape 2 (socle + module par mode dans `rulebook.py`, règles verrouillées dans le bloc partagé, ~520–600 tokens au lieu de ~1 680, messages de conflit alignés sur `edit_file`, la release est publiée par l'utilisateur) ; étape 3 (question à la saisie, `engine.rule_submit`) ; étape 5 (CLAUDE.md, inventaire). **Reste à faire** : liste de règles structurée pour `p.rules`/`agent.rules` (migration), `lint` par sujet et analyse IA limitée aux sujets en doute (étape 4), actions du prompt filtrées par droits et mode, défauts de droits servis par le serveur (contradiction 9). Pas de règles propres aux rôles/modules (option 1). Détail : `CLAUDE.md`, section « Règles par défaut, sujets et priorité ».

> **Mise à jour (24 nov. 2026)** : prise en compte des évolutions « gros fichiers » déjà implémentées (commits `5c52789`, `08cd9c5`, `b211c17`, voir `CLAUDE.md`, section « Gros fichiers ») : lecture par plage, `edit_file`, `write_chunk`, garde-fou anti-troncature, verrou tenu pendant l'édition, historique abrégé selon un budget, avertissement de relecture. Elles touchent R03, l'exception de sécurité (§1), le socle commun et les modules (§3), les contradictions (§4) et la mise en œuvre (§5). Le reste de la proposition est inchangé.

> **Décisions du 24 nov. 2026** : (a) conflit « règle large plus récente vs règle spécifique » → **une question à la saisie** (déjà le cas au §1.3) ; (b) **la release est publiée par l'utilisateur** : le texte « la publication revient à l'agent principal » est retiré du module GitHub (le code fait déjà ainsi : la publication est une tâche lancée depuis l'interface, pas une action d'agent) ; (c) question sur l'ordre des portées : voir §6.

Objectifs : (1) supprimer les contradictions, (2) réduire les tokens, (3) les règles spécifiques l'emportent sur les règles internes (qui ne sont que des **défauts**), (4) une règle plus récente qui change une règle spécifique la **remplace**.
Base : `docs/REGLES-INTERNES.md`.

## 1. Un seul modèle de règle, un seul ordre de priorité

Toute règle, y compris les règles internes, devient un enregistrement : `{id, sujet, portée, texte, date, replaces[]}`.

- **Sujet** : clé de la matière traitée (`branches`, `validation_mr`, `fin_de_tâche`, `conflits`, `fichier_contexte`, `interruption`, `coordination`…). Les règles internes en portent une ; une règle utilisateur en reçoit une à la saisie.
- **Portée**, de la plus forte à la plus faible : **étape de workflow / tâche → agent → rôle → module → global projet → défaut interne**.

Ordre de résolution, par sujet, calculé **par l'application** (sans IA) :
1. La portée la plus étroite gagne (donc tout règle utilisateur bat un défaut interne portant le même sujet).
2. À portée égale, la plus récente gagne et **désactive** l'ancienne (« annule et remplace », comme les instructions aujourd'hui).
3. Une règle plus **récente mais de portée plus large** ne remplace une règle spécifique que si elle le déclare (`replaces`) ou si tu le confirmes. À la saisie, l'application détecte le même sujet et pose **une** question : « Remplace la règle n° X de <agent> / La garde (la spécifique reste) / Sujets différents ».
4. Seules les règles **effectives** sont envoyées aux agents. Les règles remplacées vont dans `rules_log` (historique), jamais dans le prompt.

**Exception de sécurité** : les garde-fous du code (droits, verrous, interdits d'écriture `.git/ .github/ .agents/`, budget, contrôles approuvés, jeton jamais exposé) ne sont **pas** des règles par défaut : aucune règle utilisateur ne les lève (il faut changer le droit ou le réglage correspondant). S'y ajoutent les garde-fous « fichiers » : **refus d'un `write_file` bien plus court que le fichier existant** (seul `confirm_shrink:true` du paramètre d'action le contourne, jamais une règle), **unicité et tout-ou-rien des remplacements de `edit_file`**, **verrou tenu pendant toute l'opération lecture + écriture** en répertoire local, **reconstitution et contrôle d'empreinte de `write_chunk`**. Une règle comme « réécris toujours le fichier entier » ne peut donc pas les neutraliser.

## 2. Ce que les règles utilisateur deviennent

- `p.rules` et `agent.rules` (texte libre) → **liste de règles** (migration : une règle par puce/ligne, sujet « à classer »).
- Message utilisateur dans un dialogue : l'agent principal doit classer chaque consigne durable en règle (portée + sujet + `replaces`) via l'action `instruction`, sinon c'est une consigne ponctuelle sans portée durable.
- Réponse à une décision structurante : règle de clé `dec:<question>` (déjà le cas), reprise dans le même modèle.
- Workflow : une étape est une consigne de portée « étape » ; elle bat global/défaut pendant son déroulement, et cède devant une consigne plus récente de même sujet.
- Analyse de cohérence : le contrôle déterministe (`lint`) compare les **sujets** ; l'IA n'est appelée que sur les sujets en doute, avec les seules règles effectives (plus de rapport qui grossit).

## 3. Règles internes réécrites : socle commun + un module par mode

Le mode de l'agent (GitHub / répertoire local / git local) décide du module injecté : **fini les règles qui se contredisent** (R02/R10 vs R11/R16/R17), et chaque agent ne reçoit que ce qui le concerne. Ancienne taille : ~1 680 tokens ; nouvelle : **~590 à 640 tokens (−62 à −65 %)** (la règle « fichiers » du socle ajoute ~70 tokens ; elle remplace les deux mentions « write_file = contenu complet » des modules).

### Socle commun (tous les modes)
```
1. Tu es un agent d'une équipe de développement. Réponds en français, concis et factuel.
2. Priorité des règles (déjà arbitrée par l'application : ne suis que les règles listées) : étape de workflow/tâche > ton agent > ton rôle > module > globale > règles par défaut. À portée égale, la plus récente. Contradiction restante : applique la plus prioritaire et signale-la à l'agent principal.
3. Respecte tes droits. Une création soumise à validation va dans « Décisions » : continue ce qui n'en dépend pas, ne la recrée pas.
4. Choix structurant ou ambiguïté (architecture, périmètre, coût) : ask_user avec 2-3 options et ta recommandation, arrête-toi sur cette tâche. Erreur technique : analyse, corrige, continue ; si vraiment bloqué, écris à l'agent utile ou ask_user. Ne laisse jamais une tâche en suspens sans le dire.
5. Une tâche n'est jamais terminée d'office : « à vérifier » (task_status) + compte rendu précis ; un vérificateur habilité autre que toi la clôture ; refus → corrige.
6. Une interruption ne remplace pas ta tâche : réponds, puis reprends-la sans attendre (section « Ta tâche en cours »), sauf décision en attente.
7. Avant de toucher ce qui dépend d'un autre agent ou l'impacte, écris-lui ; réponds aux messages ; avancement d'un autre : get_progress d'abord.
8. Ton fichier de contexte (chemin dans « Dépôt et intégration ») fait foi ; mets-le à jour avec context.
9. Pas de terminal ni d'environnement : tout passe par les actions. Action refusée : lis le message, n'invente aucune cause ; get_blockers avant toute question technique.
10. Fichiers : lis seulement la zone utile (read_file from_line/to_line ; 12 000 caractères max par lecture), puis modifie avec edit_file (extrait exact et unique). write_file : création ou petit fichier. write_chunk : gros fichier (morceaux numérotés, reconstitués par l'application). Les anciens résultats de lecture sont abrégés : ne réécris jamais un fichier entier à partir d'eux, ne relis pas en boucle.
11. Modules facultatifs. Bug : bug_fix puis « corrigé », vérifié par le QA. Workflow : run_workflow ; étape confiée : wf_step ok/ko.
12. Contrôles automatiques : tu les proposes (propose_check, explication simple sans jargon), tu ne les exécutes ni ne les modifies ; échec bloquant : corrige.
13. Pour agir, termine par un bloc action.
```
### Module « GitHub »
```
Travaille sur ta branche, jamais sur la base. Pull automatique ; en fin de travail merge_request vers release/<version> ; validation par un agent habilité ; une tâche dépendante attend la fusion. Conflit : règle du projet (resolve_merge si confié). Tu ne publies jamais une release : c'est l'utilisateur qui la publie. Ne touche ni .github/ ni .agents/.
```
### Module « répertoire local partagé »
```
Répertoire partagé : lock_file (liste, en une fois) avant de modifier, unlock_file dès fini, ne contourne jamais un verrou ; ni pull ni merge request. edit_file garde le verrou pendant toute l'édition : si le fichier est tenu par un autre agent, la demande attend.
```
### Module « git local »
```
Worktree à toi seul, sans verrou ; rien n'est enregistré sans commit ; seulement status/commit/diff/search/sync/git_log/git_show, aucune commande git libre. Fin de tâche : soumission automatique.
```
Autres économies : l'historique envoyé à l'IA garde désormais intacts les retours de fichiers jusqu'à un budget (`HIST_BUDGET`, 36 000 caractères) et abrège les doublons de relecture (déjà en place) ; n'injecter dans `ACTIONS` que les actions permises par les droits et le mode de l'agent  (la section `ACTIONS` a grossi d'environ 250 tokens avec `edit_file`, `write_chunk` et la lecture par plage : `edit_file`/`write_chunk` suivent le même droit et le même mode que `write_file`) ; le socle et le module restent dans les blocs de prompt **mis en cache** (aucun contenu changeant) ; l'analyse de cohérence reçoit les règles effectives, pas les règles intégrées complètes.

## 4. Contradictions résolues

| # | Contradiction | Résolution |
|---|---|---|
| 1 | R02/R03/R04/R10 vs R11/R16/R17 (modes) | Module par mode ; R11 réduite à « pas de terminal » |
| 2 | Fichier de contexte à 3 emplacements | Le chemin est donné par « Dépôt et intégration » ; la règle ne le fixe plus |
| 3 | « Le principal valide les MR » vs droit `mr_review` | « Validateur habilité » partout ; le principal l'a par défaut |
| 4 | Spécifique vs récent | Ordre unique §1 : portée d'abord, puis date, `replaces` explicite |
| 5 | R06 s'arrêter / R19 reprendre | « sauf décision en attente » écrit dans la règle 6 |
| 6 | R14 continuer / R06 ne pas improviser | Règle 4 : erreur technique = continuer ; choix structurant = demander |
| 7 | R15 « principal ou QA » vs code | « Vérificateur habilité autre que toi » |
| 8 | R02 « release finale réalisée par l'agent principal » vs code (publication lancée par l'utilisateur) | Décision du 24 nov. : l'utilisateur publie ; module GitHub : « tu ne publies jamais une release » |
| 9 | Droits par défaut dupliqués (`rights.py` / `index.html`) | Le front lit les défauts servis par le serveur (une seule source) |
| 10 | R03 « `write_file` remplace le fichier entier : contenu complet » vs garde-fou anti-troncature et `edit_file` | Règle 10 du socle (modifier = `edit_file`, `write_file` = création/petit fichier) ; le garde-fou reste dans le code (§1) |
| 11 | Messages de conflit de fusion « réécris le fichier COMPLET avec `write_file` » (`engine.py`) vs règle 10 | Aligner ces textes : retirer les marqueurs de conflit avec `edit_file` (étape 2) |

## 5. Mise en œuvre proposée (par étapes)
1. Modèle de règle + migration de `rules` en listes ; résolution `effective_rules(p, a)` et tests.
2. Sujets sur les règles internes (R03 → sujet `écriture_fichiers`) ; module par mode dans `sys_prompt` (blocs 1/2 en cache) ; règle « fichiers » du socle ; alignement des messages de conflit sur `edit_file` ; tests de non-régression.
3. Détection à la saisie (même sujet → question unique) ; action `instruction` étendue (portée, sujet, `replaces`).
4. `lint` par sujet ; analyse IA limitée aux sujets en doute.
5. Mise à jour de `CLAUDE.md` et de `docs/REGLES-INTERNES.md` (dont R03 : elle décrit encore « contenu complet »).

## 6. Points à valider
- ~~Question à la saisie ou « la spécifique gagne »~~ : **tranché, question à la saisie**.
- ~~Publication de la release~~ : **tranché, l'utilisateur publie**.
- **Règles par rôle et par module** (ancien « module sous rôle ? ») : aujourd'hui l'application n'a que des règles **globales du projet** et **par agent** (plus les workflows). Les portées « rôle » et « module » étaient une anticipation. Options : (1) ne pas les créer pour l'instant — ordre étape > agent > global > défaut ; (2) les créer avec module > rôle ; (3) les créer avec rôle > module. Recommandation : option 1 maintenant, option 2 si le besoin apparaît.
