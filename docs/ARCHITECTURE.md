# Architecture

## Pourquoi un routeur, et pas plus simple

La solution évidente serait d'écrire l'amont de repli dans
`~/.claude/settings.json` au moment où le quota tombe. Elle ne marche pas :
**Claude Code ne relit `settings.json` qu'au démarrage de session.** Y écrire le
repli ne l'appliquerait qu'à la session *suivante* — c'est-à-dire jamais au
moment où on en a besoin, puisqu'on est en train de travailler.

D'où le montage retenu : `settings.json` pointe **une fois pour toutes** sur un
routeur local, et c'est le routeur qui choisit l'amont **à chaque requête**, en
relisant un fichier d'état. Une session ouverte depuis des heures bascule au
message suivant.

C'est la seule raison d'être du routeur. Tout le reste en découle.

## Les trois pièces

```
src/router.py     Proxy HTTP sur 127.0.0.1:8099. Décide de l'amont, réécrit
                  les en-têtes, intercepte le 429, relaie en flux.
src/bridge.py     Traduction Anthropic <-> OpenAI. Fonctions pures, aucun
                  état, aucun réseau — donc testable au doigt.
src/fallback.py   État, catalogue de modèles, sondes, bascules, CLI `dbl`.
```

`install.sh` copie les trois dans `~/.doublure`, écrit le LaunchAgent, et sert
lui-même de hook `SessionStart`. `uninstall.sh` défait exactement ça.

## Le chemin d'une requête

```
claude
  │  POST http://127.0.0.1:8099/v1/messages
  ▼
router.py  do_POST
  │
  ├─ read_state()            mode courant, cache 1 s (relu à chaud)
  ├─ overrides()             réécrit le nom de modèle vers un modèle gratuit
  │
  ├─ mode « native » ────────────────────────────────────────────┐
  │    direct("native")                                          │
  │      x-api-key retiré, Authorization: Bearer <jeton OAuth>    │
  │      anthropic-beta: oauth-2025-04-20 en tête                 │
  │      ──► api.anthropic.com                                    │
  │                                                               │
  │    si 429 et rien n'est encore parti au client :              │
  │      set_mode(premier de la chaîne, "auto: quota atteint")     │
  │      retry_after(resp) → quand retenter le natif              │
  │      la MÊME requête repart ci-dessous  ─────────────────────┐│
  │                                                             ││
  ├─ mode « or » ◄──────────────────────────────────────────────┘│
  │    direct("or")                                              │
  │      or_body() : modèle → nvidia/...:free, chemin préfixé /api│
  │      ──► openrouter.ai  (parle l'API Anthropic nativement)    │
  │                                                               │
  └─ mode « zen » / « kilo » ◄───────────────────────────────────┘
       bridged()
         bridge.to_openai(corps)      Anthropic → OpenAI
         ──► passerelle /chat/completions
         bridge.stream_to_anthropic() OpenAI SSE → Anthropic SSE
  │
  ▼
stream()  re-découpe le corps amont pour que le SSE reste du SSE
```

### Le point délicat : la reprise sur `429`

```python
if mode == "native" and resp.status == 429:
    resp.read(); conn.close()          # on vide et on ferme proprement
    due  = retry_after(resp)           # quand le quota revient
    prov = chain()[0]
    set_mode(prov, "auto: quota Claude atteint", due)
    ...                                # la même requête repart
```

Deux choses en font une reprise et pas un simple message d'erreur :

1. **Aucun octet n'est encore parti vers le client.** C'est vérifiable : la
   réponse amont n'a pas encore été passée à `stream()`. Une fois le premier
   `data:` écrit, rejouer dupliquerait une réponse commencée — on remonte alors
   l'erreur telle quelle. C'est la limite documentée du dispositif.
2. **Le routeur écrit l'état lui-même**, au lieu d'appeler le CLI. La bascule a
   lieu au milieu d'une requête où un client attend ; lancer un sous-processus
   Python le ferait patienter le temps d'un démarrage d'interpréteur.

`retry_after()` lit, dans l'ordre : `retry-after`, puis
`anthropic-ratelimit-unified-reset`, `-requests-reset`, `-tokens-reset`. Une
valeur `> 1e9` est une époque, sinon c'est un délai. Sans rien d'exploitable,
30 minutes.

### Le chien de garde

Un thread de fond, toutes les 60 secondes :

- si le mode est `native`, il n'a rien à faire ;
- si la raison ne commence pas par `"auto"`, il ne touche à rien — **un repli
  manuel n'est jamais défait** ;
- sinon, passé `retryNativeAt`, il repasse en natif.

Il attrape `Exception` largement et journalise : un thread de fond qui meurt
laisserait le repli armé pour toujours, en silence.

## La traduction d'API

`bridge.py` ne fait que transformer des dictionnaires. Trois subtilités, chacune
apprise en la cassant :

- **Les `tool_result` deviennent des messages `role: "tool"` distincts**, placés
  *avant* le nouveau tour utilisateur. Les laisser dans le contenu utilisateur
  fait perdre au modèle le lien avec l'appel d'outil.
- **Les index de blocs de contenu sont partagés** : le texte occupe l'index 0,
  chaque appel d'outil prend le suivant. Claude Code recoud le flux sur ces
  index ; les mélanger produit une réponse muette ou tronquée.
- **Le contenu renvoyé n'est jamais vide.** Anthropic refuse un contenu vide, et
  un modèle gratuit répond parfois par du néant — un bloc texte vide évite un
  `400` incompréhensible côté client.

Les images sont converties en `data:` URI. Vers un modèle non multimodal, elles
deviennent la note littérale `[image non transmise : modèle non multimodal]` —
mieux qu'un plantage, et l'utilisateur comprend ce qui manque.

`count_tokens` est estimé localement (`mots × 0,75`) : les passerelles n'ont pas
cet endpoint, et un `404` casserait la compaction de contexte de Claude Code.

## La réécriture du nom de modèle, obligatoire

Laissé tel quel, `claude-opus-5` envoyé à OpenRouter est servi **depuis le vrai
Anthropic et facturé au crédit**. Le repli ne serait plus gratuit. Chaque alias
est donc réécrit vers un modèle `:free` de la passerelle, toujours, sans
exception. Les tables vivent dans les deux fichiers : `router.py` fait la
réécriture réelle, `fallback.py` sert l'affichage et les sondes — elles doivent
rester alignées.

## L'état

Un seul fichier, `~/.doublure/state.json`, relu à chaud avec un cache d'une
seconde. Écriture atomique (`.tmp` + `os.replace`) : un `state.json` tronqué
laisserait le routeur sans mode.

Les emplacements des versions précédentes sont **lus, jamais écrits** — une
installation existante ne doit perdre ni sa clé ni son mode courant.

## Ce qui rend l'installation auto-réparante

Le hook `SessionStart` rejoue `install.sh --quiet` à chaque lancement de
`claude`. L'installeur est idempotent : il répare l'`env` de `settings.json`,
recharge le LaunchAgent si le plist a changé, redémarre le routeur s'il manque.
Le hook `exit 0` toujours — un échec ici ne doit jamais empêcher de travailler.

`ensure_hook()` **ajoute un groupe** au lieu de réécrire la liste : d'autres
hooks `SessionStart` peuvent déjà s'y trouver, et les écraser serait casser un
montage qu'on n'a pas posé.

Détail qui a coûté un bug : il faut évaluer les deux vérifications séparément.

```python
env_done = ensure_router_env()
changed  = ensure_hook(arg) or env_done   # et non l'inverse : `or`
                                          # court-circuiterait le second
```

## Le démarrage du routeur

Le LaunchAgent est la voie normale (`RunAtLoad`, `KeepAlive`,
`ThrottleInterval 10`). Mais il peut ne pas être chargé — première
installation, session SSH sans domaine graphique. `start_router()` retombe
alors sur un `Popen` détaché : sans routeur, Claude Code n'a plus d'amont du
tout, ce qui est bien pire que de ne pas passer par launchd.
