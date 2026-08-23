# claude-fallback

Quand le quota de Claude Code est atteint, la session s'arrête net jusqu'à la
reprise de la fenêtre. `claude-fallback` lui donne un plan B : la requête qui
vient de se faire refuser **repart aussitôt vers un modèle gratuit**, et le
travail continue. Rien à relancer, aucun message perdu.

Le retour aux comptes Claude est automatique dès que la fenêtre de quota
annoncée par Anthropic est écoulée.

```bash
git clone https://github.com/<toi>/claude-fallback && cd claude-fallback
./install.sh
```

Claude Code ne lit `settings.json` qu'au démarrage de session : après la
première installation, relance tes sessions ouvertes. Ensuite, plus rien à
faire — l'installation se vérifie elle-même à chaque lancement de `claude`.

## Comment ça marche

`settings.json` pointe une fois pour toutes sur un routeur local
(`127.0.0.1:8099`) au lieu de `api.anthropic.com`. Le routeur décide **à chaque
requête** où l'envoyer :

| mode | amont | clé requise |
|---|---|---|
| `native` | `api.anthropic.com`, avec ton jeton OAuth Claude Code | — |
| `zen` | opencode Zen | non |
| `kilo` | Kilo | non |
| `or` | OpenRouter (parle l'API Anthropic nativement) | `OPENROUTER_API_KEY` |

Pourquoi un routeur plutôt qu'une réécriture de `settings.json` : ce fichier
n'est relu qu'au démarrage de session. Y écrire le repli ne l'appliquerait qu'à
la session *suivante* — inutile à l'instant précis où le quota tombe. Le mode
vit donc dans un fichier d'état relu à chaud : une session ouverte depuis des
heures bascule sans redémarrer.

Le nom du modèle est **toujours** réécrit vers un modèle gratuit. Laissé tel
quel, `claude-opus-5` serait servi par OpenRouter depuis le vrai Anthropic et
facturé au crédit : le repli doit rester gratuit.

## Utilisation

```bash
cfb                    # état courant
cfb on                 # forcer le repli (premier fournisseur joignable)
cfb on kilo            # forcer un fournisseur précis
cfb off                # revenir aux comptes Claude
cfb auto off           # désarmer le repli automatique
cfb models             # modèles servis par chaque fournisseur
cfb probe              # re-sonder les catalogues gratuits
cfb json               # état complet, pour un dashboard
```

Un repli pris **à la main** n'est jamais défait tout seul : seul un repli
automatique s'annule au retour du quota, sinon l'outil contredirait un choix
explicite.

## OpenRouter (facultatif)

Les deux premières passerelles ne demandent aucune clé. OpenRouter est le seul
amont à parler l'API Anthropic nativement, mais son palier gratuit est plafonné
à 50 requêtes par jour. Pour l'activer :

```bash
echo 'OPENROUTER_API_KEY=sk-or-...' >> ~/.claude-fallback/.env
```

Sans clé, il est simplement retiré de la chaîne d'essai — le tenter donnerait
un 401 présenté comme une panne du repli alors que les autres auraient répondu.

## Ce qui est installé

```
~/.claude-fallback/          router.py, bridge.py, fallback.py, cfb, état, logs
                             client-id : ton identifiant client OAuth, lu
                             dans ta propre installation de Claude Code
~/Library/LaunchAgents/com.claude-fallback.router.plist
~/.claude/settings.json      env → routeur, + un hook SessionStart
```

Le hook SessionStart rejoue `install.sh --quiet` : il répare l'`env`, recharge
le LaunchAgent et redémarre le routeur s'il manque. C'est ce qui rend
l'installation auto-réparante sans jamais bloquer le lancement de `claude`.

```bash
./uninstall.sh    # retire tout, y compris les clés posées dans settings.json
```

## Ce qui ne sort jamais de la machine

Aucun identifiant n'est embarqué dans ce dépôt.

- **Jeton du compte Claude** : lu à la demande dans le trousseau macOS
  (`Claude Code-credentials`), rafraîchi sur place, jamais copié ailleurs.
- **Identifiant client OAuth** : extrait de *ta* propre installation de Claude
  Code au moment de l'installation, puis mis en cache dans
  `~/.claude-fallback/client-id`. Il est le même pour tout le monde, mais c'est
  à chacun de le prendre chez soi. `CLAUDE_OAUTH_CLIENT_ID` le surcharge.
- **Clé OpenRouter** : dans `~/.claude-fallback/.env`, hors dépôt.

## Limites

- **macOS uniquement** : le jeton du mode natif vient du trousseau et le
  service est un LaunchAgent. Un portage Linux/systemd est possible, pas fait.
- Les modèles gratuits sont nettement moins bons qu'Opus ou Sonnet. C'est un
  filet, pas un remplacement.
- Le repli n'est pris que si Anthropic répond `429` **avant** le premier octet
  de la réponse. Un quota atteint au milieu d'un flux SSE remonte l'erreur
  telle quelle : rejouer aurait dupliqué une réponse déjà commencée.
- Les passerelles gratuites ne servent que `/v1/messages` (et le comptage de
  jetons, estimé localement). Le reste de l'API Anthropic répond `404` en mode
  repli.
