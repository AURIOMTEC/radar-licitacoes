"""SQLite + FTS5. Um único arquivo, sem servidor de banco."""
import os
import sqlite3
import threading
from contextlib import contextmanager

from . import config

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS contratacoes (
    id                INTEGER PRIMARY KEY,
    ctrl              TEXT UNIQUE NOT NULL,      -- numeroControlePNCP
    cnpj              TEXT NOT NULL,
    ano               INTEGER NOT NULL,
    seq               INTEGER NOT NULL,
    numero_compra     TEXT,
    processo          TEXT,
    objeto            TEXT,
    info_compl        TEXT,
    modalidade_id     INTEGER,
    modalidade_nome   TEXT,
    modo_disputa_nome TEXT,
    situacao_nome     TEXT,
    srp               INTEGER,
    valor_estimado    REAL,
    data_abertura     TEXT,   -- ISO local (Brasília), como vem da API
    data_encerramento TEXT,
    data_publicacao   TEXT,
    data_atualizacao  TEXT,
    orgao_cnpj        TEXT,
    orgao_nome        TEXT,
    esfera_id         TEXT,
    unidade_nome      TEXT,
    municipio         TEXT,
    uf                TEXT,
    link_origem       TEXT,
    first_seen        TEXT NOT NULL,
    last_seen         TEXT NOT NULL,
    itens_status      TEXT NOT NULL DEFAULT 'pendente',  -- pendente|ok|erro
    itens_tentativas  INTEGER NOT NULL DEFAULT 0,
    itens_count       INTEGER
);
CREATE INDEX IF NOT EXISTS ix_c_enc   ON contratacoes(data_encerramento);
CREATE INDEX IF NOT EXISTS ix_c_uf    ON contratacoes(uf);
CREATE INDEX IF NOT EXISTS ix_c_itens ON contratacoes(itens_status);
CREATE INDEX IF NOT EXISTS ix_c_first ON contratacoes(first_seen);

CREATE TABLE IF NOT EXISTS itens (
    id              INTEGER PRIMARY KEY,
    contratacao_id  INTEGER NOT NULL REFERENCES contratacoes(id) ON DELETE CASCADE,
    numero          INTEGER,
    descricao       TEXT,
    info_compl      TEXT,
    quantidade      REAL,
    unidade         TEXT,
    valor_unit      REAL,
    valor_total     REAL,
    tipo            TEXT,     -- Material / Serviço
    ncm             TEXT,
    sigiloso        INTEGER,
    UNIQUE (contratacao_id, numero)
);
CREATE INDEX IF NOT EXISTS ix_i_c ON itens(contratacao_id);

-- rowid do FTS = id da tabela base
CREATE VIRTUAL TABLE IF NOT EXISTS contratacoes_fts USING fts5(
    objeto, info_compl, orgao, local,
    tokenize = 'unicode61 remove_diacritics 2'
);
CREATE VIRTUAL TABLE IF NOT EXISTS itens_fts USING fts5(
    descricao, info_compl,
    tokenize = 'unicode61 remove_diacritics 2'
);

CREATE TABLE IF NOT EXISTS buscas (
    id          INTEGER PRIMARY KEY,
    nome        TEXT NOT NULL,
    consulta    TEXT NOT NULL,
    ufs         TEXT NOT NULL DEFAULT '',   -- "SP,PR" ou vazio = todas
    modalidades TEXT NOT NULL DEFAULT '',   -- "6,8" ou vazio = todas
    valor_min   REAL,
    valor_max   REAL,
    whatsapp    TEXT NOT NULL DEFAULT '',   -- números separados por vírgula
    ativo       INTEGER NOT NULL DEFAULT 1,
    criado_em   TEXT NOT NULL
);

-- Registro do que já foi avisado (ou marcado como "já existia" na criação)
CREATE TABLE IF NOT EXISTS alertas (
    busca_id        INTEGER NOT NULL REFERENCES buscas(id) ON DELETE CASCADE,
    contratacao_id  INTEGER NOT NULL REFERENCES contratacoes(id) ON DELETE CASCADE,
    tipo            TEXT NOT NULL,          -- enviado | base | falha
    em              TEXT NOT NULL,
    PRIMARY KEY (busca_id, contratacao_id)
);

CREATE TABLE IF NOT EXISTS coletas (
    id        INTEGER PRIMARY KEY,
    tipo      TEXT NOT NULL,     -- editais | itens | alertas | purga
    inicio    TEXT NOT NULL,
    fim       TEXT,
    status    TEXT NOT NULL,     -- rodando | ok | erro | parcial
    novos     INTEGER DEFAULT 0,
    atualizados INTEGER DEFAULT 0,
    requisicoes INTEGER DEFAULT 0,
    erros     INTEGER DEFAULT 0,
    msg       TEXT
);
"""

_lock = threading.Lock()


def connect() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(os.path.abspath(config.DB_PATH)), exist_ok=True)
    con = sqlite3.connect(config.DB_PATH, timeout=30, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA busy_timeout=30000")
    return con


def init() -> None:
    with _lock:
        con = connect()
        con.executescript(SCHEMA)
        con.commit()
        con.close()


@contextmanager
def conn():
    con = connect()
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


# ---------------------------------------------------------------------------
# Manutenção dos índices FTS (feita manualmente para controlar o rowid)

def fts_contratacao(con: sqlite3.Connection, row_id: int) -> None:
    r = con.execute(
        "SELECT objeto, info_compl, orgao_nome, unidade_nome, municipio, uf "
        "FROM contratacoes WHERE id=?", (row_id,)).fetchone()
    con.execute("DELETE FROM contratacoes_fts WHERE rowid=?", (row_id,))
    if r:
        orgao = " ".join(x for x in (r["orgao_nome"], r["unidade_nome"]) if x)
        local = " ".join(x for x in (r["municipio"], r["uf"]) if x)
        con.execute(
            "INSERT INTO contratacoes_fts(rowid, objeto, info_compl, orgao, local) "
            "VALUES (?,?,?,?,?)",
            (row_id, r["objeto"] or "", r["info_compl"] or "", orgao, local))


def apagar_itens(con: sqlite3.Connection, contratacao_id: int) -> None:
    con.execute(
        "DELETE FROM itens_fts WHERE rowid IN (SELECT id FROM itens WHERE contratacao_id=?)",
        (contratacao_id,))
    con.execute("DELETE FROM itens WHERE contratacao_id=?", (contratacao_id,))


def apagar_contratacoes(con: sqlite3.Connection, ids: list[int]) -> None:
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        q = ",".join("?" * len(chunk))
        con.execute(f"DELETE FROM itens_fts WHERE rowid IN "
                    f"(SELECT id FROM itens WHERE contratacao_id IN ({q}))", chunk)
        con.execute(f"DELETE FROM contratacoes_fts WHERE rowid IN ({q})", chunk)
        con.execute(f"DELETE FROM contratacoes WHERE id IN ({q})", chunk)  # cascade itens/alertas


def kv_status(con: sqlite3.Connection) -> dict:
    out = {}
    out["total"] = con.execute("SELECT COUNT(*) FROM contratacoes").fetchone()[0]
    out["itens"] = con.execute("SELECT COUNT(*) FROM itens").fetchone()[0]
    out["itens_pend"] = con.execute(
        "SELECT COUNT(*) FROM contratacoes WHERE itens_status='pendente'").fetchone()[0]
    out["itens_erro"] = con.execute(
        "SELECT COUNT(*) FROM contratacoes WHERE itens_status='erro'").fetchone()[0]
    out["por_uf"] = con.execute(
        "SELECT uf, COUNT(*) n FROM contratacoes GROUP BY uf ORDER BY n DESC").fetchall()
    out["por_mod"] = con.execute(
        "SELECT modalidade_nome, COUNT(*) n FROM contratacoes "
        "GROUP BY modalidade_nome ORDER BY n DESC").fetchall()
    out["coletas"] = con.execute(
        "SELECT * FROM coletas ORDER BY id DESC LIMIT 25").fetchall()
    return out
