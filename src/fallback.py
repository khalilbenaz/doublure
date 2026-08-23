#!/usr/bin/env python3
"""Repli de Claude Code sur des modeles gratuits quand le quota est atteint.

Claude Code n'a pas de plan B : quand la fenetre de quota est pleine, la
session s'arrete net jusqu'a la reprise. Ce module en donne un — trois
passerelles qui servent des modeles gratuits — et le rend automatique.

Le montage tient en deux pieces :

  router.py   un routeur local (127.0.0.1:8099) sur lequel `settings.json`
              pointe en permanence. C'est lui qui decide, *a chaque requete*,
              d'aller chez Anthropic ou chez une passerelle gratuite.
  ce module   l'etat, le catalogue des modeles et les bascules manuelles.

Pourquoi un routeur plutot que reecrire `settings.json` : Claude Code ne relit
ce fichier qu'au demarrage de session. Y ecrire le repli ne l'aurait applique
qu'a la session suivante — inutile au moment ou l'on en a besoin. Le mode vit
donc dans l'etat, relu a chaud : une session ouverte depuis des heures bascule
sans redemarrer.

Le nom de modele est TOUJOURS reecrit vers un modele gratuit de la passerelle.
Laisse tel quel, « claude-opus-5 » serait servi par OpenRouter depuis le vrai
Anthropic et facture au credit : le repli doit rester gratuit.
"""

import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

HOME = os.path.expanduser("~")
SETTINGS = os.path.join(HOME, ".claude", "settings.json")
# Tout ce que l'outil ecrit tient dans un seul dossier : le desinstaller, c'est
# le supprimer, sans avoir a deviner ce qui a ete seme ailleurs.
DBL_DIR = os.path.join(HOME, ".doublure")
STATE_FILE = os.path.join(DBL_DIR, "state.json")
ENV_FILE = os.path.join(DBL_DIR, ".env")
# Emplacements des versions precedentes : lus, jamais ecrits, pour qu'une
# installation existante ne perde ni sa cle ni son mode courant.
LEGACY_ENV = (os.path.join(HOME, ".fcc", ".env"),)
LEGACY_STATE = (os.path.join(HOME, ".claude-swap-backup", "fallback.json"),)
OR_BASE = "https://openrouter.ai/api/v1"
# Conserve : le dashboard s'en sert encore pour nommer l'amont du repli.
FCC_BASE = OR_BASE
# Le routeur : settings.json pointe dessus en permanence, et c'est lui qui
# choisit l'amont a chaque requete. C'est ce qui rend la bascule immediate
# pour une session Claude Code deja ouverte — settings.json n'etant relu
# qu'au demarrage, l'y ecrire ne suffisait pas.
# Le port n'a de raison de changer qu'en test, ou une seconde installation
# ne doit pas se battre avec celle de la machine pour le meme port.
ROUTER_PORT = os.environ.get("DOUBLURE_PORT", "8099")
ROUTER_BASE = f"http://127.0.0.1:{ROUTER_PORT}"
ROUTER_LABEL = "com.doublure.router"
ROUTER_PY = os.path.join(DBL_DIR, "router.py")
HOOK_SH = os.path.join(DBL_DIR, "hook.sh")

# Le repli reste arme ce temps-la avant de retenter Anthropic, quand l'amont
# n'a pas dit lui-meme quand le quota repart.
RETRY_NATIVE_AFTER = 30 * 60


# --------------------------------------------------------------------------
# Etat persistant
# --------------------------------------------------------------------------

DEFAULT_STATE = {
    "auto": True,        # le repli automatique est-il arme
    "mode": "native",    # native | zen | kilo | or
    "since": 0,
    "reason": "",
    "lastError": "",
    # Epoque a partir de laquelle le routeur retente Anthropic. Pose par le
    # repli automatique, d'apres l'echeance annoncee par l'amont quand il en
    # annonce une : sans elle on resterait sur les modeles gratuits bien apres
    # le retour du quota.
    "retryNativeAt": 0,
}


def state():
    for path in (STATE_FILE,) + LEGACY_STATE:
        try:
            with open(path) as fh:
                return {**DEFAULT_STATE, **json.load(fh)}
        except (OSError, ValueError):
            continue
    return dict(DEFAULT_STATE)


def set_state(**kw):
    cur = state()
    cur.update(kw)
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(cur, fh, indent=1)
    os.replace(tmp, STATE_FILE)
    return cur


# --------------------------------------------------------------------------
# settings.json : le seul endroit qui decide vers quoi pointe Claude Code
# --------------------------------------------------------------------------

def read_settings():
    try:
        with open(SETTINGS) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def write_settings(data):
    """Ecriture atomique — settings.json est lu a chaque demarrage de session."""
    tmp = SETTINGS + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, SETTINGS)


def proxy_env():
    """L'env pose dans settings.json — il ne change plus jamais.

    Il designe le routeur, pas un amont : c'est l'etat (mode) qui decide ou
    part la requete. Une session ouverte suit donc la bascule sans redemarrer.
    """
    return {
        "ANTHROPIC_BASE_URL": ROUTER_BASE,
        # Le routeur remplace cette valeur par le vrai jeton du compte actif ;
        # elle n'est la que parce que Claude Code exige un identifiant.
        "ANTHROPIC_AUTH_TOKEN": "doublure",
        # Le routeur est en loopback : jamais via un proxy HTTP d'entreprise.
        "NO_PROXY": "127.0.0.1,localhost,::1",
        "no_proxy": "127.0.0.1,localhost,::1",
    }


def router_alive(timeout=2):
    try:
        with urllib.request.urlopen(f"{ROUTER_BASE}/__router", timeout=timeout) as r:
            return json.loads(r.read().decode()).get("mode") is not None
    except Exception:
        return False


def start_router(wait=10):
    """Demarre le routeur, puis attend qu'il reponde.

    Le LaunchAgent est la voie normale : il le relance au login et apres un
    crash. Mais l'agent peut ne pas etre charge — premiere installation,
    session SSH sans domaine graphique. Plutot que d'echouer la, on lance le
    routeur directement : sans lui, Claude Code n'a plus d'amont du tout.
    """
    if router_alive():
        return True, "routeur deja en ligne"
    subprocess.run(["launchctl", "kickstart", "-k",
                    f"gui/{os.getuid()}/{ROUTER_LABEL}"],
                   capture_output=True, text=True)
    for _ in range(3):
        if router_alive():
            return True, "routeur demarre"
        time.sleep(1)
    if os.path.exists(ROUTER_PY):
        os.makedirs(DBL_DIR, exist_ok=True)
        log = open(os.path.join(DBL_DIR, "router.log"), "a")
        subprocess.Popen([sys.executable, ROUTER_PY],
                         stdout=log, stderr=subprocess.STDOUT,
                         start_new_session=True)
        for _ in range(wait):
            if router_alive():
                return True, "routeur demarre (hors LaunchAgent)"
            time.sleep(1)
    return False, "routeur injoignable apres demarrage"


def ensure_router_env():
    """Garantit que settings.json designe le routeur. Idempotent."""
    data = read_settings()
    env = dict(data.get("env") or {})
    changed = False
    for key, value in proxy_env().items():
        if env.get(key) != value:
            env[key] = value
            changed = True
    # Trace d'un montage ou la passerelle etait designee en direct : elle
    # ferait decouvrir a Claude Code les modeles de l'amont au lieu des notres.
    if env.pop("CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY", None):
        changed = True
    if changed:
        data["env"] = env
        write_settings(data)
    return changed


def ensure_hook(cmd=None):
    """Inscrit le hook SessionStart qui verifie l'installation a chaque lancement.

    On ajoute un groupe au lieu de reecrire la liste : d'autres hooks
    SessionStart peuvent deja s'y trouver, et les ecraser serait casser un
    montage qu'on n'a pas pose.
    """
    cmd = cmd or HOOK_SH
    data = read_settings()
    hooks = dict(data.get("hooks") or {})
    groups = list(hooks.get("SessionStart") or [])
    for grp in groups:
        for h in (grp.get("hooks") or []):
            if h.get("command") == cmd:
                return False
    groups.append({"hooks": [{"type": "command", "command": cmd}]})
    hooks["SessionStart"] = groups
    data["hooks"] = hooks
    write_settings(data)
    return True


def mode():
    """Le mode est dans l'etat : c'est ce fichier que le routeur relit.

    Avant, il se lisait dans settings.json — donc il ne prenait effet qu'a la
    session suivante. Le routeur rend l'etat effectif tout de suite.
    """
    cur = state().get("mode")
    if cur in PROVIDERS:
        return cur
    # « fcc » est l'ancien nom du repli, quand il n'y avait qu'OpenRouter.
    return "or" if cur == "fcc" else "native"


# --------------------------------------------------------------------------
# Fournisseurs de repli
# --------------------------------------------------------------------------
# Trois passerelles servent des modeles gratuits. Elles ne se valent pas :
#
#   zen  — opencode Zen, sans cle, sans plafond journalier constate ;
#   kilo — passerelle Kilo, sans cle non plus, catalogue plus large ;
#   or   — OpenRouter, seul a parler l'API Anthropic nativement, mais limite
#          a 50 requetes/jour sous 10 credits achetes (1000 au-dela).
#
# D'ou l'ordre par defaut : les deux sans plafond d'abord, OpenRouter en
# dernier recours puisque son quota s'epuise en une session de travail.
#
# Les tables de modeles doivent rester alignees sur celles du routeur : c'est
# lui qui reecrit reellement le nom, ceci n'est que l'affichage et la sonde.

OR_MODELS = {
    "opus":   "nvidia/nemotron-3-ultra-550b-a55b:free",
    "sonnet": "nvidia/nemotron-3-ultra-550b-a55b:free",
    "fable":  "nvidia/nemotron-3.5-lightning:free",
    "haiku":  "nvidia/nemotron-3-nano-30b-a3b:free",
}

ZEN_MODELS = {
    "opus":   "nemotron-3-ultra-free",
    "sonnet": "nemotron-3-ultra-free",
    "fable":  "nemotron-3.5-lightning-free",
    "haiku":  "nemotron-3.5-lightning-free",
}

KILO_MODELS = {
    "opus":   "nvidia/nemotron-3-ultra-550b-a55b:free",
    "sonnet": "nvidia/nemotron-3-ultra-550b-a55b:free",
    "fable":  "nvidia/nemotron-3-super-120b-a12b:free",
    "haiku":  "nvidia/nemotron-3.5-lightning:free",
}

PROVIDERS = {
    "zen": {
        "label": "opencode Zen",
        "base": "https://opencode.ai/zen/v1",
        "models": ZEN_MODELS,
        "key": False,
    },
    "kilo": {
        "label": "Kilo",
        "base": "https://api.kilo.ai/api/gateway/v1",
        "models": KILO_MODELS,
        "key": False,
    },
    "or": {
        "label": "OpenRouter",
        "base": OR_BASE,
        "models": OR_MODELS,
        "key": True,
    },
}

CHAIN = ("zen", "kilo", "or")

# Catalogue des modeles gratuits : liste vivante, relue chez la passerelle.
#
# Figer une liste ici la condamne a vieillir — les passerelles ajoutent et
# retirent des modeles gratuits en permanence. On interroge donc /models et
# l'on ne garde que ce qui est gratuit, avec un cache disque pour ne pas
# payer un aller-retour reseau a chaque affichage du dashboard.

FREE_CACHE = os.path.join(DBL_DIR, "free-models.json")
FREE_TTL = 6 * 3600

# Repli si la passerelle ne repond pas : les modeles valides a la main.
FALLBACK_CATALOG = {
    "zen": [("nemotron-3-ultra-free", "Nemotron 3 Ultra", ""),
            ("nemotron-3.5-lightning-free", "Nemotron 3.5 Lightning", "")],
    "kilo": [("nvidia/nemotron-3-ultra-550b-a55b:free", "Nemotron 3 Ultra", ""),
             ("nvidia/nemotron-3-super-120b-a12b:free", "Nemotron 3 Super", ""),
             ("nvidia/nemotron-3.5-lightning:free", "Nemotron 3.5 Lightning", "")],
    "or": [("nvidia/nemotron-3-ultra-550b-a55b:free", "Nemotron 3 Ultra", ""),
           ("nvidia/nemotron-3.5-lightning:free", "Nemotron 3.5 Lightning", "")],
}

ALIASES = ("opus", "sonnet", "fable", "haiku")

_FREE_ID = re.compile(r"[-:/]free$", re.I)


def _pretty(mid, name):
    """« NVIDIA: Nemotron 3 Ultra (free) » -> « Nemotron 3 Ultra ».

    Le prefixe editeur et le suffixe « (free) » sont deja portes par la carte
    du fournisseur : les repeter sur chaque entree rend la liste illisible.
    """
    lab = (name or "").strip()
    if not lab:
        lab = mid.split("/", 1)[-1].rsplit(":", 1)[0]
        lab = _FREE_ID.sub("", lab).replace("-", " ").replace("_", " ")
        lab = re.sub(r"\bv(\d)", r"v\1", lab).strip()
        return lab[:1].upper() + lab[1:] if lab else mid
    lab = re.sub(r"\s*\(free\)\s*$", "", lab, flags=re.I)
    lab = lab.split(":", 1)[-1].strip() if ":" in lab else lab
    return lab or mid


def _fetch_free(pid):
    """Modeles gratuits declares par la passerelle. [] si elle ne repond pas."""
    cfg = PROVIDERS[pid]
    headers = {"User-Agent": UA}
    if cfg["key"]:
        k = or_key()
        if not k:
            return []
        headers["Authorization"] = f"Bearer {k}"
    try:
        req = urllib.request.Request(cfg["base"] + "/models", headers=headers)
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read().decode()).get("data") or []
    except Exception:
        return []

    out = []
    for m in data:
        mid = str(m.get("id") or "")
        if not mid:
            continue
        # Deux signaux : l'identifiant suffixe « :free » / « -free », et le
        # drapeau isFree quand la passerelle le publie (Kilo). Un modele sans
        # l'un des deux peut etre facture : on ne le propose pas.
        if not (_FREE_ID.search(mid) or m.get("isFree") is True):
            continue
        ctx = m.get("context_length") or (m.get("top_provider") or {}).get("context_length")
        hint = f"contexte {int(ctx) // 1000}k" if isinstance(ctx, (int, float)) and ctx else ""
        out.append((mid, _pretty(mid, m.get("name")), hint))
    out.sort(key=lambda e: e[1].lower())
    return out


def _cache_read():
    try:
        with open(FREE_CACHE) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def catalog(pid, refresh=False):
    """Modeles gratuits proposables pour `pid` : (id, nom, indication).

    Le cache disque evite d'interroger la passerelle a chaque rendu ; passer
    refresh=True force la relecture (bouton « Vérifier » du dashboard).
    """
    if pid not in PROVIDERS:
        return []
    cache = _cache_read()
    entry = cache.get(pid) or {}
    fresh = (not refresh
             and entry.get("at", 0) + FREE_TTL > time.time()
             and entry.get("models"))
    if fresh:
        return [tuple(e) for e in entry["models"]]

    live = _fetch_free(pid)
    if live:
        cache[pid] = {"at": int(time.time()), "models": [list(e) for e in live]}
        try:
            os.makedirs(os.path.dirname(FREE_CACHE), exist_ok=True)
            tmp = FREE_CACHE + ".tmp"
            with open(tmp, "w") as fh:
                json.dump(cache, fh, indent=1)
            os.replace(tmp, FREE_CACHE)
        except OSError:
            pass
        return live
    # La passerelle ne repond pas : on garde le dernier bon cache, sinon les
    # modeles valides a la main — le dashboard ne doit jamais afficher zero
    # choix juste parce qu'un /models a expire.
    if entry.get("models"):
        return [tuple(e) for e in entry["models"]]
    return FALLBACK_CATALOG.get(pid, [])


def refresh_catalog():
    """Relit la liste des modeles gratuits chez chaque passerelle."""
    counts = {pid: len(catalog(pid, refresh=True)) for pid in PROVIDERS}
    return counts


def models_for(pid):
    """Table alias -> modele du fournisseur, surcharges du dashboard appliquees.

    Le routeur relit la meme surcharge a chaud : changer un modele prend effet
    au message suivant, sans redemarrer ni rouvrir de session.
    """
    table = dict(PROVIDERS[pid]["models"])
    over = (state().get("models") or {}).get(pid)
    if isinstance(over, dict):
        allowed = {ref for ref, *_ in catalog(pid)}
        for alias, ref in over.items():
            if alias in table and ref in allowed:
                table[alias] = ref
    return table


def set_model(pid, alias, ref):
    """Fixe le modele servi par `pid` pour `alias`. Renvoie (ok, detail)."""
    if pid not in PROVIDERS:
        return False, f"fournisseur inconnu : {pid}"
    if alias not in ALIASES:
        return False, f"role inconnu : {alias}"
    names = {r: lab for r, lab, *_ in catalog(pid)}
    if ref not in names:
        return False, "modele hors catalogue"
    over = dict(state().get("models") or {})
    tbl = dict(over.get(pid) or {})
    tbl[alias] = ref
    over[pid] = tbl
    set_state(models=over)
    return True, f"{PROVIDERS[pid]['label']} — {alias} sert {names[ref]}"


def reset_models(pid):
    """Rend au fournisseur ses modeles par defaut."""
    over = dict(state().get("models") or {})
    if over.pop(pid, None) is None:
        return False, "aucune surcharge a annuler"
    set_state(models=over)
    return True, f"{PROVIDERS[pid]['label']} : modeles par defaut retablis"



# Cloudflare refuse « Python-urllib » devant Zen : sans agent explicite, la
# sonde revient en 403 sans rapport avec la disponibilite du service.
UA = "doublure/1.0"


def chain():
    """Ordre d'essai des fournisseurs, personnalisable via l'etat."""
    custom = state().get("chain")
    if isinstance(custom, list):
        keep = [p for p in custom if p in PROVIDERS]
        if keep:
            return keep + [p for p in CHAIN if p not in keep]
    return list(CHAIN)


def or_key():
    """Cle OpenRouter — environnement, puis fichier .env de l'installation."""
    key = os.environ.get("OPENROUTER_API_KEY")
    if key:
        return key
    for path in (ENV_FILE,) + LEGACY_ENV:
        try:
            with open(path) as fh:
                for line in fh:
                    if line.startswith("OPENROUTER_API_KEY="):
                        val = line.split("=", 1)[1].strip().strip("\"'")
                        if val:
                            return val
        except OSError:
            continue
    return None


def or_quota(timeout=15):
    """Plafond restant du palier gratuit : (restant, total, reset_epoch).

    OpenRouter n'expose x-ratelimit-* que sur un 429. Tant qu'il reste du
    quota, on ne sait donc pas combien : (None, None, 0) veut dire « pas
    encore epuise », pas « inconnu et inquietant ».
    """
    key = or_key()
    if not key:
        return None, None, 0
    payload = json.dumps({
        "model": OR_MODELS["haiku"],
        "max_tokens": 1,
        "messages": [{"role": "user", "content": "."}],
    }).encode()
    req = urllib.request.Request(
        f"{OR_BASE}/messages", data=payload, method="POST",
        headers={"Authorization": f"Bearer {key}",
                 "Content-Type": "application/json",
                 "anthropic-version": "2023-06-01"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            r.read()
            return None, None, 0          # sert encore : quota non atteint
    except urllib.error.HTTPError as e:
        if e.code != 429:
            return None, None, 0
        h = e.headers
        def num(name):
            try:
                return int(h.get(name))
            except (TypeError, ValueError):
                return None
        reset = num("x-ratelimit-reset") or 0
        return num("x-ratelimit-remaining"), num("x-ratelimit-limit"), reset // 1000
    except Exception:
        return None, None, 0


def check(pid, timeout=25):
    """(ok, detail) — ce fournisseur peut-il servir une requete maintenant ?

    Une vraie generation, pas un ping : c'est le seul moyen de distinguer
    « joignable » de « repond effectivement », et c'est ce qui manquait au
    repli precedent, qui basculait puis echouait en vol.
    """
    cfg = PROVIDERS[pid]
    if pid == "or":
        key = or_key()
        if not key:
            return False, "OPENROUTER_API_KEY absente de ~/.doublure/.env"
        req = urllib.request.Request(
            f"{OR_BASE}/key", headers={"Authorization": f"Bearer {key}",
                                       "User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = json.loads(r.read().decode()).get("data") or {}
        except urllib.error.HTTPError as e:
            return False, f"OpenRouter refuse la cle (HTTP {e.code})"
        except Exception as e:
            return False, f"OpenRouter injoignable ({type(e).__name__})"
        remaining, total, reset = or_quota()
        if remaining == 0:
            when = time.strftime("%H:%M", time.localtime(reset)) if reset else "?"
            return False, f"quota gratuit epuise ({total}/jour) — retour a {when}"
        tier = "gratuit" if data.get("is_free_tier") else "paye"
        return True, f"OpenRouter joignable (palier {tier})"

    payload = json.dumps({
        "model": models_for(pid)["haiku"],
        "max_tokens": 800,
        "messages": [{"role": "user", "content": "dis OK"}],
    }).encode()
    req = urllib.request.Request(
        f"{cfg['base']}/chat/completions", data=payload, method="POST",
        headers={"Content-Type": "application/json", "User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        if e.code == 429:
            return False, f"{cfg['label']} : plafond atteint (HTTP 429)"
        return False, f"{cfg['label']} refuse la requete (HTTP {e.code})"
    except Exception as e:
        return False, f"{cfg['label']} injoignable ({type(e).__name__})"
    return True, f"{cfg['label']} joignable (sans cle)"


def pick(timeout=25):
    """Premier fournisseur de la chaine qui repond : (id, detail) ou (None, why)."""
    notes = []
    for pid in chain():
        ok, detail = check(pid, timeout)
        if ok:
            return pid, detail
        notes.append(detail)
    return None, " · ".join(notes) or "aucun fournisseur de repli"


# Conserves pour le dashboard, qui les appelle encore.
def reachable(timeout=10):
    cur = mode()
    return check(cur if cur in PROVIDERS else chain()[0], timeout)


def server_alive():
    return reachable()[0]


def models_ok():
    """Le repli est exploitable si au moins un fournisseur repond."""
    pid, detail = pick()
    return bool(pid), detail


def ensure_models(force_probe=False):
    """Plus de sonde de qualification a lancer : les modeles sont figes et
    deja valides sur les cinq usages reels de Claude Code. force_probe est
    conserve pour le dashboard, qui propose « re-verifier »."""
    return models_ok()


# --------------------------------------------------------------------------
# Bascule
# --------------------------------------------------------------------------

def to_fcc(reason="", force_probe=False, provider=None):
    """Bascule le routeur sur un fournisseur gratuit. Le nom reste `to_fcc` :
    le dashboard et le surveillant l'appellent ainsi.

    Sans `provider`, on prend le premier de la chaine qui repond vraiment.
    """
    if provider:
        if provider not in PROVIDERS:
            return False, f"fournisseur inconnu : {provider}"
        ok, detail = check(provider)
        pid = provider if ok else None
    else:
        pid, detail = pick()

    if not pid:
        set_state(lastError=f"repli: {detail}")
        return False, f"repli indisponible — {detail}"

    up, note = start_router()
    if not up:
        set_state(lastError=f"routeur: {note}")
        return False, f"routeur indisponible — {note}"
    ensure_router_env()
    # Le mode vit dans l'etat : le routeur le relit a la requete suivante,
    # donc les sessions deja ouvertes basculent sans redemarrer.
    set_state(mode=pid, since=int(time.time()), reason=reason, lastError="")
    return True, detail


def to_native(reason=""):
    """Retour aux comptes Claude — l'env reste sur le routeur, seul l'etat bouge."""
    up, note = start_router()
    if not up:
        # Sans routeur en ligne, laisser settings.json pointer dessus
        # couperait toute sortie : on retire l'env plutot que de bloquer.
        data = read_settings()
        env = dict(data.get("env") or {})
        for key in list(proxy_env()):
            env.pop(key, None)
        if env:
            data["env"] = env
        else:
            data.pop("env", None)
        write_settings(data)
        set_state(mode="native", since=int(time.time()), reason=reason,
                  lastError=f"routeur: {note}")
        return True, f"comptes Claude natifs (routeur absent — {note})"
    ensure_router_env()
    set_state(mode="native", since=int(time.time()), reason=reason,
              lastError="")
    return True, "comptes Claude natifs"

# ---------------------------------------------------------------------------
# Etat
# ---------------------------------------------------------------------------


def summary():
    """Etat complet pour l'affichage du dashboard."""
    st = state()
    cur = mode()
    order = chain()

    # Le fournisseur actif est sonde en premier : c'est celui dont l'etat
    # compte. Les autres suivent, pour que le dashboard montre le recours.
    probe = [cur] if cur in PROVIDERS else []
    probe += [p for p in order if p not in probe]

    providers, first_ok = [], None
    for pid in probe:
        ok, detail = check(pid)
        if ok and first_ok is None:
            first_ok = pid
        providers.append({
            "id": pid,
            "label": PROVIDERS[pid]["label"],
            "base": PROVIDERS[pid]["base"],
            "needsKey": PROVIDERS[pid]["key"],
            "ok": ok,
            "detail": detail,
            "active": pid == cur,
            "models": models_for(pid),
            "catalog": list(catalog(pid)),
            "custom": bool((state().get("models") or {}).get(pid)),
        })
    providers.sort(key=lambda p: order.index(p["id"]))

    active_id = cur if cur in PROVIDERS else order[0]
    active = PROVIDERS[active_id]
    active_models = models_for(active_id)
    remaining, total, reset = or_quota() if cur == "or" else (None, None, 0)
    refs = sorted(set(active_models.values()))
    alive = next((p["ok"] for p in providers if p["id"] == (cur if cur in PROVIDERS else order[0])), False)
    detail = next((p["detail"] for p in providers if p["id"] == (cur if cur in PROVIDERS else order[0])), "")

    return {
        "mode": cur,
        "provider": cur if cur in PROVIDERS else "",
        "providers": providers,
        "chain": order,
        "auto": bool(st.get("auto")),
        "since": st.get("since") or 0,
        "reason": st.get("reason") or "",
        "lastError": st.get("lastError") or "",
        "alive": alive,
        "aliveDetail": detail,
        "upstream": active["base"],
        # Plafond du palier gratuit, propre a OpenRouter. remaining vaut None
        # tant qu'il en reste : le quota n'est chiffre que sur un 429.
        "quota": {"remaining": remaining, "total": total, "reset": reset},
        "checkedAt": int(time.time()),
        "probed": len(refs),
        "healthy": refs,
        "textOnly": [],
        "roles": {
            "MODEL": active_models["sonnet"],
            "MODEL_OPUS": active_models["opus"],
            "MODEL_SONNET": active_models["sonnet"],
            "MODEL_HAIKU": active_models["haiku"],
            "MODEL_FABLE": active_models["fable"],
        },
        "rolesOk": bool(first_ok),
        "rolesDetail": detail,
        "suggested": {},
        "models": {r: {"ok": True, "tools": True} for r in refs},
    }


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    arg = sys.argv[2] if len(sys.argv) > 2 else None

    if cmd == "on":
        print(to_fcc(reason="manuel", provider=arg)[1])
    elif cmd == "off":
        print(to_native(reason="manuel")[1])
    elif cmd == "auto":
        val = (arg or "on") == "on"
        set_state(auto=val)
        print(f"repli automatique {'arme' if val else 'desarme'}")
    elif cmd == "probe":
        print(ensure_models(force_probe=True)[1])
    elif cmd == "install":
        # Idempotent : rejoue a chaque demarrage de session par le hook.
        # Les deux doivent tourner : chainer par `or` ferait sauter le second
        # des que le premier a change quelque chose.
        env_done = ensure_router_env()
        changed = ensure_hook(arg) or env_done
        ok, note = start_router()
        print(f"settings.json {'mis a jour' if changed else 'deja bon'} — {note}")
        sys.exit(0 if ok else 1)
    elif cmd == "json":
        print(json.dumps(summary(), indent=1, ensure_ascii=False))
    elif cmd == "models":
        for pid in chain():
            mark = "*" if pid == mode() else " "
            print(f"{mark} {pid:5s} {PROVIDERS[pid]['label']}")
            for alias, ref in models_for(pid).items():
                print(f"      {alias:7s} {ref}")
    else:
        st = state()
        cur = mode()
        if cur == "native":
            print("mode    native — comptes Claude")
        else:
            print(f"mode    repli {cur} ({PROVIDERS[cur]['label']})")
            for alias, ref in models_for(cur).items():
                print(f"        {alias:7s} {ref}")
        print(f"auto    {'arme' if st.get('auto') else 'desarme'}")
        if st.get("reason"):
            print(f"raison  {st['reason']}")
        if st.get("lastError"):
            print(f"erreur  {st['lastError']}")
        nxt = st.get("retryNativeAt") or 0
        if nxt > time.time():
            print(f"natif   retente dans {int((nxt - time.time()) / 60)} min")
        print(f"routeur {'en ligne' if router_alive() else 'hors ligne'} "
              f"({ROUTER_BASE})")


if __name__ == "__main__":
    main()
