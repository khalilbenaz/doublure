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
  │    active_account()      compte à servir, en sautant les repos│
  │    direct("native")                                           │
  │      x-api-key retiré, Authorization: Bearer <jeton du compte> │
  │      anthropic-beta: oauth-2025-04-20 en tête                 │
  │      ──► api.anthropic.com                                    │
  │                                                               │
  │    si 429 et rien n'est encore parti au client :              │
  │      rest_account(compte, retry_after(resp))   mise au repos   │
  │      next_account() → un autre compte ? on rejoue ici même     │
  │      plus aucun compte libre :                                │
  │        set_mode(premier de la chaîne, "auto: quota atteint")   │
  │        la MÊME requête repart ci-dessous ────────────────────┐│
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
    due = retry_after(resp)            # quand ce compte revient
    resp.read(); conn.close()          # on vide et on ferme proprement
    rest_account(account["name"], due) # ce compte-là se repose

    tried = {account["name"]}          # d'abord un AUTRE compte Claude
    while (nxt := next_account(tried)):
        conn, resp = self.direct("native", body, nxt)
        if resp.status != 429:
            return self.stream("native", path, conn, resp)
        due = min(due, retry_after(resp))
        rest_account(nxt["name"], ...); tried.add(nxt["name"])

    set_mode(chain()[0], "auto: quota Claude atteint", due)   # alors le gratuit
```

Deux choses en font une reprise et pas un simple message d'erreur :

1. **Aucun octet n'est encore parti vers le client.** C'est vérifiable : la
   réponse amont n'a pas encore été passée à `stream()`. Une fois le premier
   `data:` écrit, rejouer dupliquerait une réponse commencée — on remonte alors
   l'erreur telle quelle. C'est la limite documentée du dispositif.
2. **Le routeur écrit l'état lui-même**, au lieu d'appeler le CLI. La bascule a
   lieu au milieu d'une requête où un client attend ; lancer un sous-processus
   Python le ferait patienter le temps d'un démarrage d'interpréteur.

L'ordre compte : un vrai Opus sur un second abonnement vaut mieux qu'un modèle
gratuit, donc **les comptes passent tous avant la première passerelle**. La date
de retour au natif est celle du **premier compte à se libérer** (`min` des
repos) : rester en repli plus longtemps que nécessaire coûte en qualité.

`retry_after()` lit, dans l'ordre : `retry-after`, puis
`anthropic-ratelimit-unified-reset`, `-requests-reset`, `-tokens-reset`. Une
valeur `> 1e9` est une époque, sinon c'est un délai. Sans rien d'exploitable,
30 minutes.

### Plusieurs comptes Claude

Le compte n'est pas choisi au démarrage mais **à chaque requête**, par
`active_account()` — d'où le fait qu'un changement de compte ne demande ni
`/login` ni nouvelle session.

```
all_accounts()   « claude »            = l'entrée du trousseau de Claude Code,
                                          lue, jamais modifiée
                 « <nom> »             = copies posées par `dbl accounts add`,
                                          service trousseau « Doublure-<nom> »

active_account() le compte demandé dans l'état s'il est libre,
                 sinon le premier libre dans l'ordre ci-dessus

rest_account()   met un compte au repos jusqu'à la date annoncée par Anthropic
next_account()   le suivant qui n'est ni au repos, ni déjà essayé,
                 ni absent du trousseau
```

Trois décisions valent d'être dites :

- **`accounts.json` ne contient aucun secret** — un nom, un service de
  trousseau, une date de repos. Le jeton reste dans le trousseau, chaque compte
  dans son propre service. Le fichier peut être lu, sauvegardé, versionné sans
  rien exposer.
- **Le compte de la session est traité comme les autres pour le repos.** Son
  `cooldownUntil` est mémorisé dans le même fichier même s'il n'y avait pas
  d'entrée au départ (`all_accounts()` fusionne les deux) — sans ça, il serait
  réessayé en boucle sur son propre `429`.
- **Un compte dont l'entrée de trousseau a disparu est sauté**, pas proposé :
  il donnerait un `401` présenté comme une panne du routeur.

`dbl accounts add <nom>` ne refait pas l'OAuth : il **copie** l'entrée que
Claude Code vient d'écrire, sous un nom à nous. C'est ce qui permet de gérer
plusieurs comptes sans réimplémenter la connexion — et le rafraîchissement de
jeton, lui, se fait compte par compte, chacun réécrit dans son propre service.

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
