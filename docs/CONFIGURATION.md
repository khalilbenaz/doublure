# Configuration

Rien n'est obligatoire. Tout ce qui suit sert à s'écarter du comportement par
défaut.

## Variables d'environnement

| Variable | Défaut | Effet |
|---|---|---|
| `DOUBLURE_PORT` | `8099` | Port du routeur. À changer si le port est pris, ou pour faire tourner une seconde installation sans qu'elle se batte avec la première. Doit être posé pour **le routeur et le CLI** : ils doivent désigner le même port. |
| `OPENROUTER_API_KEY` | — | Clé OpenRouter. Cherchée d'abord dans l'environnement, puis dans `~/.doublure/.env`. |
| `FCC_PORT` | `8082` | Port de Free Claude Code. Doit être posé pour **le routeur et le CLI**, comme `DOUBLURE_PORT`. |
| `CLAUDE_OAUTH_CLIENT_ID` | découvert | Force l'identifiant client OAuth au lieu de le lire dans l'installation locale de Claude Code. Utile si le bundle `cli.js` est introuvable (installation exotique). |

## Fichiers

```
~/.doublure/state.json     mode courant, ordre des fournisseurs, surcharges
~/.doublure/accounts.json  comptes Claude : nom, service trousseau, repos
~/.doublure/state.lock     verrou des deux fichiers ci-dessus, partagé par le
                           routeur et le CLI (vide, jamais lu — seul son
                           `flock` compte ; le supprimer est sans effet)
~/.doublure/.env           OPENROUTER_API_KEY=sk-or-...
~/.doublure/client-id      identifiant client OAuth mis en cache
~/.doublure/free-models.json   catalogue des modèles gratuits (cache 6 h)
~/.doublure/router.log     journal du routeur
~/.doublure/install.log    journal du hook SessionStart

~/.fcc/.env                config de Free Claude Code : lue, jamais écrite par
                           doublure. Se modifie chez lui, par son /admin.
```

## `state.json`

Écrit par le CLI et par le routeur, relu à chaud (cache 1 s). L'éditer à la
main marche, mais `dbl` est plus sûr.

| Clé | Type | Sens |
|---|---|---|
| `mode` | `"native"` \| `"fcc"` \| `"zen"` \| `"kilo"` \| `"or"` | Amont courant. |
| `auto` | booléen | Le repli automatique est-il armé. |
| `since` | époque | Depuis quand ce mode. |
| `reason` | texte | Pourquoi. Commence par `"auto"` si c'est le dispositif qui a décidé — **c'est ce préfixe qui autorise le chien de garde à défaire la bascule**. `"manuel"` est intouchable. |
| `lastError` | texte | Dernier échec rencontré, pour affichage. |
| `retryNativeAt` | époque | À partir de quand retenter Anthropic. `0` = jamais programmé. Passé cette date, le retour n'a lieu que si le relevé de quota le confirme ; sinon elle est repoussée. |
| `chain` | liste | Ordre d'essai des fournisseurs. Absent = `["fcc", "zen", "kilo", "or"]`. `fcc` est retiré à la volée si rien n'écoute sur son port. |
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

## Changer l'ordre des fournisseurs

```bash
python3 - <<'PY'
import json, os
p = os.path.expanduser("~/.doublure/state.json")
s = json.load(open(p))
s["chain"] = ["kilo", "zen"]        # OpenRouter écarté, Kilo d'abord
json.dump(s, open(p, "w"), indent=1)
PY
```

Les fournisseurs inconnus sont ignorés ; ceux que tu omets sont remis à la
fin, jamais perdus. `native` n'est pas un fournisseur de repli et se retire
tout seul de la liste. Sans clé, `or` en sort aussi.

## Changer les modèles

Chaque fournisseur sert quatre rôles — `opus`, `sonnet`, `fable`, `haiku` — qui
correspondent aux modèles que Claude Code demande. Pour voir l'existant :

```bash
dbl models
```

La surcharge vit dans `state.json`, sous `models` :

```json
{
  "models": {
    "kilo": { "haiku": "nvidia/nemotron-3.5-lightning:free" }
  }
}
```

Deux garde-fous : l'alias doit exister, et le modèle doit être **dans le
catalogue gratuit relu chez la passerelle**. Un identifiant hors catalogue est
refusé — mieux vaut un refus net qu'une facturation surprise.

Le catalogue est relu chez la passerelle (`/models`), filtré sur le suffixe
`:free`/`-free` et le drapeau `isFree`, et mis en cache 6 heures. `dbl probe`
force la relecture. Si la passerelle ne répond pas, le dernier bon cache sert ;
à défaut, une liste validée à la main — le dispositif ne doit jamais se
retrouver avec zéro modèle proposable juste parce qu'un `/models` a expiré.

## Free Claude Code

FCC est le premier maillon de la chaîne quand il tourne. Ce n'est pas une
passerelle comme les autres : il parle déjà l'API Anthropic et fait sa propre
correspondance de modèles. Doublure lui transmet donc le nom de modèle **tel
que Claude Code l'a demandé** (`claude-opus-…`, `claude-haiku-…`) et ne
retouche que l'authentification — `x-api-key: doublure` à la place du jeton
OAuth, et `anthropic-beta` débarrassé de sa partie `oauth-…`, qu'un amont non
Anthropic refuse.

La correspondance vit chez FCC, dans `~/.fcc/.env` :

| Clé | Rôle |
|---|---|
| `MODEL` | défaut, quand aucun alias ne correspond |
| `MODEL_OPUS` / `MODEL_SONNET` / `MODEL_HAIKU` / `MODEL_FABLE` | un par palier demandé par Claude Code |
| `MODEL_FALLBACKS` | cascade **interne** à FCC, chaîne séparée par des virgules (pas un tableau JSON) |

`dbl models` affiche ces valeurs à chaud, sans les recopier : changer la config
chez FCC suffit, il n'y a rien à resynchroniser ici. `MODEL_FALLBACKS` mérite
d'être rempli — laissé vide, un modèle arrivé en fin de vie fait répondre `410`
sans qu'aucune reprise n'ait lieu côté FCC ; la requête retombe alors sur le
maillon suivant de doublure, ce qui marche mais coûte un aller-retour.

Si rien n'écoute sur le port, `fcc` est retiré de la chaîne pour la requête en
cours — pas d'erreur, pas d'attente. Le résultat de la sonde est gardé 30
secondes : un proxy qu'on relance est repris tout seul.

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

## Le repos des passerelles gratuites

Une passerelle qui vient de refuser est écartée quelques minutes, pour ne pas
repayer son échec à chaque message. Deux constantes, en tête de `router.py` :

| Constante | Défaut | Rôle |
|---|---|---|
| `PROVIDER_REST` | `300` s | Repos après un `429` ou un `5xx`. Court : ces passerelles sont partagées, une saturation passe vite. |
| `PROVIDER_REST_NET` | `60` s | Repos après une panne réseau ou un amont injoignable. Encore plus court : c'est souvent une coupure de quelques secondes. |

Ce repos vit **en mémoire**, pas dans `state.json` : redémarrer le routeur le
remet à zéro, et c'est voulu. Si toutes les passerelles sont au repos en même
temps, les repos sont purgés et la chaîne est retentée — rendre une erreur alors
qu'une passerelle est peut-être revenue serait le mauvais choix.

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
