"""Busca no objeto/órgão/local do edital e na descrição dos itens."""
from __future__ import annotations

import html
from dataclasses import dataclass, field
from datetime import timedelta

from . import config, db
from .collector import agora, iso
from .query import negativos_fts, to_fts

ORDENS = {
    "encerramento": "c.data_encerramento IS NULL, c.data_encerramento ASC",
    "relevancia": "a.sc ASC",
    "valor": "c.valor_estimado IS NULL, c.valor_estimado DESC",
    "publicacao": "c.data_publicacao DESC",
}


@dataclass
class Filtros:
    consulta: str
    ufs: list[str] = field(default_factory=list)
    modalidades: list[int] = field(default_factory=list)
    valor_min: float | None = None
    valor_max: float | None = None
    prazo_min_dias: int = 0      # encerramento a pelo menos N dias de agora
    incluir_encerrados: bool = False
    ordem: str = "encerramento"


def _where(f: Filtros, params: list) -> str:
    w = []
    if not f.incluir_encerrados:
        limite = agora() + timedelta(days=max(0, f.prazo_min_dias or 0))
        w.append("(c.data_encerramento IS NULL OR c.data_encerramento >= ?)")
        params.append(iso(limite))
    if f.ufs:
        w.append(f"c.uf IN ({','.join('?' * len(f.ufs))})")
        params += f.ufs
    if f.modalidades:
        w.append(f"c.modalidade_id IN ({','.join('?' * len(f.modalidades))})")
        params += f.modalidades
    if f.valor_min is not None:
        w.append("c.valor_estimado >= ?")
        params.append(f.valor_min)
    if f.valor_max is not None:
        w.append("c.valor_estimado <= ?")
        params.append(f.valor_max)
    return (" AND " + " AND ".join(w)) if w else ""


def _base_sql(f: Filtros, params: list, extra_where: str = "") -> str:
    q = to_fts(f.consulta)
    neg = negativos_fts(f.consulta)
    params += [q, q]
    sql = """
    WITH hits AS (
        SELECT rowid AS cid, bm25(contratacoes_fts, 10.0, 3.0, 1.0, 0.5) AS sc, 1 AS via
          FROM contratacoes_fts WHERE contratacoes_fts MATCH ?
        UNION ALL
        SELECT i.contratacao_id, bm25(itens_fts, 6.0, 1.5), 2
          FROM itens_fts JOIN itens i ON i.id = itens_fts.rowid
         WHERE itens_fts MATCH ?
    ),
    a AS (SELECT cid, MIN(sc) AS sc, MAX(via = 1) AS via_obj, MAX(via = 2) AS via_item
            FROM hits GROUP BY cid)
    SELECT c.*, a.sc, a.via_obj, a.via_item
      FROM a JOIN contratacoes c ON c.id = a.cid
     WHERE 1=1"""
    if neg:
        sql += " AND c.id NOT IN (SELECT rowid FROM contratacoes_fts WHERE contratacoes_fts MATCH ?)"
        params.append(neg)
    sql += _where(f, params) + extra_where
    return sql


def ids_que_casam(f: Filtros, entre_ids_sql: str = "", extra_params: list | None = None) -> list[int]:
    params: list = []
    sql = _base_sql(f, params, entre_ids_sql)
    params += extra_params or []
    with db.conn() as con:
        return [r["id"] for r in con.execute(sql, params)]


def _marcar(texto: str) -> str:
    return (html.escape(texto or "")
            .replace("\x02", "<mark>").replace("\x03", "</mark>"))


def buscar(f: Filtros, pagina: int = 1, por_pagina: int = 40) -> dict:
    params: list = []
    sql = _base_sql(f, params)
    ordem = ORDENS.get(f.ordem, ORDENS["encerramento"])
    with db.conn() as con:
        total = con.execute(f"SELECT COUNT(*) FROM ({sql})", params).fetchone()[0]
        soma = con.execute(f"SELECT SUM(valor_estimado) FROM ({sql})", params).fetchone()[0]
        rows = con.execute(f"{sql} ORDER BY {ordem}, c.id LIMIT ? OFFSET ?",
                           params + [por_pagina, (pagina - 1) * por_pagina]).fetchall()
        ids = [r["id"] for r in rows]
        q = to_fts(f.consulta)
        destaques, itens = {}, {}
        if ids:
            ph = ",".join("?" * len(ids))
            for r in con.execute(
                    f"SELECT rowid, highlight(contratacoes_fts, 0, char(2), char(3)) h "
                    f"FROM contratacoes_fts WHERE contratacoes_fts MATCH ? AND rowid IN ({ph})",
                    [q] + ids):
                destaques[r["rowid"]] = _marcar(r["h"])
            for r in con.execute(
                    f"SELECT i.contratacao_id cid, i.numero, i.quantidade, i.unidade, i.valor_unit,"
                    f" i.sigiloso, highlight(itens_fts, 0, char(2), char(3)) h "
                    f"FROM itens_fts JOIN itens i ON i.id = itens_fts.rowid "
                    f"WHERE itens_fts MATCH ? AND i.contratacao_id IN ({ph}) "
                    f"ORDER BY i.contratacao_id, i.numero", [q] + ids):
                lst = itens.setdefault(r["cid"], [])
                lst.append({**dict(r), "h": _marcar(r["h"])})
    resultados = []
    for r in rows:
        d = dict(r)
        d["objeto_html"] = destaques.get(r["id"]) or html.escape(r["objeto"] or "")
        d["itens_casados"] = itens.get(r["id"], [])
        d["url_pncp"] = url_pncp(d)
        resultados.append(d)
    return {"total": total, "soma": soma, "resultados": resultados,
            "pagina": pagina, "paginas": max(1, -(-total // por_pagina))}


def url_pncp(c: dict) -> str:
    return f"{config.PNCP_PORTAL_URL}/app/editais/{c['cnpj']}/{c['ano']}/{c['seq']}"
