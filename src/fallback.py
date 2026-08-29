#!/usr/bin/env python3
"""Repli de Claude Code sur des modeles gratuits quand le quota est atteint.

Claude Code n'a pas de plan B : quand la fenetre de quota est pleine, la
session s'arrete net jusqu'a la reprise. Ce module en donne un — une
quarantaine de fournisseurs de modeles gratuits, tenus par providers.py — et
le rend automatique.

Le montage tient en deux pieces :

  router.py   un routeur local (127.0.0.1:8099) sur lequel `settings.json`
              pointe en permanence. C'est lui qui decide, *a chaque requete*,
              d'aller chez Anthropic ou chez une passerelle gratuite.
  ce module   l'etat, l'affichage et les bascules manuelles.
  providers.py le registre des fournisseurs : cles, catalogues, sante,
              correspondance des paliers. Doublure ne delegue plus ce travail.

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
import socket
import urllib.error
import urllib.parse
import urllib.request

# Pose a cote de ce fichier par l'installeur, comme router.py et bridge.py.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import providers  # noqa: E402  (le chemin doit etre pose avant l'import)
import statefile  # noqa: E402

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
# Free Claude Code, installe en local : proxy qui parle l'API Anthropic et
# couvre une cinquantaine de fournisseurs. Son catalogue et sa propre chaine
# de repli sont entretenus chez lui, pas ici.
FCC_BASE = "http://127.0.0.1:%s" % os.environ.get("FCC_PORT", "8082")
FCC_ENV = os.path.join(HOME, ".fcc", ".env")
FCC_TOKEN = "doublure"
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
    """Modifie l'etat champ par champ, sous le verrou partage avec le routeur.

    Le routeur ecrit le meme fichier depuis un autre processus. Lire et
    reecrire sans verrou effacait sa derniere decision — une bascule
    automatique annulee par un simple changement de compte.
    """
    with statefile.file_lock():
        cur = state()
        cur.update(kw)
        statefile.write_json(STATE_FILE, cur)
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
    # ~/.claude peut ne pas exister : sur une machine ou `claude` n'a jamais
    # tourne, l'installation echouait ici avec un FileNotFoundError.
    os.makedirs(os.path.dirname(SETTINGS), exist_ok=True)
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


def router_usage(timeout=2):
    """Quota par compte, tel que le routeur l'a vu. {} si hors ligne.

    C'est lui qui interroge /api/oauth/usage en tache de fond : la CLI lit son
    releve plutot que d'en emettre un de plus.
    """
    try:
        with urllib.request.urlopen(f"{ROUTER_BASE}/__router",
                                    timeout=timeout) as r:
            data = json.loads(r.read().decode())
    except Exception:
        return {}
    out = {}
    for row in data.get("accounts") or ():
        if isinstance(row, dict) and row.get("usage") is not None:
            out[row.get("name")] = (row["usage"], row.get("usageWindow"))
    return out


def router_pool(timeout=2):
    """Comptes du pool claude-swap, vus par le routeur.

    Le routeur est la seule autorite : c'est lui qui lit le cache de
    claude-swap, ecarte les slots dont l'entree de trousseau a disparu et
    reconnait celui qui est deja le compte de la session. Recopier ce tri ici
    donnerait deux verites qui divergent des que l'une bouge.
    """
    try:
        with urllib.request.urlopen(f"{ROUTER_BASE}/__router",
                                    timeout=timeout) as r:
            data = json.loads(r.read().decode())
    except Exception:
        return []
    out = []
    for row in data.get("accounts") or ():
        if isinstance(row, dict) and row.get("source") == "claude-swap":
            out.append({"name": row.get("name"),
                        "service": "claude-swap",
                        "swapAccount": row.get("swapAccount"),
                        "cooldownUntil": float(row.get("restUntil") or 0)})
    return out


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
    if usable(cur):
        return cur
    # « fcc » designait autrefois OpenRouter ; c'est desormais un vrai
    # fournisseur (le proxy local), donc rendu par le test ci-dessus.
    return "native"


# --------------------------------------------------------------------------
# Fournisseurs de repli
# --------------------------------------------------------------------------
# Le registre est dans providers.py : les fournisseurs, leurs cles, leur
# catalogue de modeles et leur sante. Ce fichier n'en garde que ce que la
# ligne de commande affiche.
#
# Une seule entree n'est pas dans le registre : Free Claude Code. Ce n'est
# pas un fournisseur mais un proxy local qui parle deja l'API Anthropic —
# donc un maillon de plus, en fin de chaine, jamais un fondement. S'il n'est
# pas lance, il disparait de la chaine sans erreur.

ALIASES = providers.TIERS
FCC_LABEL = "Free Claude Code"

# Noms courts d'avant le registre — la meme table que le routeur, pour que
# `dbl on zen` continue de marcher et qu'un etat ancien pointe au bon endroit.
# « open_router » y renvoie vers « or » : OpenRouter parle l'API Anthropic
# nativement, donc plus fidelement que par la traduction.
ALIAS_SHORT = {"zen": "opencode_zen", "go": "opencode_go", "nim": "nvidia_nim",
               "open_router": "or", "openrouter": "or"}


def resolve(pid):
    return ALIAS_SHORT.get(pid, pid) if isinstance(pid, str) else pid


def reg(pid):
    """Id du registre derriere un mode. « or » est servi en API Anthropic par
    le routeur, mais c'est le meme fournisseur au bout du fil."""
    return "open_router" if pid == "or" else pid

# Cloudflare refuse « Python-urllib » devant plusieurs passerelles : sans
# agent explicite, la sonde revient en 403 sans rapport avec le service.
UA = providers.UA


def usable(pid):
    """`pid` designe-t-il un fournisseur que doublure sait servir ?"""
    if pid == "fcc":
        return True
    pid = reg(pid)
    return pid in providers.CATALOG and providers.usable(pid)


def label(pid):
    if pid == "fcc":
        return FCC_LABEL
    return providers.label(reg(pid))


def base(pid):
    if pid == "fcc":
        return FCC_BASE
    return (providers.CATALOG.get(reg(pid)) or {}).get("base") or ""


def needs_key(pid):
    """Ce fournisseur reclame-t-il une cle qui n'est pas encore posee ?"""
    pid = reg(pid)
    if pid == "fcc" or pid not in providers.CATALOG:
        return False
    cfg = providers.CATALOG[pid]
    if cfg.get("keyless") or cfg.get("local"):
        return False
    return not providers.key(pid)


def fcc_up(ttl=30):
    """Le proxy Free Claude Code ecoute-t-il ? Sonde TCP, en cache 30 s."""
    now = time.time()
    if now - _fcc_probe["at"] < ttl:
        return _fcc_probe["up"]
    url = urllib.parse.urlsplit(FCC_BASE)
    up = False
    try:
        socket.create_connection((url.hostname or "127.0.0.1",
                                  url.port or 8082), timeout=0.4).close()
        up = True
    except OSError:
        pass
    _fcc_probe.update(at=now, up=up)
    return up


_fcc_probe = {"at": 0.0, "up": False}


def fcc_models():
    """Table palier -> modele telle que FCC la sert, lue dans son .env.

    Figer ces noms ici les condamnerait a mentir : c'est le reglage de FCC
    (MODEL_OPUS, MODEL_SONNET...) qui decide, et il change depuis son propre
    tableau de bord.
    """
    out, default = {}, ""
    try:
        with open(FCC_ENV) as fh:
            for line in fh:
                line = line.strip()
                if line.startswith("MODEL="):
                    default = line[len("MODEL="):].strip()
                    continue
                for alias in ALIASES:
                    pref = "MODEL_%s=" % alias.upper()
                    if line.startswith(pref):
                        val = line[len(pref):].strip()
                        if val and val != "None":
                            out[alias] = val
    except OSError:
        pass
    return {a: out.get(a) or default or "(non configure)" for a in ALIASES}


_FREE_ID = re.compile(r"[-:/]free$", re.I)


def _pretty(mid, name=None):
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


def _fetch_fcc():
    """Catalogue de FCC : « fournisseur/modele », deja filtre par lui.

    Il n'expose pas /models mais son API d'administration, et ses entrees sont
    des identifiants nus — le fournisseur sert d'indication.
    """
    try:
        req = urllib.request.Request(FCC_BASE + "/admin/api/models",
                                     headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read().decode()).get("models") or []
    except Exception:
        return []
    out = []
    for mid in data:
        mid = str(mid)
        if not mid:
            continue
        prov, _, rest = mid.partition("/")
        out.append((mid, _pretty(rest or mid), prov))
    out.sort(key=lambda e: (e[2], e[1].lower()))
    return out


def catalog(pid, refresh=False):
    """Modeles proposables pour `pid` : (id, nom, indication).

    Le registre tient le cache disque et le rafraichit en fond ; ici on ne
    fait que l'habiller pour l'affichage.
    """
    if pid == "fcc":
        # FCC prefixe ses modeles par le fournisseur (« nim/meta/llama-... ») ;
        # la note se lit quand meme sur l'identifiant, donc le meme filtre
        # ecarte ici aussi plongements, rerank, image et audio.
        return [e for e in _fetch_fcc() if providers._score(e[0]) > 0]
    pid = reg(pid)
    if pid not in providers.CATALOG:
        return []
    # Meme regle que le registre : quand un fournisseur publie des variantes
    # gratuites, on ne propose que celles-la. Offrir le jumeau payant du meme
    # modele ferait facturer un repli dont tout l'interet est d'etre gratuit.
    pool = providers._free_only(providers.models(pid, refresh))
    # Les catalogues melangent generation et le reste : plongements, rerank,
    # image, audio. Rien de tout ca ne peut tenir une session Claude Code, et
    # les proposer noierait les modeles qui le peuvent.
    out = [(mid, _pretty(mid), "") for mid in pool if providers._score(mid) > 0]
    out.sort(key=lambda e: e[1].lower())
    return out


def refresh_catalog():
    """Relit la liste des modeles chez chaque fournisseur de la chaine."""
    return {pid: len(catalog(pid, refresh=True)) for pid in chain()}


def models_for(pid):
    """Table palier -> modele du fournisseur, surcharges appliquees.

    Le routeur relit la meme surcharge a chaud : changer un modele prend effet
    au message suivant, sans redemarrer ni rouvrir de session.
    """
    if pid == "fcc":
        return fcc_models()
    over = (state().get("models") or {}).get(pid)
    pid = reg(pid)
    if pid not in providers.CATALOG:
        return {a: "(inconnu)" for a in ALIASES}
    table = providers.tiers(pid, over if isinstance(over, dict) else None)
    return {a: table.get(a) or "(aucun)" for a in ALIASES}


def set_model(pid, alias, ref):
    """Fixe le modele servi par `pid` pour `alias`. Renvoie (ok, detail)."""
    pid = resolve(pid)
    if not usable(pid) or pid == "fcc":
        return False, f"fournisseur inconnu : {pid}"
    if alias not in ALIASES:
        return False, f"palier inconnu : {alias}"
    names = {r: lab for r, lab, *_ in catalog(pid)}
    if ref not in names:
        return False, "modele hors catalogue"
    over = dict(state().get("models") or {})
    tbl = dict(over.get(pid) or {})
    tbl[alias] = ref
    over[pid] = tbl
    set_state(models=over)
    return True, f"{label(pid)} — {alias} sert {names[ref]}"


def reset_models(pid):
    """Rend au fournisseur ses modeles deduits du catalogue."""
    pid = resolve(pid)
    over = dict(state().get("models") or {})
    if over.pop(pid, None) is None:
        return False, "aucune surcharge a annuler"
    set_state(models=over)
    return True, f"{label(pid)} : modeles par defaut retablis"


def chain():
    """Ordre d'essai des fournisseurs, personnalisable via l'etat.

    Elle est deduite du registre : tout fournisseur dont la cle est posee (ou
    qui n'en demande pas, ou qui tourne en local) y entre, dans l'ordre de
    preference. FCC ferme la marche quand il ecoute.
    """
    order, seen = [], set()
    for pid in [resolve(p) for p in providers.configured()]:
        if pid in seen:
            continue
        seen.add(pid)
        if pid != "or" or providers.key("open_router"):
            order.append(pid)
    if fcc_up():
        order.append("fcc")
    custom = state().get("chain")
    if isinstance(custom, list):
        custom = [resolve(p) for p in custom if isinstance(p, str)]
        keep = [p for p in custom if p in order]
        if keep:
            return keep + [p for p in order if p not in keep]
    return order or ["opencode_zen"]


def or_quota(timeout=15):
    """Plafond restant du palier gratuit OpenRouter : (restant, total, reset).

    OpenRouter n'expose x-ratelimit-* que sur un 429. Tant qu'il reste du
    quota, on ne sait donc pas combien : (None, None, 0) veut dire « pas
    encore epuise », pas « inconnu et inquietant ».
    """
    key = providers.key("open_router")
    model = providers.pick("open_router", "haiku")
    if not key or not model:
        return None, None, 0
    payload = json.dumps({
        "model": model,
        "max_tokens": 1,
        "messages": [{"role": "user", "content": "."}],
    }).encode()
    req = urllib.request.Request(
        f"{OR_BASE}/messages", data=payload, method="POST",
        headers={"Authorization": f"Bearer {key}",
                 "Content-Type": "application/json",
                 "User-Agent": UA,
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


def _check_fcc(timeout):
    """FCC parle l'API Anthropic : /v1/messages, pas /chat/completions.

    Une vraie generation reste la seule preuve qu'il sert — un modele arrive
    en fin de vie rend un 410 sans que rien d'autre ne bouge (vu le
    2026-08-26 sur llama-3.3-nemotron-super-49b).
    """
    payload = json.dumps({
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 64,
        "messages": [{"role": "user", "content": "dis OK"}],
    }).encode()
    req = urllib.request.Request(
        FCC_BASE + "/v1/messages", data=payload, method="POST",
        headers={"Content-Type": "application/json",
                 "x-api-key": FCC_TOKEN,
                 "anthropic-version": "2023-06-01",
                 "User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read().decode())
            det = (body.get("error") or {}).get("message") or ""
        except Exception:
            det = ""
        head = det.splitlines()[0][:120] if det else ""
        return False, ("FCC refuse (HTTP %d) %s" % (e.code, head)).strip()
    except Exception as e:
        return False, ("FCC injoignable (%s) — lance `fcc-server` ou son "
                       "application" % type(e).__name__)
    return True, "FCC sert %s" % fcc_models()["haiku"]


def check(pid, timeout=25, force=False):
    """(ok, detail) — ce fournisseur peut-il servir une requete maintenant ?

    Une vraie generation avec un outil, pas un ping : c'est le seul moyen de
    distinguer « joignable » de « sert Claude Code », et c'est ce qui manquait
    au repli precedent, qui basculait puis echouait en vol. Le releve est
    garde par le registre — une sonde coute une requete, on ne la repaie pas a
    chaque affichage. `timeout` n'est utilise que pour FCC : le registre a son
    propre delai, un gros modele gratuit pouvant mettre une minute.
    """
    if pid == "fcc":
        return _check_fcc(timeout)
    model = models_for(pid).get("haiku")
    pid = reg(pid)
    if pid not in providers.CATALOG:
        return False, f"fournisseur inconnu : {pid}"
    why = (providers.CATALOG[pid].get("special") or "").strip()
    if why:
        return False, f"{label(pid)} : {why}"
    if needs_key(pid):
        env = providers.CATALOG[pid].get("env") or "la cle"
        return False, f"{label(pid)} : {env} absente de ~/.doublure/.env"
    if providers.CATALOG[pid].get("local") and not providers._local_up(pid):
        return False, f"{label(pid)} n'ecoute pas en local"
    if not model or model.startswith("("):
        return False, f"{label(pid)} n'annonce aucun modele utilisable"
    rep = providers.probe(pid, model, force=force)
    code = rep.get("code") or 0
    if code == 429:
        return False, f"{label(pid)} : plafond atteint (HTTP 429)"
    if not rep.get("ok"):
        det = (rep.get("error") or "").splitlines()[0][:120]
        return False, f"{label(pid)} refuse ({code or 'reseau'}) {det}".strip()
    if not rep.get("tools"):
        # Claude Code ne sait rien faire sans tool_use : un modele qui rend du
        # beau texte mais ignore les outils bloquerait a la premiere lecture
        # de fichier. Mieux vaut le declarer inapte tout de suite.
        return False, f"{label(pid)} : {model} ignore les outils"
    return True, "%s sert %s (%d ms)" % (label(pid), _pretty(model),
                                         rep.get("ms") or 0)


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
    order = chain()
    return check(cur if usable(cur) else (order[0] if order else "fcc"), timeout)


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
        provider = resolve(provider)
        if not usable(provider):
            return False, f"fournisseur inconnu : {provider}"
        ok, detail = check(provider, force=True)
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


def peek(pid):
    """Ce que l'on sait deja d'un fournisseur, sans depenser une requete.

    Rend (ok, detail) ou (None, detail) quand rien n'a encore ete sonde. Le
    tableau de bord liste toute la chaine : la sonder entierement couterait
    une generation par fournisseur a chaque affichage, alors qu'un releve de
    la semaine passee dit la meme chose.
    """
    if pid == "fcc":
        return (True, "FCC ecoute") if fcc_up() else (False, "FCC n'ecoute pas")
    model = models_for(pid).get("haiku")
    pid = reg(pid)
    if pid not in providers.CATALOG:
        return False, f"fournisseur inconnu : {pid}"
    why = (providers.CATALOG[pid].get("special") or "").strip()
    if why:
        return False, f"{label(pid)} : {why}"
    if needs_key(pid):
        env = providers.CATALOG[pid].get("env") or "la cle"
        return False, f"{env} absente de ~/.doublure/.env"
    if not model or model.startswith("("):
        return False, "aucun modele utilisable annonce"
    rep = providers.health(pid).get(model)
    if not isinstance(rep, dict):
        return None, f"{_pretty(model)} — pas encore sonde"
    if rep.get("ok") and rep.get("tools"):
        return True, "%s (%d ms)" % (_pretty(model), rep.get("ms") or 0)
    if rep.get("ok"):
        return False, f"{_pretty(model)} ignore les outils"
    det = (rep.get("error") or "").splitlines()[0][:90]
    return False, f"refus {rep.get('code') or 'reseau'} {det}".strip()


def summary():
    """Etat complet pour l'affichage du dashboard."""
    st = state()
    cur = mode()
    order = chain()

    # Le fournisseur actif est le seul reellement sonde : c'est celui dont
    # l'etat compte. Les autres sont rendus depuis le releve de sante, deja
    # sur le disque — un tableau de bord ne doit pas declencher quarante
    # generations pour s'afficher.
    active_id = cur if usable(cur) else (order[0] if order else "fcc")
    rows, first_ok = [], None
    for pid in [active_id] + [p for p in order if p != active_id]:
        ok, detail = check(pid) if pid == active_id else peek(pid)
        if ok and first_ok is None:
            first_ok = pid
        rows.append({
            "id": pid,
            "label": label(pid),
            "base": base(pid),
            "needsKey": needs_key(pid),
            "ok": ok,
            "detail": detail,
            "active": pid == cur,
            "probed": pid == active_id,
            "models": models_for(pid),
            "catalog": list(catalog(pid)) if pid == active_id else [],
            "custom": bool((st.get("models") or {}).get(pid)),
        })

    active_models = models_for(active_id)
    remaining, total, reset = or_quota() if active_id == "or" else (None, None, 0)
    refs = sorted({r for r in active_models.values() if not r.startswith("(")})
    alive = bool(rows and rows[0]["ok"])
    detail = rows[0]["detail"] if rows else ""

    return {
        "mode": cur,
        "provider": cur if usable(cur) else "",
        "providers": rows,
        "chain": order,
        "auto": bool(st.get("auto")),
        "since": st.get("since") or 0,
        "reason": st.get("reason") or "",
        "lastError": st.get("lastError") or "",
        "alive": alive,
        "aliveDetail": detail,
        "upstream": base(active_id),
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


# --------------------------------------------------------------------------
# Comptes Claude — plusieurs abonnements, rotation avant le gratuit
# --------------------------------------------------------------------------
# Le routeur fait la rotation ; ici on ne fait que nommer les comptes. Ajouter
# un compte = copier l'entree du trousseau que Claude Code vient d'ecrire, sous
# un nom a nous. Rien n'est stocke en clair : le fichier accounts.json ne
# contient que des noms et des dates de repos.

KEYCHAIN_SERVICE = "Claude Code-credentials"
KEYCHAIN_PREFIX = "Doublure-"
ACCOUNTS_FILE = os.path.join(DBL_DIR, "accounts.json")
NAME_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$")


def keychain_read(service, account=None):
    """Lit une entree du trousseau. `account` vise une entree precise dans un
    service partage : claude-swap y range tous ses comptes sous le meme nom de
    service."""
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


def keychain_write(service, blob):
    return subprocess.run(
        ["security", "add-generic-password", "-U", "-s", service,
         "-a", os.environ.get("USER", "claude"), "-w", json.dumps(blob)],
        capture_output=True, text=True, timeout=10).returncode == 0


def keychain_delete(service):
    subprocess.run(["security", "delete-generic-password", "-s", service],
                   capture_output=True, text=True, timeout=10)


def accounts():
    try:
        with open(ACCOUNTS_FILE) as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return []
    out = []
    for item in (data.get("accounts") or []) if isinstance(data, dict) else []:
        if isinstance(item, dict) and item.get("name"):
            row = {"name": str(item["name"]),
                   "service": item.get("service")
                   or KEYCHAIN_PREFIX + str(item["name"]),
                   "cooldownUntil": float(item.get("cooldownUntil") or 0)}
            if item.get("swapAccount"):
                row["swapAccount"] = str(item["swapAccount"])
            out.append(row)
    return out


def save_accounts(items):
    with statefile.file_lock():
        statefile.write_json(ACCOUNTS_FILE, {"accounts": items})


def account_label(blob):
    """Ce que l'entree du trousseau dit du compte, pour s'y reconnaitre."""
    oauth = (blob or {}).get("claudeAiOauth") or {}
    bits = []
    if oauth.get("subscriptionType"):
        bits.append(str(oauth["subscriptionType"]))
    exp = (oauth.get("expiresAt") or 0) / 1000.0
    if exp:
        left = int((exp - time.time()) / 60)
        bits.append(f"jeton {left} min" if left > 0 else "jeton expire")
    return ", ".join(bits) or "?"


def accounts_add(name):
    """Enregistre le compte connecte *maintenant* sous ce nom.

    Marche a marche : `claude` puis `/login` avec le second compte, puis
    `dbl accounts add <nom>`. On copie ce que Claude Code a ecrit, on ne
    refait pas l'OAuth.
    """
    if not name or not NAME_OK.match(name):
        return 1, ("nom attendu : lettres, chiffres, . _ - "
                   "(ex. `dbl accounts add perso`)")
    if name == "claude":
        return 1, "« claude » designe deja le compte de la session"
    blob = keychain_read(KEYCHAIN_SERVICE)
    if not blob:
        return 1, ("aucun compte connecte dans le trousseau — lance `claude` "
                   "et connecte-toi, puis reessaie")
    service = KEYCHAIN_PREFIX + name
    # Relire et reecrire sous le meme verrou : deux `dbl accounts` concurrents
    # perdaient l'un des deux comptes.
    with statefile.file_lock():
        items = accounts()
        for acc in items:
            if acc["name"] == name:
                acc["cooldownUntil"] = 0
                break
        else:
            items.append({"name": name, "service": service,
                          "cooldownUntil": 0})
        if not keychain_write(service, blob):
            return 1, "le trousseau a refuse l'ecriture"
        save_accounts(items)
    return 0, f"compte « {name} » enregistre ({account_label(blob)})"


def accounts_rm(name):
    with statefile.file_lock():
        items = accounts()
        keep = [a for a in items if a["name"] != name]
        if len(keep) == len(items):
            return 1, f"aucun compte « {name} »"
        keychain_delete(KEYCHAIN_PREFIX + name)
        save_accounts(keep)
    st = state()
    if st.get("account") == name:
        # Sinon l'etat pointerait un compte disparu ; sans clef « account »,
        # le routeur reprend le premier libre.
        set_state(account=None)
    return 0, f"compte « {name} » retire (trousseau compris)"


def accounts_use(name):
    """Force le compte a utiliser. « auto » rend la main a la rotation."""
    if name in (None, "auto"):
        set_state(account=None)
        return 0, "choix du compte : automatique"
    pool = {a["name"]: a for a in router_pool()}
    known = ["claude"] + list(pool) + [a["name"] for a in accounts()]
    if name not in known:
        return 1, f"aucun compte « {name} » — connus : {', '.join(known)}"
    if name in pool:
        service = "claude-swap"
        entry = pool[name].get("swapAccount")
    else:
        service = (KEYCHAIN_SERVICE if name == "claude"
                   else KEYCHAIN_PREFIX + name)
        entry = None
    if not keychain_read(service, entry):
        return 1, f"le trousseau n'a plus l'entree du compte « {name} »"
    set_state(account=name)
    return 0, f"compte actif : {name} (immediat, sans relancer la session)"


def accounts_list():
    """Tous les comptes, dans l'ordre d'essai du routeur."""
    now = time.time()
    want = state().get("account")
    rows = [{"name": "claude", "service": KEYCHAIN_SERVICE, "cooldownUntil": 0}]
    rows += router_pool()
    known = {r["name"] for r in rows}
    for acc in accounts():
        if acc["service"] == KEYCHAIN_SERVICE or acc["name"] == "claude":
            rows[0]["cooldownUntil"] = acc["cooldownUntil"]
            continue
        if acc["name"] in known:
            # Un compte du pool mis au repos a une ligne dans accounts.json :
            # c'est le repos qui compte, pas une seconde entree.
            for r in rows:
                if r["name"] == acc["name"]:
                    r["cooldownUntil"] = acc["cooldownUntil"]
            continue
        known.add(acc["name"])
        rows.append(acc)
    # Le compte servi maintenant : celui demande s'il est libre, sinon le
    # premier qui l'est — meme regle que le routeur.
    active = None
    for row in rows:
        if row["name"] == want and row["cooldownUntil"] <= now:
            active = row["name"]
            break
    if active is None:
        for row in rows:
            if row["cooldownUntil"] <= now:
                active = row["name"]
                break
    seen = router_usage()
    out = []
    for row in rows:
        blob = keychain_read(row["service"], row.get("swapAccount"))
        rest = row["cooldownUntil"]
        if not blob:
            etat = "trousseau vide"
        elif rest > now:
            etat = f"repos {int((rest - now) / 60) + 1} min"
        else:
            etat = "pret"
        bits = []
        if row["name"] in seen:
            pct, win = seen[row["name"]]
            bits.append(f"quota {pct:.0f} %" + (f" ({win})" if win else ""))
        if row.get("service") == "claude-swap":
            bits.append("claude-swap")
        bits.append(account_label(blob))
        out.append(f"{'*' if row['name'] == active else ' '} "
                   f"{row['name']:<14s} {etat:<16s} {' — '.join(bits)}")
    return out


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
    elif cmd in ("accounts", "account"):
        sub = arg or "list"
        who = sys.argv[3] if len(sys.argv) > 3 else None
        if sub == "add":
            code, note = accounts_add(who)
        elif sub in ("rm", "remove", "del"):
            code, note = accounts_rm(who)
        elif sub == "use":
            code, note = accounts_use(who)
        elif sub in ("list", "ls"):
            print("\n".join(accounts_list()))
            code, note = 0, None
        else:
            code, note = 1, ("usage : dbl accounts [list|add <nom>|"
                             "rm <nom>|use <nom|auto>]")
        if note:
            print(note)
        sys.exit(code)
    elif cmd == "probe":
        # Sans argument : le premier de la chaine qui repond, comme avant.
        # Avec : on force la sonde d'un fournisseur precis, releve de sante
        # perime compris — c'est le seul moyen de rendre son verdict a un
        # fournisseur mis de cote une heure plus tot.
        if arg:
            ok, note = check(resolve(arg), force=True)
            print(note)
            sys.exit(0 if ok else 1)
        print(ensure_models(force_probe=True)[1])
    elif cmd in ("providers", "prov"):
        order = chain()
        for pid in providers.PREFERENCE + ("or", "fcc"):
            if pid in ("open_router",) or not usable(pid):
                continue
            mark = "*" if pid == mode() else ("+" if pid in order else " ")
            if pid == "fcc":
                note = "ecoute" if fcc_up() else "arrete"
            elif needs_key(pid):
                note = "cle absente : " + (providers.CATALOG[reg(pid)]
                                           .get("env") or "?")
            elif (providers.CATALOG[reg(pid)].get("special") or ""):
                note = providers.CATALOG[reg(pid)]["special"]
            elif providers.CATALOG[reg(pid)].get("local"):
                # Un serveur local n'a pas de cle a poser : ce qui decide,
                # c'est qu'il tourne. Le dire « pret » quand il est eteint
                # ferait chercher une cle qui n'existe pas.
                if not providers._local_up(reg(pid)):
                    note = "n'ecoute pas (%s)" % providers.CATALOG[reg(pid)]["base"]
                elif not providers.models(reg(pid)):
                    note = "ecoute, aucun modele installe"
                else:
                    note = "ecoute"
            else:
                note = "pret"
            print(f"{mark} {pid:14s} {label(pid):22s} {note}")
        print("\n* sert maintenant   + dans la chaine de repli")
    elif cmd == "key":
        # La cle est ecrite dans ~/.doublure/.env en 0600. Sans valeur, on dit
        # seulement si elle est posee : l'afficher serait la publier dans
        # l'historique du terminal.
        who = resolve(arg) if arg else None
        val = sys.argv[3] if len(sys.argv) > 3 else None
        if not who or not usable(who) or who == "fcc":
            print("usage : dbl key <fournisseur> [cle]")
            sys.exit(1)
        if not val:
            print(f"{label(who)} : cle "
                  f"{'posee' if providers.key(reg(who)) else 'absente'}")
            sys.exit(0)
        providers.set_key(reg(who), val)
        ok, note = check(who, force=True)
        print(f"{label(who)} — {note}")
        sys.exit(0 if ok else 1)
    elif cmd == "import-fcc":
        got = providers.import_fcc_keys()
        if not got:
            print("aucune cle a reprendre dans ~/.fcc/.env")
        else:
            print("reprises : " + ", ".join(sorted(got)))
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
    elif cmd == "model":
        # Surcharger un palier, ou rendre au fournisseur ses modeles deduits.
        # Le routeur relit l'etat a chaud : la surcharge prend au message
        # suivant, sans rouvrir de session.
        alias = sys.argv[3] if len(sys.argv) > 3 else None
        ref = sys.argv[4] if len(sys.argv) > 4 else None
        if not arg or not alias:
            print("usage : dbl model <fournisseur> <opus|sonnet|fable|haiku> "
                  "<modele>\n        dbl model <fournisseur> reset")
            sys.exit(1)
        if alias == "reset":
            ok, note = reset_models(arg)
        elif not ref:
            print("modele manquant — dbl models %s pour le catalogue" % arg)
            sys.exit(1)
        else:
            ok, note = set_model(arg, alias, ref)
        print(note)
        sys.exit(0 if ok else 1)
    elif cmd == "models":
        # Avec un fournisseur : son catalogue complet, pas seulement les
        # quatre paliers — c'est ce qu'il faut pour choisir une surcharge.
        if arg:
            pid = resolve(arg)
            if not usable(pid):
                print(f"fournisseur inconnu : {arg}")
                sys.exit(1)
            for alias, ref in models_for(pid).items():
                print(f"{alias:7s} {ref}")
            cat = catalog(pid)
            print(f"\n{len(cat)} modeles servis par {label(pid)}")
            for mid, name, hint in cat:
                print(f"  {mid:52s} {name} {hint}".rstrip())
            sys.exit(0)
        for pid in chain():
            mark = "*" if pid == mode() else " "
            print(f"{mark} {pid:14s} {label(pid)}")
            for alias, ref in models_for(pid).items():
                print(f"      {alias:7s} {ref}")
    else:
        st = state()
        cur = mode()
        if cur == "native":
            print("mode    native — comptes Claude")
        else:
            print(f"mode    repli {cur} ({label(cur)})")
            for alias, ref in models_for(cur).items():
                print(f"        {alias:7s} {ref}")
        # Les comptes sont montres dans les deux cas : en repli, c'est la
        # seule facon de voir quand un vrai compte redevient disponible.
        for line in accounts_list():
            print(f"compte {line}")
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
