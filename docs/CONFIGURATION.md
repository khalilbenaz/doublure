# Configuration

Rien n'est obligatoire. Tout ce qui suit sert à s'écarter du comportement par
défaut.

## Variables d'environnement

| Variable | Défaut | Effet |
|---|---|---|
| `DOUBLURE_PORT` | `8099` | Port du routeur. À changer si le port est pris, ou pour faire tourner une seconde installation sans qu'elle se batte avec la première. Doit être posé pour **le routeur et le CLI** : ils doivent désigner le même port. |
| `OPENROUTER_API_KEY` | — | Clé OpenRouter. Cherchée d'abord dans l'environnement, puis dans `~/.doublure/.env`. |
| `CLAUDE_OAUTH_CLIENT_ID` | découvert | Force l'identifiant client OAuth au lieu de le lire dans l'installation locale de Claude Code. Utile si le bundle `cli.js` est introuvable (installation exotique). |

## Fichiers

```
~/.doublure/state.json     mode courant, ordre des fournisseurs, surcharges
~/.doublure/.env           OPENROUTER_API_KEY=sk-or-...
~/.doublure/client-id      identifiant client OAuth mis en cache
~/.doublure/free-models.json   catalogue des modèles gratuits (cache 6 h)
~/.doublure/router.log     journal du routeur
~/.doublure/install.log    journal du hook SessionStart
```

## `state.json`

Écrit par le CLI et par le routeur, relu à chaud (cache 1 s). L'éditer à la
main marche, mais `dbl` est plus sûr.

| Clé | Type | Sens |
|---|---|---|
| `mode` | `"native"` \| `"zen"` \| `"kilo"` \| `"or"` | Amont courant. |
| `auto` | booléen | Le repli automatique est-il armé. |
| `since` | époque | Depuis quand ce mode. |
| `reason` | texte | Pourquoi. Commence par `"auto"` si c'est le dispositif qui a décidé — **c'est ce préfixe qui autorise le chien de garde à défaire la bascule**. `"manuel"` est intouchable. |
| `lastError` | texte | Dernier échec rencontré, pour affichage. |
| `retryNativeAt` | époque | À partir de quand retenter Anthropic. `0` = jamais programmé. |
| `chain` | liste | Ordre d'essai des fournisseurs. Absent = `["zen", "kilo", "or"]`. |
| `models` | objet | Surcharges de modèles, par fournisseur (voir plus bas). |

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
