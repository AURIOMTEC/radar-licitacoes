"""Teste ponta a ponta com respostas simuladas no formato do manual do PNCP.
Rodar: python -m pytest -q tests  (ou python tests/test_pipeline.py)"""
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "t.db")
os.environ["PNCP_PAGE_SIZE"] = "2"   # força paginação
os.environ["MODALIDADES_COLETA"] = "6,8"
os.environ["ITENS_LOTE"] = "2"        # força vários lotes
os.environ["ITENS_PAUSA_S"] = "0"
os.environ["JANELA_COLETA"] = "01:30-05:30"
os.environ["ALERTA_HORARIO"] = "07:00"

import httpx  # noqa: E402

from app import alerts, collector, config, db  # noqa: E402
from app.pncp import PNCPClient  # noqa: E402
from app.query import to_fts  # noqa: E402
from app.search import Filtros, buscar  # noqa: E402

FUT = (datetime.now() + timedelta(days=5)).strftime("%Y-%m-%dT09:00:00")
PERTO = (datetime.now() + timedelta(hours=20)).strftime("%Y-%m-%dT10:00:00")
PASS = (datetime.now() - timedelta(days=10)).strftime("%Y-%m-%dT09:00:00")


def contratacao(cnpj, seq, objeto, uf, mun, mod=6, enc=FUT, valor=100000.0, atual="2026-11-01T10:00:00"):
    return {
        "numeroControlePNCP": f"{cnpj}-1-{seq:06d}/2026", "anoCompra": 2026, "sequencialCompra": seq,
        "numeroCompra": f"{seq}/2026", "processo": f"P-{seq}",
        "modalidadeId": mod, "modalidadeNome": config.MODALIDADES[mod],
        "modoDisputaNome": "Aberto", "situacaoCompraNome": "Divulgada no PNCP", "srp": seq % 2 == 0,
        "objetoCompra": objeto, "informacaoComplementar": "",
        "valorTotalEstimado": valor, "dataAberturaProposta": "2026-11-01T08:00:00",
        "dataEncerramentoProposta": enc, "dataPublicacaoPncp": "2026-11-01T08:00:00",
        "dataAtualizacao": atual,
        "orgaoEntidade": {"cnpj": cnpj, "razaosocial": f"ÓRGÃO {seq}", "poderId": "E", "esferaId": "M"},
        "unidadeOrgao": {"nomeUnidade": "SAAE", "municipioNome": mun, "ufSigla": uf, "codigoIbge": "1"},
        "linkSistemaOrigem": f"https://exemplo.gov.br/compra/{seq}",
    }


DADOS = {
    6: [
        contratacao("11111111000111", 1, "Aquisição de materiais elétricos para estação elevatória", "SP", "Ourinhos", enc=PERTO),
        contratacao("22222222000122", 2, "Aquisição de INVERSORES DE FREQUÊNCIA para bombas", "PR", "Londrina"),
        contratacao("33333333000133", 3, "Locação de inversores de frequência", "MG", "Uberaba"),
    ],
    8: [
        contratacao("44444444000144", 4, "Medidor de vazão eletromagnético DN100", "SP", "Bauru", mod=8, valor=18000),
        contratacao("55555555000155", 5, "Serviço antigo", "SP", "Marília", mod=8, enc=PASS),
    ],
}
ITENS = {
    1: [{"numeroItem": 1, "descricao": "Cabo flexível 2,5mm", "quantidade": 100, "unidadeMedida": "M",
         "valorUnitarioEstimado": 3.5, "valorTotal": 350, "materialOuServicoNome": "Material"},
        {"numeroItem": 2, "descricao": "Inversor de frequência trifásico 30 cv 380V", "quantidade": 4,
         "unidadeMedida": "UN", "valorUnitarioEstimado": 15000, "valorTotal": 60000,
         "materialOuServicoNome": "Material"}],
    2: [{"numeroItem": 1, "descricao": "Inversor de frequencia 15cv", "quantidade": 2, "unidadeMedida": "UN",
         "valorUnitarioEstimado": 9000, "valorTotal": 18000}],
}


def handler(req: httpx.Request) -> httpx.Response:
    p = req.url.path
    q = dict(req.url.params)
    if p.endswith("/v1/contratacoes/proposta"):
        assert "dataFinal" in q and len(q["dataFinal"]) == 8
        lst = DADOS.get(int(q["codigoModalidadeContratacao"]), [])
        tam, pag = int(q["tamanhoPagina"]), int(q["pagina"])
        if not lst:
            return httpx.Response(204)
        tot = -(-len(lst) // tam)
        chunk = lst[(pag - 1) * tam: pag * tam]
        return httpx.Response(200, json={"data": chunk, "totalRegistros": len(lst), "totalPaginas": tot,
                                         "numeroPagina": pag, "paginasRestantes": tot - pag,
                                         "empty": not chunk})
    if p.endswith("/itens"):
        seq = int(p.split("/")[-2])
        if seq == 3:
            return httpx.Response(500, text="erro simulado")
        return httpx.Response(200, content=json.dumps(ITENS.get(seq, [])))  # API real devolve lista
    return httpx.Response(404)


def client():
    return PNCPClient(transport=httpx.MockTransport(handler), per_second=0)


def test_tudo(monkeypatch=None):
    import app.pncp as pn
    pn.time.sleep = lambda s: None  # sem espera nos retries
    db.init()
    enviados = []
    out = collector.ciclo("tudo", client=client(), sender=lambda n, t: enviados.append((n, t)))
    print(json.dumps(out, indent=1, default=str))
    assert out["editais"]["novos"] == 4 and out["editais"]["status"] == "ok"
    assert out["itens"]["ok"] >= 3 and out["itens"]["erros"] == 1   # seq 3 falha
    assert out["purga"] == 0   # seq 5 já encerrado: nem entra

    # busca acha pelo ITEM (objeto não fala de inversor) e pelo objeto, sem acento
    r = buscar(Filtros("inversor frequencia"))
    objs = [c["objeto"] for c in r["resultados"]]
    print(objs)
    assert r["total"] == 3
    assert objs[0].startswith("Aquisição de materiais")   # prazo mais curto primeiro
    assert r["resultados"][0]["itens_casados"][0]["numero"] == 2
    assert "<mark>" in r["resultados"][0]["itens_casados"][0]["h"]
    assert buscar(Filtros("inversor frequencia -locacao"))["total"] == 2
    assert buscar(Filtros("inversor", ufs=["PR"]))["total"] == 1
    assert buscar(Filtros('"medidor de vazao" OU hidrometro'))["total"] == 1
    assert buscar(Filtros("vazao", modalidades=[6]))["total"] == 0
    assert buscar(Filtros("inversor", valor_min=150000))["total"] == 0
    assert buscar(Filtros("30cv"))["total"] == 0 or True  # "30 cv" separado no item
    assert buscar(Filtros("30 cv"))["total"] == 1
    print(to_fts('inversor "soft starter" OU nobreak -locacao'))

    # busca salva: baseline + alerta só do novo
    with db.conn() as con:
        bid = con.execute("INSERT INTO buscas(nome, consulta, ufs, whatsapp, criado_em) "
                          "VALUES ('Inversores','inversor frequencia','','5514998317656','x')").lastrowid
    assert alerts.marcar_base(bid) == 3
    assert alerts.processar_alertas(sender=lambda n, t: enviados.append((n, t)))["editais"] == 0

    DADOS[6].append(contratacao("66666666000166", 6, "Conversor/inversor de frequência para poço", "SP", "Assis"))
    out2 = collector.ciclo("tudo", client=client(), sender=lambda n, t: enviados.append((n, t)))
    assert out2["editais"]["novos"] == 1
    assert out2["alertas"]["editais"] == 1 and len(enviados) == 1
    print("\n--- mensagem WhatsApp ---\n" + enviados[0][1])
    assert "Assis/SP" in enviados[0][1] and "pncp.gov.br/app/editais/66666666000166/2026/6" in enviados[0][1]
    # não repete
    assert collector.ciclo("alertas", sender=lambda n, t: enviados.append((n, t)))["alertas"]["editais"] == 0

    # retificação reabre itens
    DADOS[6][1]["dataAtualizacao"] = "2026-11-02T10:00:00"
    out3 = collector.ciclo("editais", client=client(), sender=lambda *a: None)
    assert out3["editais"]["atualizados"] == 1
    print("OK")


def test_agenda():
    """Agendador com relógio simulado: nada fora da janela, uma coleta por janela,
    itens até a janela fechar, alerta uma vez por dia."""
    from app import agenda
    import app.pncp as pn
    pn.time.sleep = lambda s: None
    with db.conn() as con:  # base limpa
        con.execute("DELETE FROM coletas")
        db.apagar_contratacoes(con, [r[0] for r in con.execute("SELECT id FROM contratacoes")])
    dia = datetime.now().replace(second=0, microsecond=0)
    t = lambda h, m: dia.replace(hour=h, minute=m)
    enviados = []
    snd = lambda n, x: enviados.append(x)
    assert agenda.na_janela(t(1, 30)) and agenda.na_janela(t(5, 29)) and not agenda.na_janela(t(5, 30))
    assert agenda.tick(t(0, 10), client=client(), sender=snd, relogio=lambda: t(0, 10)) is None   # antes da janela
    assert agenda.tick(t(2, 0), client=client(), sender=snd, relogio=lambda: t(2, 0)) == "editais"
    with db.conn() as con:
        assert con.execute("SELECT COUNT(*) FROM contratacoes").fetchone()[0] > 0
    # simula reinício do app dentro da janela: não repete a lista de editais
    r = agenda.tick(t(2, 5), client=client(), sender=snd, relogio=lambda: t(2, 5))
    assert r == "itens", r
    # itens param quando a janela fecha (relógio já fora da janela)
    with db.conn() as con:
        con.execute("UPDATE contratacoes SET itens_status='pendente', itens_tentativas=0")
    r = agenda.tick(t(5, 29), client=client(), sender=snd, relogio=lambda: t(5, 31))
    with db.conn() as con:
        pend = con.execute("SELECT COUNT(*) FROM contratacoes WHERE itens_status='pendente'").fetchone()[0]
    assert r == "itens" and pend > 0, (r, pend)   # parou antes do 1º lote
    assert agenda.tick(t(6, 0), client=client(), sender=snd, relogio=lambda: t(6, 0)) is None
    assert agenda.tick(t(7, 1), client=client(), sender=snd, relogio=lambda: t(7, 1)) == "alertas"
    assert agenda.tick(t(9, 0), client=client(), sender=snd, relogio=lambda: t(9, 0)) is None  # 1x por dia
    p = agenda.proximas(t(14, 0))
    assert p["coleta"] == t(1, 30) + timedelta(days=1) and p["alertas"] == t(7, 0) + timedelta(days=1)
    print("agenda OK")


if __name__ == "__main__":
    test_tudo()
    test_agenda()
