"""Configuração via variáveis de ambiente (ver .env.example)."""
import os
from zoneinfo import ZoneInfo


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default


def _list_int(name: str, default: str) -> list[int]:
    raw = _env(name, default)
    return [int(x) for x in raw.replace(";", ",").split(",") if x.strip().isdigit()]


TZ = ZoneInfo(_env("TZ_APP", "America/Sao_Paulo"))

DB_PATH = _env("DB_PATH", "data/radar.db")

# --- PNCP --------------------------------------------------------------
# API de Consulta (Manual PNCP API Consultas v1.0, seção 3.1)
PNCP_CONSULTA_URL = _env("PNCP_CONSULTA_URL", "https://pncp.gov.br/api/consulta")
# API do PNCP (Manual de Integração, 11.13 Consultar Itens de uma Contratação)
PNCP_API_URL = _env("PNCP_API_URL", "https://pncp.gov.br/api/pncp")
# Portal público: https://pncp.gov.br/app/editais/{cnpj}/{ano}/{sequencial}
PNCP_PORTAL_URL = _env("PNCP_PORTAL_URL", "https://pncp.gov.br")

# Modalidades coletadas (tabela "Modalidade de Contratação" do manual):
# 4 Concorrência Eletrônica, 5 Concorrência Presencial, 6 Pregão Eletrônico,
# 7 Pregão Presencial, 8 Dispensa, 10 Manifestação de Interesse,
# 12 Credenciamento. Leilões (1, 13) ficam de fora: neles o órgão VENDE.
MODALIDADES_COLETA = _list_int("MODALIDADES_COLETA", "4,5,6,7,8,10,12")

# O manual diz máximo 500, mas a API real só aceita até 50 ("Tamanho de página
# inválido" para 100 e 500 — teste na VPS em 03/11/2026).
PAGE_SIZE = _int("PNCP_PAGE_SIZE", 50)
# dataFinal do /contratacoes/proposta é o LIMITE DE ENCERRAMENTO das propostas:
# com a data de hoje a API devolve só o que encerra hoje (17 pregões), com +30 dias
# 17.549, com +2 anos 17.729 (medido em 03/11/2026). Por isso: hoje + N dias.
DATA_FINAL_DIAS = _int("DATA_FINAL_DIAS", 730)
HTTP_TIMEOUT = _int("PNCP_TIMEOUT_S", 60)
# VPS de 2 núcleos: pouca concorrência, taxa baixa e pausa entre lotes
REQ_POR_SEGUNDO = float(_env("PNCP_REQ_POR_SEGUNDO", "2"))
ITENS_CONCORRENCIA = _int("ITENS_CONCORRENCIA", 2)
ITENS_LOTE = _int("ITENS_LOTE", 300)              # editais por lote de itens
ITENS_PAUSA_S = _int("ITENS_PAUSA_S", 20)         # pausa entre lotes
ITENS_POR_CICLO = _int("ITENS_POR_CICLO", 6000)  # teto por execução manual

COLETA_AUTOMATICA = _env("COLETA_AUTOMATICA", "1") == "1"
# Tarefas pesadas só de madrugada (horário de Brasília). A janela é verificada
# a cada minuto; a última execução fica gravada no banco (tabela coletas), então
# reiniciar o app NÃO dispara coleta.
JANELA_COLETA = _env("JANELA_COLETA", "01:30-05:30")
# Alertas de WhatsApp: uma vez por dia, depois da coleta da madrugada
ALERTA_HORARIO = _env("ALERTA_HORARIO", "07:00")


def _hm(s: str) -> tuple[int, int]:
    h, m = s.strip().split(":")
    return int(h), int(m)


JANELA_INI, JANELA_FIM = (_hm(x) for x in JANELA_COLETA.split("-"))
ALERTA_HM = _hm(ALERTA_HORARIO)
# Encerrados há mais de N dias, ou não vistos há N dias, saem do banco
PURGA_DIAS = _int("PURGA_DIAS", 2)

# --- App ---------------------------------------------------------------
APP_USER = _env("APP_USER")
APP_PASSWORD = _env("APP_PASSWORD")
APP_PUBLIC_URL = _env("APP_PUBLIC_URL", "http://localhost:8090").rstrip("/")

# --- WhatsApp ----------------------------------------------------------
# WHATSAPP_PROVIDER = evolution | zapi | none
WHATSAPP_PROVIDER = _env("WHATSAPP_PROVIDER", "none").lower()
EVOLUTION_URL = _env("EVOLUTION_URL").rstrip("/")
EVOLUTION_INSTANCE = _env("EVOLUTION_INSTANCE")
EVOLUTION_APIKEY = _env("EVOLUTION_APIKEY")
ZAPI_INSTANCE_ID = _env("ZAPI_INSTANCE_ID")
ZAPI_TOKEN = _env("ZAPI_TOKEN")
ZAPI_CLIENT_TOKEN = _env("ZAPI_CLIENT_TOKEN")
ALERTA_MAX_POR_MSG = _int("ALERTA_MAX_POR_MSG", 8)
# Esconde editais que encerram em menos de N dias corridos (dá tempo de montar a
# proposta). Dispensa: aviso mínimo de 3 dias úteis (art. 75 §3º Lei 14.133);
# pregão de bens: 8 dias úteis (art. 55, I, a).
PRAZO_MIN_PADRAO = _int("PRAZO_MIN_PADRAO", 5)
PRAZO_OPCOES = [0, 3, 5, 10, 15]

MODALIDADES = {
    1: "Leilão - Eletrônico", 2: "Diálogo Competitivo", 3: "Concurso",
    4: "Concorrência - Eletrônica", 5: "Concorrência - Presencial",
    6: "Pregão - Eletrônico", 7: "Pregão - Presencial",
    8: "Dispensa de Licitação", 9: "Inexigibilidade",
    10: "Manifestação de Interesse", 11: "Pré-qualificação",
    12: "Credenciamento", 13: "Leilão - Presencial",
}

UFS = ["AC", "AL", "AM", "AP", "BA", "CE", "DF", "ES", "GO", "MA", "MG", "MS",
       "MT", "PA", "PB", "PE", "PI", "PR", "RJ", "RN", "RO", "RR", "RS", "SC",
       "SE", "SP", "TO"]
