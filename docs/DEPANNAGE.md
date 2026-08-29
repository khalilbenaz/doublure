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

Si le journal montre un `EADDRINUSE`, le port est pris par autre chose — voir
juste en dessous, c'est le piège le plus coûteux du lot.

### Le routeur répond, mais rien de ce qui a changé ne s'applique

Le symptôme est trompeur : `dbl` dit « routeur en ligne », les requêtes passent,
et pourtant une nouveauté (un compte ajouté, une constante modifiée, une mise à
jour) reste sans effet. C'est qu'**un autre programme occupe le port** — une
installation précédente, un routeur maison, une seconde installation de
Doublure. Le vrai routeur, lui, meurt à chaque démarrage sur
`[Errno 48] Address already in use` et launchd le relance en boucle, sans que
rien ne remonte à la surface.

Qui écoute vraiment :

```bash
lsof -nP -iTCP:8099 -sTCP:LISTEN          # le PID qui tient le port
ps -ww -o args= -p <PID>                  # et le script qu'il exécute
grep "indisponible" ~/.doublure/router.log | tail -3
```

La sonde locale tranche aussi : si `/__router` ne rend **pas** de champ
`accounts`, ce n'est pas ce routeur-ci qui répond.

```bash
curl -s http://127.0.0.1:8099/__router | python3 -m json.tool
```

S'il s'agit d'un LaunchAgent tiers, sors-le et garde son plist de côté :

```bash
grep -rl 8099 ~/Library/LaunchAgents/
launchctl bootout gui/$(id -u)/<le.job.trouvé>
mv ~/Library/LaunchAgents/<le.job.trouvé>.plist{,.disabled}   # réversible
launchctl kickstart -k gui/$(id -u)/com.doublure.router
```

Si les deux doivent cohabiter, donne un autre port à Doublure :
`DOUBLURE_PORT=8123 ./install.sh` (et le même `DOUBLURE_PORT` pour `dbl`).

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
3. **Aucun fournisseur ne répondait.** Toute la chaîne est tentée avant qu'une
   erreur remonte, donc c'est bien qu'aucun n'a pu servir. `dbl probe` le dit,
   `lastError` dans `dbl` porte le détail, et le journal nomme chaque échec :

   ```bash
   grep "repli .* indisponible" ~/.doublure/router.log | tail -5
   ```

### Le repli reste armé alors que le quota est revenu

Regarde la raison :

```bash
dbl | grep raison
```

Si elle vaut `manuel`, c'est voulu : un repli pris à la main n'est jamais défait
tout seul. `dbl off` pour revenir.

Si elle commence par `auto`, le chien de garde attend `retryNativeAt`. La ligne
`natif retente dans N min` de `dbl` donne l'échéance. Elle vient de la date de
remise à zéro annoncée par Anthropic, de l'en-tête `retry-after` d'un `429`, ou
vaut 30 minutes par défaut.

**L'échéance peut être repoussée.** Passé la date, le chien de garde vérifie le
dernier relevé de quota : si tous les comptes sont encore annoncés pleins, il
reporte au lieu de rebasculer pour rien. Le journal le dit :

```bash
grep "repousse" ~/.doublure/router.log | tail -3
```

Si tu veux revenir tout de suite sans attendre cette confirmation :

```bash
dbl off
```

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

### « cle absente : XXX_API_KEY »

Attendu : un fournisseur sans clé quitte simplement la chaîne, sans erreur.
`dbl providers` nomme la variable attendue par chacun. Pour la poser :

```bash
dbl key groq gsk_...        # écrit ~/.doublure/.env en 0600, puis sonde
dbl import-fcc              # ou reprendre celles de Free Claude Code
```

### « quota gratuit épuisé (50/jour) »

Le palier gratuit d'OpenRouter est à bout pour la journée. Les trois
fournisseurs sans clé n'ont pas de plafond constaté : `dbl on zen`.

### Tous les paliers d'un fournisseur montrent le même petit modèle

Son catalogue est presque vide, ou tout ce qu'il contient a été écarté. Regarde
ce qui reste vraiment proposable :

```bash
dbl models nim              # les paliers, puis le catalogue complet
```

Un catalogue anormalement court vient souvent d'un `/models` qui a répondu à
moitié ; `dbl probe nim` force la relecture. Si le fournisseur ne publie que
des variantes payantes, il n'a rien de gratuit à offrir : c'est voulu qu'il
serve peu.

### Un modèle répond mais n'appelle jamais d'outil

Il est inutilisable par Claude Code, qui travaille par appels d'outils. La
sonde note les deux faits séparément :

```bash
dbl probe nim               # « repond, ignore les outils » le dit explicitement
```

Le relevé (`~/.doublure/health.json`) écarte alors ce modèle du classement,
pour 1 heure. Il est retenté ensuite : une panne passagère ne le condamne pas.

### Le modèle gratuit répond n'importe quoi, ou rien

Les modèles gratuits sont nettement plus faibles. Certains renvoient du JSON
d'appel d'outil malformé — la traduction le tolère, mais le résultat reste
approximatif. Essaie un autre fournisseur (`dbl providers` les liste), ou
attends le retour du quota. C'est un filet, pas un remplacement.

### Une image n'a pas été transmise

Le message `[image non transmise : modèle non multimodal]` apparaît quand le
modèle gratuit ne prend pas d'images. Mieux qu'un plantage, mais le modèle
travaille sans la pièce jointe : ne t'attends pas à ce qu'il la commente.

### Une commande de l'API Anthropic répond `404` en mode repli

Attendu. Les fournisseurs gratuits ne servent que `/v1/messages`, plus le
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
