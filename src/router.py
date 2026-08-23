#!/usr/bin/env python3
"""Routeur d'API pour Claude Code — le repli, sans redemarrer le terminal.

Le probleme : Claude Code lit `~/.claude/settings.json` au demarrage de session
seulement. Y ecrire le repli ne l'appliquerait qu'a la session suivante —
inutile au moment exact ou le quota tombe.

La solution : `settings.json` pointe une fois pour toutes sur ce routeur, qui
decide *a chaque requete* ou l'envoyer :

  mode natif    -> https://api.anthropic.com, avec le jeton OAuth du compte
                   actif (lu dans le trousseau macOS de Claude Code)
  mode zen/kilo -> passerelles OpenAI-compatibles, traduites par `bridge.py`
  mode or       -> https://openrouter.ai/api/v1, seul amont a parler l'API
                   Anthropic nativement (/v1/messages, SSE, tool_use)

Plusieurs comptes Claude peuvent etre enregistres : sur un 429, le routeur
essaie d'abord le compte suivant qui n'est pas en repos, et ce n'est qu'une
fois tous les comptes epuises qu'il passe aux passerelles gratuites. Le compte
utilise est choisi requete par requete, sans toucher a la session en cours.

Le repli se prend tout seul : un 429 d'Anthropic est intercepte *avant* qu'un
octet ne soit parti vers le client, l'etat bascule, et la meme requete repart
par le compte suivant ou par la passerelle gratuite. L'utilisateur ne perd pas son message et n'a rien
a relancer. Un chien de garde revient au natif quand la fenetre de quota
annoncee par l'amont est ecoulee.

Le nom de modele est toujours reecrit : laisse tel quel, « claude-opus-5 »
serait servi par OpenRouter depuis le vrai Anthropic et facture au credit.

Ecoute sur 127.0.0.1 uniquement. Les corps sont relayes en flux (SSE compris),
jamais bufferises : le streaming de Claude Code reste du streaming.
"""

import glob
import http.client
import http.server
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

HOST = "127.0.0.1"
PORT = int(os.environ.get("DOUBLURE_PORT", "8099"))

HOME = os.path.expanduser("~")
# Le pont vit a cote de ce fichier, ou qu'on l'ait installe.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bridge  # noqa: E402  (le chemin doit etre pose avant l'import)

DBL_DIR = os.path.join(HOME, ".doublure")
STATE = os.path.join(DBL_DIR, "state.json")
# Etats des versions precedentes : lus en secours, jamais ecrits.
LEGACY_STATE = (os.path.join(HOME, ".claude", "fcc-fallback.json"),
                os.path.join(HOME, ".claude-swap-backup", "fallback.json"))

UPSTREAM_NATIVE = ("api.anthropic.com", 443, True)
UPSTREAM_OR = ("openrouter.ai", 443, True)

# OpenRouter sert l'API Anthropic sous /api/v1 ; Claude Code appelle /v1.
OR_PREFIX = "/api"
ENV_FILE = os.path.join(DBL_DIR, ".env")
LEGACY_ENV = (os.path.join(HOME, ".fcc", ".env"),)

# Reecriture obligatoire du nom de modele : laisse tel quel, « claude-opus-5 »
# est route par OpenRouter vers le VRAI Anthropic et facture au credit. Le
# repli doit rester gratuit, donc chaque alias Claude Code est traduit vers un
# modele « :free ». Sonde du 2026-08-22 : ces trois-la passent streaming,
# tool_use, aller-retour tool_result et un contexte de 28k.
OR_MODELS = {
    "opus":   "nvidia/nemotron-3-ultra-550b-a55b:free",
    "sonnet": "nvidia/nemotron-3-ultra-550b-a55b:free",
    "fable":  "nvidia/nemotron-3.5-lightning:free",
    "haiku":  "nvidia/nemotron-3-nano-30b-a3b:free",
}
OR_DEFAULT = OR_MODELS["sonnet"]

# --------------------------------------------------------------------------
# Passerelles gratuites parlant OpenAI : opencode Zen et Kilo
# --------------------------------------------------------------------------
# OpenRouter sert l'API Anthropic telle quelle, ces deux-la non : elles n'ont
# que /chat/completions. Le module oai_bridge traduit requete, reponse et flux
# SSE, de sorte que la session Claude Code ne voie aucune difference.
#
# Ni l'une ni l'autre ne demande de cle : le repli reste disponible meme quand
# le quota OpenRouter (50 req/jour sous 10 credits) est epuise.
#
# Modeles retenus le 2026-08-22 apres sonde sur les cinq usages reels de
# Claude Code — texte, streaming, tool_use, aller-retour tool_result,
# contexte de 30k. Seuls les 5/5 figurent ici.
BRIDGES = {
    "zen": {
        "label": "opencode Zen",
        "host": "opencode.ai", "port": 443, "tls": True,
        "path": "/zen/v1/chat/completions",
        "models": {
            "opus":   "nemotron-3-ultra-free",
            "sonnet": "nemotron-3-ultra-free",
            "fable":  "nemotron-3.5-lightning-free",
            "haiku":  "nemotron-3.5-lightning-free",
        },
    },
    "kilo": {
        "label": "Kilo",
        "host": "api.kilo.ai", "port": 443, "tls": True,
        "path": "/api/gateway/v1/chat/completions",
        "models": {
            "opus":   "nvidia/nemotron-3-ultra-550b-a55b:free",
            "sonnet": "nvidia/nemotron-3-ultra-550b-a55b:free",
            "fable":  "nvidia/nemotron-3-super-120b-a12b:free",
            "haiku":  "nvidia/nemotron-3.5-lightning:free",
        },
    },
}

MODES = ("native", "or", "zen", "kilo")
# Ordre d'essai des replis quand le natif tombe sur un 429. Les deux premiers
# ne demandent aucune cle : ils marchent pour n'importe qui, tout de suite.
DEFAULT_CHAIN = ("zen", "kilo", "or")
# Delai de repli quand l'amont n'annonce pas lui-meme son echeance.
RETRY_NATIVE_DEFAULT = 30 * 60

# Cloudflare refuse « Python-urllib » en amont : un agent explicite evite un
# 403 qui n'a rien a voir avec la requete elle-meme.
BRIDGE_UA = "doublure/1.0"

KEYCHAIN_SERVICE = "Claude Code-credentials"
# Comptes Claude supplementaires. Le fichier ne contient que des metadonnees
# (nom, repos en cours) : chaque jeton reste dans le trousseau, sous son propre
# service « Doublure-<nom> ». Rien de secret n'atterrit sur le disque.
ACCOUNTS_FILE = os.path.join(DBL_DIR, "accounts.json")
KEYCHAIN_PREFIX = "Doublure-"
# Repos par defaut d'un compte qui vient de rendre un 429 sans dire quand
# revenir. Plus court que RETRY_NATIVE_DEFAULT : reessayer un compte ne coute
# qu'une requete, alors que rester en repli gratuit coute en qualite.
ACCOUNT_COOLDOWN_DEFAULT = 15 * 60
# L'identifiant client OAuth de Claude Code n'est PAS ecrit en dur ici :
# c'est celui de l'installation de l'utilisateur, retrouve sur sa machine
# par client_id(). Le distribuer serait partager le notre.
CLIENT_ID_FILE = os.path.join(DBL_DIR, "client-id")
OAUTH_TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
# On rafraichit avant l'echeance : une requete ne doit jamais partir avec un
# jeton qui expire pendant son vol.
REFRESH_MARGIN = 300

# En-tetes qui decrivent *ce lien-ci* et non le message : ne jamais relayer.
HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "transfer-encoding", "upgrade", "host", "content-length",
}

STATE_TTL = 1.0          # le mode est relu au plus une fois par seconde
_state_cache = {"at": 0.0, "mode": "native"}
_state_lock = threading.Lock()

# Indexe par service de trousseau : deux comptes ne partagent pas un cache.
_token_cache = {}
_token_lock = threading.Lock()
_cid_cache = {"id": ""}
_cid_lock = threading.Lock()


def log(msg):
    sys.stderr.write(f"{time.strftime('%H:%M:%S')} {msg}\n")
    sys.stderr.flush()


# --------------------------------------------------------------------------
# Mode courant : ce que l'etat memorise, relu a chaud
# --------------------------------------------------------------------------

def current_mode():
    now = time.time()
    with _state_lock:
        if now - _state_cache["at"] < STATE_TTL:
            return _state_cache["mode"]
    mode = "native"
    for path in (STATE,) + LEGACY_STATE:
        try:
            with open(path) as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and data.get("mode") in MODES + ("fcc",):
            # « fcc » est l'ancien nom du repli : il vaut « or » desormais.
            mode = "or" if data["mode"] == "fcc" else data["mode"]
            break
    with _state_lock:
        _state_cache.update(at=now, mode=mode)
    return mode


def read_state():
    for path in (STATE,) + LEGACY_STATE:
        try:
            with open(path) as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                return data
        except (OSError, ValueError):
            continue
    return {}


def set_mode(mode, reason, retry_at=0):
    """Ecrit le mode dans l'etat, atomiquement, et purge le cache de lecture.

    Le routeur ecrit l'etat lui-meme au lieu d'appeler le CLI : la bascule a
    lieu au milieu d'une requete client qui attend, et lancer un sous-processus
    la ferait patienter le temps d'un demarrage de Python.
    """
    data = read_state()
    data.update(mode=mode, since=int(time.time()), reason=reason,
                retryNativeAt=int(retry_at))
    os.makedirs(DBL_DIR, exist_ok=True)
    tmp = STATE + ".tmp"
    try:
        with open(tmp, "w") as fh:
            json.dump(data, fh, indent=1, ensure_ascii=False)
            fh.write("\n")
        os.replace(tmp, STATE)
    except OSError as e:
        log(f"etat non ecrit ({type(e).__name__}) — bascule en memoire seule")
    with _state_lock:
        _state_cache.update(at=time.time(), mode=mode)


def chain():
    """Ordre des replis a essayer. Sans cle, OpenRouter est ecarte.

    Le tenter sans cle donnerait un 401 presente au client comme une panne du
    repli, alors que les deux autres passerelles auraient repondu.
    """
    order = read_state().get("chain") or list(DEFAULT_CHAIN)
    order = [p for p in order if p in MODES and p != "native"]
    if not or_key():
        order = [p for p in order if p != "or"]
    return order or ["zen"]


def retry_after(resp):
    """Quand retenter Anthropic, d'apres ce que l'amont a annonce."""
    hdr = resp.getheader("retry-after")
    if hdr:
        try:
            return time.time() + max(0, int(float(hdr)))
        except ValueError:
            pass
    for name in ("anthropic-ratelimit-unified-reset",
                 "anthropic-ratelimit-requests-reset",
                 "anthropic-ratelimit-tokens-reset"):
        raw = resp.getheader(name)
        if not raw:
            continue
        try:                      # epoque en secondes
            val = float(raw)
            return val if val > 1e9 else time.time() + val
        except ValueError:
            pass
    return time.time() + RETRY_NATIVE_DEFAULT


def watchdog():
    """Ramene au natif quand la fenetre de quota est censee etre repartie.

    Un repli choisi a la main n'est jamais defait ici : seul un repli
    automatique s'annule tout seul, sinon l'outil contredirait l'utilisateur.
    """
    while True:
        time.sleep(60)
        try:
            st = read_state()
            if st.get("mode", "native") == "native":
                continue
            if not str(st.get("reason", "")).startswith("auto"):
                continue
            due = st.get("retryNativeAt") or 0
            if due and time.time() >= due:
                set_mode("native", "auto: fenetre de quota supposee repartie")
                log("retour au natif (fenetre de quota ecoulee)")
        except Exception as e:                      # un thread de fond ne meurt pas
            log(f"watchdog: {type(e).__name__}")


# --------------------------------------------------------------------------
# Jeton OAuth du compte actif — trousseau macOS, rafraichi si besoin
# --------------------------------------------------------------------------

_CLIENT_ID_RE = re.compile(rb'CLIENT_ID\s*:\s*"([0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12})"')

# Emplacements ou npm, Homebrew, nvm, fnm et volta posent le paquet.
_CLI_GLOBS = (
    "/opt/homebrew/lib/node_modules/@anthropic-ai/claude-code/cli.js",
    "/usr/local/lib/node_modules/@anthropic-ai/claude-code/cli.js",
    HOME + "/.claude/local/node_modules/@anthropic-ai/claude-code/cli.js",
    HOME + "/.npm-global/lib/node_modules/@anthropic-ai/claude-code/cli.js",
    HOME + "/.nvm/versions/node/*/lib/node_modules/@anthropic-ai/claude-code/cli.js",
    HOME + "/.local/share/fnm/node-versions/*/installation/lib/node_modules/@anthropic-ai/claude-code/cli.js",
    HOME + "/.volta/tools/image/packages/@anthropic-ai/claude-code/lib/node_modules/@anthropic-ai/claude-code/cli.js",
)


def _cli_bundles():
    """Chemins plausibles du bundle cli.js, le plus fiable d'abord."""
    seen, out = set(), []

    def add(path):
        if path and path not in seen and os.path.isfile(path):
            seen.add(path)
            out.append(path)

    exe = shutil.which("claude")
    if exe:
        # Le binaire est un lien vers .../claude-code/bin/claude.exe ; cli.js
        # est a la racine du paquet, quelques niveaux plus haut. On remonte
        # plutot que de coder la profondeur, qui varie selon l'installeur.
        cur = os.path.dirname(os.path.realpath(exe))
        for _ in range(4):
            add(os.path.join(cur, "cli.js"))
            parent = os.path.dirname(cur)
            if parent == cur:
                break
            cur = parent
    for pattern in _CLI_GLOBS:
        # Ordre decroissant : la version de node la plus recente d'abord.
        for path in sorted(glob.glob(pattern), reverse=True):
            add(path)
    return out


def _scan_client_id():
    """Cherche CLIENT_ID dans le bundle de la CLI installee. "" si absent."""
    for path in _cli_bundles():
        try:
            with open(path, "rb") as fh:
                # Le bundle fait une dizaine de Mo : on le balaie par tranches,
                # avec un chevauchement pour ne pas couper le motif en deux.
                tail = b""
                while True:
                    chunk = fh.read(1 << 20)
                    if not chunk:
                        break
                    found = _CLIENT_ID_RE.search(tail + chunk)
                    if found:
                        return found.group(1).decode()
                    tail = chunk[-128:]
        except OSError:
            continue
    return ""


def client_id():
    """Identifiant client OAuth, pris sur l'installation locale de Claude Code.

    Rien n'est ecrit en dur : la valeur appartient a l'utilisateur, pas au
    depot. Elle est mise en cache dans ~/.doublure/client-id — relire
    onze mega-octets a chaque rafraichissement de jeton serait absurde.
    """
    forced = (os.environ.get("CLAUDE_OAUTH_CLIENT_ID") or "").strip()
    if forced:
        return forced
    with _cid_lock:
        if _cid_cache["id"]:
            return _cid_cache["id"]
    try:
        with open(CLIENT_ID_FILE) as fh:
            found = fh.read().strip()
    except OSError:
        found = ""
    if not found:
        found = _scan_client_id()
        if found:
            try:
                os.makedirs(DBL_DIR, exist_ok=True)
                tmp = CLIENT_ID_FILE + ".tmp"
                with open(tmp, "w") as fh:
                    fh.write(found + "\n")
                os.replace(tmp, CLIENT_ID_FILE)
            except OSError:
                pass
    with _cid_lock:
        _cid_cache["id"] = found
    return found


def read_keychain(service=KEYCHAIN_SERVICE):
    try:
        out = subprocess.run(
            ["security", "find-generic-password", "-s", service, "-w"],
            capture_output=True, text=True, timeout=10)
    except Exception:
        return None
    if out.returncode != 0:
        return None
    try:
        return json.loads(out.stdout)
    except ValueError:
        return None


def write_keychain(blob, service=KEYCHAIN_SERVICE):
    """Reecrit l'entree du trousseau (-U remplace celle qui existe)."""
    try:
        subprocess.run(
            ["security", "add-generic-password", "-U", "-s", service,
             "-a", os.environ.get("USER", "claude"), "-w", json.dumps(blob)],
            capture_output=True, text=True, timeout=10)
    except Exception as e:
        log(f"trousseau non reecrit : {type(e).__name__}")


# --------------------------------------------------------------------------
# Plusieurs comptes Claude — rotation avant de tomber sur le gratuit
# --------------------------------------------------------------------------
# Le compte de la session Claude Code est toujours la, sous le nom « claude » :
# c'est l'entree que Claude Code gere lui-meme, on ne fait que la lire. Les
# autres sont des copies posees par `dbl accounts add`, chacune dans son propre
# service de trousseau. Le routeur choisit le compte a chaque requete, donc
# changer de compte ne demande ni relogin ni redemarrage de session.

def read_accounts():
    """Metadonnees des comptes, dans l'ordre d'essai."""
    try:
        with open(ACCOUNTS_FILE) as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    out = []
    for item in data.get("accounts") or ():
        if isinstance(item, dict) and item.get("name"):
            out.append({"name": str(item["name"]),
                        "service": item.get("service")
                        or KEYCHAIN_PREFIX + str(item["name"]),
                        "cooldownUntil": float(item.get("cooldownUntil") or 0)})
    return out


def write_accounts(accounts):
    os.makedirs(DBL_DIR, exist_ok=True)
    tmp = ACCOUNTS_FILE + ".tmp"
    try:
        with open(tmp, "w") as fh:
            json.dump({"accounts": accounts}, fh, indent=1, ensure_ascii=False)
            fh.write("\n")
        os.replace(tmp, ACCOUNTS_FILE)
    except OSError as e:
        log(f"comptes non ecrits ({type(e).__name__})")


def all_accounts():
    """Le compte de la session, puis ceux enregistres — sans doublon.

    Le premier est toujours celui auquel Claude Code est connecte : c'est le
    comportement d'origine quand aucun compte n'a ete ajoute.
    """
    session = {"name": "claude", "service": KEYCHAIN_SERVICE,
               "cooldownUntil": 0}
    out = [session]
    seen = {KEYCHAIN_SERVICE}
    for acc in read_accounts():
        if acc["service"] in seen:
            # Le compte de la session a lui aussi un repos memorise : le
            # perdre ici le ferait reessayer en boucle sur son 429.
            if acc["service"] == KEYCHAIN_SERVICE:
                session["cooldownUntil"] = acc["cooldownUntil"]
            continue
        seen.add(acc["service"])
        out.append(acc)
    return out


def active_account():
    """Compte a utiliser maintenant : celui de l'etat, sinon le premier libre.

    Un compte marque en repos est saute. Si tous le sont, on rend quand meme
    le premier : c'est a l'appelant de decider du repli, pas a cette fonction
    de refuser une requete que le quota a peut-etre deja laisse repasser.
    """
    accounts = all_accounts()
    by_name = {a["name"]: a for a in accounts}
    want = read_state().get("account")
    now = time.time()
    if want and want in by_name and by_name[want]["cooldownUntil"] <= now:
        return by_name[want]
    for acc in accounts:
        if acc["cooldownUntil"] <= now:
            return acc
    return accounts[0]


def set_active_account(name):
    """Note dans l'etat le compte a utiliser. Relu a chaud comme le mode."""
    data = read_state()
    data["account"] = name
    os.makedirs(DBL_DIR, exist_ok=True)
    tmp = STATE + ".tmp"
    try:
        with open(tmp, "w") as fh:
            json.dump(data, fh, indent=1, ensure_ascii=False)
            fh.write("\n")
        os.replace(tmp, STATE)
    except OSError as e:
        log(f"etat non ecrit ({type(e).__name__})")


def rest_account(name, until):
    """Met un compte au repos jusqu'a `until`.

    Le compte de la session n'echappe pas a la regle : son repos est garde
    dans le meme fichier, meme s'il n'a pas d'entree propre au depart.
    """
    accounts = read_accounts()
    for acc in accounts:
        if acc["name"] == name:
            acc["cooldownUntil"] = float(until)
            break
    else:
        accounts.insert(0, {"name": name,
                            "service": KEYCHAIN_SERVICE if name == "claude"
                            else KEYCHAIN_PREFIX + name,
                            "cooldownUntil": float(until)})
    write_accounts(accounts)


def next_account(exclude, now=None):
    """Prochain compte utilisable, en sautant ceux au repos et `exclude`."""
    now = time.time() if now is None else now
    for acc in all_accounts():
        if acc["name"] in exclude:
            continue
        if acc["cooldownUntil"] > now:
            continue
        if not read_keychain(acc["service"]):
            # Entree disparue du trousseau (compte revoque, trousseau
            # nettoye) : on ne la propose pas, elle donnerait un 401.
            continue
        return acc
    return None


def refresh_token(refresh):
    cid = client_id()
    if not cid:
        # Sans identifiant on ne peut pas rafraichir. L'appelant retombe sur
        # le jeton en place, qui vaut encore quelques minutes.
        raise RuntimeError("identifiant client OAuth introuvable — "
                           "installe Claude Code, ou pose "
                           "CLAUDE_OAUTH_CLIENT_ID dans l'environnement")
    payload = json.dumps({
        "grant_type": "refresh_token",
        "refresh_token": refresh,
        "client_id": cid,
    }).encode()
    req = urllib.request.Request(
        OAUTH_TOKEN_URL, data=payload, method="POST",
        headers={"Content-Type": "application/json",
                 "User-Agent": "doublure/1.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode())


def access_token(service=KEYCHAIN_SERVICE):
    """Jeton d'un compte, rafraichi si son echeance approche.

    Le cache est indexe par service : deux comptes en rotation ne doivent pas
    se voler leur jeton.
    """
    now = time.time()
    with _token_lock:
        entry = _token_cache.get(service) or {}
        # Cache court : le compte connecte peut changer a tout moment, on
        # veut le voir vite, mais pas relire le trousseau a chaque requete.
        if entry.get("token") and now - entry.get("at", 0) < 5:
            return entry["token"]

    blob = read_keychain(service)
    if not blob:
        return None
    oauth = blob.get("claudeAiOauth") or {}
    token = oauth.get("accessToken")
    expires = (oauth.get("expiresAt") or 0) / 1000.0

    if token and expires and expires - now < REFRESH_MARGIN:
        refresh = oauth.get("refreshToken")
        if refresh:
            try:
                data = refresh_token(refresh)
            except Exception as e:
                log(f"rafraichissement impossible ({type(e).__name__}) — "
                    f"on tente le jeton en place")
            else:
                token = data.get("access_token") or token
                oauth["accessToken"] = token
                if data.get("refresh_token"):
                    oauth["refreshToken"] = data["refresh_token"]
                if data.get("expires_in"):
                    oauth["expiresAt"] = int((now + data["expires_in"]) * 1000)
                blob["claudeAiOauth"] = oauth
                write_keychain(blob, service)
                log(f"jeton OAuth rafraichi ({service})")

    with _token_lock:
        _token_cache[service] = {"at": now, "token": token, "expires": expires}
    return token


# --------------------------------------------------------------------------
# OpenRouter : cle, nom de modele, corps de requete
# --------------------------------------------------------------------------

_or_key_cache = {"at": 0.0, "key": None}


def or_key():
    """Cle OpenRouter, lue dans le .env de l'installation, gardee 60 s."""
    now = time.time()
    if _or_key_cache["key"] and now - _or_key_cache["at"] < 60:
        return _or_key_cache["key"]
    key = os.environ.get("OPENROUTER_API_KEY")
    for path in ((ENV_FILE,) + LEGACY_ENV) if not key else ():
        try:
            with open(path) as fh:
                for line in fh:
                    if line.startswith("OPENROUTER_API_KEY="):
                        key = line.split("=", 1)[1].strip().strip("\"'")
                        break
        except OSError:
            continue
        if key:
            break
    _or_key_cache.update(at=now, key=key or None)
    return key or None


# --------------------------------------------------------------------------
# Modeles servis : table par defaut, surchargee depuis le dashboard
# --------------------------------------------------------------------------

_models_cache = {"at": 0.0, "over": {}}


def overrides():
    """Surcharges alias -> modele choisies par l'utilisateur, relues a chaud.

    Meme TTL que le mode : un changement s'applique au message suivant, sans
    redemarrer le routeur ni rouvrir la session.
    """
    now = time.time()
    with _state_lock:
        if now - _models_cache["at"] < STATE_TTL:
            return _models_cache["over"]
    data = read_state()
    over = data["models"] if isinstance(data.get("models"), dict) else {}
    with _state_lock:
        _models_cache.update(at=now, over=over)
    return over


def table_for(mode, default):
    """Table effective d'un mode : le defaut, puis la surcharge du dashboard.

    Une surcharge n'est retenue que si elle nomme un alias connu et une valeur
    non vide : un etat corrompu ne doit pas envoyer un nom vide en amont.
    """
    table = dict(default)
    over = overrides().get(mode)
    if isinstance(over, dict):
        for alias, ref in over.items():
            if alias in table and isinstance(ref, str) and ref.strip():
                table[alias] = ref.strip()
    return table


def or_model(name):
    """Alias Claude Code -> modele gratuit OpenRouter.

    Sans cette traduction, « claude-opus-5-20260401 » est servi par OpenRouter
    depuis le vrai Anthropic et facture : le repli couterait de l'argent au
    lieu d'en economiser. Un nom qui contient deja « / » est un identifiant
    OpenRouter explicite, on le laisse passer.
    """
    table = table_for("or", OR_MODELS)
    if not isinstance(name, str) or "/" in name:
        return name
    low = name.lower()
    for alias, target in table.items():
        if alias in low:
            return target
    return table["sonnet"]


def or_body(body):
    """Adapte le corps a OpenRouter. Renvoie le corps (re)serialise.

    Deux retouches seulement, le reste de l'API Anthropic passe tel quel :
      - le nom de modele (sinon facturation, cf. or_model) ;
      - « cache_control », propre a Anthropic et sans effet ici, retire pour
        ne pas risquer un refus du fournisseur en amont.
    """
    if not body:
        return body
    try:
        data = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return body  # pas du JSON : on ne touche a rien
    if not isinstance(data, dict):
        return body

    if data.get("model"):
        data["model"] = or_model(data["model"])

    def strip_cc(node):
        if isinstance(node, dict):
            node.pop("cache_control", None)
            for v in node.values():
                strip_cc(v)
        elif isinstance(node, list):
            for v in node:
                strip_cc(v)

    strip_cc(data.get("system"))
    strip_cc(data.get("messages"))
    strip_cc(data.get("tools"))
    return json.dumps(data).encode()


# --------------------------------------------------------------------------
# Passerelles OpenAI : choix du modele et appel amont
# --------------------------------------------------------------------------

def bridge_model(mode, name):
    """Alias Claude Code -> modele gratuit de la passerelle.

    Meme garde-fou que pour OpenRouter : un nom « claude-* » laisse tel quel
    serait au mieux inconnu, au pire facture. On ne laisse passer un nom brut
    que s'il figure deja au catalogue de la passerelle.
    """
    table = table_for(mode, BRIDGES[mode]["models"])
    if not isinstance(name, str):
        return table["sonnet"]
    if name in table.values():
        return name
    low = name.lower()
    for alias, target in table.items():
        if alias in low:
            return target
    return table["sonnet"]


# --------------------------------------------------------------------------
# Relais
# --------------------------------------------------------------------------

class Router(http.server.BaseHTTPRequestHandler):
    server_version = "claude-router"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass  # le relais loggue lui-meme, une ligne par requete utile

    def handle_one_request(self):
        try:
            super().handle_one_request()
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    # -- petites reponses locales -----------------------------------------
    def local(self, code, payload, ctype="application/json"):
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self.relay()

    def do_POST(self):
        self.relay()

    def do_DELETE(self):
        self.relay()

    def do_PUT(self):
        self.relay()

    def do_OPTIONS(self):
        self.relay()

    def do_HEAD(self):
        self.relay()

    # -- passerelles OpenAI (Zen, Kilo) -----------------------------------
    def bridged(self, mode, path, body):
        """Sert /v1/messages depuis une passerelle qui ne parle qu'OpenAI."""
        cfg = BRIDGES[mode]

        try:
            data = json.loads(body) if body else {}
        except (ValueError, UnicodeDecodeError):
            data = {}
        if not isinstance(data, dict):
            data = {}

        # Comptage de jetons : la passerelle n'a pas ce point d'entree, et un
        # 404 ferait echouer la compaction cote Claude Code.
        if path.endswith("/count_tokens"):
            n = bridge.count_tokens(data)
            log(f"{mode} count_tokens -> {n}")
            return self.local(200, {"input_tokens": n})

        if not path.endswith("/messages"):
            # Le reste de l'API Anthropic (modeles, lots...) n'a pas
            # d'equivalent : mieux vaut le dire que relayer dans le vide.
            return self.local(404, {"type": "error", "error": {
                "type": "not_found_error",
                "message": f"{path} n'est pas servi par le repli {cfg['label']}."}})

        model = bridge_model(mode, data.get("model"))
        stream = bool(data.get("stream"))
        payload = json.dumps(bridge.to_openai(data, model)).encode()

        headers = {
            "Content-Type": "application/json",
            "Content-Length": str(len(payload)),
            "User-Agent": BRIDGE_UA,
            "Accept": "text/event-stream" if stream else "application/json",
            "Host": cfg["host"],
        }

        cls = http.client.HTTPSConnection if cfg["tls"] else http.client.HTTPConnection
        try:
            conn = cls(cfg["host"], cfg["port"], timeout=900)
            conn.request("POST", cfg["path"], body=payload, headers=headers)
            resp = conn.getresponse()
        except Exception as e:
            log(f"{mode} {path} -> injoignable ({type(e).__name__})")
            return self.local(502, {"type": "error", "error": {
                "type": "router_error",
                "message": f"{cfg['label']} injoignable ({type(e).__name__})"}})

        if resp.status >= 400:
            detail = resp.read(2000).decode("utf-8", "replace")
            try:
                conn.close()
            except Exception:
                pass
            log(f"{mode} {path} -> {resp.status} ({model})")
            kind = "rate_limit_error" if resp.status == 429 else "api_error"
            return self.local(resp.status, {"type": "error", "error": {
                "type": kind,
                "message": f"{cfg['label']} / {model} : {detail[:400]}"}})

        log(f"{mode} {self.command} {path} -> {resp.status} ({model}"
            f"{', flux' if stream else ''})")

        if not stream:
            try:
                upstream = json.load(resp)
            except ValueError:
                upstream = {}
            finally:
                try:
                    conn.close()
                except Exception:
                    pass
            return self.local(200, bridge.to_anthropic(upstream, data.get("model") or model))

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        try:
            for event in bridge.stream_to_anthropic(resp, data.get("model") or model):
                self.wfile.write(b"%x\r\n%s\r\n" % (len(event), event))
                self.wfile.flush()
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def relay(self):
        path = urllib.parse.urlparse(self.path).path

        # Sonde locale : dit ou partent les requetes, sans en emettre une.
        if path == "/__router":
            mode = current_mode()
            now = time.time()
            accounts = [{"name": a["name"],
                         "resting": a["cooldownUntil"] > now,
                         "restUntil": int(a["cooldownUntil"]) or None}
                        for a in all_accounts()]
            if mode == "native":
                acc = active_account()
                return self.local(200, {
                    "mode": mode,
                    "upstream": "api.anthropic.com",
                    "account": acc["name"],
                    "accounts": accounts,
                    "hasToken": bool(access_token(acc["service"])),
                })
            if mode in BRIDGES:
                cfg = BRIDGES[mode]
                return self.local(200, {
                    "mode": mode,
                    "label": cfg["label"],
                    "upstream": cfg["host"] + cfg["path"],
                    "accounts": accounts,
                    "hasToken": True,     # ces passerelles n'exigent pas de cle
                    "models": cfg["models"],
                })
            return self.local(200, {
                "mode": mode,
                "label": "OpenRouter",
                "upstream": "openrouter.ai/api/v1",
                "accounts": accounts,
                "hasToken": bool(or_key()),
                "models": OR_MODELS,
            })

        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        body = self.rfile.read(length) if length else None

        mode = current_mode()
        if mode in BRIDGES:
            return self.bridged(mode, path, body)

        account = active_account() if mode == "native" else None
        got = self.direct(mode, body, account)
        if got is None:
            return                       # l'erreur a deja ete rendue au client
        conn, resp = got

        # Quota Claude atteint. C'est le seul endroit ou le repli peut se
        # prendre sans rien perdre : aucun octet n'est encore parti vers le
        # client, donc la meme requete repart ailleurs et le message de
        # l'utilisateur arrive quand meme. Une fois le SSE commence, il est
        # trop tard — on ne rejoue plus, on laisse passer l'erreur.
        if mode == "native" and resp.status == 429:
            due = retry_after(resp)
            try:
                resp.read()
            except Exception:
                pass
            conn.close()
            rest_account(account["name"], due)
            log(f"429 sur le compte « {account['name']} » — repos "
                f"{max(1, round((due - time.time()) / 60))} min")

            # Un autre compte Claude d'abord : un vrai Opus vaut mieux qu'un
            # modele gratuit, et la requete n'a encore rien envoye au client.
            tried = {account["name"]}
            while True:
                nxt = next_account(tried)
                if nxt is None:
                    break
                set_active_account(nxt["name"])
                log(f"bascule sur le compte « {nxt['name']} »")
                got = self.direct("native", body, nxt)
                if got is None:
                    return
                conn, resp = got
                if resp.status != 429:
                    return self.stream("native", path, conn, resp)
                nxt_due = retry_after(resp)
                try:
                    resp.read()
                except Exception:
                    pass
                conn.close()
                rest_account(nxt["name"], nxt_due)
                due = min(due, nxt_due)
                tried.add(nxt["name"])
                log(f"429 aussi sur « {nxt['name']} »")

            # Tous les comptes sont au repos : c'est maintenant que le
            # gratuit sert a quelque chose. La date de retour est celle du
            # premier compte a se liberer.
            prov = chain()[0]
            set_mode(prov, "auto: quota Claude atteint", due)
            log(f"tous les comptes epuises -> repli {prov}, natif retente "
                f"dans {max(1, round((due - time.time()) / 60))} min")
            if prov in BRIDGES:
                return self.bridged(prov, path, body)
            got = self.direct(prov, body)
            if got is None:
                return
            conn, resp = got
            mode = prov

        return self.stream(mode, path, conn, resp)

    def direct(self, mode, body, account=None):
        """Relaie tel quel vers un amont qui parle l'API Anthropic.

        Rend (connexion, reponse), ou None apres avoir rendu l'erreur au
        client. La reponse n'est pas consommee : l'appelant decide encore de
        la renvoyer ou de rejouer ailleurs.
        """
        path = urllib.parse.urlparse(self.path).path
        host, port, tls = UPSTREAM_OR if mode == "or" else UPSTREAM_NATIVE
        # Chemin amont : OpenRouter prefixe l'API Anthropic par /api.
        path_up = self.path

        headers = {}
        for key, value in self.headers.items():
            if key.lower() in HOP_BY_HOP:
                continue
            headers[key] = value

        if mode == "native":
            acc = account or active_account()
            token = access_token(acc["service"])
            if not token:
                self.local(503, {"type": "error", "error": {
                    "type": "router_error",
                    "message": f"aucun jeton OAuth pour le compte "
                               f"« {acc['name']} » dans le trousseau — lance "
                               f"`claude` et connecte-toi une fois, ou "
                               f"`dbl accounts rm {acc['name']}`."}})
                return None
            # Le compte actif remplace ce que Claude Code avait mis : c'est
            # tout l'interet du routeur, la session n'a rien a relire.
            headers.pop("x-api-key", None)
            headers.pop("X-Api-Key", None)
            headers["Authorization"] = f"Bearer {token}"
            beta = headers.get("anthropic-beta", "")
            if "oauth-2025-04-20" not in beta:
                headers["anthropic-beta"] = \
                    ("oauth-2025-04-20," + beta) if beta else "oauth-2025-04-20"
        else:
            key = or_key()
            if not key:
                self.local(503, {"type": "error", "error": {
                    "type": "router_error",
                    "message": "OPENROUTER_API_KEY introuvable dans "
                               "~/.doublure/.env — repli impossible."}})
                return None
            headers.pop("x-api-key", None)
            headers.pop("X-Api-Key", None)
            # Propres a Anthropic : sans objet en amont, et « beta » peut
            # faire refuser la requete.
            headers.pop("anthropic-beta", None)
            headers.pop("Anthropic-Beta", None)
            headers["Authorization"] = f"Bearer {key}"
            headers["HTTP-Referer"] = "https://claude.ai/code"
            headers["X-Title"] = "Claude Code"
            body = or_body(body)
            if body is not None:
                headers["Content-Length"] = str(len(body))
            if not path_up.startswith(OR_PREFIX + "/"):
                path_up = OR_PREFIX + path_up
        headers["Host"] = host if port in (80, 443) else f"{host}:{port}"

        cls = http.client.HTTPSConnection if tls else http.client.HTTPConnection
        try:
            conn = cls(host, port, timeout=900)
            conn.request(self.command, path_up, body=body, headers=headers)
            resp = conn.getresponse()
        except Exception as e:
            log(f"{mode} {path} -> injoignable ({type(e).__name__})")
            self.local(502, {"type": "error", "error": {
                "type": "router_error",
                "message": f"amont {host}:{port} injoignable ({type(e).__name__})"}})
            return None
        return conn, resp

    def stream(self, mode, path, conn, resp):
        """Renvoie la reponse amont au client, au fil de l'eau."""
        log(f"{mode} {self.command} {path} -> {resp.status}")

        # On reforme la reponse : le corps repart en chunked pour que le SSE
        # sorte au fil de l'eau, sans attendre la fin de la generation.
        self.send_response(resp.status)
        for key, value in resp.getheaders():
            # Content-Encoding est conserve : http.client ne decompresse pas,
            # le corps repart tel quel et le client le decode lui-meme.
            if key.lower() in HOP_BY_HOP:
                continue
            self.send_header(key, value)
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        try:
            while True:
                chunk = resp.read(8192)
                if not chunk:
                    break
                self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
                self.wfile.flush()
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        finally:
            try:
                conn.close()
            except Exception:
                pass


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    try:
        srv = Server((HOST, PORT), Router)
    except OSError as e:
        sys.exit(f"port {PORT} indisponible : {e}")
    threading.Thread(target=watchdog, daemon=True).start()
    log(f"routeur pret sur http://{HOST}:{PORT} (mode {current_mode()})")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
