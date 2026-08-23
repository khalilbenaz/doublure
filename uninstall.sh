#!/usr/bin/env bash
# Retire le repli : LaunchAgent, hook, cles env, et le dossier d'etat.
set -uo pipefail
DEST="$HOME/.doublure"
LABEL="com.doublure.router"
PY=$(command -v python3) || PY=python3

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null
rm -f "$HOME/Library/LaunchAgents/$LABEL.plist"
pkill -f "$DEST/router.py" 2>/dev/null

"$PY" - "$DEST" <<'PYEOF'
import json, os, sys
dest = sys.argv[1]
path = os.path.expanduser("~/.claude/settings.json")
try:
    with open(path) as fh:
        data = json.load(fh)
except (OSError, ValueError):
    sys.exit("settings.json illisible — a nettoyer a la main")
# On retire les cles qu'on avait posees, pas l'objet env : d'autres cles y
# vivent peut-etre, et Claude Code sans env ne vaut pas mieux.
env = data.get("env") or {}
for k in ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "NO_PROXY", "no_proxy"):
    env.pop(k, None)
data["env"] = env
groups = []
for grp in (data.get("hooks") or {}).get("SessionStart") or []:
    grp = dict(grp)
    grp["hooks"] = [h for h in (grp.get("hooks") or [])
                    if dest not in (h.get("command") or "")]
    if grp["hooks"]:
        groups.append(grp)
if data.get("hooks") is not None:
    if groups:
        data["hooks"]["SessionStart"] = groups
    else:
        data["hooks"].pop("SessionStart", None)
tmp = path + ".tmp"
with open(tmp, "w") as fh:
    json.dump(data, fh, indent=2, ensure_ascii=False)
    fh.write("\n")
os.replace(tmp, path)
print("settings.json nettoye")
PYEOF

# Copies de comptes dans le trousseau : elles contiennent de vrais jetons, il
# n'est pas question de les laisser derriere. L'entree de Claude Code lui-meme
# (« Claude Code-credentials ») n'est pas touchee : ta session reste connectee.
if [ -f "$DEST/accounts.json" ]; then
  "$PY" - "$DEST" <<'PYEOF'
import json, os, subprocess, sys
try:
    with open(os.path.join(sys.argv[1], "accounts.json")) as fh:
        items = (json.load(fh) or {}).get("accounts") or []
except (OSError, ValueError):
    items = []
gone = 0
for acc in items:
    svc = acc.get("service") or ""
    if not svc.startswith("Doublure-"):
        continue
    if subprocess.run(["security", "delete-generic-password", "-s", svc],
                      capture_output=True).returncode == 0:
        gone += 1
if gone:
    print(f"{gone} compte(s) retire(s) du trousseau")
PYEOF
fi

rm -rf "$DEST"
echo "desinstalle — relance tes sessions Claude Code."
