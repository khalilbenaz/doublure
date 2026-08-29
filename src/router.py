#!/usr/bin/env python3
"""Routeur d'API pour Claude Code — le repli, sans redemarrer le terminal.

Le probleme : Claude Code lit `~/.claude/settings.json` au demarrage de session
seulement. Y ecrire le repli ne l'appliquerait qu'a la session suivante —
inutile au moment exact ou le quota tombe.

La solution : `settings.json` pointe une fois pour toutes sur ce routeur, qui
decide *a chaque requete* ou l'envoyer :

  mode natif    -> https://api.anthropic.com, avec le jeton OAuth du compte
                   actif (lu dans le trousseau macOS de Claude Code)
  mode <fournisseur> -> une des passerelles du registre (providers.py) :
                   API OpenAI, traduite par `bridge.py`. Le registre porte
                   une quarantaine de fournisseurs, tient son catalogue de
                   modeles et deduit lui-meme le modele de chaque palier.
  mode or       -> https://openrouter.ai/api/v1, amont qui parle l'API
                   Anthropic nativement (/v1/messages, SSE, tool_use)
  mode fcc      -> proxy Free Claude Code local (127.0.0.1:8082), s'il
                   tourne : un maillon de plus, jamais une dependance

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

import datetime
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
import providers  # noqa: E402
import statefile  # noqa: E402

DBL_DIR = os.path.join(HOME, ".doublure")
STATE = os.path.join(DBL_DIR, "state.json")
# Etats des versions precedentes : lus en secours, jamais ecrits.
LEGACY_STATE = (os.path.join(HOME, ".claude", "fcc-fallback.json"),
                os.path.join(HOME, ".claude-swap-backup", "fallback.json"))

UPSTREAM_NATIVE = ("api.anthropic.com", 443, True)
UPSTREAM_OR = ("openrouter.ai", 443, True)

# Free Claude Code, s'il est installe en local (http://127.0.0.1:8082). Il
# parle l'API Anthropic nativement. Doublure porte desormais son propre
# registre de fournisseurs (providers.py) : FCC n'est plus qu'un maillon
# supplementaire, essaye s'il ecoute, retire sans bruit sinon. Rien ici n'en
# depend.
FCC_PORT = int(os.environ.get("FCC_PORT", "8082"))
UPSTREAM_FCC = ("127.0.0.1", FCC_PORT, False)
FCC_ENV = os.path.join(HOME, ".fcc", ".env")
# FCC n'exige pas de cle par defaut : ce jeton est un marqueur, pas un
# secret — il satisfait juste le controle « une cle est presente ».
FCC_TOKEN = "doublure"

# OpenRouter sert l'API Anthropic sous /api/v1 ; Claude Code appelle /v1.
OR_PREFIX = "/api"
# Les cles vivent dans ~/.doublure/.env, resolues par providers.py — qui lit
# aussi celui de FCC, en lecture seule, pour ne pas redemander ce qui est deja
# pose la.

# Reecriture obligatoire du nom de modele : laisse tel quel, « claude-opus-5 »
# est route par OpenRouter vers le VRAI Anthropic et facture au credit. Le
# repli doit rester gratuit, donc chaque alias Claude Code est traduit. La
# table n'est plus ecrite ici : elle vient du registre, qui deduit le modele
# de chaque palier du catalogue reellement servi (providers.tiers).

# --------------------------------------------------------------------------
# Passerelles parlant OpenAI : le registre
# --------------------------------------------------------------------------
# OpenRouter sert l'API Anthropic telle quelle ; les autres non : elles n'ont
# que /chat/completions. `bridge.py` traduit requete, reponse et flux SSE, de
# sorte que la session Claude Code ne voie aucune difference.
#
# La liste n'est plus ecrite ici. `providers.py` porte le registre — une
# quarantaine de fournisseurs, leurs bases, leurs cles, leurs ecarts de
# dialecte — et deduit le modele de chaque palier du catalogue que le
# fournisseur annonce vraiment. Ajouter une cle dans ~/.doublure/.env suffit
# a faire entrer un fournisseur dans la chaine ; il n'y a rien a coder.

# Noms courts historiques. `dbl on zen` doit continuer de marcher : c'est le
# nom que la documentation et l'habitude connaissent. OpenRouter, lui, est
# ramene sur le mode « or » : il parle l'API Anthropic nativement, ce qui est
# plus fidele que de passer par la traduction.
ALIASES = {
    "zen": "opencode_zen", "go": "opencode_go", "nim": "nvidia_nim",
    "open_router": "or", "openrouter": "or",
}


def resolve(mode):
    """Nom court -> identifiant du registre."""
    return ALIASES.get(mode, mode) if isinstance(mode, str) else mode


def modes():
    """Modes acceptables. Calcule, pas fige : une cle ajoutee suffit."""
    return ("native", "fcc", "or") + tuple(providers.PREFERENCE)


def mode_ok(mode):
    return isinstance(mode, str) and resolve(mode) in modes()


def is_bridged(mode):
    """Ce mode passe-t-il par la traduction OpenAI ?"""
    return mode in providers.CATALOG and providers.usable(mode)


def bridge_cfg(mode):
    """Ou et comment joindre une passerelle du registre."""
    host, port, tls, _prefix = providers.endpoint(mode)
    return {"label": providers.label(mode), "host": host, "port": port,
            "tls": tls, "path": providers.chat_path(mode)}


# Ordre d'essai des replis quand le natif tombe sur un 429. Il n'est plus
# ecrit a la main : c'est l'ordre de preference du registre, restreint aux
# fournisseurs joignables. FCC est place en queue — utile s'il tourne,
# jamais necessaire.
# Delai de repli quand l'amont n'annonce pas lui-meme son echeance.
RETRY_NATIVE_DEFAULT = 30 * 60

# Rafraichissement des catalogues de modeles, en fond. Une demi-heure : un
# fournisseur ajoute ou retire un modele en jours, pas en minutes.
CATALOG_POLL = 30 * 60

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
# Repos d'une passerelle gratuite qui vient de refuser (429) ou de tomber
# (5xx). Court : ces passerelles sont partagees, une saturation passe vite.
PROVIDER_REST = 5 * 60
# Panne reseau ou amont injoignable : encore plus court, c'est souvent une
# coupure d'une poignee de secondes.
PROVIDER_REST_NET = 60
# Anthropic annonce l'epuisement avant de le refuser : cet endpoint rend le
# taux d'utilisation par fenetre et la date de remise a zero. Un 429 est un
# refus subi ; ceci est le meme fait, connu a l'avance.
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
USAGE_BETA = "oauth-2025-04-20"
# Sonde de fond, jamais sur le chemin d'une requete : une seule interrogation
# par compte et par minute, et le relais ne lit qu'un cache.
USAGE_POLL = 60.0
# Seuil de mise au repos. Pas 100 % : la derniere requete avant la limite peut
# etre celle qui la depasse, et on prefere basculer un cheveu trop tot.
USAGE_THRESHOLD = 95.0
# L'identifiant client OAuth de Claude Code n'est PAS ecrit en dur ici :
# c'est celui de l'installation de l'utilisateur, retrouve sur sa machine
# par client_id(). Le distribuer serait partager le notre.
CLIENT_ID_FILE = os.path.join(DBL_DIR, "client-id")
OAUTH_TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
# On rafraichit avant l'echeance : une requete ne doit jamais partir avec un
# jeton qui expire pendant son vol.
REFRESH_MARGIN = 300
# Repos d'un compte dont le jeton vient d'etre refuse (401). Court : le
# compte n'est pas epuise, il attend qu'un couple OAuth valide soit ecrit
# dans le trousseau par un autre detenteur (claude-swap, un run Atelier).
# Plus long, il ferait travailler la session en repli gratuit alors que le
# vrai Opus est redevenu joignable.
AUTH_REST = 120

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

# Une seule requete a la fois mene une bascule. Claude Code emet plusieurs
# requetes en parallele : sans ce verrou, chacune brule tous les comptes sur
# le meme 429 et ecrase la decision des autres.
_switch_lock = threading.Lock()
# Passerelles gratuites au repos apres un echec. En memoire seulement : une
# indisponibilite de cinq minutes n'a pas a survivre au routeur.
_provider_rest = {}
_provider_lock = threading.Lock()


def log(msg):
    sys.stderr.write(f"{time.strftime('%H:%M:%S')} {msg}\n")
    sys.stderr.flush()


def _write_json(path, data):
    ok = statefile.write_json(path, data)
    if not ok:
        log(f"{os.path.basename(path)} non ecrit — bascule en memoire seule")
    return ok


def rest_provider(name, secs):
    """Met une passerelle gratuite au repos : la chaine la sautera."""
    with _provider_lock:
        _provider_rest[name] = time.time() + float(secs)


def provider_resting(name):
    with _provider_lock:
        return _provider_rest.get(name, 0) > time.time()


def clear_provider_rest():
    with _provider_lock:
        _provider_rest.clear()


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
        if isinstance(data, dict) and mode_ok(data.get("mode")):
            # « fcc » designait autrefois OpenRouter ; il designe desormais
            # le proxy Free Claude Code local, meilleur repli que lui — un
            # etat ancien pointe donc au bon endroit sans traduction. Les
            # anciens noms courts (« zen », « nim ») sont traduits ici, une
            # fois : le reste du routeur ne connait que les ids du registre.
            mode = resolve(data["mode"])
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


def update_state(**kw):
    """Modifie l'etat champ par champ, sans perdre ce qu'un autre thread ecrit.

    La lecture et l'ecriture tiennent dans un seul verrou : sinon deux threads
    lisent le meme etat, en modifient chacun un champ, et le second efface la
    modification du premier — un changement de compte annulant une bascule de
    mode, par exemple.
    """
    with statefile.file_lock():
        data = read_state()
        data.update(kw)
        _write_json(STATE, data)
        return data


def set_mode(mode, reason, retry_at=0):
    """Ecrit le mode dans l'etat, atomiquement, et purge le cache de lecture.

    Le routeur ecrit l'etat lui-meme au lieu d'appeler le CLI : la bascule a
    lieu au milieu d'une requete client qui attend, et lancer un sous-processus
    la ferait patienter le temps d'un demarrage de Python.
    """
    update_state(mode=mode, since=int(time.time()), reason=reason,
                 retryNativeAt=int(retry_at))
    with _state_lock:
        _state_cache.update(at=time.time(), mode=mode)


_fcc_probe = {"at": 0.0, "up": False}


def fcc_up():
    """Le proxy Free Claude Code ecoute-t-il ? Sonde TCP, en cache 30 s.

    Sans cette verification, un FCC arrete ferait payer un refus de
    connexion a chaque requete de repli avant de passer au suivant.
    """
    now = time.time()
    if now - _fcc_probe["at"] < 30:
        return _fcc_probe["up"]
    up = False
    try:
        socket.create_connection(UPSTREAM_FCC[:2], timeout=0.4).close()
        up = True
    except OSError:
        pass
    _fcc_probe.update(at=now, up=up)
    return up


def fcc_models():
    """Modeles que FCC servira par alias, lus dans son propre .env.

    Purement informatif (sonde /__router, statusline) : la correspondance
    est faite par FCC, le routeur ne reecrit aucun nom pour lui.
    """
    out = {}
    try:
        with open(FCC_ENV) as fh:
            for line in fh:
                line = line.strip()
                if line.startswith("MODEL="):
                    out.setdefault("default", line[len("MODEL="):].strip())
                    continue
                for alias in ("opus", "sonnet", "fable", "haiku"):
                    pref = "MODEL_%s=" % alias.upper()
                    if line.startswith(pref):
                        val = line[len(pref):].strip()
                        if val and val != "None":
                            out[alias] = val
    except OSError:
        pass
    return out


def chain():
    """Ordre des replis a essayer, du plus prometteur au dernier recours.

    Deduit du registre : les fournisseurs joignables (cle presente, ou qui
    servent sans cle), dans l'ordre de preference. Un ordre fixe dans l'etat
    par l'utilisateur gagne sur le defaut.

    Un fournisseur sans cle est ecarte plutot que tente : son 401 serait
    presente au client comme une panne du repli, alors que le suivant aurait
    repondu.
    """
    order = read_state().get("chain")
    if order and isinstance(order, list):
        order = [resolve(p) for p in order if isinstance(p, str)]
    else:
        order = [resolve(p) for p in providers.configured()] + ["fcc"]
    out, seen = [], set()
    for prov in order:
        if prov in seen or prov == "native":
            continue
        seen.add(prov)
        if prov == "fcc":
            if fcc_up():
                out.append(prov)
        elif prov == "or":
            if or_key():
                out.append(prov)
        elif is_bridged(prov) and (providers.key(prov)
                                   or providers.keyless(prov)):
            out.append(prov)
    return out or ["opencode_zen"]


def provider_rows():
    """Etat du registre, pour la sonde /__router et le CLI.

    Les cles ne sortent jamais d'ici — seulement le fait qu'il y en ait une.
    """
    live = set(chain())
    rows = []
    for prov in providers.PREFERENCE:
        if not providers.usable(prov):
            continue
        name = resolve(prov)
        cfg = providers.CATALOG[prov]
        rows.append({
            "id": name, "label": providers.label(prov),
            "hasKey": bool(providers.key(prov)),
            "keyless": providers.keyless(prov),
            "local": bool(cfg.get("local")),
            "inChain": name in live,
            "resting": provider_resting(name),
        })
    return rows


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


def catalog_watch():
    """Tient les catalogues de modeles chauds, en fond.

    Sans ce fil, la premiere requete de repli paierait le listage HTTP du
    fournisseur avant de pouvoir choisir un modele — quelques centaines de
    millisecondes, parfois plus si l'amont traine. Ici c'est fait a l'avance,
    hors du chemin des requetes, et le cache disque tient six heures.
    """
    while True:
        try:
            for prov in providers.configured():
                if resolve(prov) in ("or", "native"):
                    continue      # servis en API Anthropic, aucun listage
                providers.models(prov)
        except Exception as e:
            log(f"catalogue: {type(e).__name__}: {e}")
        time.sleep(CATALOG_POLL)


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
            if not due or time.time() < due:
                continue

            # La date est atteinte. Avant de rebasculer, confirmer avec le
            # dernier releve de quota : une date de retour optimiste renverrait
            # au natif pour reprendre un 429 et retomber en repli — un
            # aller-retour perdu, et une requete client qui l'attend.
            later = quota_still_full()
            if later:
                set_mode(st.get("mode"), st.get("reason") or "auto", later)
                log(f"quota toujours annonce plein — retour au natif repousse "
                    f"de {max(1, round((later - time.time()) / 60))} min")
                continue

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


def read_keychain(service=KEYCHAIN_SERVICE, account=None):
    """Lit une entree du trousseau.

    `account` vise une entree precise a l'interieur d'un service partage :
    claude-swap range tous ses comptes sous le meme service et les distingue
    par leur compte (« account-1-... »).
    """
    cmd = ["security", "find-generic-password", "-s", service]
    if account:
        cmd += ["-a", account]
    try:
        out = subprocess.run(
            cmd + ["-w"], capture_output=True, text=True, timeout=10)
    except Exception:
        return None
    if out.returncode != 0:
        return None
    try:
        return json.loads(out.stdout)
    except ValueError:
        return None


def write_keychain(blob, service=KEYCHAIN_SERVICE, account=None):
    """Reecrit l'entree du trousseau (-U remplace celle qui existe).

    Le pool de claude-swap est en lecture seule : il rafraichit ses jetons
    lui-meme, et deux ecrivains sur la meme entree se marcheraient dessus.
    """
    if service == SWAP_SERVICE:
        return
    try:
        subprocess.run(
            ["security", "add-generic-password", "-U", "-s", service,
             "-a", account or os.environ.get("USER", "claude"),
             "-w", json.dumps(blob)],
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
            row = {"name": str(item["name"]),
                   "service": item.get("service")
                   or KEYCHAIN_PREFIX + str(item["name"]),
                   "cooldownUntil": float(item.get("cooldownUntil") or 0)}
            if item.get("swapAccount"):
                row["swapAccount"] = str(item["swapAccount"])
            out.append(row)
    return out


def write_accounts(accounts):
    with statefile.file_lock():
        _write_json(ACCOUNTS_FILE, {"accounts": accounts})


def all_accounts():
    """Tous les comptes Claude utilisables, dans l'ordre d'essai.

    Le premier est toujours celui auquel Claude Code est connecte : c'est le
    comportement d'origine quand aucun compte n'a ete ajoute. Viennent ensuite
    ceux de claude-swap, puis ceux ajoutes ici par `dbl accounts add`.

    L'ordre a une raison : un compte du pool claude-swap est un abonnement
    complet, il vaut mieux que n'importe quel modele gratuit, et le tenter ne
    coute qu'une requete. Sans cette lecture, doublure basculait au gratuit
    alors que le pool avait encore du quota.
    """
    session = {"name": "claude", "service": KEYCHAIN_SERVICE,
               "cooldownUntil": 0}
    out = [session]
    # La cle d'unicite est (service, entree) : le pool claude-swap range tous
    # ses comptes sous un service unique, un dedoublonnage par service seul
    # n'en garderait qu'un.
    seen = {(KEYCHAIN_SERVICE, None)}
    # Les noms servent au second dedoublonnage : mettre un compte du pool au
    # repos lui cree une ligne dans accounts.json, et sans ce filtre elle
    # reviendrait comme un compte a part entiere — vers un service de
    # trousseau qui n'existe pas, donc un 401 garanti.
    names = {"claude"}
    rests = {a["name"]: a["cooldownUntil"] for a in read_accounts()}
    for acc in swap_accounts():
        key = (acc["service"], acc.get("swapAccount"))
        if key in seen or acc["name"] in names:
            continue
        seen.add(key)
        names.add(acc["name"])
        # Le repos vit chez nous, pas chez claude-swap : c'est notre 429 qu'il
        # traduit, pas le sien.
        acc["cooldownUntil"] = rests.get(acc["name"], 0)
        out.append(acc)
    for acc in read_accounts():
        key = (acc["service"], acc.get("swapAccount"))
        if acc["service"] == SWAP_SERVICE and key not in seen:
            # Ligne laissee par un repos sur un slot que claude-swap ne
            # propose plus : compte retire, ou doublon du compte de la session
            # ecarte plus haut. La garder le ferait revenir comme un compte a
            # part entiere, et c'est precisement le doublon qu'on vient de
            # supprimer.
            continue
        if key in seen or acc["name"] in names:
            # Le compte de la session a lui aussi un repos memorise : le
            # perdre ici le ferait reessayer en boucle sur son 429.
            if acc["service"] == KEYCHAIN_SERVICE and acc["name"] == "claude":
                session["cooldownUntil"] = acc["cooldownUntil"]
            continue
        seen.add(key)
        names.add(acc["name"])
        out.append(acc)
    return out


# --------------------------------------------------------------------------
# Comptes geres par claude-swap
# --------------------------------------------------------------------------
# claude-swap tient son propre pool, dans un unique service de trousseau
# « claude-swap » avec une entree par slot : « account-<n>-<email> ». Sans
# cette lecture, doublure ne voyait que le compte de la session et partait au
# modele gratuit alors qu'un autre abonnement Claude etait libre — c'est
# exactement l'ordre inverse de ce qu'on veut.
#
# On lit, on n'ecrit jamais : le pool reste la propriete de claude-swap, qui
# rafraichit ses jetons lui-meme. Un compte qu'il retire disparait tout seul
# d'ici a la sonde suivante.
SWAP_SERVICE = "claude-swap"
SWAP_USAGE = os.path.join(HOME, ".claude-swap-backup", "cache", "usage.json")
# Les entrees du trousseau ne sont pas listables sans mot de passe : on part
# des slots declares dans le cache d'usage de claude-swap, seul index lisible.
SWAP_TTL = 60.0
_swap_cache = {"at": 0.0, "accounts": []}
_swap_lock = threading.Lock()


def swap_accounts():
    """Comptes claude-swap utilisables, dans l'ordre de leurs slots.

    Rend des dictionnaires de la meme forme que read_accounts() pour que le
    reste du routeur n'ait pas a distinguer leur origine. Le nom porte son
    slot (« swap1 ») : c'est ce qui apparait dans les journaux et dans
    `dbl accounts`, et ca reste stable meme si l'email change de casse.
    """
    now = time.time()
    with _swap_lock:
        if now - _swap_cache["at"] < SWAP_TTL:
            return list(_swap_cache["accounts"])
    out = []
    try:
        with open(SWAP_USAGE) as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        data = {}
    slots = (data.get("accounts") or {}) if isinstance(data, dict) else {}
    # Le compte de la session est presque toujours l'un des slots — c'est
    # claude-swap qui l'y a mis. On l'identifie par son jeton de
    # rafraichissement, seule valeur stable commune aux deux entrees (ni
    # l'adresse ni l'organisation ne figurent dans le blob de Claude Code) :
    # sans ca, le meme abonnement serait tente deux fois, et son 429 compte
    # comme un compte de plus brule dans le journal.
    session = ((read_keychain() or {}).get("claudeAiOauth")
               or {}).get("refreshToken")
    # Le jeton de rafraichissement tourne a chaque renouvellement : des que
    # l'un des deux exemplaires se rafraichit, la comparaison ci-dessus ne
    # reconnait plus le doublon et le meme abonnement repart en deux comptes.
    # Chacun rafraichit alors le couple de l'autre, ce qui le revoque — c'est
    # la boucle de 401 « please run /login » que la session ne pouvait pas
    # reparer. On retient donc le slot une fois pour toutes.
    known = str(read_state().get("sessionSlot") or "")
    for slot in sorted(slots, key=lambda s: (len(s), s)):
        info = slots[slot] if isinstance(slots[slot], dict) else {}
        email = info.get("email")
        if not email:
            continue
        entry = "account-%s-%s" % (slot, email)
        oauth = (read_keychain(SWAP_SERVICE, entry) or {}).get("claudeAiOauth")
        if not oauth:
            # Le fichier de cache de claude-swap garde des slots dont
            # l'entree de trousseau a disparu (compte retire, trousseau
            # nettoye). Les proposer ne donnerait que des 401, et ils
            # apparaitraient comme des comptes disponibles dans `dbl`.
            continue
        if session and oauth.get("refreshToken") == session:
            if slot != known:
                update_state(sessionSlot=slot)
            continue
        if slot == known:
            # Meme abonnement que la session, reconnu avant que les deux
            # exemplaires ne divergent. Le tenter serait tenter le compte de
            # la session une seconde fois, sous un autre nom.
            continue
        out.append({
            "name": "swap%s" % slot,
            "service": SWAP_SERVICE,
            "swapAccount": entry,
            "email": email,
            "cooldownUntil": 0,
        })
    with _swap_lock:
        _swap_cache.update(at=now, accounts=list(out))
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
    update_state(account=name)


def rest_account(name, until, account=None):
    """Met un compte au repos jusqu'a `until`.

    Le compte de la session n'echappe pas a la regle : son repos est garde
    dans le meme fichier, meme s'il n'a pas d'entree propre au depart.

    `account` est le compte complet quand on l'a sous la main : la ligne creee
    reprend alors son service et son entree de trousseau, au lieu du
    « Doublure-<nom> » deduit du nom — faux pour un compte du pool claude-swap.
    """
    # Relecture et ecriture sous le meme verrou : la sonde de quota et une
    # requete qui prend un 429 mettent au repos en meme temps, et le repos
    # perdu ferait reessayer un compte en boucle sur son propre refus.
    with statefile.file_lock():
        accounts = read_accounts()
        for acc in accounts:
            if acc["name"] == name:
                acc["cooldownUntil"] = float(until)
                break
        else:
            row = {"name": name,
                   "service": (account or {}).get("service")
                   or (KEYCHAIN_SERVICE if name == "claude"
                       else KEYCHAIN_PREFIX + name),
                   "cooldownUntil": float(until)}
            if (account or {}).get("swapAccount"):
                row["swapAccount"] = account["swapAccount"]
            accounts.insert(0, row)
        write_accounts(accounts)


def next_account(exclude, now=None):
    """Prochain compte utilisable, en sautant ceux au repos et `exclude`."""
    now = time.time() if now is None else now
    for acc in all_accounts():
        if acc["name"] in exclude:
            continue
        if acc["cooldownUntil"] > now:
            continue
        if not read_keychain(acc["service"], acc.get("swapAccount")):
            # Entree disparue du trousseau (compte revoque, trousseau
            # nettoye) : on ne la propose pas, elle donnerait un 401.
            continue
        return acc
    return None


def _drop(conn, resp):
    """Vide et ferme une reponse amont qu'on ne renverra pas au client."""
    try:
        resp.read()
    except Exception:
        pass
    try:
        conn.close()
    except Exception:
        pass


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


def access_token(service=KEYCHAIN_SERVICE, account=None, fresh=False):
    """Jeton d'un compte, rafraichi si son echeance approche.

    Le cache est indexe par (service, entree) : les comptes de claude-swap
    partagent un service, et un cache par service seul leur ferait voler leur
    jeton l'un a l'autre.

    `fresh` saute le cache et force le rafraichissement quelle que soit
    l'echeance annoncee : c'est la reponse a un 401. Un jeton revoque cote
    Anthropic garde une echeance lointaine, seul un couple neuf passe. Rend
    None plutot qu'un jeton dont on sait deja qu'il sera refuse.
    """
    now = time.time()
    key = (service, account)
    if not fresh:
        with _token_lock:
            entry = _token_cache.get(key) or {}
            # Cache court : le compte connecte peut changer a tout moment, on
            # veut le voir vite, mais pas relire le trousseau a chaque requete.
            if entry.get("token") and now - entry.get("at", 0) < 5:
                return entry["token"]

    blob = read_keychain(service, account)
    if not blob:
        return None
    oauth = blob.get("claudeAiOauth") or {}
    token = oauth.get("accessToken")
    expires = (oauth.get("expiresAt") or 0) / 1000.0

    if token and (fresh or (expires and expires - now < REFRESH_MARGIN)):
        refresh = oauth.get("refreshToken")
        if refresh:
            try:
                data = refresh_token(refresh)
            except Exception as e:
                # Le couple OAuth a ete tourne par un autre detenteur du meme
                # compte — claude-swap, un run Atelier, une session `claude`
                # lancee a cote. Le notre est mort, mais le sien vient d'etre
                # ecrit dans le trousseau : on relit une fois avant de rendre
                # les armes.
                other = ((read_keychain(service, account) or {})
                         .get("claudeAiOauth") or {})
                if other.get("accessToken") \
                        and other["accessToken"] != token:
                    token = other["accessToken"]
                    expires = (other.get("expiresAt") or 0) / 1000.0
                    log(f"rafraichissement impossible ({type(e).__name__}) — "
                        f"jeton repris du trousseau ({account or service})")
                elif fresh:
                    # Appele apres un 401 : le jeton en place vient justement
                    # d'etre refuse, le reservir ne ferait qu'un 401 de plus.
                    log(f"rafraichissement impossible ({type(e).__name__}) — "
                        f"plus de jeton valide pour « {account or service} »")
                    with _token_lock:
                        _token_cache.pop(key, None)
                    return None
                else:
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
                write_keychain(blob, service, account)
                log(f"jeton OAuth rafraichi ({account or service})")

    with _token_lock:
        _token_cache[key] = {"at": now, "token": token, "expires": expires}
    return token


# --------------------------------------------------------------------------
# Quota annonce — /api/oauth/usage, sonde en tache de fond
# --------------------------------------------------------------------------

# Cle (service, entree de trousseau), pas le service seul : le pool
# claude-swap range tous ses comptes sous un service unique, une cle par
# service ferait lire a chaque compte le releve de son voisin.
_usage_cache = {}          # (service, entree) -> {"at": ..., "data": ...}
_usage_lock = threading.Lock()


def fetch_usage(service, account=None):
    """Taux d'utilisation du compte, tel qu'Anthropic le declare.

    Rend None si la question ne peut pas etre posee (pas de jeton, reseau,
    endpoint change). Ne jamais transformer un echec de sonde en repos : on
    laisserait un compte valide sur le banc pour une panne reseau.
    """
    token = access_token(service, account)
    if not token:
        return None
    req = urllib.request.Request(USAGE_URL, headers={
        "Authorization": f"Bearer {token}",
        "anthropic-beta": USAGE_BETA,
        "User-Agent": providers.UA,
    })
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read().decode())
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    with _usage_lock:
        _usage_cache[(service, account)] = {"at": time.time(), "data": data}
    return data


def usage_snapshot(service, account=None):
    """Dernier releve connu, sans rien emettre. Perime au bout de 5 minutes."""
    with _usage_lock:
        entry = _usage_cache.get((service, account))
    if not entry or time.time() - entry["at"] > 5 * USAGE_POLL:
        return None
    return entry["data"]


def _reset_at(value):
    """Date de remise a zero : ISO 8601 ou epoque, en secondes."""
    if isinstance(value, (int, float)):
        return float(value) if value > 1e9 else None
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.datetime.fromisoformat(
            value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def usage_verdict(data):
    """(epuise, jusqu'a, fenetre, pourcentage) d'apres un releve.

    On regarde toutes les fenetres declarees, pas seulement les cinq heures :
    la limite hebdomadaire tombe aussi, et plus longtemps.
    """
    worst = (0.0, None, None)
    windows = []
    for key in ("five_hour", "seven_day", "seven_day_opus",
                "seven_day_sonnet"):
        win = data.get(key)
        if isinstance(win, dict) and win.get("utilization") is not None:
            windows.append((key, win.get("utilization"),
                            _reset_at(win.get("resets_at"))))
    for lim in data.get("limits") or []:
        if isinstance(lim, dict) and lim.get("percent") is not None:
            windows.append((lim.get("kind") or "limite", lim["percent"],
                            _reset_at(lim.get("resets_at"))))
    for name, pct, until in windows:
        try:
            pct = float(pct)
        except (TypeError, ValueError):
            continue
        if pct > worst[0]:
            worst = (pct, until, name)
    pct, until, name = worst
    return pct >= USAGE_THRESHOLD, until, name, pct


def quota_still_full():
    """Date de retour a retenir si TOUS les comptes sont encore annonces
    epuises ; None des qu'un seul est utilisable ou inconnu.

    Le doute profite au retour au natif : sans releve — routeur qui vient de
    demarrer, sonde en panne, endpoint modifie — on rend None. Retenir un
    compte valide en repli gratuit sur un silence serait le pire des deux
    mondes ; au pire on reprend un 429, qui sait se rattraper tout seul.
    """
    now = time.time()
    soonest = None
    for acc in all_accounts():
        data = usage_snapshot(acc["service"], acc.get("swapAccount"))
        if data is None:
            return None
        spent, until, _, _ = usage_verdict(data)
        if not spent:
            return None
        if until:
            soonest = until if soonest is None else min(soonest, until)
    if soonest is None and not all_accounts():
        return None
    # Plancher : une date de remise a zero deja passee alors que le compte est
    # toujours donne plein ferait repousser a chaque tour de garde, une ligne
    # de journal par minute pour rien.
    return max(soonest or 0, now + 5 * 60)


def usage_watch():
    """Met au repos les comptes qu'Anthropic declare epuises, avant le 429.

    Le repos passe par rest_account() : tout le reste du routeur — choix du
    compte, rotation, retour au natif — continue de fonctionner sans savoir
    d'ou vient l'information.
    """
    while True:
        try:
            now = time.time()
            for acc in all_accounts():
                data = fetch_usage(acc["service"], acc.get("swapAccount"))
                if data is None:
                    continue
                spent, until, name, pct = usage_verdict(data)
                if not spent:
                    continue
                until = until or (now + ACCOUNT_COOLDOWN_DEFAULT)
                if until <= now:
                    continue
                # Deja au repos pour au moins aussi longtemps : ne pas
                # reecrire le fichier a chaque tour de sonde.
                if acc["cooldownUntil"] >= until - USAGE_POLL:
                    continue
                rest_account(acc["name"], until, acc)
                log(f"quota annonce a {pct:.0f} % ({name}) sur le compte "
                    f"« {acc['name']} » — repos preventif "
                    f"{max(1, round((until - now) / 60))} min")
        except Exception as e:                      # un thread de fond ne meurt pas
            log(f"sonde de quota: {type(e).__name__}")
        time.sleep(USAGE_POLL)


# --------------------------------------------------------------------------
# OpenRouter : cle, nom de modele, corps de requete
# --------------------------------------------------------------------------

def or_key():
    """Cle OpenRouter — le registre la resout, ici on garde juste le nom court.

    Une seule resolution pour tout le monde : environnement, puis
    ~/.doublure/.env, puis le .env de FCC en dernier ressort.
    """
    return providers.key("open_router")


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
    if not isinstance(name, str) or "/" in name:
        return name
    return (providers.pick("open_router", name, overrides().get("or"))
            or name)


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
    """Alias Claude Code -> modele du fournisseur, via le registre.

    Meme garde-fou que pour OpenRouter : un nom « claude-* » laisse tel quel
    serait au mieux inconnu, au pire facture. Le registre ne laisse passer un
    nom brut que s'il figure deja au catalogue du fournisseur.

    Rend None si le fournisseur n'annonce aucun modele exploitable — c'est le
    cas d'un serveur local allume mais vide. L'appelant passe alors au
    suivant plutot que d'envoyer un nom vide en amont.
    """
    return providers.pick(mode, name, overrides().get(mode))


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
    def bridged(self, mode, path, body, cascade=False):
        """Sert /v1/messages depuis une passerelle qui ne parle qu'OpenAI.

        Rend (servi, echec). `servi` est vrai des qu'une reponse est partie au
        client. En mode `cascade`, un echec ne rend rien au client : c'est
        l'appelant qui essaiera la passerelle suivante, et le message de
        l'utilisateur n'est pas perdu pour autant.
        """
        cfg = bridge_cfg(mode)

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
            self.local(200, {"input_tokens": n})
            return True, None

        if not path.endswith("/messages"):
            # Le reste de l'API Anthropic (modeles, lots...) n'a pas
            # d'equivalent ici. En chaine, une autre passerelle peut savoir le
            # servir : on ne repond 404 que si aucune n'a pu.
            err = {"status": 404, "rest": 0,
                   "detail": f"{path} n'est pas servi par {cfg['label']}"}
            if cascade:
                return False, err
            self.local(404, {"type": "error", "error": {
                "type": "not_found_error",
                "message": f"{path} n'est pas servi par le repli {cfg['label']}."}})
            return True, None

        model = bridge_model(mode, data.get("model"))
        if not model:
            # Fournisseur joignable mais sans catalogue exploitable : un
            # serveur local allume et vide, ou un listage qui ne repond pas.
            # Le tenter enverrait un nom de modele vide.
            err = {"status": 503, "rest": PROVIDER_REST,
                   "detail": f"{cfg['label']} n'annonce aucun modele utilisable"}
            if cascade:
                return False, err
            self.local(503, {"type": "error", "error": {
                "type": "router_error", "message": err["detail"]}})
            return True, None

        stream = bool(data.get("stream"))
        payload = json.dumps(bridge.to_openai(data, model)).encode()
        # Ecarts de dialecte du fournisseur : champ de longueur maximale,
        # plancher exige, champs refuses. Sans ca, une requete par ailleurs
        # correcte se fait refuser par un fournisseur sur six.
        payload = providers.chat_body(mode, payload)

        headers = {
            "Content-Type": "application/json",
            "Content-Length": str(len(payload)),
            "User-Agent": providers.ua(mode),
            "Accept": "text/event-stream" if stream else "application/json",
            "Host": cfg["host"] if cfg["port"] in (80, 443)
                    else "%s:%d" % (cfg["host"], cfg["port"]),
        }
        # La plupart des fournisseurs veulent un porteur ; les trois locaux
        # une chaine fixe ; deux passerelles rien du tout.
        tok = providers.key(mode)
        if tok:
            headers["Authorization"] = "Bearer " + tok

        cls = http.client.HTTPSConnection if cfg["tls"] else http.client.HTTPConnection
        try:
            conn = cls(cfg["host"], cfg["port"], timeout=900)
            conn.request("POST", cfg["path"], body=payload, headers=headers)
            resp = conn.getresponse()
        except Exception as e:
            log(f"{mode} {path} -> injoignable ({type(e).__name__})")
            err = {"status": 502, "rest": PROVIDER_REST_NET,
                   "detail": f"{cfg['label']} injoignable ({type(e).__name__})"}
            if cascade:
                return False, err
            self.local(502, {"type": "error", "error": {
                "type": "router_error", "message": err["detail"]}})
            return True, None

        if resp.status >= 400:
            detail = resp.read(2000).decode("utf-8", "replace")
            try:
                conn.close()
            except Exception:
                pass
            log(f"{mode} {path} -> {resp.status} ({model})")
            # Un 429 ou un 5xx dit que *la passerelle* est indisponible : on la
            # met au repos. Un autre 4xx met en cause la requete elle-meme, et
            # la passerelle suivante rendrait la meme reponse : rien a punir.
            rest = (PROVIDER_REST if resp.status == 429 or resp.status >= 500
                    else 0)
            err = {"status": resp.status, "rest": rest,
                   "detail": f"{cfg['label']} / {model} : {detail[:400]}"}
            if cascade:
                return False, err
            kind = "rate_limit_error" if resp.status == 429 else "api_error"
            self.local(resp.status, {"type": "error", "error": {
                "type": kind, "message": err["detail"]}})
            return True, None

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
            self.local(200, bridge.to_anthropic(upstream,
                                                data.get("model") or model))
            return True, None

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
        return True, None

    # -- chaine de repli ---------------------------------------------------
    def api_served(self, prov, path, body):
        """Repli qui parle deja l'API Anthropic : relais direct, sans pont.

        OpenRouter et le proxy Free Claude Code sont dans ce cas — aucune
        traduction OpenAI a faire, contrairement aux passerelles de
        registre. Meme contrat que bridged() : (servi, echec), et rien n'est
        rendu au client sur un echec — l'appelant essaie le suivant.
        """
        label = "Free Claude Code" if prov == "fcc" else "OpenRouter"
        conn, resp, err = self.direct(prov, body, quiet=True)
        if err:
            return False, err
        if resp.status >= 400:
            try:
                detail = resp.read(2000).decode("utf-8", "replace")
            except Exception:
                detail = ""
            try:
                conn.close()
            except Exception:
                pass
            log(f"{prov} {path} -> {resp.status}")
            rest = (PROVIDER_REST if resp.status == 429 or resp.status >= 500
                    else 0)
            return False, {"status": resp.status, "rest": rest,
                           "detail": f"{label} : {detail[:400]}"}
        self.stream(prov, path, conn, resp)
        return True, None

    def serve_fallback(self, path, body, order, persist=False, due=0):
        """Sert la requete par la premiere passerelle de `order` qui repond.

        La chaine existait deja mais seul son premier maillon etait essaye :
        une passerelle gratuite saturee — le cas le plus courant — rendait son
        429 au client alors que les deux autres auraient repondu. Or c'est
        exactement la promesse de l'outil : le message ne se perd pas.
        """
        live = [p for p in order if not provider_resting(p)]
        if not live:
            # Toutes au repos : mieux vaut retenter que rendre une erreur.
            clear_provider_rest()
            live = list(order)
        first = None
        for prov in live:
            if is_bridged(prov):
                served, err = self.bridged(prov, path, body, cascade=True)
            else:
                served, err = self.api_served(prov, path, body)
            if served:
                # La passerelle qui a repondu devient celle qu'on essaiera en
                # premier : inutile de repayer l'echec a chaque requete.
                if persist and prov != current_mode():
                    set_mode(prov, "auto: quota Claude atteint",
                             due or (time.time() + RETRY_NATIVE_DEFAULT))
                return
            if err.get("rest"):
                rest_provider(prov, err["rest"])
            first = first or err
            if live[-1] != prov:
                log(f"repli {prov} indisponible ({err['status']}) — suivant")

        # Aucune n'a pu servir : on rend l'echec du repli prefere, c'est le
        # plus parlant. Les autres sont dans le journal.
        err = first or {"status": 502, "detail": "aucun repli configure"}
        code = err["status"]
        self.local(code, {"type": "error", "error": {
            "type": "rate_limit_error" if code == 429 else "api_error",
            "message": f"aucun repli disponible — {err['detail']}"}})

    def relay(self):
        path = urllib.parse.urlparse(self.path).path

        # Sonde locale : dit ou partent les requetes, sans en emettre une.
        if path == "/__router":
            mode = current_mode()
            now = time.time()
            accounts = []
            for a in all_accounts():
                row = {"name": a["name"],
                       "resting": a["cooldownUntil"] > now,
                       "restUntil": int(a["cooldownUntil"]) or None}
                if a.get("swapAccount"):
                    # Utile au diagnostic : un compte venu du pool
                    # claude-swap n'apparait dans aucun de nos fichiers.
                    row["source"] = "claude-swap"
                    row["email"] = a.get("email")
                    # L'entree de trousseau, pas son contenu : le CLI en a
                    # besoin pour lire le bon compte dans un service partage.
                    row["swapAccount"] = a["swapAccount"]
                seen = usage_snapshot(a["service"], a.get("swapAccount"))
                if seen:
                    _, _, win, pct = usage_verdict(seen)
                    row["usage"] = round(pct, 1)
                    row["usageWindow"] = win
                accounts.append(row)
            if mode == "native":
                acc = active_account()
                return self.local(200, {
                    "mode": mode,
                    "upstream": "api.anthropic.com",
                    "account": acc["name"],
                    "accounts": accounts,
                    "hasToken": bool(access_token(acc["service"],
                                                  acc.get("swapAccount"))),
                    "providers": provider_rows(),
                })
            if is_bridged(mode):
                cfg = bridge_cfg(mode)
                return self.local(200, {
                    "mode": mode,
                    "label": cfg["label"],
                    "upstream": cfg["host"] + cfg["path"],
                    "accounts": accounts,
                    "hasToken": bool(providers.key(mode))
                                or providers.keyless(mode),
                    "models": providers.tiers(mode, overrides().get(mode)),
                    "providers": provider_rows(),
                })
            if mode == "fcc":
                return self.local(200, {
                    "mode": mode,
                    "label": "Free Claude Code",
                    "upstream": f"127.0.0.1:{FCC_PORT}/v1",
                    "accounts": accounts,
                    "hasToken": fcc_up(),
                    "models": fcc_models(),
                    "providers": provider_rows(),
                })
            return self.local(200, {
                "mode": mode,
                "label": "OpenRouter",
                "upstream": "openrouter.ai/api/v1",
                "accounts": accounts,
                "hasToken": bool(or_key()),
                "models": providers.tiers("open_router",
                                          overrides().get("or")),
                "providers": provider_rows(),
            })

        # Quota affiche par Claude Code (« 5h — % / 7j — % »). Le client
        # interroge ce point d'entree a travers nous : sans reponse il tombe
        # dans la chaine de repli, ou aucun fournisseur ne sait le servir, et
        # l'indicateur reste vide. On le sert depuis le compte natif actif,
        # dont le releve est deja tenu a jour par la sonde de fond.
        if path == "/api/oauth/usage":
            acc = active_account()
            data = usage_snapshot(acc["service"], acc.get("swapAccount"))
            if data is None:
                data = fetch_usage(acc["service"], acc.get("swapAccount"))
            if data is None:
                log("usage: aucun releve disponible")
                return self.local(503, {"type": "error", "error": {
                    "type": "router_error",
                    "message": "releve de quota indisponible"}})
            log(f"usage servi depuis « {acc['name']} »")
            return self.local(200, data)

        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        body = self.rfile.read(length) if length else None

        mode = current_mode()

        # Fin de la fenetre de quota. Le chien de garde ne passe qu'une fois
        # par minute : une requete qui arrive entre-temps n'a aucune raison de
        # partir en repli alors que le natif est redevenu disponible. Le releve
        # de quota est deja en cache, cette verification n'emet rien.
        if mode != "native":
            st = read_state()
            due = st.get("retryNativeAt") or 0
            if (str(st.get("reason", "")).startswith("auto") and due
                    and time.time() >= due and not quota_still_full()):
                set_mode("native", "auto: fenetre de quota ecoulee")
                log("retour au natif (verifie a la requete)")
                mode = "native"

        # Le quota est deja annonce epuise sur *tous* les comptes : le 429 est
        # connu d'avance, aller le chercher ne ferait que perdre un
        # aller-retour. active_account() ne rend un compte au repos que
        # lorsqu'aucun autre n'est libre — c'est exactement ce cas.
        if mode == "native" and active_account()["cooldownUntil"] > time.time():
            with _switch_lock:
                mode = current_mode()          # relu : un autre a pu basculer
                if mode == "native":
                    due = min(a["cooldownUntil"] for a in all_accounts())
                    prov = chain()[0]
                    set_mode(prov, "auto: quota annonce atteint", due)
                    log(f"quota annonce epuise sur tous les comptes -> repli "
                        f"{prov} sans attendre le 429, natif retente dans "
                        f"{max(1, round((due - time.time()) / 60))} min")
                    mode = prov

        if mode != "native":
            # Deja en repli : on part de la passerelle en place, mais la chaine
            # entiere reste disponible si elle ne repond pas.
            st = read_state()
            order = [mode] + [p for p in chain() if p != mode]
            return self.serve_fallback(
                path, body, order,
                persist=str(st.get("reason", "")).startswith("auto"),
                due=st.get("retryNativeAt") or 0)

        account = active_account()
        conn, resp, err = self.direct("native", body, account)
        if err:
            return                       # l'erreur a deja ete rendue au client

        # Jeton refuse. Laisser passer ce 401 afficherait « Please run /login »
        # dans la session, et /login n'y peut rien : depuis qu'elle passe par
        # nous, la session ne porte plus son propre jeton. On force donc un
        # couple neuf et on rejoue une fois — rien n'est encore parti vers le
        # client, la requete est rejouable telle quelle.
        if resp.status == 401:
            _drop(conn, resp)
            log(f"401 sur le compte « {account['name']} » — jeton revoque, "
                f"on en redemande un")
            if access_token(account["service"], account.get("swapAccount"),
                            fresh=True):
                conn, resp, err = self.direct("native", body, account)
                if err:
                    return
            else:
                conn, resp = None, None

        # Quota atteint, ou jeton toujours refuse. C'est le seul endroit ou le
        # repli peut se prendre sans rien perdre : aucun octet n'est encore
        # parti vers le client, donc la meme requete repart ailleurs et le
        # message de l'utilisateur arrive quand meme. Une fois le SSE commence,
        # il est trop tard — on ne rejoue plus, on laisse passer l'erreur.
        if resp is not None and resp.status not in (401, 429):
            return self.stream("native", path, conn, resp)

        if resp is None:
            status, due = 401, time.time() + AUTH_REST
        else:
            status = resp.status
            due = (retry_after(resp) if status == 429
                   else time.time() + AUTH_REST)
            _drop(conn, resp)
        rest_account(account["name"], due, account)
        log(f"{status} sur le compte « {account['name']} » — repos "
            f"{max(1, round((due - time.time()) / 60))} min")

        # Une seule requete mene la rotation. Claude Code en emet plusieurs en
        # parallele : sans ce verrou, chacune brule tous les comptes sur le
        # meme quota et le journal devient illisible.
        held = _switch_lock.acquire()
        try:
            if current_mode() != "native":
                # Une autre requete a deja fait la rotation pendant notre 429 :
                # on suit sa decision au lieu de la refaire.
                mode = current_mode()
                _switch_lock.release()
                held = False
                order = [mode] + [p for p in chain() if p != mode]
                return self.serve_fallback(path, body, order)

            # Un autre compte Claude d'abord : un vrai Opus vaut mieux qu'un
            # modele gratuit, et la requete n'a encore rien envoye au client.
            tried = {account["name"]}
            while True:
                nxt = next_account(tried)
                if nxt is None:
                    break
                set_active_account(nxt["name"])
                log(f"bascule sur le compte « {nxt['name']} »")
                conn, resp, err = self.direct("native", body, nxt)
                if err:
                    return
                if resp.status not in (401, 429):
                    # Le verrou tombe avant de streamer : une generation dure
                    # des minutes, et rien ne doit attendre derriere.
                    _switch_lock.release()
                    held = False
                    return self.stream("native", path, conn, resp)
                nxt_due = (retry_after(resp) if resp.status == 429
                           else time.time() + AUTH_REST)
                try:
                    resp.read()
                except Exception:
                    pass
                conn.close()
                rest_account(nxt["name"], nxt_due, nxt)
                due = min(due, nxt_due)
                tried.add(nxt["name"])
                log(f"{resp.status} aussi sur « {nxt['name']} »")

            # Tous les comptes sont au repos : c'est maintenant que le
            # gratuit sert a quelque chose. La date de retour est celle du
            # premier compte a se liberer.
            set_mode(chain()[0],
                     "auto: jeton Claude refuse" if status == 401
                     else "auto: quota Claude atteint", due)
            log(f"tous les comptes epuises -> repli {chain()[0]}, natif "
                f"retente dans {max(1, round((due - time.time()) / 60))} min")
        finally:
            if held:
                _switch_lock.release()

        # Hors verrou : servir peut prendre le temps d'une generation entiere.
        return self.serve_fallback(path, body, chain(), persist=True, due=due)

    def direct(self, mode, body, account=None, quiet=False):
        """Relaie tel quel vers un amont qui parle l'API Anthropic.

        Rend (connexion, reponse, None), ou (None, None, echec). La reponse
        n'est pas consommee : l'appelant decide encore de la renvoyer ou de
        rejouer ailleurs. Sauf en mode `quiet`, un echec a deja ete rendu au
        client — `quiet` sert la chaine de repli, qui veut essayer la
        passerelle suivante plutot que de conclure.
        """
        path = urllib.parse.urlparse(self.path).path
        if mode == "or":
            host, port, tls = UPSTREAM_OR
        elif mode == "fcc":
            host, port, tls = UPSTREAM_FCC
        else:
            host, port, tls = UPSTREAM_NATIVE
        # Chemin amont : OpenRouter prefixe l'API Anthropic par /api.
        path_up = self.path

        headers = {}
        for key, value in self.headers.items():
            if key.lower() in HOP_BY_HOP:
                continue
            headers[key] = value

        if mode == "native":
            acc = account or active_account()
            token = access_token(acc["service"], acc.get("swapAccount"))
            if not token:
                err = {"status": 503, "rest": 0,
                       "detail": f"aucun jeton OAuth pour le compte "
                                 f"« {acc['name']} » dans le trousseau — lance "
                                 f"`claude` et connecte-toi une fois, ou "
                                 f"`dbl accounts rm {acc['name']}`."}
                if not quiet:
                    self.local(503, {"type": "error", "error": {
                        "type": "router_error", "message": err["detail"]}})
                return None, None, err
            # Le compte actif remplace ce que Claude Code avait mis : c'est
            # tout l'interet du routeur, la session n'a rien a relire.
            headers.pop("x-api-key", None)
            headers.pop("X-Api-Key", None)
            headers["Authorization"] = f"Bearer {token}"
            beta = headers.get("anthropic-beta", "")
            if "oauth-2025-04-20" not in beta:
                headers["anthropic-beta"] = \
                    ("oauth-2025-04-20," + beta) if beta else "oauth-2025-04-20"
        elif mode == "fcc":
            # FCC fait lui-meme la correspondance alias -> modele
            # (MODEL_OPUS, MODEL_SONNET...) : reecrire le nom du modele ici
            # lui retirerait ce reglage. Le corps repart donc tel quel.
            #
            # Le jeton OAuth du compte Claude n'a rien a faire chez lui, et
            # « anthropic-beta: oauth-2025-04-20 » ferait refuser la
            # requete : les deux sautent, remplaces par un marqueur.
            for name in ("x-api-key", "X-Api-Key",
                         "Authorization", "authorization"):
                headers.pop(name, None)
            headers["x-api-key"] = FCC_TOKEN
            beta = (headers.pop("anthropic-beta", None)
                    or headers.pop("Anthropic-Beta", None) or "")
            kept = ",".join(p for p in beta.split(",")
                            if p and "oauth" not in p)
            if kept:
                headers["anthropic-beta"] = kept
        else:
            key = or_key()
            if not key:
                err = {"status": 503, "rest": 0,
                       "detail": "OPENROUTER_API_KEY introuvable dans "
                                 "~/.doublure/.env"}
                if not quiet:
                    self.local(503, {"type": "error", "error": {
                        "type": "router_error",
                        "message": err["detail"] + " — repli impossible."}})
                return None, None, err
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
            err = {"status": 502, "rest": PROVIDER_REST_NET,
                   "detail": f"amont {host}:{port} injoignable "
                             f"({type(e).__name__})"}
            if not quiet:
                self.local(502, {"type": "error", "error": {
                    "type": "router_error", "message": err["detail"]}})
            return None, None, err
        return conn, resp, None

    def stream(self, mode, path, conn, resp):
        """Renvoie la reponse amont au client, au fil de l'eau."""
        log(f"{mode} {self.command} {path} -> {resp.status}")

        # Une reponse sans corps ne doit pas repartir en chunked : annoncer
        # un corps sur un HEAD, un 204 ou un 304 laisse le client attendre
        # quelque chose qui n'arrivera jamais.
        bodyless = self.command == "HEAD" or resp.status in (204, 304)

        # On reforme la reponse : le corps repart en chunked pour que le SSE
        # sorte au fil de l'eau, sans attendre la fin de la generation.
        self.send_response(resp.status)
        for key, value in resp.getheaders():
            # Content-Encoding est conserve : http.client ne decompresse pas,
            # le corps repart tel quel et le client le decode lui-meme.
            if key.lower() in HOP_BY_HOP:
                continue
            self.send_header(key, value)
        if not bodyless:
            self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        if bodyless:
            try:
                resp.read()
            except Exception:
                pass
            try:
                conn.close()
            except Exception:
                pass
            return

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
    threading.Thread(target=usage_watch, daemon=True).start()
    threading.Thread(target=catalog_watch, daemon=True).start()
    log(f"routeur pret sur http://{HOST}:{PORT} (mode {current_mode()})")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
