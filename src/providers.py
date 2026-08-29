#!/usr/bin/env python3
"""Fournisseurs, cles, catalogue de modeles et sante — en propre.

Doublure deleguait tout ca a Free Claude Code : il porte une cinquantaine de
fournisseurs, entretient son catalogue et suit la sante de chaque modele. Le
prix de ce confort etait une dependance — FCC arrete ou desinstalle, et la
moitie de la chaine gratuite disparaissait avec lui.

Ce module reprend le travail ici : le registre des fournisseurs (identifiant,
base, variable de cle), la lecture des cles, la decouverte des modeles
reellement servis, leur sante mesuree, et le choix du modele derriere chaque
palier Claude Code (opus / sonnet / fable / haiku).

Rien ici n'a besoin de FCC. S'il tourne, il reste un maillon de plus dans la
chaine, pas son fondement.

Aucune dependance : uniquement la bibliotheque standard, comme le reste.
"""

import http.client
import json
import os
import re
import threading
import time
import urllib.parse

HOME = os.path.expanduser("~")
DBL_DIR = os.path.join(HOME, ".doublure")
ENV_FILE = os.path.join(DBL_DIR, ".env")
# Le .env de Free Claude Code : lu en source de cles, jamais ecrit. Qui a
# deja configure FCC n'a rien a resaisir.
FCC_ENV = os.path.join(HOME, ".fcc", ".env")
CATALOG_FILE = os.path.join(DBL_DIR, "catalog.json")
HEALTH_FILE = os.path.join(DBL_DIR, "health.json")

# Cloudflare et quelques autres refusent « Python-urllib » : un agent
# explicite evite un 403 qui n'a rien a voir avec la requete.
UA = "doublure/1.0"

CATALOG_TTL = 6 * 3600      # un catalogue de modeles bouge en jours, pas en minutes
HEALTH_TTL_OK = 7 * 86400   # un modele qui marche continue de marcher
HEALTH_TTL_BAD = 3600       # un modele casse peut revenir : on retente dans l'heure
LIST_TIMEOUT = 12
PROBE_TIMEOUT = 90          # un gros modele gratuit peut mettre une minute


# --------------------------------------------------------------------------
# Registre
# --------------------------------------------------------------------------
# Porte du catalogue de Free Claude Code (config/provider_catalog.py) : les
# bases, les noms de variables de cle et l'ordre de preference viennent de la.
# Les champs en plus decrivent ce que doublure doit savoir pour appeler
# l'amont sans son code : chemin de listage, forme de la reponse, et les
# ecarts de dialecte OpenAI releves dans ses profils
# (providers/openai_chat/profiles.py).
#
# Defauts implicites, ecrits seulement quand ils changent :
#   list="/models"  field="data"  chat="/chat/completions"
#   maxtok="max_tokens"  ua=UA
#
# `env`     variable qui porte la cle (dans ~/.doublure/.env ou l'environnement)
# `static`  cle fixe, pour les serveurs locaux qui en exigent une sans la lire
# `local`   tourne sur la machine : gratuit et prive, mais rarement installe
# `special` auth que ce module ne sait pas faire — exclu, avec sa raison

CATALOG = {
    # -- passerelles sans plafond constate, en tete ------------------------
    "nvidia_nim": {
        "label": "NVIDIA NIM", "base": "https://integrate.api.nvidia.com/v1",
        "env": "NVIDIA_NIM_API_KEY",
        "url": "https://build.nvidia.com/settings/api-keys",
    },
    "open_router": {
        "label": "OpenRouter", "base": "https://openrouter.ai/api/v1",
        "env": "OPENROUTER_API_KEY", "url": "https://openrouter.ai/keys",
    },
    "groq": {
        "label": "Groq", "base": "https://api.groq.com/openai/v1",
        "env": "GROQ_API_KEY", "url": "https://console.groq.com/keys",
    },
    "opencode_zen": {
        # Sert sans cle : c'est le maillon qui repond meme quand tout le
        # reste est plafonne ou non configure.
        "label": "OpenCode Zen", "base": "https://opencode.ai/zen/v1",
        "env": "OPENCODE_API_KEY", "url": "https://opencode.ai/auth",
        "keyless": True, "ua": "opencode",
    },
    "opencode_go": {
        "label": "OpenCode Go", "base": "https://opencode.ai/zen/go/v1",
        "env": "OPENCODE_API_KEY", "url": "https://opencode.ai/auth",
        "keyless": True, "ua": "opencode",
    },
    "kilo": {
        "label": "Kilo.ai", "base": "https://api.kilo.ai/api/gateway",
        "env": "KILO_API_KEY", "url": "https://app.kilo.ai",
        "keyless": True,
    },
    "cerebras": {
        "label": "Cerebras", "base": "https://api.cerebras.ai/v1",
        "env": "CEREBRAS_API_KEY", "url": "https://cloud.cerebras.ai",
        "maxtok": "max_completion_tokens",
    },
    "cline_pass": {
        "label": "ClinePass", "base": "https://api.cline.bot/api/v1",
        "env": "CLINE_API_KEY", "url": "https://app.cline.bot",
        "list": "/ai/cline/recommended-models", "field": "clinePass",
    },

    # -- gros fournisseurs a cle ------------------------------------------
    "xai": {
        "label": "xAI (Grok)", "base": "https://api.x.ai/v1",
        "env": "XAI_API_KEY",
        "url": "https://console.x.ai/team/default/api-keys",
        "list": "/language-models", "field": "models", "id": "id",
        "defmaxtok": 32000,
    },
    "gemini": {
        "label": "Gemini",
        "base": "https://generativelanguage.googleapis.com/v1beta/openai",
        "env": "GEMINI_API_KEY", "url": "https://aistudio.google.com/apikey",
    },
    "deepseek": {
        "label": "DeepSeek", "base": "https://api.deepseek.com",
        "env": "DEEPSEEK_API_KEY",
        "url": "https://platform.deepseek.com/api_keys",
    },
    "mistral": {
        "label": "Mistral", "base": "https://api.mistral.ai/v1",
        "env": "MISTRAL_API_KEY", "url": "https://console.mistral.ai/",
    },
    "mistral_codestral": {
        "label": "Mistral Codestral", "base": "https://codestral.mistral.ai/v1",
        "env": "CODESTRAL_API_KEY", "url": "https://console.mistral.ai/",
    },
    "together": {
        "label": "Together AI", "base": "https://api.together.ai/v1",
        "env": "TOGETHER_API_KEY",
        "url": "https://api.together.ai/settings/api-keys",
        # Rend un tableau nu, pas un objet { data: [...] }.
        "field": None, "defmaxtok": 32000,
    },
    "deepinfra": {
        "label": "DeepInfra", "base": "https://api.deepinfra.com/v1/openai",
        "env": "DEEPINFRA_API_KEY", "url": "https://deepinfra.com/dash/api_keys",
        "defmaxtok": 32000,
    },
    "siliconflow": {
        "label": "SiliconFlow", "base": "https://api.siliconflow.com/v1",
        "env": "SILICONFLOW_API_KEY",
        "url": "https://cloud.siliconflow.com/account/ak", "defmaxtok": 32000,
    },
    "nebius": {
        "label": "Nebius Token Factory",
        "base": "https://api.tokenfactory.nebius.com/v1",
        "env": "NEBIUS_API_KEY",
        "url": "https://tokenfactory.nebius.com/project/api-keys",
        "defmaxtok": 32000,
    },
    "chutes": {
        "label": "Chutes", "base": "https://llm.chutes.ai/v1",
        "env": "CHUTES_API_KEY",
        "url": "https://chutes.ai/docs/getting-started/authentication",
        "defmaxtok": 32000,
    },
    "featherless": {
        "label": "Featherless AI", "base": "https://api.featherless.ai/v1",
        "env": "FEATHERLESS_API_KEY",
        "url": "https://featherless.ai/account/api-keys", "defmaxtok": 32000,
    },
    "novita": {
        "label": "Novita AI", "base": "https://api.novita.ai/openai/v1",
        "env": "NOVITA_API_KEY",
        "url": "https://novita.ai/settings/key-management", "defmaxtok": 32000,
    },
    "fireworks": {
        "label": "Fireworks", "base": "https://api.fireworks.ai/inference/v1",
        "env": "FIREWORKS_API_KEY",
        "url": "https://fireworks.ai/account/api-keys", "defmaxtok": 32000,
    },
    "sambanova": {
        "label": "SambaNova", "base": "https://api.sambanova.ai/v1",
        "env": "SAMBANOVA_API_KEY", "url": "https://cloud.sambanova.ai/apis",
    },
    "minimax": {
        "label": "MiniMax", "base": "https://api.minimax.io/v1",
        "env": "MINIMAX_API_KEY",
        "url": "https://platform.minimax.io/user-center/"
               "basic-information/interface-key",
        "maxtok": "max_completion_tokens", "defmaxtok": 32000,
    },
    "kimi": {
        "label": "Kimi", "base": "https://api.moonshot.ai/v1",
        "env": "KIMI_API_KEY",
        "url": "https://platform.moonshot.cn/console/api-keys",
        "defmaxtok": 32000,
    },
    "kimi_code": {
        "label": "Kimi Code", "base": "https://api.kimi.com/coding/v1",
        "env": "KIMI_CODE_API_KEY", "url": "https://www.kimi.com/code/console",
        "maxtok": "max_completion_tokens", "ua": "free-claude-code",
    },
    "zai": {
        "label": "Z.ai Coding Plan",
        "base": "https://api.z.ai/api/coding/paas/v4",
        "env": "ZAI_API_KEY", "url": "https://z.ai/manage-apikey/apikey-list",
    },
    "zai_api": {
        "label": "Z.ai API", "base": "https://api.z.ai/api/paas/v4",
        "env": "ZAI_API_KEY", "url": "https://z.ai/manage-apikey/apikey-list",
    },
    "qwencloud": {
        "label": "QwenCloud Token Plan",
        "base": "https://token-plan.ap-southeast-1.maas.aliyuncs.com"
                "/compatible-mode/v1",
        "env": "QWENCLOUD_API_KEY", "url": "https://home.qwencloud.com/api-keys",
        "defmaxtok": 32000,
    },
    "qwencloud_coding": {
        "label": "QwenCloud Coding Plan",
        "base": "https://coding-intl.dashscope.aliyuncs.com/v1",
        "env": "QWENCLOUD_CODING_API_KEY",
        "url": "https://home.qwencloud.com/api-keys",
    },
    "wafer": {
        "label": "Wafer", "base": "https://pass.wafer.ai/v1",
        "env": "WAFER_API_KEY", "url": "https://www.wafer.ai/pass",
        "defmaxtok": 32000,
    },
    "vercel": {
        "label": "Vercel AI Gateway", "base": "https://ai-gateway.vercel.sh/v1",
        "env": "AI_GATEWAY_API_KEY", "url": "https://vercel.com/docs/ai-gateway",
    },
    "huggingface": {
        "label": "Hugging Face", "base": "https://router.huggingface.co/v1",
        "env": "HUGGINGFACE_API_KEY",
        "url": "https://huggingface.co/settings/tokens",
    },
    "cohere": {
        "label": "Cohere", "base": "https://api.cohere.ai/compatibility/v1",
        "env": "COHERE_API_KEY", "url": "https://dashboard.cohere.com/api-keys",
        # Refuse un « name » sur un message et plusieurs champs OpenAI.
        "strip_names": True,
        "drop": ("frequency_penalty", "presence_penalty", "logprobs",
                 "top_logprobs", "n", "logit_bias", "parallel_tool_calls"),
    },
    "github_models": {
        "label": "GitHub Models", "base": "https://models.github.ai/inference",
        "env": "GITHUB_MODELS_TOKEN", "url": "https://github.com/settings/tokens",
    },
    "tokenrouter": {
        "label": "TokenRouter", "base": "https://api.tokenrouter.com/v1",
        "env": "TOKENROUTER_API_KEY", "url": "https://www.tokenrouter.com/",
    },
    "nararoute": {
        "label": "NaraRoute", "base": "https://router.bynara.id/v1",
        "env": "NARAROUTE_API_KEY", "url": "https://router.bynara.id/keys",
    },
    "agnes": {
        "label": "Agnes AI", "base": "https://apihub.agnes-ai.com/v1",
        "env": "AGNES_API_KEY", "url": "https://agnes-ai.com/",
        "defmaxtok": 32000,
    },
    "zenmux": {
        "label": "ZenMux", "base": "https://zenmux.ai/api/v1",
        "env": "ZENMUX_API_KEY", "url": "https://zenmux.ai/platform/pay-as-you-go",
        "maxtok": "max_completion_tokens", "defmaxtok": 32000,
    },
    "wandb": {
        "label": "W&B Inference", "base": "https://api.inference.wandb.ai/v1",
        "env": "WANDB_API_KEY", "url": "https://wandb.ai/settings",
        "maxtok": "max_completion_tokens", "defmaxtok": 32000,
    },
    "bedrock": {
        "label": "Amazon Bedrock",
        "base": "https://bedrock-mantle.us-east-1.api.aws/v1",
        "env": "AWS_BEARER_TOKEN_BEDROCK",
        "url": "https://console.aws.amazon.com/bedrock/",
    },
    "ollama_cloud": {
        "label": "Ollama Cloud", "base": "https://ollama.com/v1",
        "env": "OLLAMA_API_KEY", "url": "https://ollama.com/settings/keys",
    },

    # -- local : gratuit, prive, hors ligne --------------------------------
    "ollama": {
        "label": "Ollama", "base": "http://localhost:11434/v1",
        "static": "ollama", "local": True,
        "url": "https://ollama.com/download",
    },
    "lmstudio": {
        "label": "LM Studio", "base": "http://localhost:1234/v1",
        "static": "lm-studio", "local": True, "url": "https://lmstudio.ai/",
    },
    "llamacpp": {
        "label": "llama.cpp", "base": "http://localhost:8080/v1",
        "static": "llamacpp", "local": True,
        "url": "https://github.com/ggml-org/llama.cpp",
    },

    # -- auth que ce module ne sait pas faire ------------------------------
    # Les declarer sans les servir serait pire que de les omettre : `dbl`
    # dirait « pas de cle » sur un fournisseur qui, lui, dirait 401.
    "azure_openai": {
        "label": "Azure OpenAI", "base": "", "env": "AZURE_OPENAI_API_KEY",
        "url": "https://ai.azure.com/",
        "special": "base par deploiement + en-tete « api-key », a configurer "
                   "a la main dans .env (AZURE_OPENAI_BASE_URL)",
    },
    "cloudflare": {
        "label": "Cloudflare Workers AI",
        "base": "https://api.cloudflare.com/client/v4",
        "env": "CLOUDFLARE_API_TOKEN",
        "url": "https://dash.cloudflare.com/profile/api-tokens",
        "special": "base portee par le compte (/accounts/<id>/ai/v1), "
                   "CLOUDFLARE_ACCOUNT_ID requis",
    },
    "vertex": {
        "label": "Google Vertex AI", "base": "https://aiplatform.googleapis.com",
        "url": "https://cloud.google.com/docs/authentication",
        "special": "jeton ADC gcloud, pas une cle API",
    },
    "openai": {
        "label": "OpenAI / ChatGPT",
        "base": "https://chatgpt.com/backend-api/codex",
        "url": "https://chatgpt.com/",
        "special": "compte connecte (OAuth Codex), pas une cle API",
    },
}

# Ordre de preference par defaut. Il suit celui de FCC — NIM, OpenRouter et
# Groq en tete — avec les deux passerelles sans cle placees juste apres :
# elles repondent sur une machine ou rien n'est configure, ce qui est
# exactement le cas d'un repli.
PREFERENCE = (
    "nvidia_nim", "open_router", "groq", "opencode_zen", "kilo",
    "cerebras", "opencode_go", "gemini", "cline_pass", "xai",
    "together", "deepinfra", "siliconflow", "nebius", "chutes",
    "featherless", "novita", "fireworks", "sambanova", "vercel",
    "huggingface", "github_models", "cohere", "kimi", "kimi_code",
    "zai", "zai_api", "qwencloud", "qwencloud_coding", "minimax",
    "deepseek", "mistral", "mistral_codestral", "wafer", "tokenrouter",
    "nararoute", "agnes", "zenmux", "wandb", "bedrock", "ollama_cloud",
    "ollama", "lmstudio", "llamacpp",
)

TIERS = ("opus", "sonnet", "fable", "haiku")

# Paliers de secours, pour les fournisseurs dont le catalogue n'a pas pu etre
# liste : reseau coupe, listage non servi, ou reponse d'une forme inattendue.
# Sans ce filet, une panne de listage retirerait de la chaine les deux seules
# passerelles qui repondent sans cle — exactement quand on en a besoin.
#
# Sondes le 2026-08-22 sur les cinq usages reels de Claude Code : texte, flux
# SSE, tool_use, aller-retour tool_result, contexte de 30 k. Seuls les 5/5
# figurent ici.
SEED = {
    "opencode_zen": {
        "opus": "nemotron-3-ultra-free",
        "sonnet": "nemotron-3-ultra-free",
        "fable": "nemotron-3.5-lightning-free",
        "haiku": "nemotron-3.5-lightning-free",
    },
    "kilo": {
        "opus": "nvidia/nemotron-3-ultra-550b-a55b:free",
        "sonnet": "nvidia/nemotron-3-ultra-550b-a55b:free",
        "fable": "nvidia/nemotron-3-super-120b-a12b:free",
        "haiku": "nvidia/nemotron-3.5-lightning:free",
    },
    # OpenRouter est servi en API Anthropic native par le routeur, mais le
    # nom du modele doit quand meme etre reecrit : « claude-opus-5 » laisse
    # tel quel serait route vers le VRAI Anthropic et facture au credit.
    "open_router": {
        "opus": "nvidia/nemotron-3-ultra-550b-a55b:free",
        "sonnet": "nvidia/nemotron-3-ultra-550b-a55b:free",
        "fable": "nvidia/nemotron-3.5-lightning:free",
        "haiku": "nvidia/nemotron-3-nano-30b-a3b:free",
    },
}


def usable(prov):
    """Un fournisseur que ce module sait appeler (auth ordinaire, base connue)."""
    cfg = CATALOG.get(prov)
    return bool(cfg) and not cfg.get("special") and bool(cfg.get("base"))


def label(prov):
    cfg = CATALOG.get(prov) or {}
    return cfg.get("label") or prov


def _cfg(prov, field, default=None):
    return (CATALOG.get(prov) or {}).get(field, default)


def ua(prov):
    """Agent utilisateur a presenter : le sien s'il en exige un, le notre sinon.

    Deux fournisseurs verifient l'agent (opencode attend « opencode », Kimi
    Code « free-claude-code ») et refusent tout le reste.
    """
    return _cfg(prov, "ua") or UA


def keyless(prov):
    """Sert sans cle : peut rester dans la chaine sur une machine nue."""
    return bool(_cfg(prov, "keyless")) or bool(_cfg(prov, "static"))


# --------------------------------------------------------------------------
# Cles
# --------------------------------------------------------------------------

_env_cache = {"at": 0.0, "vals": {}}
_lock = threading.Lock()


def _parse_env(path):
    """Lit un fichier .env en dict. Un fichier absent n'est pas une erreur."""
    out = {}
    try:
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                name, _, value = line.partition("=")
                out[name.strip()] = value.strip().strip("\"'")
    except OSError:
        pass
    return out


def env_values():
    """Toutes les cles connues : notre .env, puis celui de FCC, gardees 60 s.

    L'ordre compte : une valeur ecrite dans ~/.doublure/.env doit gagner sur
    celle que FCC garde de son cote, sinon corriger une cle ici n'aurait
    aucun effet.
    """
    now = time.time()
    with _lock:
        if _env_cache["vals"] and now - _env_cache["at"] < 60:
            return _env_cache["vals"]
    vals = _parse_env(FCC_ENV)
    vals.update(_parse_env(ENV_FILE))
    with _lock:
        _env_cache.update(at=now, vals=vals)
    return vals


def forget_env():
    """Oublie le cache de cles — a appeler apres avoir ecrit dans .env."""
    with _lock:
        _env_cache.update(at=0.0, vals={})


def key(prov):
    """Cle d'un fournisseur : environnement, puis .env, puis cle fixe.

    L'environnement passe devant : un `KILO_API_KEY=... dbl probe` doit
    pouvoir tester une cle sans l'ecrire sur le disque.
    """
    cfg = CATALOG.get(prov) or {}
    name = cfg.get("env")
    if name:
        val = os.environ.get(name) or env_values().get(name)
        if val:
            return val
    return cfg.get("static") or None


def set_key(prov, value):
    """Ecrit (ou retire) la cle d'un fournisseur dans ~/.doublure/.env.

    Le fichier est reecrit ligne a ligne pour garder les commentaires et les
    autres cles : ce .env est aussi celui que l'utilisateur edite a la main.
    """
    name = _cfg(prov, "env")
    if not name:
        raise ValueError("%s ne prend pas de cle" % prov)
    os.makedirs(DBL_DIR, exist_ok=True)
    lines, seen = [], False
    try:
        with open(ENV_FILE) as fh:
            for line in fh:
                if line.strip().startswith(name + "="):
                    seen = True
                    if value:
                        lines.append("%s=%s\n" % (name, value))
                    continue
                lines.append(line)
    except OSError:
        pass
    if value and not seen:
        if lines and not lines[-1].endswith("\n"):
            lines.append("\n")
        lines.append("%s=%s\n" % (name, value))
    tmp = ENV_FILE + ".tmp"
    with open(tmp, "w") as fh:
        fh.writelines(lines)
    os.chmod(tmp, 0o600)     # une cle n'a rien a faire en lecture publique
    os.replace(tmp, ENV_FILE)
    forget_env()


def configured(include_local=True):
    """Fournisseurs joignables : cle presente, sans cle assumee, ou local.

    Un fournisseur local absent est ecarte tout de suite : le tenter
    donnerait une erreur de connexion presentee comme une panne du repli.
    """
    out = []
    for prov in PREFERENCE:
        if not usable(prov):
            continue
        cfg = CATALOG[prov]
        if cfg.get("local"):
            # Ecouter ne suffit pas : Ollama fraichement installe repond sur
            # son port avec un catalogue vide. Le mettre dans la chaine ferait
            # perdre un tour a chaque repli pour rien.
            if include_local and _local_up(prov) and models(prov):
                out.append(prov)
            continue
        if key(prov) or cfg.get("keyless"):
            out.append(prov)
    return out


_up_cache = {}


def _local_up(prov, ttl=30):
    """Un serveur local ecoute-t-il ? Sonde TCP brute, gardee 30 s."""
    import socket
    now = time.time()
    hit = _up_cache.get(prov)
    if hit and now - hit[0] < ttl:
        return hit[1]
    host, port, _tls, _pfx = endpoint(prov)
    try:
        socket.create_connection((host, port), timeout=0.4).close()
        up = True
    except OSError:
        up = False
    _up_cache[prov] = (now, up)
    return up


# --------------------------------------------------------------------------
# Appels HTTP
# --------------------------------------------------------------------------

def endpoint(prov):
    """(hote, port, tls, prefixe) de la base d'un fournisseur."""
    base = _cfg(prov, "base") or ""
    # Une base surchargee dans .env gagne : c'est ce qui permet de pointer un
    # LM Studio distant ou un deploiement Azure sans toucher au code.
    over = env_values().get(_cfg(prov, "env", "") and
                            _cfg(prov, "env").replace("_API_KEY", "_BASE_URL")
                            or "")
    if over:
        base = over
    parts = urllib.parse.urlsplit(base)
    tls = parts.scheme != "http"
    port = parts.port or (443 if tls else 80)
    return parts.hostname or "localhost", port, tls, parts.path.rstrip("/")


def request(prov, method, path, body=None, timeout=LIST_TIMEOUT, accept=None):
    """Un appel HTTP vers un fournisseur. Rend (statut, corps, erreur).

    Le corps est rendu en octets : c'est a l'appelant de le lire en JSON ou
    de le presenter tel quel dans un message d'erreur.
    """
    host, port, tls, prefix = endpoint(prov)
    # Un chemin de listage peut etre absolu (DeepInfra sort de sa base).
    if path.startswith("http"):
        parts = urllib.parse.urlsplit(path)
        host, tls = parts.hostname, parts.scheme != "http"
        port = parts.port or (443 if tls else 80)
        full = parts.path
    else:
        full = prefix + path
    headers = {"User-Agent": _cfg(prov, "ua") or UA,
               "Accept": accept or "application/json",
               "Host": host if port in (80, 443) else "%s:%d" % (host, port)}
    tok = key(prov)
    if tok:
        headers["Authorization"] = "Bearer " + tok
    if prov == "open_router":
        # OpenRouter attribue les requetes a une application : sans ces deux
        # en-tetes, le palier gratuit est plus vite refuse.
        headers["HTTP-Referer"] = "https://claude.ai/code"
        headers["X-Title"] = "Claude Code"
    if body is not None:
        headers["Content-Type"] = "application/json"
        headers["Content-Length"] = str(len(body))
    cls = (http.client.HTTPSConnection if tls
           else http.client.HTTPConnection)
    conn = None
    try:
        conn = cls(host, port, timeout=timeout)
        conn.request(method, full, body=body, headers=headers)
        resp = conn.getresponse()
        data = resp.read()
        return resp.status, data, None
    except Exception as e:
        return 0, b"", "%s: %s" % (type(e).__name__, e)
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


# --------------------------------------------------------------------------
# Catalogue de modeles
# --------------------------------------------------------------------------

def _read_json(path):
    try:
        with open(path) as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(data, fh, indent=1, sort_keys=True)
    os.replace(tmp, path)


def _ids_from(payload, prov):
    """Identifiants de modeles d'une reponse de listage.

    Trois formes existent dans la nature : { data: [...] }, un tableau nu, et
    un objet a champ nomme (Cline, xAI). Le champ est declare au catalogue.
    """
    field = _cfg(prov, "field", "data")
    items = payload
    if field is not None and isinstance(payload, dict):
        items = payload.get(field)
    if not isinstance(items, list):
        return []
    idf = _cfg(prov, "id", "id")
    out = []
    for item in items:
        if isinstance(item, str):
            out.append(item)
        elif isinstance(item, dict):
            val = item.get(idf) or item.get("name") or item.get("model")
            if isinstance(val, str) and val.strip():
                out.append(val.strip())
    return sorted(set(out))


def models(prov, refresh=False):
    """Modeles servis par un fournisseur. Cache disque, TTL de 6 h.

    Un echec de listage ne vide pas le cache : mieux vaut un catalogue d'hier
    qu'aucun catalogue, et c'est la sante qui ecartera un modele retire.
    """
    cache = _read_json(CATALOG_FILE)
    hit = cache.get(prov)
    now = time.time()
    if (not refresh and isinstance(hit, dict)
            and now - hit.get("at", 0) < CATALOG_TTL):
        return list(hit.get("models") or [])
    status, data, err = request(prov, "GET", _cfg(prov, "list", "/models"))
    if err or status >= 400:
        return list((hit or {}).get("models") or [])
    try:
        ids = _ids_from(json.loads(data.decode("utf-8", "replace")), prov)
    except ValueError:
        ids = []
    if not ids:
        return list((hit or {}).get("models") or [])
    cache[prov] = {"at": now, "models": ids}
    _write_json(CATALOG_FILE, cache)
    return ids


# --------------------------------------------------------------------------
# Sante : un modele qui repond, et qui sait appeler un outil
# --------------------------------------------------------------------------
# Claude Code ne sait rien faire sans tool_use : un modele qui rend du beau
# texte mais ignore les outils est inutilisable ici. La sonde verifie donc les
# deux, et le catalogue effectif ne retient que les modeles qui passent.

PROBE_TOOL = {
    "type": "function",
    "function": {
        "name": "lire",
        "description": "Lit un fichier du disque.",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
}
PROBE_MSG = ("Appelle l'outil lire sur /etc/hosts. Reponds uniquement par "
             "l'appel d'outil.")


def health(prov=None):
    """Releve de sante brut, tel qu'il est sur le disque."""
    data = _read_json(HEALTH_FILE)
    if prov is None:
        return data
    pre = prov + "/"
    return {k[len(pre):]: v for k, v in data.items() if k.startswith(pre)}


def _fresh(entry, now):
    if not isinstance(entry, dict):
        return False
    ttl = HEALTH_TTL_OK if entry.get("ok") else HEALTH_TTL_BAD
    return now - entry.get("at", 0) < ttl


def probe(prov, model, force=False):
    """Sonde un modele : repond-il, et sait-il appeler un outil ?

    Rend le releve : {ok, tools, ms, code, error, at}. Une sonde coute une
    requete, donc le resultat est garde longtemps quand il est bon.
    """
    store = _read_json(HEALTH_FILE)
    ref = "%s/%s" % (prov, model)
    now = time.time()
    if not force and _fresh(store.get(ref), now):
        return store[ref]

    body = json.dumps({
        "model": model,
        "max_tokens": 64,
        "messages": [{"role": "user", "content": PROBE_MSG}],
        "tools": [PROBE_TOOL],
    }).encode()
    body = chat_body(prov, body)
    start = time.time()
    status, data, err = request(prov, "POST", "/chat/completions", body,
                               timeout=PROBE_TIMEOUT)
    ms = int((time.time() - start) * 1000)
    out = {"at": now, "ms": ms, "code": status, "ok": False, "tools": False}
    if err:
        out["error"] = err
    elif status >= 400:
        out["error"] = data.decode("utf-8", "replace")[:200]
    else:
        try:
            payload = json.loads(data.decode("utf-8", "replace"))
        except ValueError:
            payload = {}
        choices = payload.get("choices")
        first = choices[0] if isinstance(choices, list) and choices else {}
        msg = first.get("message") if isinstance(first, dict) else {}
        msg = msg if isinstance(msg, dict) else {}
        out["ok"] = bool(msg) or bool(first.get("finish_reason"))
        calls = msg.get("tool_calls")
        out["tools"] = bool(calls) or first.get("finish_reason") == "tool_calls"
        if not out["ok"]:
            out["error"] = "reponse sans choices"
    store[ref] = out
    _write_json(HEALTH_FILE, store)
    return out


# --------------------------------------------------------------------------
# Choix du modele derriere chaque palier
# --------------------------------------------------------------------------
# FCC lit MODEL_OPUS / MODEL_SONNET... dans son .env : une table tenue a la
# main, qui vieillit des qu'un fournisseur renomme un modele. Ici le palier
# est deduit du catalogue reel, et une surcharge reste possible.

_SIZE = re.compile(r"(\d+(?:[.,]\d+)?)\s*b\b", re.I)
_ACTIVE = re.compile(r"a(\d+(?:[.,]\d+)?)b", re.I)
# Un modele de code ou de raisonnement vaut mieux qu'un modele d'image ou
# d'embedding portant le meme nombre de parametres.
_BAD = ("embed", "rerank", "whisper", "tts", "audio", "image", "vision-only",
        "guard", "moderation", "ocr", "diffusion", "video", "clip", "bge",
        "e5-", "sdxl", "flux")
# Marqueurs d'*usage*, pas de famille : « starcoder » est un modele de
# completion, il decroche des le premier tool_result meme s'il a la bonne
# taille. Ce qu'on cherche, c'est un modele instruit ou conversationnel.
_GOOD = ("instruct", "chat", "-it", "reason", "think", "thinking")
# Familles connues pour tenir une boucle a outils : simple depart de tri
# quand deux modeles annoncent la meme taille.
_FAMILY = ("nemotron", "qwen", "llama", "deepseek", "glm", "kimi", "mistral",
           "gpt-oss", "minimax", "gemma", "phi")


def _score(model):
    """Note grossiere d'un identifiant de modele : plus gros = plus capable.

    Rien d'exact : le nombre de parametres dans le nom est le seul signal
    disponible sans catalogue enrichi. Un « :free » ne change pas la note —
    la gratuite est deja garantie par le choix du fournisseur.
    """
    low = model.lower()
    if any(bad in low for bad in _BAD):
        return -1.0
    sizes = [float(m.replace(",", ".")) for m in _SIZE.findall(low)]
    active = [float(m.replace(",", ".")) for m in _ACTIVE.findall(low)]
    # « 550b-a55b » : 550 est la taille totale, 55 les parametres actifs. On
    # note sur le total, qui reflete la capacite, pas sur l'actif.
    total = max(sizes) if sizes else 0.0
    if total <= 0:
        # Sans taille annoncee, on ne peut pas classer : une note plancher
        # laisse ces modeles derriere ceux qui s'annoncent.
        total = 1.0
    bonus = 0.5 if any(g in low for g in _GOOD) else 0.0
    bonus += 0.5 if any(f in low for f in _FAMILY) else 0.0
    fast = min(active) if active else total
    return total + bonus + fast / 1000.0


def _known_good(prov, candidates):
    """Trie les candidats : sante connue d'abord, puis note decroissante."""
    rel = health(prov)
    now = time.time()

    def rank(model):
        entry = rel.get(model)
        # La sante sert a ecarter, pas a promouvoir : un modele verifie bon et
        # un modele inconnu partagent le meme rang, et c'est la note qui les
        # separe. Sinon sonder un petit modele le ferait passer devant un
        # 550B jamais teste — un palier « opus » servi par un nano parce qu'il
        # a eu la chance d'etre sonde le premier.
        if _fresh(entry, now) and not entry.get("ok"):
            state = 2              # verifie casse
        elif _fresh(entry, now) and not entry.get("tools"):
            state = 1              # repond mais ignore les outils
        else:
            state = 0              # verifie bon, ou pas encore teste
        return (state, -_score(model), model)

    return sorted(candidates, key=rank)


def _free_only(pool):
    """Ne garde que les variantes gratuites quand le fournisseur en publie.

    OpenRouter sert le meme modele en « :free » et en payant. Prendre le
    second reviendrait a facturer un repli dont tout l'interet est de ne rien
    couter — et le nom, lui, ne le dit pas au moment de l'appel.
    """
    free = [m for m in pool if ":free" in m.lower()]
    return free or pool


def _small(pool):
    """Le plus petit modele encore capable de tenir une boucle a outils.

    Le critere n'est pas « le plus petit » : un modele de complétion de code
    ou un 3 B decroche des le premier tool_result. On prend donc le plus
    petit qui annonce a la fois une taille credible et un usage
    conversationnel, et on n'elargit que si rien ne convient.
    """
    for lo, hi, need_good in ((15, 130, True), (15, 400, True),
                              (8, 400, False), (0, 1e9, False)):
        cand = [m for m in pool
                if lo <= _score(m) <= hi
                and (not need_good or any(g in m.lower() for g in _GOOD))]
        if cand:
            return min(cand, key=lambda m: (_score(m), m))
    return pool[-1]


def tiers(prov, override=None):
    """Palier Claude Code -> modele du fournisseur.

    Trois sources, dans cet ordre : les modeles sondes a la main quand ils
    sont toujours servis (SEED), puis la deduction depuis le catalogue reel,
    puis la surcharge de l'utilisateur qui a toujours le dernier mot.

    Le plus gros modele sain sert opus et sonnet — sur une passerelle
    gratuite un palier ne coute rien de plus, alors autant prendre le
    meilleur. Les deux petits paliers vont au plus rapide qui sache encore
    appeler un outil : c'est ce que Claude Code utilise pour ses taches de
    fond, ou la latence compte plus que la finesse.
    """
    live = models(prov)
    pool = _free_only([m for m in live if _score(m) > 0])
    seed = SEED.get(prov) or {}

    out = {}
    if pool:
        ranked = _known_good(prov, pool)
        # Les modeles connus casses ne doivent pas devenir un palier s'il
        # reste autre chose : on coupe la queue quand elle est sure.
        rel, now = health(prov), time.time()
        sane = [m for m in ranked
                if not (_fresh(rel.get(m), now) and not rel[m].get("ok"))]
        ranked = sane or ranked
        big, small = ranked[0], _small(ranked)
        mid = ranked[1] if len(ranked) > 1 else big
        out = {"opus": big, "sonnet": big,
               "fable": mid if _score(mid) < _score(big) else small,
               "haiku": small}

    # Un modele sonde a la main vaut mieux qu'un modele deduit d'un nom :
    # sur les passerelles dont le catalogue n'annonce aucune taille (noms de
    # code opaques), la deduction n'a rien pour trancher. On ne garde le
    # choix sonde que s'il est encore servi — sinon il rendrait un 404.
    for tier, ref in seed.items():
        if tier in TIERS and (not live or ref in live):
            out[tier] = ref

    if not out:
        return {}
    for tier in TIERS:
        out.setdefault(tier, out.get("sonnet") or next(iter(out.values())))
    if isinstance(override, dict):
        for tier, ref in override.items():
            if tier in TIERS and isinstance(ref, str) and ref.strip():
                out[tier] = ref.strip()
    return out


def pick(prov, alias, override=None):
    """Nom de modele demande par Claude Code -> modele du fournisseur.

    Un nom qui figure deja au catalogue du fournisseur passe tel quel : c'est
    un choix explicite de l'utilisateur, pas un alias a traduire.
    """
    table = tiers(prov, override)
    if not table:
        return None
    if isinstance(alias, str):
        if alias in models(prov):
            return alias
        low = alias.lower()
        for tier in TIERS:
            if tier in low:
                return table.get(tier) or table["sonnet"]
    return table["sonnet"]


# --------------------------------------------------------------------------
# Dialecte : les ecarts par fournisseur
# --------------------------------------------------------------------------

def chat_body(prov, body):
    """Applique les ecarts de dialecte d'un fournisseur au corps OpenAI.

    Trois seulement, ceux qui font echouer une requete autrement correcte :
    le champ de longueur maximale (certains n'acceptent que
    « max_completion_tokens »), un plancher quand l'amont l'exige, et les
    champs qu'il refuse.
    """
    if not body:
        return body
    try:
        data = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return body
    if not isinstance(data, dict):
        return body

    field = _cfg(prov, "maxtok", "max_tokens")
    if field != "max_tokens" and "max_tokens" in data:
        data[field] = data.pop("max_tokens")
    floor = _cfg(prov, "defmaxtok")
    if floor and not data.get(field):
        data[field] = floor
    for name in _cfg(prov, "drop", ()) or ():
        data.pop(name, None)
    if _cfg(prov, "strip_names"):
        for msg in data.get("messages") or []:
            if isinstance(msg, dict):
                msg.pop("name", None)
    return json.dumps(data).encode()


def chat_path(prov):
    """Chemin de completion, prefixe de la base compris."""
    _h, _p, _t, prefix = endpoint(prov)
    return prefix + _cfg(prov, "chat", "/chat/completions")


def import_fcc_keys():
    """Recopie dans notre .env les cles que FCC detenait. Rend les noms pris.

    Sans ca, desinstaller FCC emporterait des cles que l'utilisateur croyait
    a lui — elles ne vivaient que dans ~/.fcc/.env.
    """
    src = _parse_env(FCC_ENV)
    mine = _parse_env(ENV_FILE)
    taken = []
    for prov, cfg in CATALOG.items():
        name = cfg.get("env")
        if not name or name in mine or not src.get(name):
            continue
        set_key(prov, src[name])
        mine[name] = src[name]
        taken.append(name)
    return taken
