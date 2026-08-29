# Configuration

Rien n'est obligatoire. Tout ce qui suit sert à s'écarter du comportement par
défaut.

## Variables d'environnement

| Variable | Défaut | Effet |
|---|---|---|
| `DOUBLURE_PORT` | `8099` | Port du routeur. À changer si le port est pris, ou pour faire tourner une seconde installation sans qu'elle se batte avec la première. Doit être posé pour **le routeur et le CLI** : ils doivent désigner le même port. |
| `<PROV>_API_KEY` | — | Clé d'un fournisseur (`OPENROUTER_API_KEY`, `GROQ_API_KEY`, `NVIDIA_NIM_API_KEY`…). Cherchée dans l'environnement, puis `~/.doublure/.env`, puis `~/.fcc/.env`. `dbl providers` nomme la variable attendue par chacun. L'environnement passe devant : `GROQ_API_KEY=… dbl probe groq` teste une clé sans l'écrire. |
| `FCC_PORT` | `8082` | Port de Free Claude Code. Doit être posé pour **le routeur et le CLI**, comme `DOUBLURE_PORT`. |
| `CLAUDE_OAUTH_CLIENT_ID` | découvert | Force l'identifiant client OAuth au lieu de le lire dans l'installation locale de Claude Code. Utile si le bundle `cli.js` est introuvable (installation exotique). |

## Fichiers

```
~/.doublure/state.json     mode courant, ordre des fournisseurs, surcharges
~/.doublure/accounts.json  comptes Claude : nom, service trousseau, repos
~/.doublure/state.lock     verrou des deux fichiers ci-dessus, partagé par le
                           routeur et le CLI (vide, jamais lu — seul son
                           `flock` compte ; le supprimer est sans effet)
~/.doublure/.env           les clés des fournisseurs, en 0600 (`dbl key`)
~/.doublure/client-id      identifiant client OAuth mis en cache
~/.doublure/catalog.json   catalogues de modèles par fournisseur (cache 6 h)
~/.doublure/health.json    relevé de santé par modèle : répond-il, appelle-t-il
                           un outil (7 j si bon, 1 h si cassé)
~/.doublure/router.log     journal du routeur
~/.doublure/install.log    journal du hook SessionStart

~/.fcc/.env                config de Free Claude Code : lue en source de clés
                           (`dbl import-fcc`), **jamais écrite** par doublure.
```

## `state.json`

Écrit par le CLI et par le routeur, relu à chaud (cache 1 s). L'éditer à la
main marche, mais `dbl` est plus sûr.

| Clé | Type | Sens |
|---|---|---|
| `mode` | `"native"` \| `"fcc"` \| `"or"` \| un id du registre | Amont courant. Les anciens noms courts (`"zen"`, `"nim"`, `"go"`) sont encore acceptés et traduits à la lecture. |
| `auto` | booléen | Le repli automatique est-il armé. |
| `since` | époque | Depuis quand ce mode. |
| `reason` | texte | Pourquoi. Commence par `"auto"` si c'est le dispositif qui a décidé — **c'est ce préfixe qui autorise le chien de garde à défaire la bascule**. `"manuel"` est intouchable. |
| `lastError` | texte | Dernier échec rencontré, pour affichage. |
| `retryNativeAt` | époque | À partir de quand retenter Anthropic. `0` = jamais programmé. Passé cette date, le retour n'a lieu que si le relevé de quota le confirme ; sinon elle est repoussée. |
| `chain` | liste | Ordre d'essai des fournisseurs. Absent = déduit du registre (tout fournisseur configuré, dans l'ordre de préférence, puis `fcc` s'il écoute). |
| `models` | objet | Surcharges de modèles, par fournisseur (voir plus bas). |
| `account` | texte | Compte Claude à utiliser. Absent ou `null` = rotation automatique. Un compte au repos est sauté même s'il est nommé ici. |

## `accounts.json`

Écrit par `dbl accounts`, relu par le routeur à chaque requête. **Aucun secret
n'y figure** : seulement le nom, le service de trousseau correspondant, et la
date de fin de repos.

```json
{
 "accounts": [
  {"name": "claude", "service": "Claude Code-credentials", "cooldownUntil": 1787527572},
  {"name": "perso",  "service": "Doublure-perso",          "cooldownUntil": 0}
 ]
}
```

| Clé | Sens |
|---|---|
| `name` | Ce que tu tapes dans `dbl accounts use <nom>`. Lettres, chiffres, `.`, `_`, `-`, 32 caractères max. |
| `service` | Service du trousseau macOS où vit le jeton. `Doublure-<nom>` pour les comptes ajoutés ; `Claude Code-credentials` pour celui de la session ; `claude-swap` pour ceux du pool. |
| `swapAccount` | Entrée de trousseau visée à l'intérieur d'un service partagé (`account-<slot>-<adresse>`). Présente seulement pour les comptes venus de claude-swap. |
| `cooldownUntil` | Époque jusqu'à laquelle le compte est sauté. Posée soit par la sonde de quota (date de remise à zéro annoncée), soit par un `429` (en-tête `retry-after`). `0` = disponible. |

Le compte nommé `claude` existe toujours, même absent du fichier : c'est
l'entrée que Claude Code gère lui-même. Le routeur la **lit** et n'y écrit que
pour le rafraîchissement de son propre jeton — exactement ce que Claude Code
ferait.

Effacer le fichier ne casse rien : on retombe sur le seul compte de la session,
et les copies restent dans le trousseau (à retirer avec `security
delete-generic-password -s Doublure-<nom>`, ou en les réenregistrant puis
`dbl accounts rm`).

## Ajouter, retirer, forcer un compte

```bash
dbl accounts                 # liste, l'étoile marque celui qui sert
dbl accounts add <nom>       # enregistre le compte connecté maintenant
dbl accounts rm <nom>        # retire, trousseau compris
dbl accounts use <nom>       # force ce compte, dès la requête suivante
dbl accounts use auto        # rend la main à la rotation
```

`add` copie l'entrée de trousseau **actuelle** : c'est donc `claude` puis
`/login` avec l'autre compte, puis `dbl accounts add <nom>`. Aucun OAuth n'est
rejoué.

`use` ne survit pas au repos : si le compte forcé rend un `429`, il est mis au
repos et la rotation reprend. Pour rester sur un compte coûte que coûte, il n'y
a rien — et c'est volontaire : refuser de servir une requête serait pire.

## Les comptes de claude-swap

Si [claude-swap](https://github.com/) gère un pool d'abonnements Claude, doublure
le lit et l'ajoute derrière le compte de la session, **avant** tout modèle
gratuit : un abonnement entier vaut mieux qu'un modèle libre, et le tenter ne
coûte qu'une requête. C'était le défaut le plus coûteux du dispositif — il
partait au gratuit alors qu'un autre abonnement était libre.

La lecture est **en lecture seule** :

```
~/.claude-swap-backup/cache/usage.json   liste des slots (cache 60 s)
trousseau, service « claude-swap »       un item par slot, jamais réécrit
```

Le jeton n'est jamais rafraîchi par doublure pour ces comptes : claude-swap
tient son propre rafraîchissement, et deux écrivains sur la même entrée se
marcheraient dessus. Doublure se contente donc du jeton en place — s'il est
expiré, c'est au démon `claude-swap auto` de le renouveler.

Deux tris à l'entrée, pour ne proposer que des comptes réels :

- le slot dont le **jeton de rafraîchissement** est celui de la session est
  écarté — c'est le même abonnement, le compter deux fois brûlerait son quota
  deux fois dans le journal ;
- un slot sans entrée de trousseau est écarté : le fichier de cache de
  claude-swap garde des comptes retirés depuis, qui ne donneraient que des
  `401`.

Ces comptes apparaissent dans `dbl accounts` (mention `claude-swap`) et dans
`/__router` (`"source": "claude-swap"`). `dbl accounts use swap2` marche comme
pour les autres. Rien à configurer : ajouter un compte dans claude-swap suffit,
doublure le voit au tour suivant.

## Les clés des fournisseurs

Doublure connaît 48 fournisseurs, dont 44 appelables. Aucun n'est actif
d'avance : il entre dans la chaîne quand il a une clé, qu'il n'en demande pas,
ou — pour un serveur local — qu'il écoute **et** sert au moins un modèle.

```bash
dbl providers             # tous, avec la variable attendue par chacun
dbl key groq gsk_...      # écrit dans ~/.doublure/.env (0600), puis sonde
dbl key groq              # « posee » ou « absente » — jamais la valeur
dbl import-fcc            # reprend celles de ~/.fcc/.env, sans jamais l'écrire
```

`~/.doublure/.env` est un fichier `CLE=valeur` par ligne, créé en `0600` et
réécrit de façon atomique. Rien ne le relit pour l'afficher : ni `/__router`
ni `dbl key` ne rendent une valeur de clé.

Quatre entrées du registre sont marquées `special` : leur authentification n'est
pas une simple clé d'API (jeton de session, OAuth interactif). `dbl providers`
dit pourquoi, au lieu de prétendre qu'il manque une clé.

## Changer l'ordre des fournisseurs

```bash
python3 - <<'EOF'
import json, os
p = os.path.expanduser("~/.doublure/state.json")
s = json.load(open(p))
s["chain"] = ["kilo", "opencode_zen"]   # ces deux-là d'abord, puis le reste
json.dump(s, open(p, "w"), indent=1)
EOF
```

Les fournisseurs inconnus sont ignorés ; ceux que tu omets sont remis à la
fin, jamais perdus. `native` n'est pas un fournisseur de repli et se retire
tout seul de la liste. Un fournisseur sans clé en sort aussi : le tenter
donnerait un `401` présenté comme une panne du repli alors qu'un autre aurait
répondu.

## Changer les modèles

Chaque fournisseur sert quatre paliers — `opus`, `sonnet`, `fable`, `haiku` —
qui correspondent aux modèles que Claude Code demande. Aucune liste n'est
écrite en dur : ils sont **déduits du catalogue** du fournisseur.

```bash
dbl models                # les paliers de chaque maillon de la chaîne
dbl models nim            # ses paliers, puis son catalogue complet
```

La déduction, dans l'ordre :

1. le catalogue est relu chez le fournisseur (`/models` ou son équivalent) et
   mis en cache 6 h dans `~/.doublure/catalog.json` ;
2. **si des variantes gratuites existent, seules celles-là sont retenues** —
   un repli qui coûte n'a plus d'intérêt ;
3. ce qui n'est pas de la génération de texte est écarté (plongements, rerank,
   image, audio, détecteurs) ;
4. chaque modèle reçoit une note tirée de la taille annoncée dans son
   identifiant, de sa famille et des marqueurs d'usage (`instruct`, `coder`,
   `reasoning`) ; les marqueurs disqualifiants (`embed`, `guard`…) l'écartent ;
5. le relevé de santé **écarte** un modèle constaté cassé ou sourd aux outils,
   mais ne promeut jamais un modèle juste parce qu'il a été sondé ;
6. les quatre paliers prennent les mieux notés, en fenêtres décroissantes, et
   une table de modèles vérifiés à la main passe devant s'il y en a une.

Pour forcer un palier :

```bash
dbl model nim opus nvidia/nemotron-3-ultra-550b-a55b
dbl model nim reset          # rendre les modèles déduits
```

La surcharge vit dans `state.json`, sous `models` :

```json
{
  "models": {
    "kilo": { "haiku": "nvidia/nemotron-3.5-lightning:free" }
  }
}
```

Deux garde-fous : le palier doit exister, et le modèle doit être **dans le
catalogue affiché par `dbl models <fournisseur>`**. Un identifiant hors
catalogue est refusé — mieux vaut un refus net qu'une facturation surprise.

Le routeur relit la surcharge à chaud : elle prend au message suivant, sans
rouvrir de session.

## La santé des modèles

Un modèle peut répondre et **ignorer les outils** — Claude Code ne s'en sert
alors à rien. La sonde envoie une vraie requête avec un outil et note les deux
faits séparément, dans `~/.doublure/health.json` : gardés 7 jours si bon,
1 heure si cassé, pour qu'un modèle qui revient soit repris de lui-même.

```bash
dbl probe                 # le premier de la chaîne qui répond
dbl probe nim             # forcer un fournisseur, relevé périmé compris
```

Rien ne sonde tout seul : ce serait des dizaines de requêtes pour rien. Les
catalogues, eux, sont réchauffés en fond par le routeur toutes les 30 minutes
(`CATALOG_POLL`, en tête de `router.py`) — hors du chemin de tes requêtes.

## Free Claude Code

FCC ferme la chaîne quand il tourne. Ce n'est pas un fournisseur comme les
autres : il parle déjà l'API Anthropic et fait sa propre correspondance de
modèles. Doublure lui transmet donc le nom de modèle **tel que Claude Code l'a
demandé** (`claude-opus-…`, `claude-haiku-…`) et ne retouche que
l'authentification — `x-api-key: doublure` à la place du jeton OAuth, et
`anthropic-beta` débarrassé de sa partie `oauth-…`, qu'un amont non Anthropic
refuse.

Sa correspondance vit chez lui, dans `~/.fcc/.env` :

| Clé | Rôle |
|---|---|
| `MODEL` | défaut, quand aucun alias ne correspond |
| `MODEL_OPUS` / `MODEL_SONNET` / `MODEL_HAIKU` / `MODEL_FABLE` | un par palier demandé par Claude Code |
| `MODEL_FALLBACKS` | cascade **interne** à FCC, chaîne séparée par des virgules (pas un tableau JSON) |

`dbl models fcc` affiche ces valeurs à chaud, sans les recopier. Le même
fichier sert de source de clés à `dbl import-fcc` — en lecture seule :
doublure n'y écrit jamais.

Si rien n'écoute sur le port, `fcc` est retiré de la chaîne pour la requête en
cours — pas d'erreur, pas d'attente. Le résultat de la sonde est gardé 30
secondes : un proxy qu'on relance est repris tout seul.

FCC n'est **pas** nécessaire : sans lui, la chaîne est celle du registre.

## La sonde de quota

Le routeur interroge `https://api.anthropic.com/api/oauth/usage` une fois par
minute et par compte, en tâche de fond, pour mettre au repos ceux qu'Anthropic
déclare épuisés **avant** qu'ils ne rendent un `429`. Trois constantes, en tête
de `router.py` :

| Constante | Défaut | Rôle |
|---|---|---|
| `USAGE_POLL` | `60.0` s | Intervalle entre deux tours de sonde. Un tour interroge tous les comptes. |
| `USAGE_THRESHOLD` | `95.0` % | Au-delà, le compte est mis au repos. Pas 100 : la requête suivante peut être celle qui dépasse. |
| `USAGE_BETA` | `oauth-2025-04-20` | En-tête `anthropic-beta` exigé par l'endpoint. |

Le taux vu se lit dans `dbl status` (colonne `quota`) et dans la sonde locale :

```bash
curl -s http://127.0.0.1:8099/__router | python3 -m json.tool
```

Pour désarmer la prévoyance et ne garder que le `429` comme déclencheur, mettre
`USAGE_THRESHOLD` à `101.0` dans `~/.doublure/router.py` puis
`launchctl kickstart -k gui/$(id -u)/com.doublure.router`. Un seuil plus bas
(80 par exemple) bascule plus tôt, au prix de quota Claude laissé sur la table.

## Le repos des fournisseurs gratuits

Un fournisseur qui vient de refuser est écarté quelques minutes, pour ne pas
repayer son échec à chaque message. Deux constantes, en tête de `router.py` :

| Constante | Défaut | Rôle |
|---|---|---|
| `PROVIDER_REST` | `300` s | Repos après un `429` ou un `5xx`. Court : ces services sont partagés, une saturation passe vite. |
| `PROVIDER_REST_NET` | `60` s | Repos après une panne réseau ou un amont injoignable. Encore plus court : c'est souvent une coupure de quelques secondes. |

Ce repos vit **en mémoire**, pas dans `state.json` : redémarrer le routeur le
remet à zéro, et c'est voulu. Si tous les fournisseurs sont au repos en même
temps, les repos sont purgés et la chaîne est retentée — rendre une erreur alors
qu'un fournisseur est peut-être revenu serait le mauvais choix.

## Désarmer le repli automatique

```bash
dbl auto off
```

Le routeur continue de router, mais un `429` remonte tel quel au lieu de
déclencher une bascule. À utiliser quand on préfère attendre le retour du quota
plutôt que de travailler avec un modèle plus faible.

## Faire tourner deux installations

```bash
DOUBLURE_PORT=8123 ./install.sh
```

Le port entre dans le plist (`EnvironmentVariables`) et dans l'`env` de
`settings.json`. Le CLI a besoin de la même variable pour parler au bon
routeur :

```bash
DOUBLURE_PORT=8123 dbl
```

## Sonder le routeur directement

```bash
curl -s http://127.0.0.1:8099/__router | python3 -m json.tool
```

Point d'entrée local, hors amont : il répond même sans réseau. C'est ce que
`dbl` interroge pour dire « en ligne » ou « hors ligne ».
