"""Versão exibida no rodapé: v1.<número de commits>, para conferir que o deploy entrou.

O scripts/deploy-vps.sh grava o arquivo VERSION; se não existir, tenta o git."""
import os
import subprocess

_RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _versao() -> str:
    try:
        with open(os.path.join(_RAIZ, "VERSION"), encoding="utf-8") as f:
            v = f.read().strip()
            if v:
                return v
    except OSError:
        pass
    try:
        n = subprocess.run(["git", "rev-list", "--count", "HEAD"], cwd=_RAIZ,
                           capture_output=True, text=True, timeout=5).stdout.strip()
        if n.isdigit():
            return f"v1.{int(n):04d}"
    except (OSError, subprocess.SubprocessError):
        pass
    return "dev"


VERSAO = _versao()
