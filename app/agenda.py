"""Agendador: coleta pesada só na janela da madrugada; alerta uma vez por dia.

Regras (briefing da VPS, 2 núcleos):
  * nada roda ao subir o app — a decisão é sempre "já rodou nesta janela?",
    consultando a tabela `coletas`, então reinícios não repetem trabalho;
  * dentro da janela: 1) lista de editais + purga (uma vez por janela);
    2) itens pendentes em lotes com pausa, até a janela fechar;
  * no horário de alerta: uma rodada de alertas por dia.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from . import collector, config, db
from .collector import iso

log = logging.getLogger("agenda")


def _min(hm: tuple[int, int]) -> int:
    return hm[0] * 60 + hm[1]


def na_janela(dt: datetime) -> bool:
    m = dt.hour * 60 + dt.minute
    ini, fim = _min(config.JANELA_INI), _min(config.JANELA_FIM)
    return ini <= m < fim if ini < fim else (m >= ini or m < fim)   # suporta janela que vira a meia-noite


def inicio_janela(dt: datetime) -> datetime:
    """Início da janela que contém `dt` (assume dt dentro da janela)."""
    h, m = config.JANELA_INI
    ini = dt.replace(hour=h, minute=m, second=0, microsecond=0)
    return ini if ini <= dt else ini - timedelta(days=1)


def _rodou_desde(tipo: str, desde: datetime, msg_prefix: str | None = None,
                 qualquer_status: bool = False) -> bool:
    sql = "SELECT 1 FROM coletas WHERE tipo=? AND inicio>=?"
    if not qualquer_status:
        sql += " AND status IN ('ok','parcial')"
    params: list = [tipo, iso(desde)]
    if msg_prefix:
        sql += " AND msg LIKE ?"
        params.append(msg_prefix + "%")
    with db.conn() as con:
        return con.execute(sql + " LIMIT 1", params).fetchone() is not None


def _itens_pendentes(agora_dt: datetime) -> int:
    with db.conn() as con:
        return con.execute(
            "SELECT COUNT(*) FROM contratacoes WHERE (itens_status='pendente' "
            "OR (itens_status='erro' AND itens_tentativas<3)) "
            "AND (data_encerramento IS NULL OR data_encerramento >= ?)",
            (iso(agora_dt),)).fetchone()[0]


def tick(agora_dt: datetime | None = None, client=None, sender=None, relogio=None) -> str | None:
    """Uma verificação do agendador. Devolve o que executou (ou None).
    `relogio` permite simular o tempo nos testes."""
    relogio = relogio or collector.agora
    agora_dt = agora_dt or relogio()
    # Coleta manual (ou outra) em andamento: não disputa a trava. Sem isto, o
    # ciclo voltaria "ignorado" na hora e o laço chamaria o tick sem pausa.
    if collector.em_andamento():
        return None
    if na_janela(agora_dt):
        ini = inicio_janela(agora_dt)
        # se falhou (PNCP fora do ar), tenta de novo só depois de 30 min
        if not _rodou_desde("editais", ini) and \
                not _rodou_desde("editais", agora_dt - timedelta(minutes=30), qualquer_status=True):
            collector.ciclo("editais", client=client, origem="agenda")
            return "editais"
        if _itens_pendentes(agora_dt):
            collector.ciclo("itens", client=client, ate=lambda: na_janela(relogio()), origem="agenda")
            return "itens"
    alerta = agora_dt.replace(hour=config.ALERTA_HM[0], minute=config.ALERTA_HM[1], second=0, microsecond=0)
    if agora_dt >= alerta and not _rodou_desde("alertas", alerta, "agenda"):
        collector.ciclo("alertas", sender=sender, origem="agenda")
        return "alertas"
    return None


def proximas(agora_dt: datetime | None = None) -> dict:
    agora_dt = agora_dt or collector.agora()
    h, m = config.JANELA_INI
    jan = agora_dt.replace(hour=h, minute=m, second=0, microsecond=0)
    if jan <= agora_dt:
        jan += timedelta(days=1)
    ah, am = config.ALERTA_HM
    al = agora_dt.replace(hour=ah, minute=am, second=0, microsecond=0)
    if al <= agora_dt:
        al += timedelta(days=1)
    return {"coleta": jan, "alertas": al, "na_janela": na_janela(agora_dt)}
