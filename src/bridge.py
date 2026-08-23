"""Pont API Anthropic <-> OpenAI pour le routeur Claude Code.

OpenRouter sert l'API Anthropic telle quelle : le repli passe par un simple
relais d'octets. Les passerelles opencode Zen et Kilo, elles, ne parlent que
« chat/completions » — pas de /v1/messages. Ce module fait la traduction dans
les deux sens, y compris le flux SSE, pour qu'une session Claude Code ne voie
aucune difference.

Rien ici n'ouvre de connexion : ce sont des fonctions pures sur des dicts et
un generateur qui consomme un flux deja ouvert par l'appelant.
"""

import json
import re


# --------------------------------------------------------------------------
# Requete : Anthropic -> OpenAI
# --------------------------------------------------------------------------

def _text_of(content):
    """Aplati un contenu Anthropic (str ou liste de blocs) en texte simple."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    out = []
    for blk in content:
        if isinstance(blk, str):
            out.append(blk)
        elif isinstance(blk, dict) and blk.get("type") == "text":
            out.append(blk.get("text") or "")
    return "\n".join(p for p in out if p)


def _image_part(blk):
    """Bloc image Anthropic -> partie image_url OpenAI (data URI)."""
    src = blk.get("source") or {}
    if src.get("type") == "base64":
        media = src.get("media_type") or "image/png"
        return {"type": "image_url",
                "image_url": {"url": f"data:{media};base64,{src.get('data', '')}"}}
    if src.get("type") == "url" and src.get("url"):
        return {"type": "image_url", "image_url": {"url": src["url"]}}
    return None


def _user_content(content, multimodal):
    """Contenu utilisateur Anthropic -> contenu OpenAI.

    Renvoie (contenu, messages_tool) : les blocs tool_result ne peuvent pas
    rester dans un message user cote OpenAI, ils deviennent des messages
    « role: tool » distincts qui doivent *preceder* le reste.
    """
    if isinstance(content, str):
        return content, []
    if not isinstance(content, list):
        return "", []

    tools, parts = [], []
    for blk in content:
        if isinstance(blk, str):
            parts.append({"type": "text", "text": blk})
            continue
        if not isinstance(blk, dict):
            continue
        kind = blk.get("type")
        if kind == "text":
            parts.append({"type": "text", "text": blk.get("text") or ""})
        elif kind == "image":
            part = _image_part(blk) if multimodal else None
            # Modele non multimodal : on annonce l'image plutot que de la
            # laisser tomber en silence, sinon la reponse parle d'une image
            # qu'elle n'a jamais vue.
            parts.append(part or {"type": "text",
                                  "text": "[image non transmise : modele non multimodal]"})
        elif kind == "tool_result":
            body = blk.get("content")
            tools.append({
                "role": "tool",
                "tool_call_id": blk.get("tool_use_id") or "",
                "content": _text_of(body) if not isinstance(body, str) else body,
            })

    if not parts:
        return "", tools
    if len(parts) == 1 and parts[0]["type"] == "text":
        return parts[0]["text"], tools
    return parts, tools


def _assistant_message(content):
    """Contenu assistant Anthropic -> message OpenAI (texte + tool_calls)."""
    text, calls = [], []
    if isinstance(content, str):
        text.append(content)
    elif isinstance(content, list):
        for blk in content:
            if not isinstance(blk, dict):
                continue
            if blk.get("type") == "text":
                text.append(blk.get("text") or "")
            elif blk.get("type") == "tool_use":
                calls.append({
                    "id": blk.get("id") or "",
                    "type": "function",
                    "function": {
                        "name": blk.get("name") or "",
                        "arguments": json.dumps(blk.get("input") or {}),
                    },
                })
    msg = {"role": "assistant", "content": "\n".join(t for t in text if t) or None}
    if calls:
        msg["tool_calls"] = calls
    return msg


def to_openai(data, model, multimodal=False):
    """Corps /v1/messages Anthropic -> corps /chat/completions OpenAI."""
    msgs = []

    system = data.get("system")
    if system:
        txt = _text_of(system)
        if txt:
            msgs.append({"role": "system", "content": txt})

    for m in data.get("messages") or []:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        content = m.get("content")
        if role == "assistant":
            msgs.append(_assistant_message(content))
        else:
            body, tool_msgs = _user_content(content, multimodal)
            # Les resultats d'outils repondent a l'appel precedent : ils
            # passent avant le nouveau tour de parole de l'utilisateur.
            msgs.extend(tool_msgs)
            if body:
                msgs.append({"role": "user", "content": body})

    out = {"model": model, "messages": msgs}

    if data.get("max_tokens"):
        out["max_tokens"] = data["max_tokens"]
    for key in ("temperature", "top_p", "stream"):
        if data.get(key) is not None:
            out[key] = data[key]
    if data.get("stop_sequences"):
        out["stop"] = data["stop_sequences"]

    tools = data.get("tools") or []
    conv = []
    for t in tools:
        if not isinstance(t, dict) or not t.get("name"):
            continue  # outils serveur Anthropic (web_search...) : sans objet ici
        conv.append({"type": "function", "function": {
            "name": t["name"],
            "description": t.get("description") or "",
            "parameters": t.get("input_schema") or {"type": "object", "properties": {}},
        }})
    if conv:
        out["tools"] = conv
        choice = data.get("tool_choice")
        if isinstance(choice, dict):
            kind = choice.get("type")
            if kind == "auto":
                out["tool_choice"] = "auto"
            elif kind == "any":
                out["tool_choice"] = "required"
            elif kind == "tool" and choice.get("name"):
                out["tool_choice"] = {"type": "function",
                                      "function": {"name": choice["name"]}}
    return out


# --------------------------------------------------------------------------
# Reponse : OpenAI -> Anthropic
# --------------------------------------------------------------------------

_STOP = {
    "stop": "end_turn",
    "length": "max_tokens",
    "tool_calls": "tool_use",
    "function_call": "tool_use",
    "content_filter": "end_turn",
}


def _args(raw):
    """Arguments d'un tool_call -> dict. Un modele libre rend parfois du
    JSON approximatif ; mieux vaut un objet vide qu'une exception."""
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        val = json.loads(raw)
        return val if isinstance(val, dict) else {"value": val}
    except (ValueError, TypeError):
        return {}


def to_anthropic(data, model):
    """Reponse /chat/completions -> reponse /v1/messages."""
    choices = data.get("choices") or [{}]
    ch = choices[0] if isinstance(choices[0], dict) else {}
    msg = ch.get("message") or {}

    content = []
    text = msg.get("content")
    if isinstance(text, list):  # certains fournisseurs rendent des parties
        text = "".join(p.get("text", "") for p in text if isinstance(p, dict))
    if text:
        content.append({"type": "text", "text": text})

    for call in msg.get("tool_calls") or []:
        fn = call.get("function") or {}
        content.append({
            "type": "tool_use",
            "id": call.get("id") or "toolu_0",
            "name": fn.get("name") or "",
            "input": _args(fn.get("arguments")),
        })

    if not content:
        # Anthropic n'admet pas un contenu vide ; un bloc texte vide evite
        # que le client considere la reponse comme malformee.
        content.append({"type": "text", "text": ""})

    usage = data.get("usage") or {}
    stop = _STOP.get(ch.get("finish_reason"), "end_turn")
    if any(b["type"] == "tool_use" for b in content):
        stop = "tool_use"

    return {
        "id": data.get("id") or "msg_bridge",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content,
        "stop_reason": stop,
        "stop_sequence": None,
        "usage": {
            "input_tokens": usage.get("prompt_tokens") or 0,
            "output_tokens": usage.get("completion_tokens") or 0,
        },
    }


# --------------------------------------------------------------------------
# Flux : SSE OpenAI -> SSE Anthropic
# --------------------------------------------------------------------------

def _ev(name, payload):
    return (f"event: {name}\ndata: {json.dumps(payload)}\n\n").encode()


def stream_to_anthropic(lines, model):
    """Generateur : consomme un SSE OpenAI, rend un SSE Anthropic.

    « lines » itere sur les lignes brutes du flux amont (objets bytes).
    Les blocs sont ouverts a la volee : le texte occupe l'index 0, chaque
    appel d'outil prend l'index suivant, comme le fait l'API Anthropic.
    """
    msg_id = "msg_bridge"
    started = False
    text_open = False
    index = 0
    tools = {}           # index OpenAI -> {"idx": index Anthropic, "open": bool}
    stop = "end_turn"
    usage = {"input_tokens": 0, "output_tokens": 0}

    def start():
        return _ev("message_start", {"type": "message_start", "message": {
            "id": msg_id, "type": "message", "role": "assistant", "model": model,
            "content": [], "stop_reason": None, "stop_sequence": None,
            "usage": {"input_tokens": usage["input_tokens"], "output_tokens": 0},
        }})

    for raw in lines:
        line = raw.decode("utf-8", "replace").strip() if isinstance(raw, bytes) else str(raw).strip()
        if not line or not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload == "[DONE]":
            break
        try:
            chunk = json.loads(payload)
        except ValueError:
            continue

        if chunk.get("id"):
            msg_id = chunk["id"]
        if chunk.get("usage"):
            u = chunk["usage"]
            usage["input_tokens"] = u.get("prompt_tokens") or usage["input_tokens"]
            usage["output_tokens"] = u.get("completion_tokens") or usage["output_tokens"]

        if not started:
            started = True
            yield start()

        choices = chunk.get("choices") or []
        if not choices:
            continue
        ch = choices[0] if isinstance(choices[0], dict) else {}
        delta = ch.get("delta") or {}

        piece = delta.get("content")
        if isinstance(piece, list):
            piece = "".join(p.get("text", "") for p in piece if isinstance(p, dict))
        if piece:
            if not text_open:
                text_open = True
                yield _ev("content_block_start", {
                    "type": "content_block_start", "index": index,
                    "content_block": {"type": "text", "text": ""}})
            yield _ev("content_block_delta", {
                "type": "content_block_delta", "index": index,
                "delta": {"type": "text_delta", "text": piece}})

        for call in delta.get("tool_calls") or []:
            key = call.get("index", 0)
            fn = call.get("function") or {}
            slot = tools.get(key)
            if slot is None:
                # Le texte, s'il coulait, se termine avant l'ouverture d'un outil.
                if text_open:
                    yield _ev("content_block_stop",
                              {"type": "content_block_stop", "index": index})
                    text_open = False
                    index += 1
                elif tools:
                    index += 1
                slot = tools[key] = {"idx": index}
                yield _ev("content_block_start", {
                    "type": "content_block_start", "index": slot["idx"],
                    "content_block": {"type": "tool_use",
                                      "id": call.get("id") or f"toolu_{key}",
                                      "name": fn.get("name") or "", "input": {}}})
            args = fn.get("arguments")
            if args:
                yield _ev("content_block_delta", {
                    "type": "content_block_delta", "index": slot["idx"],
                    "delta": {"type": "input_json_delta", "partial_json": args}})

        if ch.get("finish_reason"):
            stop = _STOP.get(ch["finish_reason"], "end_turn")

    if not started:
        # Amont muet : une reponse vide mais bien formee vaut mieux qu'un
        # flux tronque, que Claude Code signalerait comme une erreur reseau.
        yield start()
        yield _ev("content_block_start", {
            "type": "content_block_start", "index": 0,
            "content_block": {"type": "text", "text": ""}})
        text_open = True

    if text_open:
        yield _ev("content_block_stop", {"type": "content_block_stop", "index": index})
    for slot in tools.values():
        yield _ev("content_block_stop", {"type": "content_block_stop", "index": slot["idx"]})
    if tools:
        stop = "tool_use"

    yield _ev("message_delta", {
        "type": "message_delta",
        "delta": {"stop_reason": stop, "stop_sequence": None},
        "usage": {"output_tokens": usage["output_tokens"]}})
    yield _ev("message_stop", {"type": "message_stop"})


# --------------------------------------------------------------------------
# Comptage de jetons
# --------------------------------------------------------------------------

_WORD = re.compile(r"\w+|[^\w\s]")


def count_tokens(data):
    """Estimation locale pour /v1/messages/count_tokens.

    Les passerelles OpenAI n'exposent pas ce point d'entree. Claude Code s'en
    sert pour decider quand compacter : une estimation raisonnable suffit,
    une erreur 404 casserait la session.
    """
    blob = []
    system = data.get("system")
    if system:
        blob.append(_text_of(system))
    for m in data.get("messages") or []:
        if isinstance(m, dict):
            blob.append(_text_of(m.get("content")))
    for t in data.get("tools") or []:
        if isinstance(t, dict):
            blob.append(json.dumps(t.get("input_schema") or {}))
            blob.append(t.get("description") or "")
    text = "\n".join(b for b in blob if b)
    # ~0,75 jeton par unite lexicale : proche du tokeniseur d'Anthropic sur
    # du texte mele de code, sans dependance externe.
    return max(1, int(len(_WORD.findall(text)) * 0.75))
