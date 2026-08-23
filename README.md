# Doublure

**La doublure de Claude Code.** Elle entre en scène quand la vedette ne peut
plus jouer — et la salle ne s'en aperçoit pas.

Quand la fenêtre de quota Claude est pleine, Claude Code s'arrête net : il n'a
pas de plan B. Doublure lui en donne deux. Le `429` d'Anthropic est intercepté
*avant* qu'un seul octet ne soit parti vers ton terminal, et **la même requête
repart aussitôt** — d'abord sur un **autre de tes comptes Claude**, et
seulement si tous sont épuisés, sur un modèle gratuit. Tu ne perds pas ton
message, tu ne relances rien, tu ne redémarres pas ta session.

```
  toi ─── claude ───► routeur local ──┬──► api.anthropic.com  compte 1 ─┐
                       127.0.0.1:8099 │                       compte 2  │ 429
                                      │                       compte 3 ─┘
                                      └──► zen / kilo / openrouter (gratuit)
                                           ▲
                                           └─ seulement quand tous ont dit 429
```

- **Plusieurs comptes Claude.** Rotation automatique sur `429`, le compte
  épuisé se met au repos, un vrai Opus avant tout modèle gratuit.
- **Zéro configuration.** Deux des trois passerelles ne demandent aucune clé.
- **Bascule à chaud.** Une session ouverte depuis six heures suit, sans
  redémarrer — changer de compte ne demande ni relogin ni nouvelle session.
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
dbl accounts           # tes comptes Claude, leur état, celui qui sert
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
compte   claude         repos 24 min     max, jeton 41 min
compte   perso          repos 11 min     pro, jeton 38 min
routeur en ligne (http://127.0.0.1:8099)
```

Un repli pris **à la main** n'est jamais défait tout seul : seul un repli
automatique s'annule au retour du quota. Contredire un choix explicite serait
pire que de rester sur un modèle plus faible.

## Plusieurs comptes Claude

Si tu as deux abonnements — un perso, un du boulot — Doublure les enchaîne. Sur
un `429`, le compte épuisé est mis au repos pour la durée qu'Anthropic annonce
lui-même, la requête repart sur le suivant, et le repli gratuit n'arrive qu'en
dernier recours.

Enregistrer un compte, c'est nommer celui auquel Claude Code est connecté *à
cet instant* :

```bash
claude                        # connecté avec le compte A
dbl accounts add perso        # → « perso » enregistré

claude                        # /login avec le compte B
dbl accounts add boulot       # → « boulot » enregistré

dbl accounts                  # les voir tous
```

```
* claude         pret             max, jeton 166 min
  perso          pret             max, jeton 166 min
  boulot         repos 12 min     pro, jeton 43 min
```

L'étoile marque le compte qui sert maintenant. `claude` est toujours là : c'est
l'entrée que Claude Code gère lui-même, elle n'est jamais modifiée.

```bash
dbl accounts use boulot   # forcer un compte, tout de suite
dbl accounts use auto     # rendre la main à la rotation
dbl accounts rm perso     # retirer (efface aussi sa copie du trousseau)
```

Le routeur choisit le compte **requête par requête** : `use` prend effet sur la
requête suivante, sans `/login` ni nouvelle session. Aucun jeton n'est écrit en
clair — chaque compte vit dans le trousseau macOS, sous son propre service
`Doublure-<nom>`, et `accounts.json` ne contient que des noms et des dates de
repos. Détails dans [Sécurité](docs/SECURITE.md).

## Les trois passerelles

Elles n'entrent en jeu qu'une fois **tous** tes comptes Claude au repos.

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
