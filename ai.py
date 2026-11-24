"""Appel des API des services IA : format Anthropic ou compatible OpenAI."""
import http.client
import json
import socket
import time
import urllib.error
import urllib.request

from store import Err


class System(str):
    """Prompt système en blocs (`.blocks`, du plus stable au plus changeant) : l'API Anthropic met en cache les blocs stables
    (prompt caching). Se comporte comme une chaîne ordinaire pour les autres usages (OpenAI, chat manuel, tests)."""
    blocks = ()

    @classmethod
    def of(cls, blocks):
        blocks = [b for b in blocks if b]
        s = cls("\n\n".join(blocks))
        s.blocks = tuple(blocks)
        return s


class Reply(str):
    """Texte de la réponse ; .usage = {"in": tokens d'entrée, "out": tokens de sortie} quand le fournisseur les donne."""
    usage = None


NET_RETRIES, NET_BACKOFF = 2, (2, 6)     # coupures réseau (connexion fermée sans réponse, délai dépassé…) : nouvelles tentatives avant d'abandonner


def _post(req):
    """Envoie la requête ; en cas de coupure réseau (RemoteDisconnected, reset, délai dépassé, hôte injoignable), réessaie après une courte attente.
    Les erreurs HTTP (quota, 4xx/5xx) ne sont jamais rejouées ici : elles sont traitées par l'appelant."""
    for n in range(NET_RETRIES + 1):
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                return json.load(r)
        except urllib.error.HTTPError:
            raise
        except (urllib.error.URLError, http.client.HTTPException, ConnectionError, socket.timeout, TimeoutError) as e:
            if n >= NET_RETRIES:
                why = e.reason if isinstance(e, urllib.error.URLError) else "%s : %s" % (type(e).__name__, e)
                raise Err(502, "Service IA injoignable après %d tentatives : %s" % (n + 1, why))
            time.sleep(NET_BACKOFF[min(n, len(NET_BACKOFF) - 1)])


def call_ai(s, system, messages, max_tokens=8192):
    key, url, model = s.get("apiKey", ""), s.get("url", "").rstrip("/"), s.get("model", "")
    if not key:
        raise Err(400, "Clé API manquante pour le service « %s » : ouvrez l’onglet Services IA et cliquez sur « Configurer la clé »" % s.get("name", "?"))
    anth = s.get("kind") == "anthropic"
    if anth:
        u = url + "/messages"
        hd = {"x-api-key": key, "anthropic-version": "2023-06-01"}
        blocks = getattr(system, "blocks", ())
        if len(blocks) > 1:       # tous les blocs sauf le dernier (le plus changeant) sont mis en cache
            sysb = [{"type": "text", "text": b} for b in blocks]
            for b in sysb[:-1]:
                b["cache_control"] = {"type": "ephemeral"}
        else:
            sysb = system
        body = {"model": model, "max_tokens": max_tokens, "system": sysb, "messages": messages}
    else:
        u = url + "/chat/completions"
        hd = {"Authorization": "Bearer " + key}
        body = {"model": model, "messages": [{"role": "system", "content": system}] + messages}
    hd.update({"Content-Type": "application/json", "User-Agent": "myteam/1.0"})
    req = urllib.request.Request(u, json.dumps(body).encode(), hd, method="POST")
    try:
        res = _post(req)
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
    us = res.get("usage") or {}
    if anth:
        text = "".join(b.get("text", "") for b in res.get("content", []))
        cr, cw = us.get("cache_read_input_tokens", 0) or 0, us.get("cache_creation_input_tokens", 0) or 0
        usage = {"in": us.get("input_tokens", 0) + cr + cw, "out": us.get("output_tokens", 0)}
        if cr or cw:                  # détail du cache : lectures (tarif réduit) et écritures (tarif majoré)
            usage.update(cache_read=cr, cache_write=cw)
    else:
        text = res["choices"][0]["message"]["content"] or ""
        usage = {"in": us.get("prompt_tokens", 0), "out": us.get("completion_tokens", 0)}
    r = Reply(text)
    r.usage = usage
    return r
