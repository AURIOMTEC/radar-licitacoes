"""Envio de WhatsApp por Evolution API (self-hosted) ou Z-API (serviço pago).

Evolution API v2:  POST {EVOLUTION_URL}/message/sendText/{instance}
                   header apikey; body {"number": "55149...", "text": "..."}
Z-API:             POST https://api.z-api.io/instances/{id}/token/{token}/send-text
                   header Client-Token; body {"phone": "55149...", "message": "..."}
"""
from __future__ import annotations

import logging
import re

import httpx

from . import config

log = logging.getLogger("whatsapp")


class WhatsAppError(Exception):
    pass


def normalizar_numero(n: str) -> str:
    d = re.sub(r"\D", "", n or "")
    if len(d) in (10, 11):      # DDD + número, sem país
        d = "55" + d
    return d


def numeros(campo: str) -> list[str]:
    return [x for x in (normalizar_numero(p) for p in re.split(r"[,;\s]+", campo or "")) if len(x) >= 12]


def _evolution(numero: str, texto: str) -> None:
    if not (config.EVOLUTION_URL and config.EVOLUTION_INSTANCE and config.EVOLUTION_APIKEY):
        raise WhatsAppError("Evolution API não configurada (EVOLUTION_URL/INSTANCE/APIKEY).")
    url = f"{config.EVOLUTION_URL}/message/sendText/{config.EVOLUTION_INSTANCE}"
    r = httpx.post(url, headers={"apikey": config.EVOLUTION_APIKEY},
                   json={"number": numero, "text": texto, "linkPreview": False}, timeout=30)
    if r.status_code >= 300:
        raise WhatsAppError(f"Evolution {r.status_code}: {r.text[:300]}")


def _zapi(numero: str, texto: str) -> None:
    if not (config.ZAPI_INSTANCE_ID and config.ZAPI_TOKEN):
        raise WhatsAppError("Z-API não configurada (ZAPI_INSTANCE_ID/ZAPI_TOKEN).")
    url = (f"https://api.z-api.io/instances/{config.ZAPI_INSTANCE_ID}"
           f"/token/{config.ZAPI_TOKEN}/send-text")
    headers = {"Client-Token": config.ZAPI_CLIENT_TOKEN} if config.ZAPI_CLIENT_TOKEN else {}
    r = httpx.post(url, headers=headers, json={"phone": numero, "message": texto}, timeout=30)
    if r.status_code >= 300:
        raise WhatsAppError(f"Z-API {r.status_code}: {r.text[:300]}")


def _nenhum(numero: str, texto: str) -> None:
    log.info("[WhatsApp desativado] para %s:\n%s", numero, texto)


def get_sender():
    return {"evolution": _evolution, "zapi": _zapi}.get(config.WHATSAPP_PROVIDER, _nenhum)


def ativo() -> bool:
    return config.WHATSAPP_PROVIDER in ("evolution", "zapi")
