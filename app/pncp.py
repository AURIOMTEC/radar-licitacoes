"""Cliente das APIs públicas do PNCP (sem chave).

Endpoints usados — conferidos nos manuais oficiais:
  * GET {CONSULTA}/v1/contratacoes/proposta
        dataFinal (AAAAMMDD, obrig.), codigoModalidadeContratacao (obrig.),
        uf, pagina (obrig.), tamanhoPagina (máx. 500)
        -> {data:[...], totalRegistros, totalPaginas, numeroPagina, paginasRestantes, empty}
        (Manual PNCP API Consultas v1.0)
  * GET {API}/v1/orgaos/{cnpj}/compras/{ano}/{sequencial}/itens
        (Manual de Integração PNCP, 11.13)
"""
from __future__ import annotations

import logging
import threading
import time

import httpx

from . import config

log = logging.getLogger("pncp")


class RateLimiter:
    def __init__(self, per_second: float):
        self.interval = 1.0 / per_second if per_second > 0 else 0
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self) -> None:
        if not self.interval:
            return
        with self._lock:
            now = time.monotonic()
            t = max(now, self._next)
            self._next = t + self.interval
        if t > now:
            time.sleep(t - now)


class PNCPError(Exception):
    pass


class PNCPClient:
    def __init__(self, transport: httpx.BaseTransport | None = None,
                 per_second: float | None = None):
        self.http = httpx.Client(
            timeout=config.HTTP_TIMEOUT,
            transport=transport,
            headers={"accept": "application/json",
                     "user-agent": "radar-licitacoes/1.0"},
            follow_redirects=True,
        )
        self.limiter = RateLimiter(config.REQ_POR_SEGUNDO if per_second is None else per_second)
        self.requisicoes = 0
        self._cnt_lock = threading.Lock()

    def close(self):
        self.http.close()

    def _get(self, url: str, params: dict | None = None, tentativas: int = 5):
        espera = 2.0
        for n in range(1, tentativas + 1):
            self.limiter.wait()
            with self._cnt_lock:
                self.requisicoes += 1
            try:
                r = self.http.get(url, params=params)
            except httpx.HTTPError as e:
                log.warning("GET %s falhou (%s) tentativa %d", url, e, n)
            else:
                if r.status_code == 204 or not r.content:
                    return None
                if r.status_code == 200:
                    return r.json()
                if r.status_code in (429, 500, 502, 503, 504):
                    log.warning("GET %s -> %d tentativa %d", url, r.status_code, n)
                else:
                    raise PNCPError(f"{r.status_code} em {r.url}: {r.text[:300]}")
            time.sleep(espera)
            espera = min(espera * 2, 60)
        raise PNCPError(f"Sem resposta após {tentativas} tentativas: {url} {params}")

    # ------------------------------------------------------------------
    def propostas_abertas(self, data_final: str, modalidade: int, uf: str | None = None):
        """Gera páginas (lista de contratações) com recebimento de propostas aberto."""
        url = f"{config.PNCP_CONSULTA_URL}/v1/contratacoes/proposta"
        pagina = 1
        while True:
            params = {"dataFinal": data_final,
                      "codigoModalidadeContratacao": modalidade,
                      "pagina": pagina,
                      "tamanhoPagina": config.PAGE_SIZE}
            if uf:
                params["uf"] = uf
            js = self._get(url, params)
            if not js:
                return
            data = js.get("data") or []
            yield data
            restantes = js.get("paginasRestantes")
            total_pag = js.get("totalPaginas")
            if not data or js.get("empty"):
                return
            if restantes is not None:
                if int(restantes) <= 0:
                    return
            elif total_pag is not None and pagina >= int(total_pag):
                return
            elif len(data) < config.PAGE_SIZE:
                return
            pagina += 1

    def itens(self, cnpj: str, ano: int, seq: int) -> list[dict]:
        """Itens de uma contratação. Tolera resposta como lista ou {itens|data:[...]}
        e paginação com ou sem suporte do servidor."""
        url = f"{config.PNCP_API_URL}/v1/orgaos/{cnpj}/compras/{ano}/{seq}/itens"
        todos: dict[int, dict] = {}
        pagina, tam = 1, 500
        while True:
            js = self._get(url, {"pagina": pagina, "tamanhoPagina": tam})
            if js is None:
                break
            if isinstance(js, dict):
                lista = js.get("itens") or js.get("data") or []
            else:
                lista = js
            novos = 0
            for it in lista:
                k = it.get("numeroItem")
                if k not in todos:
                    todos[k] = it
                    novos += 1
            # Para quando a página veio incompleta ou o servidor ignorou a paginação
            if len(lista) < tam or novos == 0 or pagina >= 20:
                break
            pagina += 1
        return list(todos.values())
