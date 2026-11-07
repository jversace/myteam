"""Appel des API des services IA : format Anthropic ou compatible OpenAI."""
import json
import urllib.error
import urllib.request

from store import Err


class Reply(str):
    """Texte de la réponse ; .usage = {"in": tokens d'entrée, "out": tokens de sortie} quand le fournisseur les donne."""
    usage = None


def call_ai(s, system, messages):
    key, url, model = s.get("apiKey", ""), s.get("url", "").rstrip("/"), s.get("model", "")
    if not key:
        raise Err(400, "Clé API manquante pour le service « %s » : ouvrez l’onglet Services IA et cliquez sur « Configurer la clé »" % s.get("name", "?"))
    anth = s.get("kind") == "anthropic"
    if anth:
        u = url + "/messages"
        hd = {"x-api-key": key, "anthropic-version": "2023-06-01"}
        body = {"model": model, "max_tokens": 8192, "system": system, "messages": messages}
    else:
        u = url + "/chat/completions"
        hd = {"Authorization": "Bearer " + key}
        body = {"model": model, "messages": [{"role": "system", "content": system}] + messages}
    hd.update({"Content-Type": "application/json", "User-Agent": "myteam/1.0"})
    req = urllib.request.Request(u, json.dumps(body).encode(), hd, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            res = json.load(r)
    except urllib.error.HTTPError as e:
        txt = e.read().decode(errors="replace")[:400]
        if e.code in (429, 529):  # quota / surcharge : l'agent se suspend puis reprend
            try:
                wait = int(e.headers.get("retry-after", "60"))
            except ValueError:
                wait = 60
            raise Err(429, "Quota atteint : " + txt, wait)
        if e.code == 404:
            raise Err(502, "Modèle ou URL introuvable (404) : vérifiez les champs « Modèle » et « URL de base » dans Services IA > Modifier (les noms de modèles changent souvent). Réponse du fournisseur : %s" % txt)
        raise Err(502, "Erreur du service IA (%s) : %s" % (e.code, txt))
    except urllib.error.URLError as e:
        raise Err(502, "Service IA injoignable : %s" % e.reason)
    us = res.get("usage") or {}
    if anth:
        text = "".join(b.get("text", "") for b in res.get("content", []))
        usage = {"in": us.get("input_tokens", 0) + us.get("cache_creation_input_tokens", 0) + us.get("cache_read_input_tokens", 0),
                 "out": us.get("output_tokens", 0)}
    else:
        text = res["choices"][0]["message"]["content"] or ""
        usage = {"in": us.get("prompt_tokens", 0), "out": us.get("completion_tokens", 0)}
    r = Reply(text)
    r.usage = usage
    return r
