"""Radar de Licitações — app web (FastAPI)."""
from __future__ import annotations

import base64
import csv
import io
import logging
import secrets
import threading
import time
from datetime import datetime
from urllib.parse import urlencode

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware

from . import agenda, alerts, collector, config, db, whatsapp
from .query import ConsultaInvalida
from .search import Filtros, buscar, url_pncp
from .versao import VERSAO

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("app")

app = FastAPI(title="Radar de Licitações", docs_url=None, redoc_url=None)
BASE = __file__.rsplit("/", 1)[0]
app.mount("/static", StaticFiles(directory=f"{BASE}/static"), name="static")
tpl = Jinja2Templates(directory=f"{BASE}/templates")


# ---------------------------------------------------------------- auth
class BasicAuth(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        if not config.APP_USER or request.url.path == "/saude":
            return await call_next(request)
        h = request.headers.get("authorization", "")
        if h.startswith("Basic "):
            try:
                u, _, p = base64.b64decode(h[6:]).decode().partition(":")
                if secrets.compare_digest(u, config.APP_USER) and secrets.compare_digest(p, config.APP_PASSWORD):
                    return await call_next(request)
            except Exception:  # noqa: BLE001
                pass
        return Response("Autenticação necessária", 401, {"WWW-Authenticate": 'Basic realm="Radar"'})


app.add_middleware(BasicAuth)


# ---------------------------------------------------------------- filtros jinja
def brl(v):
    if v is None:
        return "—"
    return "R$ " + f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def dt(s, fmt="%d/%m/%Y %H:%M"):
    if not s:
        return "—"
    try:
        return datetime.fromisoformat(str(s)).strftime(fmt)
    except ValueError:
        return s


def prazo(s):
    """Texto curto do tempo restante até o encerramento."""
    if not s:
        return ("", "")
    try:
        d = datetime.fromisoformat(s) - collector.agora()
    except ValueError:
        return ("", "")
    h = d.total_seconds() / 3600
    if h < 0:
        return ("encerrado", "enc")
    if h < 48:
        return (f"{int(h)}h restantes", "urg")
    return (f"{int(h // 24)} dias", "ok")


def num(v):
    return f"{v:g}".replace(".", ",") if isinstance(v, (int, float)) else (v or "")


tpl.env.filters.update(brl=brl, dt=dt, prazo=prazo, num=num)
tpl.env.globals.update(PRAZOS=config.PRAZO_OPCOES,
                       UFS=config.UFS, MODALIDADES=config.MODALIDADES,
                       COLETA=config.MODALIDADES_COLETA, VERSAO=VERSAO)


def valor(s: str | None):
    s = (s or "").strip().replace("R$", "").replace(" ", "")
    if not s:
        return None
    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def _prazo(v: str | None) -> int:
    if v is None or v == "":
        return config.PRAZO_MIN_PADRAO
    return int(v) if v.isdigit() else config.PRAZO_MIN_PADRAO


def filtros_req(req: Request) -> Filtros:
    qp = req.query_params
    return Filtros(
        consulta=qp.get("q", ""),
        ufs=[u for u in qp.getlist("uf") if u in config.UFS],
        modalidades=[int(m) for m in qp.getlist("mod") if m.isdigit()],
        valor_min=valor(qp.get("vmin")), valor_max=valor(qp.get("vmax")),
        incluir_encerrados=qp.get("enc") == "1",
        prazo_min_dias=_prazo(qp.get("prazo")),
        ordem=qp.get("ordem", "encerramento"))


# ---------------------------------------------------------------- páginas
@app.get("/", response_class=HTMLResponse)
def index(request: Request, p: int = 1):
    f = filtros_req(request)
    res, erro = None, None
    if f.consulta.strip():
        try:
            res = buscar(f, pagina=max(1, p))
        except ConsultaInvalida as e:
            erro = str(e)
        except Exception as e:  # erro de sintaxe FTS etc.
            log.exception("busca")
            erro = f"Não entendi a busca ({e}). Use palavras, \"frases\", OU e -exclusão."
    with db.conn() as con:
        tot = con.execute("SELECT COUNT(*) FROM contratacoes").fetchone()[0]
        ult = con.execute("SELECT fim FROM coletas WHERE tipo='editais' AND status IN ('ok','parcial') "
                          "ORDER BY id DESC LIMIT 1").fetchone()
    qs = [(k, v) for k, v in request.query_params.multi_items() if k != "p"]
    return tpl.TemplateResponse(request, "index.html", {
        "f": f, "res": res, "erro": erro, "total_base": tot,
        "ultima": ult["fim"] if ult else None, "qs": urlencode(qs),
        "rodando": collector.em_andamento()})


@app.get("/export.csv")
def export_csv(request: Request):
    f = filtros_req(request)
    res = buscar(f, pagina=1, por_pagina=5000)
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["Encerramento propostas", "Modalidade", "Órgão", "Unidade", "Município", "UF",
                "Objeto", "Valor estimado", "Itens que casaram", "Link PNCP", "Link sistema de origem"])
    for c in res["resultados"]:
        its = " | ".join(f"{i['numero']}: {i['h']}".replace("<mark>", "").replace("</mark>", "")
                         for i in c["itens_casados"])
        w.writerow([dt(c["data_encerramento"]), c["modalidade_nome"], c["orgao_nome"], c["unidade_nome"],
                    c["municipio"], c["uf"], c["objeto"],
                    f"{c['valor_estimado']:.2f}".replace(".", ",") if c["valor_estimado"] else "",
                    its, c["url_pncp"], c["link_origem"]])
    data = "﻿" + buf.getvalue()  # BOM para o Excel abrir com acentos
    return Response(data, media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="licitacoes.csv"'})


@app.get("/edital/{cid}", response_class=HTMLResponse)
def edital(request: Request, cid: int):
    with db.conn() as con:
        c = con.execute("SELECT * FROM contratacoes WHERE id=?", (cid,)).fetchone()
        if not c:
            return HTMLResponse("Edital não encontrado (pode ter sido encerrado e removido).", 404)
        its = con.execute("SELECT * FROM itens WHERE contratacao_id=? ORDER BY numero", (cid,)).fetchall()
    return tpl.TemplateResponse(request, "edital.html", {"c": c, "itens": its, "url": url_pncp(dict(c))})


# ---------------------------------------------------------------- buscas salvas
@app.get("/buscas", response_class=HTMLResponse)
def buscas(request: Request, msg: str = ""):
    with db.conn() as con:
        rows = con.execute(
            "SELECT b.*, (SELECT COUNT(*) FROM alertas a WHERE a.busca_id=b.id AND a.tipo='enviado') enviados,"
            " (SELECT MAX(em) FROM alertas a WHERE a.busca_id=b.id AND a.tipo='enviado') ultimo "
            "FROM buscas b ORDER BY b.nome").fetchall()
    return tpl.TemplateResponse(request, "buscas.html", {
        "buscas": rows, "msg": msg, "wa_ativo": whatsapp.ativo(), "provider": config.WHATSAPP_PROVIDER})


@app.get("/buscas/nova", response_class=HTMLResponse)
@app.get("/buscas/{bid}/editar", response_class=HTMLResponse)
def busca_form(request: Request, bid: int | None = None):
    if bid:
        with db.conn() as con:
            b = dict(con.execute("SELECT * FROM buscas WHERE id=?", (bid,)).fetchone())
    else:
        f = filtros_req(request)
        b = {"id": None, "nome": f.consulta, "consulta": f.consulta, "ufs": ",".join(f.ufs),
             "modalidades": ",".join(map(str, f.modalidades)), "valor_min": f.valor_min,
             "valor_max": f.valor_max, "whatsapp": "", "ativo": 1, "prazo_min": f.prazo_min_dias}
    b["ufs_l"] = [u for u in (b["ufs"] or "").split(",") if u]
    b["mods_l"] = [int(m) for m in (b["modalidades"] or "").split(",") if m.strip().isdigit()]
    return tpl.TemplateResponse(request, "busca_form.html", {"b": b, "wa_ativo": whatsapp.ativo()})


@app.post("/buscas/salvar")
async def busca_salvar(request: Request):
    fm = await request.form()
    bid = fm.get("id")
    consulta = (fm.get("consulta") or "").strip()
    try:
        from .query import to_fts
        to_fts(consulta)
    except ConsultaInvalida as e:
        return RedirectResponse(f"/buscas?{urlencode({'msg': 'Erro: ' + str(e)})}", 303)
    dados = {
        "nome": (fm.get("nome") or consulta).strip()[:80],
        "consulta": consulta,
        "ufs": ",".join(u for u in fm.getlist("uf") if u in config.UFS),
        "modalidades": ",".join(m for m in fm.getlist("mod") if m.isdigit()),
        "valor_min": valor(fm.get("vmin")), "valor_max": valor(fm.get("vmax")),
        "prazo_min": _prazo(fm.get("prazo")),
        "whatsapp": ", ".join(whatsapp.numeros(fm.get("whatsapp") or "")),
        "ativo": 1 if fm.get("ativo") else 0,
    }
    with db.conn() as con:
        if bid:
            con.execute(f"UPDATE buscas SET {', '.join(k + '=?' for k in dados)} WHERE id=?",
                        [*dados.values(), int(bid)])
            bid = int(bid)
        else:
            bid = con.execute(
                f"INSERT INTO buscas({', '.join(dados)}, criado_em) VALUES ({','.join('?' * (len(dados) + 1))})",
                [*dados.values(), collector.iso(collector.agora())]).lastrowid
    if fm.get("retroativo"):
        st = alerts.processar_alertas(busca_id=bid)
        msg = f"Busca salva. Enviados {st['editais']} edital(is) já abertos em {st['mensagens']} mensagem(ns)."
    else:
        n = alerts.marcar_base(bid)
        msg = (f"Busca salva. {n} edital(is) já abertos foram marcados como vistos; "
               f"só os novos vão gerar alerta.")
    return RedirectResponse(f"/buscas?{urlencode({'msg': msg})}", 303)


@app.post("/buscas/{bid}/excluir")
def busca_excluir(bid: int):
    with db.conn() as con:
        con.execute("DELETE FROM buscas WHERE id=?", (bid,))
    return RedirectResponse("/buscas?msg=Busca+exclu%C3%ADda", 303)


@app.post("/whatsapp/teste")
def wa_teste(numero: str = Form(...)):
    nums = whatsapp.numeros(numero)
    if not nums:
        return RedirectResponse("/buscas?msg=N%C3%BAmero+inv%C3%A1lido", 303)
    try:
        whatsapp.get_sender()(nums[0], "✅ Radar de Licitações: teste de envio OK.")
        msg = f"Mensagem de teste enviada para {nums[0]} via {config.WHATSAPP_PROVIDER}."
    except Exception as e:  # noqa: BLE001
        msg = f"Falha no envio: {e}"
    return RedirectResponse(f"/buscas?{urlencode({'msg': msg})}", 303)


# ---------------------------------------------------------------- status/coleta
@app.get("/status", response_class=HTMLResponse)
def status(request: Request, msg: str = ""):
    with db.conn() as con:
        st = db.kv_status(con)
    return tpl.TemplateResponse(request, "status.html", {
        "st": st, "msg": msg, "rodando": collector.em_andamento(), "cfg": config,
        "prox": agenda.proximas()})


@app.post("/coletar")
def coletar(tipo: str = Form("editais")):
    if tipo not in ("editais", "itens", "alertas"):
        tipo = "editais"
    if collector.em_andamento():
        return RedirectResponse("/status?msg=J%C3%A1+existe+uma+coleta+rodando", 303)
    # manual: itens limitados a UM lote, para não pesar a VPS em horário comercial
    kw = {"origem": "manual", "limite_itens": config.ITENS_LOTE if tipo == "itens" else None}
    threading.Thread(target=collector.ciclo, args=(tipo,), kwargs=kw, daemon=True).start()
    return RedirectResponse(f"/status?{urlencode({'msg': f'Coleta ({tipo}) iniciada em segundo plano.'})}", 303)


@app.get("/saude")
def saude():
    return {"ok": True}


# ---------------------------------------------------------------- agendador
def _loop():
    # Nada roda ao subir: o tick só age dentro da janela/horário e consulta o banco
    while True:
        try:
            feito = agenda.tick()
            if feito:
                log.info("Agendador executou: %s", feito)
                time.sleep(5)   # nunca gira sem pausa, mesmo se algo voltar na hora
                continue
        except Exception:  # noqa: BLE001
            log.exception("Erro no agendador")
        time.sleep(60)


@app.on_event("startup")
def _startup():
    db.init()
    log.info("Radar de Licitações %s — janela de coleta %s, alertas %s",
             VERSAO, config.JANELA_COLETA, config.ALERTA_HORARIO)
    if config.COLETA_AUTOMATICA:
        threading.Thread(target=_loop, daemon=True, name="agendador").start()
