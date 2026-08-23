#!/usr/bin/env bash
# Retire le repli : LaunchAgent, hook, cles env, et le dossier d'etat.
set -uo pipefail
DEST="$HOME/.claude-fallback"
LABEL="com.claude-fallback.router"
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

rm -rf "$DEST"
echo "desinstalle — relance tes sessions Claude Code."
