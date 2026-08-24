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

## Les quatre pièces

```
src/router.py     Proxy HTTP sur 127.0.0.1:8099. Décide de l'amont, réécrit
                  les en-têtes, surveille le quota annoncé, intercepte le
                  429, relaie en flux.
src/bridge.py     Traduction Anthropic <-> OpenAI. Fonctions pures, aucun
                  état, aucun réseau — donc testable au doigt.
src/fallback.py   État, catalogue de modèles, sondes, bascules, CLI `dbl`.
src/statefile.py  Écriture atomique et verrou inter-processus des fichiers
                  d'état. Partagé par les deux précédents, qui écrivent les
                  mêmes fichiers depuis deux processus différents.
```

`install.sh` copie les quatre dans `~/.doublure`, écrit le LaunchAgent, et sert
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
  │      tous au repos ? → repli direct, sans aller chercher le 429│
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

Le re-découpage est en `chunked`, pour que le flux ressorte au fil de l'eau sans
attendre la fin de la génération — **sauf pour une réponse sans corps**. Un
`HEAD`, un `204` ou un `304` n'a rien à annoncer : y coller un
`Transfer-Encoding: chunked` laisse le client attendre un corps qui n'arrivera
jamais.

### La sonde de quota : savoir avant de se faire refuser

Un `429` est un refus **subi** : on l'apprend en se le prenant. Anthropic
publie le même fait à l'avance, sur l'endpoint que Claude Code utilise pour son
propre affichage de quota :

```
GET https://api.anthropic.com/api/oauth/usage
Authorization: Bearer <jeton OAuth du compte>
anthropic-beta: oauth-2025-04-20
```

Il rend, par fenêtre (`five_hour`, `seven_day`, et un tableau `limits`), un
pourcentage d'utilisation et une date de remise à zéro. `usage_verdict()` garde
**la fenêtre la plus avancée** — la limite hebdomadaire tombe aussi, et pour
plus longtemps — et déclare le compte épuisé au-delà de `USAGE_THRESHOLD`
(95 %, pas 100 : la requête suivante peut être celle qui dépasse).

Trois décisions valent d'être dites :

- **La sonde tourne en tâche de fond** (`usage_watch()`, un tour par minute),
  jamais sur le chemin d'une requête. Un `GET` synchrone avant chaque message
  ajouterait sa latence à chacun, pour une information qui bouge lentement.
- **Elle passe par `rest_account()`**, comme un `429`. Tout le reste du routeur
  — choix du compte, rotation, retour au natif par le `watchdog` — continue de
  fonctionner sans savoir d'où vient l'information.
- **Un échec de sonde ne met personne au repos.** `fetch_usage()` rend `None`
  sur réseau coupé, jeton refusé ou endpoint modifié, et la boucle passe au
  compte suivant. L'endpoint n'est pas documenté : le jour où il change, on
  perd la prévoyance, pas le service — le `429` reprend son rôle de filet.

Quand *tous* les comptes sont au repos, `relay()` part en repli **sans émettre
la requête native** : le refus est déjà connu, aller le chercher ne ferait que
perdre un aller-retour.

### Le filet : la reprise sur `429`

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

### La chaîne de repli, jusqu'au bout

`chain()` rend une liste — `["zen", "kilo", "or"]` — et `serve_fallback()`
l'essaie **maillon par maillon**. C'est nécessaire, pas décoratif : une
passerelle gratuite est partagée par tout le monde, sa saturation est le cas
courant. N'essayer que le premier maillon revenait à rendre son `429` au client
alors que les deux autres auraient répondu — l'exact contraire de la promesse.

```
serve_fallback(path, body, order)
  order          la passerelle en place d'abord, puis le reste de la chaîne
  ├─ écarte celles au repos (toutes au repos ? on purge et on retente :
  │  mieux vaut réessayer que rendre une erreur)
  ├─ pour chacune : bridged() ou or_served() en mode « quiet »
  │    répond → on persiste ce choix comme nouveau mode, et on sert
  │    refuse  → rest_provider(), on passe à la suivante
  └─ toutes épuisées → on rend l'échec du repli *préféré*, le plus parlant.
                       Les autres sont dans le journal.
```

Le repos d'une passerelle est **en mémoire seulement**, jamais dans
`state.json` : une indisponibilité de cinq minutes n'a pas à survivre au
routeur. Il dure `PROVIDER_REST` (5 min) sur un `429` ou un `5xx`, et
`PROVIDER_REST_NET` (60 s) sur une panne réseau — une coupure dure souvent
quelques secondes, la punir cinq minutes serait absurde.

Le mode `quiet` de `direct()` existe pour ça : hors cascade, un échec est écrit
au client et l'affaire est close ; en cascade, l'appelant veut la main pour
essayer la suivante.

### Une seule requête bascule à la fois

Claude Code émet plusieurs requêtes en parallèle — la conversation, la
compaction, les titres, les sous-agents. Elles tapent le même mur de quota au
même instant. Sans précaution, chacune déroule la rotation complète : tous les
comptes brûlés en double, autant de bascules concurrentes qui s'écrasent, et un
journal illisible.

D'où `_switch_lock`, tenu par la seule requête qui mène la bascule :

```python
held = _switch_lock.acquire()
try:
    if current_mode() != "native":
        # une autre requête a déjà tranché pendant notre 429 :
        # on suit sa décision au lieu de la refaire
        ...
    while (nxt := next_account(tried)):
        ...
        if resp.status != 429:
            _switch_lock.release()   # AVANT de streamer
            held = False
            return self.stream("native", path, conn, resp)
finally:
    if held:
        _switch_lock.release()
```

Deux détails portent tout le poids :

- **Le mode est relu après l'acquisition.** Celle qui attendait n'a plus rien à
  décider si la première a déjà basculé ; elle suit, ce qui est aussi la bonne
  réponse pour le client.
- **Le verrou tombe avant `stream()`.** Une génération dure des minutes. Le
  garder pendant le flux transformerait un verrou de décision en sérialisation
  de tout le trafic — le remède serait pire que le mal. Même raison pour le
  `serve_fallback` final, exécuté hors verrou.

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
- passé `retryNativeAt`, il demande confirmation au dernier relevé de quota
  (`quota_still_full()`) avant de rebasculer ;
- si tous les comptes sont *encore* annoncés pleins, il repousse la date au
  lieu de repasser en natif ;
- sinon, il repasse en natif.

La confirmation évite un aller-retour perdu : une date de retour optimiste
renverrait au natif juste pour y reprendre un `429` et retomber en repli — avec
une requête client qui attend pendant ce temps. Le doute, lui, profite au
retour : sans relevé (routeur qui vient de démarrer, sonde en panne, endpoint
modifié), `quota_still_full()` rend `None` et la bascule se fait. Retenir un
compte valide en repli gratuit sur un silence serait le pire des deux mondes ;
au pire on reprend un `429`, qui sait se rattraper tout seul.

Le report a un plancher de cinq minutes. Sans lui, une date de remise à zéro
déjà passée alors que le compte est toujours donné plein ferait repousser à
chaque tour — une ligne de journal par minute pour rien.

Il attrape `Exception` largement et journalise : un thread de fond qui meurt
laisserait le repli armé pour toujours, en silence.

**La même vérification a lieu à l'entrée de chaque requête.** Le chien de garde
ne passe qu'une fois par minute : une requête qui arrive entre deux tours
partait en modèle gratuit alors que le quota était déjà revenu — jusqu'à 60
secondes de qualité perdue pour rien. Le contrôle en entrée ne coûte rien : le
relevé de quota est déjà en cache, il n'émet aucun appel réseau.

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

## L'état, et les deux façons de le corrompre

`~/.doublure/state.json` porte le mode, relu à chaud avec un cache d'une
seconde ; `accounts.json` porte les comptes et leurs repos. Tous deux sont
écrits en `.tmp` + `os.replace` : un fichier tronqué laisserait le routeur sans
mode.

Ça ne suffit pas, et les deux défauts qui restaient étaient silencieux.

**Deux threads du routeur.** `set_mode()`, `set_active_account()` et
`write_accounts()` visaient tous le même nom de fichier temporaire. Deux
écritures simultanées et le second `open(tmp, "w")` tronque ce que le premier
était en train d'écrire — `os.replace` publie alors un JSON coupé en deux. Le
nom du `.tmp` porte donc maintenant le **pid et l'identifiant de thread**.

**Deux processus.** Le routeur et le CLI `dbl` écrivent les mêmes fichiers sans
se voir. Chacun lisait, modifiait, réécrivait : un `dbl accounts use perso`
tombant au mauvais moment **effaçait la bascule automatique** que le routeur
venait de décider. Une écriture atomique ne protège pas de ça — elle garantit
que le fichier est entier, pas qu'il tient compte de ce que l'autre vient
d'écrire.

D'où `statefile.py` : un verrou `fcntl.flock` sur `~/.doublure/state.lock`, pris
par les deux programmes autour de chaque cycle lire-modifier-écrire.

```python
with statefile.file_lock():
    cur = state()
    cur.update(kw)
    statefile.write_json(STATE_FILE, cur)
```

Un détail non négociable : le verrou est **réentrant**, par compteur de
profondeur. `flock` s'applique au descripteur de fichier, pas au thread — un
second `flock(LOCK_EX)` depuis le même processus réussit sans attendre et le
premier `LOCK_UN` relâche tout. Sans le compteur, `accounts_add()` qui appelle
`save_accounts()` sous le même verrou libérerait la protection au milieu de son
propre travail, en silence.

Et un verrou qu'on ne peut pas prendre ne doit pas empêcher de router :
`file_lock` avale l'`OSError` (volume en lecture seule, descripteurs épuisés) et
laisse passer, `write_json` rend `False` et le routeur continue en mémoire.
Router sans mémoriser vaut mieux que ne pas router.

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
