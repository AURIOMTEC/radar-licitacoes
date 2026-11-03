"""Converte o que o usuário digita numa expressão FTS5 segura.

Sintaxe aceita:
  inversor frequencia         -> as duas palavras (E), com prefixo: inversor* frequencia*
  "inversor de frequência"    -> frase exata
  medidor vazao OU hidrometro -> OU (também aceita "OR" ou "|")
  inversor -solar             -> exclui
Acentos e maiúsculas são ignorados (tokenizer unicode61 remove_diacritics).
Palavras curtas comuns (de, da, para...) fora de aspas são descartadas.
"""
from __future__ import annotations

import re
import unicodedata

STOP = {"a", "o", "as", "os", "de", "da", "do", "das", "dos", "e", "em", "para",
        "com", "por", "um", "uma", "no", "na", "nos", "nas", "ao", "aos", "p", "c"}

_tok = re.compile(r'-?"[^"]*"|\S+')


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return s.lower()


def _palavras(s: str) -> list[str]:
    return re.findall(r"[0-9a-z]+", _norm(s))


class ConsultaInvalida(ValueError):
    pass


def to_fts(texto: str) -> str:
    texto = (texto or "").strip()
    if not texto:
        raise ConsultaInvalida("Digite o que procurar.")
    grupos: list[list[str]] = [[]]   # grupos OU; dentro do grupo, E
    negativos: list[str] = []
    for raw in _tok.findall(texto):
        neg = raw.startswith("-") and len(raw) > 1
        t = raw[1:] if neg else raw
        if t.upper() in ("OU", "OR", "|"):
            if grupos[-1]:
                grupos.append([])
            continue
        if t.startswith('"'):
            ws = _palavras(t.strip('"'))
            if not ws:
                continue
            termo = '"' + " ".join(ws) + '"'
        else:
            ws = [w for w in _palavras(t) if w not in STOP]
            if not ws:
                continue
            # "30cv" vira "30 cv": mantém como frase com prefixo no fim
            termo = ('"' + " ".join(ws) + '"*') if len(ws) > 1 else f'"{ws[0]}"*'
            if len(ws) == 1 and len(ws[0]) <= 2:
                termo = f'"{ws[0]}"'  # sem prefixo para tokens muito curtos
        (negativos if neg else grupos[-1]).append(termo)
    grupos = [g for g in grupos if g]
    if not grupos:
        raise ConsultaInvalida("A busca precisa ter ao menos uma palavra positiva.")
    expr = " OR ".join("(" + " AND ".join(g) + ")" for g in grupos)
    if len(grupos) > 1:
        expr = f"({expr})"
    for n in negativos:
        expr += f" NOT {n}"
    return expr


def negativos_fts(texto: str) -> str | None:
    """Só os termos de exclusão, em OU — usado para descartar o edital inteiro
    quando o objeto contém um termo excluído (mesmo que um item tenha casado)."""
    negs = []
    for raw in _tok.findall(texto or ""):
        if raw.startswith("-") and len(raw) > 1:
            ws = _palavras(raw[1:].strip('"'))
            if ws:
                negs.append('"' + " ".join(ws) + '"' + ("" if raw[1:].startswith('"') else "*"))
    return " OR ".join(negs) if negs else None


def termos_destaque(texto: str) -> list[str]:
    """Palavras positivas (normalizadas) para destacar no resultado."""
    out = []
    for raw in _tok.findall(texto or ""):
        if raw.startswith("-") or raw.upper() in ("OU", "OR", "|"):
            continue
        out += [w for w in _palavras(raw.strip('"')) if w not in STOP and len(w) > 2]
    return sorted(set(out), key=len, reverse=True)
