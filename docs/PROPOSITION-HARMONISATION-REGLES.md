# Proposition d'harmonisation des règles (à valider — rien n'est implémenté)

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

**Exception de sécurité** : les garde-fous du code (droits, verrous, interdits d'écriture `.git/ .github/ .agents/`, budget, contrôles approuvés, jeton jamais exposé) ne sont **pas** des règles par défaut : aucune règle utilisateur ne les lève (il faut changer le droit ou le réglage correspondant).

## 2. Ce que les règles utilisateur deviennent

- `p.rules` et `agent.rules` (texte libre) → **liste de règles** (migration : une règle par puce/ligne, sujet « à classer »).
- Message utilisateur dans un dialogue : l'agent principal doit classer chaque consigne durable en règle (portée + sujet + `replaces`) via l'action `instruction`, sinon c'est une consigne ponctuelle sans portée durable.
- Réponse à une décision structurante : règle de clé `dec:<question>` (déjà le cas), reprise dans le même modèle.
- Workflow : une étape est une consigne de portée « étape » ; elle bat global/défaut pendant son déroulement, et cède devant une consigne plus récente de même sujet.
- Analyse de cohérence : le contrôle déterministe (`lint`) compare les **sujets** ; l'IA n'est appelée que sur les sujets en doute, avec les seules règles effectives (plus de rapport qui grossit).

## 3. Règles internes réécrites : socle commun + un module par mode

Le mode de l'agent (GitHub / répertoire local / git local) décide du module injecté : **fini les règles qui se contredisent** (R02/R10 vs R11/R16/R17), et chaque agent ne reçoit que ce qui le concerne. Ancienne taille : ~1 680 tokens ; nouvelle : **~520 à 570 tokens (−67 à −70 %)**.

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
10. Modules facultatifs. Bug : bug_fix puis « corrigé », vérifié par le QA. Workflow : run_workflow ; étape confiée : wf_step ok/ko.
11. Contrôles automatiques : tu les proposes (propose_check, explication simple sans jargon), tu ne les exécutes ni ne les modifies ; échec bloquant : corrige.
12. Pour agir, termine par un bloc action.
```
### Module « GitHub »
```
Travaille sur ta branche, jamais sur la base. Pull automatique ; en fin de travail merge_request vers release/<version> ; validation par un agent habilité ; une tâche dépendante attend la fusion. Conflit : règle du projet (resolve_merge si confié). La publication de la release revient à l'agent principal. Ne touche ni .github/ ni .agents/. write_file = contenu complet.
```
### Module « répertoire local partagé »
```
Répertoire partagé : lock_file (liste, en une fois) avant de modifier, unlock_file dès fini, ne contourne jamais un verrou ; ni pull ni merge request. write_file = contenu complet.
```
### Module « git local »
```
Worktree à toi seul, sans verrou ; rien n'est enregistré sans commit ; seulement status/commit/diff/search/sync/git_log/git_show, aucune commande git libre. Fin de tâche : soumission automatique.
```
Autres économies : n'injecter dans `ACTIONS` que les actions permises par les droits et le mode de l'agent ; le socle et le module restent dans les blocs de prompt **mis en cache** (aucun contenu changeant) ; l'analyse de cohérence reçoit les règles effectives, pas les règles intégrées complètes.

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
| 8 | Publication par le principal vs l'application | Module GitHub : « revient à l'agent principal » (exécutée par l'application) |
| 9 | Droits par défaut dupliqués (`rights.py` / `index.html`) | Le front lit les défauts servis par le serveur (une seule source) |

## 5. Mise en œuvre proposée (par étapes)
1. Modèle de règle + migration de `rules` en listes ; résolution `effective_rules(p, a)` et tests.
2. Sujets sur les règles internes ; module par mode dans `sys_prompt` (blocs 1/2 en cache) ; tests de non-régression.
3. Détection à la saisie (même sujet → question unique) ; action `instruction` étendue (portée, sujet, `replaces`).
4. `lint` par sujet ; analyse IA limitée aux sujets en doute.
5. Mise à jour de `CLAUDE.md` et de `docs/REGLES-INTERNES.md`.

## 6. Points à valider
- Ordre des portées proposé : tâche/étape > agent > rôle > module > global > défaut. Faut-il vraiment placer « module » sous « rôle » ?
- Cas 3 (règle large plus récente vs règle spécifique) : question à la saisie (proposé) ou « la spécifique gagne » sans question ?
- Publication de la release : à confirmer dans le code avant de figer le texte du module GitHub.
