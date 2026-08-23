# Sécurité

## Aucun identifiant n'est embarqué dans ce dépôt

Vérifiable :

```bash
git grep -nEi 'sk-|Bearer [A-Za-z0-9_-]{20,}|[0-9a-f]{8}-([0-9a-f]{4}-){3}[0-9a-f]{12}' $(git rev-list --all)
```

Trois secrets entrent en jeu, aucun n'est distribué.

### Le jeton du compte Claude

Lu à la demande dans le trousseau macOS, entrée `Claude Code-credentials` —
celle que Claude Code gère lui-même. Rafraîchi sur place quand son échéance
approche (marge de 5 minutes), réécrit dans le trousseau, jamais copié
ailleurs. Il est mis en cache 5 secondes en mémoire : assez pour ne pas
interroger le trousseau à chaque requête, assez peu pour voir un changement de
compte presque tout de suite.

Il ne part que vers `api.anthropic.com`, en mode natif. **Les passerelles
gratuites ne le voient jamais** : en mode repli, l'en-tête d'autorisation est
reconstruit pour la passerelle et le jeton Anthropic n'est pas dans la requête.

### L'identifiant client OAuth

Nécessaire pour rafraîchir un jeton. Il est **le même pour toute installation de
Claude Code**, mais il n'est pas écrit en dur ici : c'est la valeur de
l'utilisateur, pas une valeur à distribuer dans un dépôt public. `client_id()`
la retrouve dans le bundle `cli.js` de *ton* installation — binaire `claude`
résolu par `realpath` puis remontée jusqu'au paquet, plus les emplacements
habituels de npm, Homebrew, nvm, fnm et volta — et la met en cache dans
`~/.doublure/client-id`. `CLAUDE_OAUTH_CLIENT_ID` la surcharge.

### La clé OpenRouter

Facultative, dans `~/.doublure/.env`, hors dépôt. Cherchée d'abord dans
l'environnement. Envoyée à `openrouter.ai` seulement.

## Le routeur n'écoute qu'en loopback

`127.0.0.1` uniquement, jamais `0.0.0.0`. Aucune authentification n'est
demandée sur le port — elle n'apporterait rien : quiconque peut atteindre le
loopback peut déjà lire le trousseau. En revanche, `NO_PROXY` est posé dans
`settings.json` pour que le trafic vers le routeur ne parte jamais dans un proxy
HTTP d'entreprise.

## Ce qui est envoyé aux passerelles gratuites

**Le contenu de tes conversations**, quand le repli est actif : messages,
extraits de code, sorties d'outils, invites système. C'est inhérent au principe
— on demande à un modèle tiers de répondre à ta place.

À en tenir compte :

- Zen et Kilo sont des passerelles gratuites : leur politique de rétention ne
  t'est pas connue et n'est pas garantie.
- Si tu travailles sur du code sous contrat ou sous NDA, **désarme le repli** :
  `dbl auto off`. Tu attendras le retour du quota, ce qui est le comportement
  d'origine de Claude Code.
- Le repli automatique se déclenche **sans te demander**, par construction —
  c'est tout l'intérêt. `auto off` est le seul interrupteur qui empêche un
  contenu de partir chez un tiers sans décision explicite de ta part.

Le mode natif, lui, ne parle qu'à Anthropic, exactement comme Claude Code sans
Doublure.

## Le hook `SessionStart`

Il exécute `~/.doublure/install.sh --quiet` à chaque lancement de `claude` —
c'est-à-dire du code sur ta machine, avec tes droits, à chaque session. Il est
dans le dépôt, lisible en entier, et fait trois choses : comparer des fichiers,
réparer `settings.json`, redémarrer le routeur. Il pointe vers la copie dans
`~/.doublure`, pas vers le clone : supprimer le clone ne casse pas un montage
qui marche, mais cela veut aussi dire que **mettre à jour le dépôt ne suffit
pas** — il faut rejouer `./install.sh`.

## Ce que `uninstall.sh` retire

Ses propres clés d'`env` (`ANTHROPIC_BASE_URL`, `ANTHROPIC_AUTH_TOKEN`,
`NO_PROXY`, `no_proxy`), les entrées de hook dont la commande pointe vers
`~/.doublure`, le LaunchAgent, et le dossier `~/.doublure` entier. Rien
d'autre : une configuration tierce dans `settings.json` survit.

Il ne touche pas au trousseau. Ton compte Claude reste connecté, comme avant.

## Signaler un problème

Ouvre une issue. S'il s'agit d'une fuite d'identifiant, écris-le en premier mot
du titre pour que ce soit traité avant le reste.
