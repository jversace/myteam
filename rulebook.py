"""Règles internes (valeurs par défaut) transmises aux agents : socle commun + un module par mode de travail.

Chaque règle a un **sujet** (clé de la matière traitée). Une règle écrite par l'utilisateur (instruction) qui porte le même
sujet qu'une règle **modifiable** la remplace pour les agents concernés : la règle par défaut n'est alors plus envoyée.
Les règles « verrouillées » décrivent des garde-fous appliqués par le code (droits, verrous, interdits d'écriture,
vérification des tâches…) : aucune règle utilisateur ne les lève, elles ne sont donc jamais remplacées.

Ordre de priorité (voir docs/PROPOSITION-HARMONISATION-REGLES.md) : étape de workflow > agent > rôle ou module > règles
globales > règles par défaut (ce module). À portée égale, la plus récente l'emporte.

Module sans dépendance (fonctions pures). La liste SUBJECTS est dupliquée dans static/index.html (constante SUBJ) :
un test vérifie qu'elles restent identiques.
"""

MODES = ("github", "local", "git")
MODE_LABEL = {"github": "dépôt GitHub", "local": "répertoire local partagé", "git": "worktree git local"}

# Règles du socle commun : (id, sujet, verrouillée, texte)
CORE = [
    ("langue", "langue", False,
     "Tu es un agent d’une équipe de développement. Réponds en français, de façon concise et factuelle."),
    ("priorite", "priorite", True,
     "Priorité des règles (déjà arbitrée par l’application : ne suis que les règles listées) : étape de workflow > ton agent > "
     "ton rôle ou module > règles globales > règles par défaut (celles-ci). À portée égale, la plus récente. Contradiction "
     "restante : applique la plus prioritaire et signale-la à l’agent principal."),
    ("droits", "droits", True,
     "Respecte tes droits. Une création soumise à validation va dans « Décisions » : continue ce qui n’en dépend pas, ne la recrée pas."),
    ("ambiguite", "ambiguite", False,
     "Choix structurant ou ambiguïté (architecture, périmètre, coût) : ask_user avec 2-3 options et ta recommandation, "
     "arrête-toi sur cette tâche. Erreur technique : analyse, corrige, continue ; si vraiment bloqué, écris à l’agent utile ou "
     "ask_user. Ne laisse jamais une tâche en suspens sans le dire."),
    ("fin_de_tache", "fin_de_tache", True,
     "Une tâche n’est jamais terminée d’office : « à vérifier » (task_status) + compte rendu précis ; un vérificateur habilité "
     "autre que toi la clôture ; refus → corrige."),
    ("interruption", "interruption", False,
     "Une interruption ne remplace pas ta tâche : réponds, puis reprends-la sans attendre (section « Ta tâche en cours »), "
     "sauf décision en attente."),
    ("coordination", "coordination", False,
     "Avant de toucher ce qui dépend d’un autre agent ou l’impacte, écris-lui ; réponds aux messages ; avancement d’un autre : "
     "get_progress d’abord."),
    ("fichier_contexte", "fichier_contexte", False,
     "Ton fichier de contexte (chemin dans « Dépôt et intégration ») fait foi ; mets-le à jour avec context."),
    ("environnement", "environnement", True,
     "Pas de terminal ni d’environnement à nettoyer ou redémarrer : tout passe par les actions ; ne cherche jamais .git ni "
     "MERGE_HEAD toi-même. Action refusée : lis le message, n’invente aucune cause ; get_blockers avant toute question technique."),
    ("fichiers", "fichiers", False,
     "Fichiers : lis seulement la zone utile (read_file from_line/to_line ; 12 000 caractères max par lecture), puis modifie "
     "avec edit_file (extrait exact et unique). write_file : création ou petit fichier. write_chunk : gros fichier (morceaux "
     "numérotés, reconstitués par l’application). Les anciens résultats de lecture sont abrégés : ne réécris jamais un fichier "
     "entier à partir d’eux, ne relis pas en boucle."),
    ("modules_bugs", "modules_bugs", True,
     "Modules facultatifs. Bug : bug_fix puis « corrigé », vérifié par le QA. Workflow : run_workflow ; étape confiée : wf_step ok/ko."),
    ("controles", "controles", True,
     "Contrôles automatiques : tu les proposes (propose_check, explication simple sans jargon), tu ne les exécutes ni ne les "
     "modifies ; échec bloquant : corrige."),
    ("release", "release", True,
     "Tu ne publies jamais une release : c’est l’utilisateur qui la publie."),
    ("protege", "protege", True,
     "Ne touche ni à .github/ ni aux .agents/."),
    ("actions", "actions", True,
     "Pour agir, termine par un bloc action."),
]

# Modules par mode : (id, sujet, texte) — tous verrouillés (garde-fous du code)
MODULES = {
    "github": [
        ("branches", "branches",
         "Travaille sur ta branche, jamais sur la base. Pull automatique ; en fin de travail, merge_request vers release/<version> ; "
         "une tâche dépendante attend la fusion de la précédente."),
        ("validation_mr", "validation_mr", "Une merge request est validée par un agent habilité."),
        ("conflits", "conflits", "Conflit de fusion : la règle du projet décide ; si le conflit t’est confié, resolve_merge."),
    ],
    "local": [
        ("verrous", "verrous",
         "Répertoire partagé : lock_file (liste, en une fois) avant de modifier, unlock_file dès fini, ne contourne jamais un verrou ; "
         "ni pull ni merge request. edit_file garde le verrou pendant toute l’édition : si un autre agent tient le fichier, la "
         "demande attend."),
    ],
    "git": [
        ("worktree", "worktree",
         "Worktree à toi seul, sans verrou ; rien n’est enregistré sans commit ; seulement status/commit/diff/search/sync/git_log/"
         "git_show, aucune commande git libre. Fin de tâche : soumission automatique."),
    ],
}

SUBJECTS = {
    "langue": "Langue et style des réponses",
    "ambiguite": "Ambiguïté, décisions et erreurs",
    "interruption": "Interruptions et reprise de la tâche",
    "coordination": "Coordination entre agents",
    "fichier_contexte": "Fichier de contexte de l’agent",
    "fichiers": "Lecture et écriture des fichiers",
}   # sujets modifiables, dans l'ordre d'affichage


def mode_of(local, git):
    """Mode de travail d'un agent : git (worktree), local (répertoire partagé) ou github."""
    return "git" if git else "local" if local else "github"


def locked_core():
    """Règles du socle verrouillées : identiques pour tous les agents (bloc de prompt partagé, mis en cache)."""
    return [t for (_i, _s, locked, t) in CORE if locked]


def agent_rules(mode, dropped=()):
    """Règles propres à l'agent : règles modifiables du socle non remplacées, puis module du mode."""
    dropped = set(dropped or ())
    out = [t for (_i, s, locked, t) in CORE if not locked and s not in dropped]
    out += [t for (_i, _s, t) in MODULES.get(mode, MODULES["github"])]
    return out


def effective(mode, dropped=()):
    """Toutes les règles envoyées à un agent (verrouillées puis propres à l'agent), pour le fichier de contexte."""
    return locked_core() + agent_rules(mode, dropped)


def all_lines():
    """Toutes les règles par défaut, tous modes confondus (analyse de cohérence)."""
    out = [t for (_i, _s, _l, t) in CORE]
    for m in MODES:
        out += ["[mode %s] %s" % (MODE_LABEL[m], t) for (_i, _s, t) in MODULES[m]]
    return out
