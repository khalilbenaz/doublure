#!/usr/bin/env bash
# Installe le repli gratuit de Claude Code. Idempotent : ce script est aussi
# le hook SessionStart, donc il tourne a chaque lancement de `claude` et se
# contente de reparer ce qui manque.
#
#   ./install.sh            installation, verbeuse
#   ./install.sh --quiet    verification silencieuse (usage du hook)
set -uo pipefail

QUIET=0
[ "${1:-}" = "--quiet" ] && QUIET=1
say() { [ "$QUIET" = 1 ] || printf '%s\n' "$*"; }
die() { printf 'doublure: %s\n' "$*" >&2; exit 1; }

HERE=$(cd "$(dirname "$0")" && pwd)
SRC="$HERE/src"; [ -d "$SRC" ] || SRC="$HERE"
DEST="$HOME/.doublure"
LABEL="com.doublure.router"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

# Un python explicite : le LaunchAgent n'a pas le PATH du shell, un
# « python3 » nu s'y resoudrait mal ou pas du tout.
PY=$(command -v python3) || die "python3 introuvable"
PY=$("$PY" -c 'import sys; print(sys.executable)')
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' \
  || die "python3 >= 3.9 requis (trouve: $($PY -V 2>&1))"

[ -f "$SRC/router.py" ] || die "sources introuvables dans $SRC"
mkdir -p "$DEST" "$HOME/Library/LaunchAgents" || die "$DEST non creable"

# --- sources -------------------------------------------------------------
if [ "$SRC" != "$DEST" ]; then
  for f in router.py bridge.py fallback.py statefile.py; do
    cmp -s "$SRC/$f" "$DEST/$f" ||
      { cp "$SRC/$f" "$DEST/$f" && changed=1 && say "copie $f"; }
  done
  cmp -s "$HERE/install.sh" "$DEST/install.sh" || cp "$HERE/install.sh" "$DEST/install.sh"
  chmod +x "$DEST/install.sh"
fi

# --- hook ----------------------------------------------------------------
# Le hook appelle l'installeur copie dans $DEST, pas celui du depot : cloner
# ailleurs, ou supprimer le clone, ne doit pas casser un montage qui marche.
cat > "$DEST/hook.sh" <<HOOK
#!/usr/bin/env bash
# Verifie le repli a chaque demarrage de session Claude Code. Ne bloque
# jamais le lancement : un echec ici ne doit pas empecher de travailler.
"$DEST/install.sh" --quiet >>"$DEST/install.log" 2>&1
exit 0
HOOK
chmod +x "$DEST/hook.sh"

# --- commande dbl --------------------------------------------------------
cat > "$DEST/dbl" <<DBL
#!/usr/bin/env bash
exec "$PY" "$DEST/fallback.py" "\$@"
DBL
chmod +x "$DEST/dbl"
# Un lien dans ~/.local/bin s'il existe deja : on ne cree pas un dossier de
# PATH que l'utilisateur n'a pas voulu, il n'y serait probablement pas.
if [ -d "$HOME/.local/bin" ] && [ ! -e "$HOME/.local/bin/dbl" ]; then
  ln -s "$DEST/dbl" "$HOME/.local/bin/dbl" && say "dbl lie dans ~/.local/bin"
fi

# --- LaunchAgent ---------------------------------------------------------
# Le port ne suit que s'il a ete choisi : sinon le plist reste sans surcharge
# et le routeur prend son defaut, comme settings.json.
PORT_ENV=""
if [ -n "${DOUBLURE_PORT:-}" ]; then
  PORT_ENV="
  <key>EnvironmentVariables</key>
  <dict><key>DOUBLURE_PORT</key><string>$DOUBLURE_PORT</string></dict>"
fi
want=$(cat <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array><string>$PY</string><string>$DEST/router.py</string></array>${PORT_ENV}
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>StandardOutPath</key><string>$DEST/router.log</string>
  <key>StandardErrorPath</key><string>$DEST/router.log</string>
</dict>
</plist>
PLIST
)
if [ "$want" != "$(cat "$PLIST" 2>/dev/null)" ]; then
  printf '%s\n' "$want" > "$PLIST"
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null
  say "LaunchAgent ecrit"
fi
launchctl bootstrap "gui/$(id -u)" "$PLIST" 2>/dev/null

# Sources copiees : le daemon tourne toujours sur l'ancien code. Le hook de
# session copiait bien les fichiers mais ne rechargeait rien — un correctif
# n'entrait en vigueur qu'au prochain redemarrage de la machine. La coupure
# dure moins d'une seconde et le routeur ne garde aucun etat en memoire.
if [ -n "$changed" ]; then
  launchctl kickstart -k "gui/$(id -u)/$LABEL" 2>/dev/null &&
    say "routeur relance"
fi

# --- identifiant client OAuth ------------------------------------------
# Retrouve maintenant, pendant qu'on a le PATH complet de l'utilisateur : le
# LaunchAgent, lui, demarre avec un PATH minimal ou `claude` peut manquer.
# La valeur vient de l'installation de l'utilisateur, jamais du depot.
"$PY" - "$DEST" <<'EOF' >/dev/null 2>&1 || true
import sys
sys.path.insert(0, sys.argv[1])
import router
router.client_id()
EOF

# --- settings.json + demarrage ------------------------------------------
out=$("$PY" "$DEST/fallback.py" install "$DEST/hook.sh" 2>&1)
code=$?
say "$out"
[ "$code" = 0 ] || { [ "$QUIET" = 1 ] && exit 0; die "$out"; }

if [ "$QUIET" = 0 ]; then
  if [ -L "$HOME/.local/bin/dbl" ]; then say ""; say "Installe. Commande : dbl"
  else say ""; say "Installe. Commande : $DEST/dbl"; fi
  cat <<TXT

  dbl                    etat courant
  dbl accounts add <nom> enregistrer le compte Claude connecte
  dbl accounts           tes comptes, et celui qui sert
  dbl on [fcc|zen|kilo|or]  forcer un repli
  dbl off                revenir aux comptes Claude
  dbl auto off           desarmer le repli automatique

Claude Code ne relit settings.json qu'au demarrage : relance tes sessions
ouvertes une derniere fois. Ensuite le repli est automatique — quand ton
quota Claude tombe, la requete en cours repart sur un autre de tes comptes
si tu en as enregistre un, sinon par un modele gratuit.
TXT
fi
