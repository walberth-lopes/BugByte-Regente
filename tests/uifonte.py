# -*- coding: utf-8 -*-
"""A fonte da Mission Control, para as guardas estruturais.

A tela deixou de ser um `app.js` a mao e passou a ser um projeto React
construido pelo Vite. As guardas continuam valendo -- e apontam para a FONTE,
nao para o pacote gerado.

O pacote gerado seria o alvo errado por dois motivos. Ele e minificado, entao
uma expressao regular sobre ele acha e deixa de achar coisas por acidente; e
ele nao existe antes de alguem rodar o build, o que faria a guarda passar em
silencio numa maquina limpa -- que e exatamente quando ela precisa falar.
"""

from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
FONTE = RAIZ / "ui" / "src"

#: O que o build gera, e ninguem escreve. Varrer isto acusaria o desenho de um
#: icone por conter a palavra `path`.
GERADOS = {"icones.gerado.js"}


def arquivos() -> list[Path]:
    """Todo arquivo de codigo que uma pessoa escreveu para a tela."""
    achados = [
        p
        for p in sorted(FONTE.rglob("*"))
        if p.suffix in (".js", ".jsx") and p.name not in GERADOS
    ]
    assert achados, (
        f"nenhuma fonte de tela em {FONTE}: a guarda passaria sem olhar nada"
    )
    return achados


def texto() -> str:
    """A fonte inteira, concatenada, para varrer de uma vez."""
    return "\n".join(p.read_text(encoding="utf-8") for p in arquivos())
