# Dépannage

## Regarder d'abord

```bash
dbl                                  # mode, raison, dernière erreur, routeur
tail -20 ~/.doublure/router.log      # décisions du routeur, bascules, erreurs
tail -20 ~/.doublure/install.log     # ce que le hook SessionStart a fait
curl -s http://127.0.0.1:8099/__router   # le routeur répond-il du tout
```

## Symptôme → cause → correctif

### `dbl` dit « routeur hors ligne »

Le LaunchAgent n'est pas chargé, ou le routeur a crashé plus vite que
`ThrottleInterval`.

```bash
launchctl kickstart -k "gui/$(id -u)/com.doublure.router"
tail -30 ~/.doublure/router.log
```

Si le journal montre un `EADDRINUSE`, le port est pris par autre chose :
réinstalle avec `DOUBLURE_PORT=8123 ./install.sh`.

### Claude Code dit « connection refused »

`settings.json` pointe sur le routeur, qui n'est pas là. Le hook devrait
réparer au prochain lancement ; pour forcer :

```bash
~/.doublure/install.sh
```

En dernier recours, `dbl off` retire l'`env` de `settings.json` si le routeur
est vraiment injoignable — mieux vaut Claude Code sans repli que Claude Code
sans sortie.

### Le repli ne s'est pas déclenché malgré un quota atteint

Trois causes possibles, dans l'ordre de fréquence :

1. **Le `429` est arrivé après le début du flux.** Rien à faire : rejouer
   aurait dupliqué une réponse commencée. C'est la limite du dispositif.
2. **`auto` est désarmé.** `dbl auto on`. (La rotation de comptes, elle, a
   quand même eu lieu : `dbl accounts` le montre.)
3. **Aucune passerelle ne répondait.** `dbl probe` le dit, et `lastError` dans
   `dbl` porte le détail.

### Le repli reste armé alors que le quota est revenu

Regarde la raison :

```bash
dbl | grep raison
```

Si elle vaut `manuel`, c'est voulu : un repli pris à la main n'est jamais défait
tout seul. `dbl off` pour revenir.

Si elle commence par `auto`, le chien de garde attend `retryNativeAt`. La ligne
`natif retente dans N min` de `dbl` donne l'échéance. Elle vient de l'en-tête
d'Anthropic, ou vaut 30 minutes par défaut.

### Un compte ajouté n'est jamais utilisé

```bash
dbl accounts
```

Trois états parlent d'eux-mêmes :

- **`trousseau vide`** : l'entrée a disparu (compte révoqué, trousseau nettoyé).
  Le routeur saute ce compte, sinon il donnerait un `401` ressemblant à une
  panne. Refais `claude` + `/login` avec ce compte, puis
  `dbl accounts add <même nom>` — l'entrée est écrasée.
- **`repos N min`** : le compte est épuisé — soit la sonde de quota l'a vu venir
  (la colonne `quota` est alors à 95 % ou plus), soit il a rendu un `429`. Il
  attend la date annoncée par Anthropic. Normal.
- **`pret` mais sans étoile** : un autre compte est forcé. `dbl accounts use
  auto` rend la main à la rotation.

### Un compte est mis au repos alors qu'il marchait très bien

Regarde la colonne `quota` de `dbl status`. Au-delà de 95 %
(`USAGE_THRESHOLD`), la sonde met le compte au repos **avant** le refus : c'est
voulu, et le journal le dit en clair.

```bash
grep "quota annonce" ~/.doublure/router.log | tail -5
```

Si le pourcentage te paraît faux, compare avec la source :

```bash
python3 - <<'PY'
import json, subprocess, urllib.request
blob = json.loads(subprocess.run(
    ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
    capture_output=True, text=True).stdout)
tok = blob["claudeAiOauth"]["accessToken"]
req = urllib.request.Request(
    "https://api.anthropic.com/api/oauth/usage",
    headers={"Authorization": f"Bearer {tok}",
             "anthropic-beta": "oauth-2025-04-20",
             "User-Agent": "doublure/1.0"})
print(json.dumps(json.load(urllib.request.urlopen(req, timeout=10)), indent=1))
PY
```

Attention : la fenêtre retenue est **la plus avancée**, pas forcément les cinq
heures. Un `seven_day` à 97 % met le compte au repos pour des jours — c'est
exact, et c'est précisément le cas qu'un `429` t'aurait appris trop tard. Pour
ne plus basculer par prévoyance, voir `USAGE_THRESHOLD` dans
[Configuration](CONFIGURATION.md#la-sonde-de-quota).

### La colonne `quota` est vide

La sonde n'a pas de relevé pour ce compte. Trois causes, dans l'ordre de
probabilité : le routeur vient de démarrer (le premier tour prend une minute),
le jeton du compte est refusé, ou l'endpoint a changé de forme. Rien n'est
cassé pour autant — sans relevé, Doublure retombe sur le `429` :

```bash
grep "sonde de quota" ~/.doublure/router.log | tail -5
```

### `dbl accounts add` dit « aucun compte connecté dans le trousseau »

`add` copie l'entrée que Claude Code vient d'écrire ; sans session connectée il
n'y a rien à copier. Lance `claude`, connecte-toi, quitte, puis réessaie.

Si tu es bien connecté, vérifie que l'entrée existe :

```bash
security find-generic-password -s "Claude Code-credentials" -w | head -c 40
```

### Les deux comptes sont épuisés presque en même temps

Attendu si les deux abonnements étaient déjà entamés : la rotation ne fabrique
pas de quota, elle utilise ceux qui restent. `dbl` montre les deux repos et
l'échéance retenue pour revenir au natif — la plus proche des deux. Dans ce cas
le repli est pris **sans même émettre la requête native** : le refus est déjà
connu, l'aller-retour serait perdu.

### « OPENROUTER_API_KEY absente »

Attendu si tu n'as pas de clé — OpenRouter quitte simplement la chaîne. Pour
l'ajouter :

```bash
echo 'OPENROUTER_API_KEY=sk-or-...' >> ~/.doublure/.env
```

### « quota gratuit épuisé (50/jour) »

Le palier gratuit d'OpenRouter est à bout pour la journée. Les deux autres
passerelles n'ont pas de plafond constaté : `dbl on zen`.

### Le modèle gratuit répond n'importe quoi, ou rien

Les modèles gratuits sont nettement plus faibles. Certains renvoient du JSON
d'appel d'outil malformé — la traduction le tolère, mais le résultat reste
approximatif. Essaie un autre fournisseur (`dbl on kilo`), ou attends le retour
du quota. C'est un filet, pas un remplacement.

### Une image n'a pas été transmise

Le message `[image non transmise : modèle non multimodal]` apparaît quand le
modèle gratuit ne prend pas d'images. Mieux qu'un plantage, mais le modèle
travaille sans la pièce jointe : ne t'attends pas à ce qu'il la commente.

### Une commande de l'API Anthropic répond `404` en mode repli

Attendu. Les passerelles gratuites ne servent que `/v1/messages`, plus le
comptage de jetons estimé localement. Le reste de l'API n'existe pas chez
elles.

### `identifiant client OAuth introuvable`

Le bundle `cli.js` de Claude Code n'a pas été trouvé — installation à un
emplacement inhabituel. Trouve-le et pose la valeur à la main :

```bash
grep -o 'CLIENT_ID:"[^"]*"' "$(dirname "$(realpath "$(command -v claude)")")"/../cli.js
```

puis ajoute `CLAUDE_OAUTH_CLIENT_ID=...` à l'environnement du LaunchAgent, ou
écris la valeur dans `~/.doublure/client-id`.

Sans elle, seul le **rafraîchissement** du jeton échoue : le jeton en place
continue de servir jusqu'à son échéance.

### Le hook ralentit le lancement de `claude`

Il rejoue `install.sh --quiet`, qui compare des fichiers et sonde le routeur —
quelques dizaines de millisecondes. S'il prend visiblement plus, lis
`~/.doublure/install.log` : un `launchctl` qui traîne est le suspect habituel.

## Repartir de zéro

```bash
./uninstall.sh && ./install.sh
```

`uninstall.sh` ne retire que ses propres clés d'`env` et que les entrées de hook
dont la commande pointe vers `~/.doublure` : une configuration tierce dans
`settings.json` survit.
