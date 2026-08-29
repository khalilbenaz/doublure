# Sécurité

## Aucun identifiant n'est embarqué dans ce dépôt

Vérifiable :

```bash
git grep -nEi 'sk-|Bearer [A-Za-z0-9_-]{20,}|[0-9a-f]{8}-([0-9a-f]{4}-){3}[0-9a-f]{12}' $(git rev-list --all)
```

Trois secrets entrent en jeu, aucun n'est distribué.

### Les jetons des comptes Claude

Lus à la demande dans le trousseau macOS. Le compte de la session vit dans
l'entrée `Claude Code-credentials`, celle que Claude Code gère lui-même :
Doublure la lit, et n'y écrit que pour le rafraîchissement — exactement ce que
Claude Code ferait. Chaque jeton est rafraîchi quand son échéance approche
(marge de 5 minutes) et réécrit dans **son** service, jamais dans celui d'un
autre compte. Le cache mémoire (5 secondes) est indexé par service : deux
comptes en rotation ne se volent pas leur jeton.

Les comptes ajoutés par `dbl accounts add <nom>` sont des **copies** de cette
entrée, chacune dans son propre service `Doublure-<nom>` du trousseau. À dire
clairement :

- **C'est bien un vrai jeton de plus dans ton trousseau**, protégé comme
  l'original par le trousseau macOS — pas par nous. Ni chiffrement maison, ni
  fichier en clair.
- `accounts.json` **ne contient aucun secret** : un nom, un nom de service, une
  date de repos. Il peut être lu ou sauvegardé sans rien exposer.
- Le jeton copié reste valable après un `/login` sur un autre compte — c'est
  tout l'intérêt, et c'est aussi ce qu'il faut savoir avant d'ajouter un compte
  qui n'est pas le tien. `dbl accounts rm <nom>` efface la copie du trousseau,
  et `./uninstall.sh` efface toutes celles que Doublure a posées.
- Vérifiable à tout moment :
  `security find-generic-password -s Doublure-<nom>` (métadonnées) — et rien
  d'autre que `Doublure-*` n'est créé par nous.

Ces jetons ne partent que vers `api.anthropic.com`, en mode natif, et chacun
seulement sur les requêtes servies par son propre compte. **Les fournisseurs
gratuits ne les voient jamais** : en mode repli, l'en-tête d'autorisation est
reconstruit pour le fournisseur et aucun jeton Anthropic n'est dans la requête.

Une exception à connaître : la **sonde de quota** envoie chaque jeton à
`https://api.anthropic.com/api/oauth/usage`, une fois par minute, pour lire le
taux d'utilisation du compte. Même hôte, même jeton, même en-tête que tes
requêtes normales — rien de nouveau ne quitte la machine, et rien de ce que la
sonde lit n'est écrit ailleurs que dans la mémoire du routeur (les pourcentages
ne sont pas persistés ; seule la date de repos atterrit dans
`accounts.json`).

### L'identifiant client OAuth

Nécessaire pour rafraîchir un jeton. Il est **le même pour toute installation de
Claude Code**, mais il n'est pas écrit en dur ici : c'est la valeur de
l'utilisateur, pas une valeur à distribuer dans un dépôt public. `client_id()`
la retrouve dans le bundle `cli.js` de *ton* installation — binaire `claude`
résolu par `realpath` puis remontée jusqu'au paquet, plus les emplacements
habituels de npm, Homebrew, nvm, fnm et volta — et la met en cache dans
`~/.doublure/client-id`. `CLAUDE_OAUTH_CLIENT_ID` la surcharge.

### Les clés des fournisseurs

Toutes facultatives, toutes dans `~/.doublure/.env`, hors dépôt. Le fichier est
créé en `0600` et réécrit de façon atomique (écriture dans un temporaire, puis
`rename`) : il n'existe à aucun instant en mode plus large. Une clé est cherchée
d'abord dans l'environnement, puis dans ce fichier, puis dans `~/.fcc/.env` —
lu **en lecture seule**, jamais écrit, pour ne pas redemander ce qui est déjà
posé là.

Une clé n'est envoyée qu'à **son** fournisseur : le registre associe chaque clé
à un seul hôte, et rien ne la recopie ailleurs.

Rien ne rend jamais une valeur de clé :

- `dbl key <fournisseur>` répond « posee » ou « absente » — l'afficher la
  publierait dans l'historique du terminal ;
- `/__router` expose, par fournisseur, s'il a une clé (`hasKey`), s'il n'en
  demande pas (`keyless`), s'il est local et s'il est dans la chaîne. Jamais la
  valeur.

## Le routeur n'écoute qu'en loopback

`127.0.0.1` uniquement, jamais `0.0.0.0`. Aucune authentification n'est
demandée sur le port — elle n'apporterait rien : quiconque peut atteindre le
loopback peut déjà lire le trousseau. En revanche, `NO_PROXY` est posé dans
`settings.json` pour que le trafic vers le routeur ne parte jamais dans un proxy
HTTP d'entreprise.

## Un compte de plus, c'est un `429` de plus toléré

La rotation ne contourne aucune limite : chaque compte garde la sienne, et un
compte épuisé est mis au repos jusqu'à la date qu'Anthropic annonce lui-même.
Doublure ne fabrique pas de quota, elle utilise ceux que tu as déjà. Si tes
conditions d'abonnement encadrent le partage de comptes, c'est à toi de ne
mettre là que les tiens.

## Ce qui est envoyé aux fournisseurs gratuits

**Le contenu de tes conversations**, quand le repli est actif : messages,
extraits de code, sorties d'outils, invites système. C'est inhérent au principe
— on demande à un modèle tiers de répondre à ta place.

À en tenir compte :

- Ce sont des services gratuits : leur politique de rétention ne t'est pas
  connue et n'est pas garantie. Certains disent entraîner sur ce qui passe.
- La sonde de santé (`dbl probe`) envoie une requête factice — « appelle
  l'outil `lire` sur `/etc/hosts` », avec un outil déclaré mais jamais exécuté :
  rien de ton travail n'y passe, et aucun fichier n'est lu.
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
`~/.doublure`, le LaunchAgent, le dossier `~/.doublure` entier, et **les copies
de comptes qu'il a posées dans le trousseau** (`Doublure-*`) — les laisser
derrière reviendrait à laisser de vrais jetons traîner. Rien d'autre : une
configuration tierce dans `settings.json` survit.

L'entrée de Claude Code lui-même n'est pas touchée : ta session reste
connectée, comme avant.

## Signaler un problème

Ouvre une issue. S'il s'agit d'une fuite d'identifiant, écris-le en premier mot
du titre pour que ce soit traité avant le reste.
