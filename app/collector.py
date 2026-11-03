"""Coleta de editais abertos (Brasil inteiro) e dos itens de cada edital.

Uso manual:  python -m app.collector [editais|itens|purga|alertas|tudo]
"""
from __future__ import annotations

import logging
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

from . import config, db
from .pncp import PNCPClient, PNCPError

log = logging.getLogger("coletor")
_run_lock = threading.Lock()


def agora() -> datetime:
    return datetime.now(config.TZ).replace(tzinfo=None, microsecond=0)


def iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _sub(d: dict | None, k: str):
    return (d or {}).get(k)


def mapear(c: dict) -> dict:
    """Campos do retorno de contratação conforme o Manual de Consultas (seção 6)."""
    org = c.get("orgaoEntidade") or {}
    uni = c.get("unidadeOrgao") or {}
    return {
        "ctrl": c.get("numeroControlePNCP"),
        "cnpj": org.get("cnpj"),
        "ano": c.get("anoCompra"),
        "seq": c.get("sequencialCompra"),
        "numero_compra": c.get("numeroCompra"),
        "processo": c.get("processo"),
        "objeto": (c.get("objetoCompra") or "").strip(),
        "info_compl": (c.get("informacaoComplementar") or "").strip(),
        "modalidade_id": c.get("modalidadeId"),
        "modalidade_nome": c.get("modalidadeNome"),
        "modo_disputa_nome": c.get("modoDisputaNome"),
        "situacao_nome": c.get("situacaoCompraNome"),
        "srp": 1 if c.get("srp") else 0,
        "valor_estimado": c.get("valorTotalEstimado"),
        "data_abertura": c.get("dataAberturaProposta"),
        "data_encerramento": c.get("dataEncerramentoProposta"),
        "data_publicacao": c.get("dataPublicacaoPncp"),
        "data_atualizacao": c.get("dataAtualizacao"),
        "orgao_cnpj": org.get("cnpj"),
        # o manual grafa "razaosocial" (minúsculo) neste serviço; aceita as duas formas
        "orgao_nome": org.get("razaosocial") or org.get("razaoSocial"),
        "esfera_id": org.get("esferaId"),
        "unidade_nome": uni.get("nomeUnidade"),
        "municipio": uni.get("municipioNome"),
        "uf": uni.get("ufSigla"),
        "link_origem": c.get("linkSistemaOrigem"),
    }


CAMPOS = [k for k in mapear({}).keys() if k != "ctrl"]


def upsert(con, c: dict, visto_em: str) -> str:
    m = mapear(c)
    if not m["ctrl"] or not m["cnpj"] or m["ano"] is None or m["seq"] is None:
        return "ignorado"
    row = con.execute("SELECT id, data_atualizacao FROM contratacoes WHERE ctrl=?",
                      (m["ctrl"],)).fetchone()
    if row is None:
        # não (re)insere o que já encerrou — evita "ressuscitar" o que a purga removeu
        if m["data_encerramento"] and m["data_encerramento"] < visto_em:
            return "ignorado"
        cols = ["ctrl"] + CAMPOS + ["first_seen", "last_seen"]
        vals = [m["ctrl"]] + [m[k] for k in CAMPOS] + [visto_em, visto_em]
        cur = con.execute(
            f"INSERT INTO contratacoes ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
            vals)
        db.fts_contratacao(con, cur.lastrowid)
        return "novo"
    sets = ", ".join(f"{k}=?" for k in CAMPOS) + ", last_seen=?"
    vals = [m[k] for k in CAMPOS] + [visto_em]
    mudou = (row["data_atualizacao"] or "") != (m["data_atualizacao"] or "")
    if mudou:  # retificação: reindexa e busca os itens de novo
        sets += ", itens_status='pendente', itens_tentativas=0"
    con.execute(f"UPDATE contratacoes SET {sets} WHERE id=?", vals + [row["id"]])
    if mudou:
        db.fts_contratacao(con, row["id"])
        return "atualizado"
    return "visto"


def _abrir_log(con, tipo: str) -> int:
    return con.execute("INSERT INTO coletas(tipo, inicio, status) VALUES (?,?,?)",
                       (tipo, iso(agora()), "rodando")).lastrowid


def _fechar_log(con, log_id: int, status: str, **kw):
    kw.setdefault("msg", None)
    sets = ", ".join(f"{k}=?" for k in kw)
    con.execute(f"UPDATE coletas SET fim=?, status=?, {sets} WHERE id=?",
                [iso(agora()), status, *kw.values(), log_id])


# ---------------------------------------------------------------------------
def coletar_editais(client: PNCPClient) -> dict:
    inicio = agora()
    visto = iso(inicio)
    # dataFinal = limite de encerramento das propostas (ver config.DATA_FINAL_DIAS)
    data_final = (inicio + timedelta(days=config.DATA_FINAL_DIAS)).strftime("%Y%m%d")
    stats = {"novos": 0, "atualizados": 0, "erros": 0}
    erros_msg = []
    with db.conn() as con:
        log_id = _abrir_log(con, "editais")
    req0 = client.requisicoes
    for mod in config.MODALIDADES_COLETA:
        try:
            for pagina in client.propostas_abertas(data_final, mod):
                with db.conn() as con:
                    for c in pagina:
                        r = upsert(con, c, visto)
                        if r == "novo":
                            stats["novos"] += 1
                        elif r == "atualizado":
                            stats["atualizados"] += 1
        except (PNCPError, ValueError) as e:
            stats["erros"] += 1
            erros_msg.append(f"modalidade {mod}: {e}")
            log.error("Falha na modalidade %s: %s", mod, e)
    status = "ok" if not stats["erros"] else ("parcial" if stats["erros"] < len(config.MODALIDADES_COLETA) else "erro")
    with db.conn() as con:
        _fechar_log(con, log_id, status, novos=stats["novos"], atualizados=stats["atualizados"],
                    requisicoes=client.requisicoes - req0, erros=stats["erros"],
                    msg="; ".join(erros_msg)[:2000] or None)
    log.info("Editais: %s", stats)
    stats["status"] = status
    return stats


def _num(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def gravar_itens(con, contratacao_id: int, itens: list[dict]) -> None:
    db.apagar_itens(con, contratacao_id)
    for it in itens:
        cur = con.execute(
            "INSERT OR REPLACE INTO itens(contratacao_id, numero, descricao, info_compl, quantidade,"
            " unidade, valor_unit, valor_total, tipo, ncm, sigiloso) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (contratacao_id, it.get("numeroItem"), (it.get("descricao") or "").strip(),
             (it.get("informacaoComplementar") or "").strip(), _num(it.get("quantidade")),
             it.get("unidadeMedida"), _num(it.get("valorUnitarioEstimado")),
             _num(it.get("valorTotal")), it.get("materialOuServicoNome"),
             it.get("ncmNbsCodigo"), 1 if it.get("orcamentoSigiloso") else 0))
        con.execute("INSERT INTO itens_fts(rowid, descricao, info_compl) VALUES (?,?,?)",
                    (cur.lastrowid, (it.get("descricao") or ""), (it.get("informacaoComplementar") or "")))
    con.execute("UPDATE contratacoes SET itens_status='ok', itens_count=? WHERE id=?",
                (len(itens), contratacao_id))


def coletar_itens(client: PNCPClient, limite: int | None = None, ate=None) -> dict:
    """Baixa itens em lotes de ITENS_LOTE com pausa entre lotes.
    `ate`: função que devolve False quando é hora de parar (fim da janela)."""
    limite = limite or config.ITENS_POR_CICLO
    total = {"ok": 0, "erros": 0, "fila": 0, "lotes": 0}
    tentados: set[int] = set()   # cada edital no máximo 1x por execução
    while total["ok"] + total["erros"] < limite:
        if ate is not None and not ate():
            break
        n = min(config.ITENS_LOTE, limite - total["ok"] - total["erros"])
        st = _lote_itens(client, n, tentados)
        total["lotes"] += 1
        total["ok"] += st["ok"]
        total["erros"] += st["erros"]
        total["fila"] = max(total["fila"], st["fila"])
        if st["fila"] < n:      # fila esvaziou
            break
        time.sleep(config.ITENS_PAUSA_S)
    return total


def _lote_itens(client: PNCPClient, limite: int, tentados: set[int]) -> dict:
    agora_iso = iso(agora())
    with db.conn() as con:
        log_id = _abrir_log(con, "itens")
        pend = con.execute(
            "SELECT id, cnpj, ano, seq FROM contratacoes "
            "WHERE (itens_status='pendente' OR (itens_status='erro' AND itens_tentativas<3)) "
            "AND (data_encerramento IS NULL OR data_encerramento >= ?) "
            "ORDER BY first_seen DESC, id DESC", (agora_iso,)).fetchall()
        pend = [r for r in pend if r["id"] not in tentados][:limite]
        tentados.update(r["id"] for r in pend)
    stats = {"ok": 0, "erros": 0, "fila": len(pend)}
    req0 = client.requisicoes
    con = db.connect()
    try:
        with ThreadPoolExecutor(max_workers=max(1, config.ITENS_CONCORRENCIA)) as ex:
            futs = {ex.submit(client.itens, r["cnpj"], r["ano"], r["seq"]): r["id"] for r in pend}
            n = 0
            for f in as_completed(futs):
                cid = futs[f]
                try:
                    gravar_itens(con, cid, f.result())
                    stats["ok"] += 1
                except Exception as e:  # noqa: BLE001
                    stats["erros"] += 1
                    con.execute("UPDATE contratacoes SET itens_status='erro', "
                                "itens_tentativas=itens_tentativas+1 WHERE id=?", (cid,))
                    log.warning("Itens de %s: %s", cid, e)
                n += 1
                if n % 50 == 0:
                    con.commit()
        con.commit()
    finally:
        con.close()
    with db.conn() as c2:
        _fechar_log(c2, log_id, "ok" if not stats["erros"] else "parcial",
                    novos=stats["ok"], requisicoes=client.requisicoes - req0, erros=stats["erros"],
                    msg=f"fila {stats['fila']}")
    log.info("Itens: %s", stats)
    return stats


def purgar() -> int:
    agora_dt = agora()
    corte = iso(agora_dt - timedelta(days=config.PURGA_DIAS))
    with db.conn() as con:
        ult = con.execute("SELECT inicio FROM coletas WHERE tipo='editais' AND status='ok' "
                          "ORDER BY id DESC LIMIT 1").fetchone()
        ids = [r[0] for r in con.execute(
            "SELECT id FROM contratacoes WHERE data_encerramento IS NOT NULL AND data_encerramento < ?",
            (corte,))]
        if ult:  # sumiu da API (cancelada/encerrada) há mais de PURGA_DIAS
            corte2 = iso(datetime.fromisoformat(ult["inicio"]) - timedelta(days=config.PURGA_DIAS))
            ids += [r[0] for r in con.execute(
                "SELECT id FROM contratacoes WHERE last_seen < ?", (corte2,))]
        ids = sorted(set(ids))
        if ids:
            db.apagar_contratacoes(con, ids)
        lid = _abrir_log(con, "purga")
        _fechar_log(con, lid, "ok", novos=0, atualizados=len(ids), msg=f"{len(ids)} removidos")
    return len(ids)


def ciclo(tipo: str = "tudo", client: PNCPClient | None = None, sender=None,
          ate=None, origem: str = "manual", limite_itens: int | None = None) -> dict:
    """Executa um ciclo; ignora se já houver outro rodando.
    tipo: editais | itens | purga | alertas | tudo"""
    if not _run_lock.acquire(blocking=False):
        return {"ignorado": "já existe uma coleta em andamento"}
    own = client is None
    client = client or PNCPClient()
    out: dict = {}
    try:
        if tipo in ("editais", "tudo"):
            out["editais"] = coletar_editais(client)
        if tipo in ("purga", "editais", "tudo"):
            out["purga"] = purgar()
        if tipo in ("itens", "tudo"):
            out["itens"] = coletar_itens(client, limite=limite_itens, ate=ate)
        if tipo in ("alertas", "tudo"):
            from .alerts import processar_alertas
            out["alertas"] = processar_alertas(sender=sender, origem=origem)
    finally:
        if own:
            client.close()
        _run_lock.release()
    return out


def em_andamento() -> bool:
    return _run_lock.locked()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    db.init()
    print(ciclo(sys.argv[1] if len(sys.argv) > 1 else "tudo"))
