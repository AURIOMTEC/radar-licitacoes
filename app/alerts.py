"""Buscas salvas -> alertas no WhatsApp dos editais novos que casam."""
from __future__ import annotations

import logging
import time
from datetime import datetime

from . import config, db
from .collector import agora, iso
from .search import Filtros, ids_que_casam, url_pncp
from .whatsapp import get_sender, numeros

log = logging.getLogger("alertas")


def filtros_da_busca(b) -> Filtros:
    return Filtros(
        consulta=b["consulta"],
        ufs=[u for u in (b["ufs"] or "").split(",") if u],
        modalidades=[int(m) for m in (b["modalidades"] or "").split(",") if m.strip().isdigit()],
        valor_min=b["valor_min"], valor_max=b["valor_max"],
        prazo_min_dias=b["prazo_min"] if "prazo_min" in b.keys() else 0)


_SQL_NAO_AVISADOS = " AND c.id NOT IN (SELECT contratacao_id FROM alertas WHERE busca_id=?)"


def marcar_base(busca_id: int) -> int:
    """Marca o que já casa hoje como 'base' para não disparar alerta retroativo."""
    with db.conn() as con:
        b = con.execute("SELECT * FROM buscas WHERE id=?", (busca_id,)).fetchone()
    ids = ids_que_casam(filtros_da_busca(b), _SQL_NAO_AVISADOS, [busca_id])
    agora_iso = iso(agora())
    with db.conn() as con:
        con.executemany("INSERT OR IGNORE INTO alertas(busca_id, contratacao_id, tipo, em) VALUES (?,?,?,?)",
                        [(busca_id, i, "base", agora_iso) for i in ids])
    return len(ids)


def _brl(v) -> str:
    if v is None:
        return "sigiloso/não informado"
    s = f"{v:,.2f}"
    return "R$ " + s.replace(",", "X").replace(".", ",").replace("X", ".")


def _data(s: str | None) -> str:
    if not s:
        return "-"
    try:
        return datetime.fromisoformat(s).strftime("%d/%m %H:%M")
    except ValueError:
        return s


def formatar(busca, linhas: list, itens: dict, total: int) -> list[str]:
    msgs, bloco = [], []
    cab = (f"*Radar de Licitações* — busca \"{busca['nome']}\"\n"
           f"{total} edital(is) novo(s):\n")
    for n, c in enumerate(linhas, 1):
        obj = (c["objeto"] or "").replace("\n", " ")
        if len(obj) > 220:
            obj = obj[:217] + "..."
        local = f"{c['municipio'] or ''}/{c['uf'] or ''}"
        txt = (f"\n*{n})* {c['modalidade_nome'] or ''} · {c['orgao_nome'] or ''} — {local}\n"
               f"{obj}\n"
               f"💰 {_brl(c['valor_estimado'])} · ⏳ propostas até {_data(c['data_encerramento'])}\n")
        for it in itens.get(c["id"], [])[:2]:
            d = (it["descricao"] or "")[:120]
            q = f"{it['quantidade']:g} {it['unidade'] or ''}".strip() if it["quantidade"] else ""
            txt += f"• Item {it['numero']}: {d}{' — ' + q if q else ''}\n"
        txt += f"🔗 {url_pncp(dict(c))}\n"
        bloco.append(txt)
        if len(bloco) == config.ALERTA_MAX_POR_MSG:
            msgs.append(bloco)
            bloco = []
    if bloco:
        msgs.append(bloco)
    out = []
    for i, b in enumerate(msgs):
        head = cab if i == 0 else f"*Radar de Licitações* — \"{busca['nome']}\" (cont. {i + 1}/{len(msgs)})\n"
        out.append(head + "".join(b))
    from urllib.parse import quote
    out[-1] += f"\nVer no app: {config.APP_PUBLIC_URL}/?q={quote(busca['consulta'])}"
    return out


def processar_alertas(sender=None, busca_id: int | None = None, origem: str = "manual") -> dict:
    sender = sender or get_sender()
    stats = {"buscas": 0, "editais": 0, "mensagens": 0, "falhas": 0}
    with db.conn() as con:
        q = "SELECT * FROM buscas WHERE ativo=1"
        buscas = con.execute(q + (" AND id=?" if busca_id else ""),
                             ((busca_id,) if busca_id else ())).fetchall()
    for b in buscas:
        dest = numeros(b["whatsapp"])
        if not dest:
            continue
        stats["buscas"] += 1
        try:
            ids = ids_que_casam(filtros_da_busca(b), _SQL_NAO_AVISADOS, [b["id"]])
        except ValueError as e:
            log.warning("Busca %s inválida: %s", b["id"], e)
            continue
        if not ids:
            continue
        with db.conn() as con:
            ph = ",".join("?" * len(ids))
            linhas = con.execute(
                f"SELECT * FROM contratacoes WHERE id IN ({ph}) "
                f"ORDER BY data_encerramento IS NULL, data_encerramento", ids).fetchall()
            from .query import to_fts
            itens: dict = {}
            for r in con.execute(
                    f"SELECT i.* FROM itens_fts JOIN itens i ON i.id=itens_fts.rowid "
                    f"WHERE itens_fts MATCH ? AND i.contratacao_id IN ({ph}) ORDER BY i.numero",
                    [to_fts(b["consulta"])] + ids):
                itens.setdefault(r["contratacao_id"], []).append(r)
        msgs = formatar(b, linhas, itens, len(linhas))
        ok = False
        for numero in dest:
            for m in msgs:
                try:
                    sender(numero, m)
                    stats["mensagens"] += 1
                    ok = True
                    time.sleep(1.0)   # espaça envios (boa prática com WhatsApp)
                except Exception as e:  # noqa: BLE001
                    stats["falhas"] += 1
                    log.error("Falha ao enviar para %s: %s", numero, e)
                    break
        if ok:  # só registra se ao menos um destino recebeu; senão tenta no próximo ciclo
            agora_iso = iso(agora())
            with db.conn() as con:
                con.executemany(
                    "INSERT OR IGNORE INTO alertas(busca_id, contratacao_id, tipo, em) VALUES (?,?,?,?)",
                    [(b["id"], i, "enviado", agora_iso) for i in ids])
            stats["editais"] += len(ids)
    with db.conn() as con:
        lid = con.execute("INSERT INTO coletas(tipo, inicio, fim, status, novos, erros, msg) "
                          "VALUES ('alertas', ?, ?, ?, ?, ?, ?)",
                          (iso(agora()), iso(agora()), "ok" if not stats["falhas"] else "parcial",
                           stats["editais"], stats["falhas"],
                           f"{origem}: {stats['mensagens']} mensagens, {stats['buscas']} buscas")).lastrowid
    return stats
