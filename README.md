# Doublure

**La doublure de Claude Code.** Elle entre en scène quand la vedette ne peut
plus jouer — et la salle ne s'en aperçoit pas.

Quand la fenêtre de quota Claude est pleine, Claude Code s'arrête net : il n'a
pas de plan B. Doublure lui en donne un. Le `429` d'Anthropic est intercepté
*avant* qu'un seul octet ne soit parti vers ton terminal, l'amont bascule sur
un modèle gratuit, et **la même requête repart aussitôt**. Tu ne perds pas ton
message, tu ne relances rien, tu ne redémarres pas ta session.

```
  toi ─── claude ───► routeur local ──┬──► api.anthropic.com      (ton compte)
                       127.0.0.1:8099 │
                                       └──► zen / kilo / openrouter (gratuit)
                                            ▲
                                            └─ bascule ici, en vol, sur 429
```

- **Zéro configuration.** Deux des trois passerelles ne demandent aucune clé.
- **Bascule à chaud.** Une session ouverte depuis six heures suit, sans
  redémarrer.
- **Auto-réparante.** Un hook `SessionStart` vérifie le montage à chaque
  lancement de `claude` et le répare tout seul.
- **Rien à toi ne sort de la machine.** Voir [Sécurité](docs/SECURITE.md).

## Installation

```bash
git clone https://github.com/khalilbenaz/doublure
cd doublure
./install.sh
```

macOS, `python3 >= 3.9`, rien d'autre — aucune dépendance à installer.

Relance tes sessions Claude Code ouvertes une dernière fois : `settings.json`
n'est relu qu'au démarrage. Ensuite, plus jamais.

```bash
./uninstall.sh    # retire tout, y compris ce qui a été posé dans settings.json
```

## Utilisation

Le plus souvent : rien. Le repli est automatique. Quand tu veux regarder ou
forcer la main :

```bash
dbl                    # état courant
dbl on                 # forcer le repli (premier fournisseur joignable)
dbl on kilo            # forcer un fournisseur précis
dbl off                # revenir aux comptes Claude
dbl auto off           # désarmer le repli automatique
dbl models             # modèles servis par chaque fournisseur
dbl probe              # re-sonder les catalogues gratuits
dbl json               # état complet en JSON, pour un tableau de bord
```

Sortie typique :

```
mode    repli zen (opencode Zen)
        opus    nemotron-3-ultra-free
        sonnet  nemotron-3-ultra-free
        fable   nemotron-3.5-lightning-free
        haiku   nemotron-3.5-lightning-free
auto    arme
raison  auto: quota Claude atteint
natif   retente dans 24 min
routeur en ligne (http://127.0.0.1:8099)
```

Un repli pris **à la main** n'est jamais défait tout seul : seul un repli
automatique s'annule au retour du quota. Contredire un choix explicite serait
pire que de rester sur un modèle plus faible.

## Les trois passerelles

| Ordre | Fournisseur | Clé | Plafond | Particularité |
|---|---|---|---|---|
| 1 | **opencode Zen** | non | aucun constaté | API OpenAI, traduite |
| 2 | **Kilo** | non | aucun constaté | API OpenAI, catalogue plus large |
| 3 | **OpenRouter** | oui | 50 req/jour (gratuit) | seul à parler l'API Anthropic |

Les deux sans plafond passent d'abord. OpenRouter est le seul à comprendre
`/v1/messages` nativement — donc le plus fidèle — mais son palier gratuit
s'épuise en une session de travail, d'où sa dernière place. Sans clé, il quitte
la chaîne : le tenter donnerait un `401` présenté comme une panne du repli
alors que les autres auraient répondu.

Pour l'activer :

```bash
echo 'OPENROUTER_API_KEY=sk-or-...' >> ~/.doublure/.env
```

## Documentation

| | |
|---|---|
| [Architecture](docs/ARCHITECTURE.md) | Pourquoi un routeur, le chemin d'une requête, la reprise sur `429`, la traduction d'API |
| [Configuration](docs/CONFIGURATION.md) | Toutes les variables, toutes les clés d'état, changer de modèle ou d'ordre |
| [Dépannage](docs/DEPANNAGE.md) | Symptôme → cause → correctif, et où lire les journaux |
| [Sécurité](docs/SECURITE.md) | Ce qui ne quitte jamais la machine, et pourquoi |

## Limites

- **macOS uniquement.** Le jeton du mode natif vient du trousseau et le service
  est un LaunchAgent. Un portage Linux/systemd est possible, pas fait.
- **Les modèles gratuits sont nettement moins bons** qu'Opus ou Sonnet. C'est
  un filet, pas un remplacement. On y finit une tâche, on n'y commence pas une
  refonte.
- **Le repli n'est pris que si le `429` arrive avant le premier octet** de la
  réponse. Un quota atteint au milieu d'un flux remonte l'erreur telle quelle :
  rejouer aurait dupliqué une réponse déjà commencée.
- **Les passerelles gratuites ne servent que `/v1/messages`** (et le comptage
  de jetons, estimé localement). Le reste de l'API Anthropic répond `404` en
  mode repli.

## Licence

MIT — voir [LICENSE](LICENSE).
