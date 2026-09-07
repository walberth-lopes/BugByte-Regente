# -*- coding: utf-8 -*-
"""Remover credenciais de texto antes de ele virar registro permanente.

Um remote HTTPS pode carregar a credencial dentro da propria URL:

    https://x-access-token:<token>@host/org/repo.git

`git remote get-url` devolve isso literalmente, e ate aqui o motor gravava esse
texto na tabela de entregas e o exibia no relatorio. O banco e um arquivo que
sobrevive ao processo, e sai em backup, em anexo de bug, em captura de tela --
um token gravado ali vaza por caminhos que ninguem esta olhando.

Achado durante o M11 lendo o que a entrega persiste, nao por um teste falhando:
com um remote sem credencial embutida -- que e o caso de qualquer clone local --
o defeito nao aparece nunca.

Puro de proposito. Sem I/O, sem vendor, sem saber o que e um token: a unica
regra e que a parte de userinfo de uma URL nunca e informacao de diagnostico.
"""

from __future__ import annotations

import re

#: `esquema://userinfo@resto`. O `userinfo` e tudo entre `//` e o `@` que vem
#: antes da primeira barra -- a definicao da propria RFC 3986, e o unico lugar
#: onde uma URL guarda credencial.
_USERINFO = re.compile(r"(?P<scheme>[A-Za-z][A-Za-z0-9+.\-]*://)"
                       r"(?P<userinfo>[^/@\s]+)@")

#: O que aparece no lugar. Dizer que havia algo ali importa: um alvo que some e
#: um alvo que parece nao ter existido.
MASK_USERINFO = "<redacted>"
MASK = MASK_USERINFO + "@"


def redact_url(text: str | None) -> str:
    """Devolve `text` sem nenhuma credencial embutida em URL.

    Opera sobre a string inteira, nao sobre uma URL isolada: mensagens de erro
    de `git` costumam trazer a URL no meio de outro texto, e e exatamente ali
    que a credencial escapa despercebida.
    """
    if not text:
        return ""
    return _USERINFO.sub(lambda m: m.group("scheme") + MASK, text)


def carries_credential(text: str | None) -> bool:
    """True quando ha userinfo numa URL dentro de `text`.

    Existe para os testes poderem afirmar a ausencia sem repetir a regex, e
    para um guard poder recusar gravar em vez de gravar mascarado quando o
    lugar for sensivel demais para conter sequer a marca.
    """
    if not text:
        return False
    # A propria mascara casa com o padrao de userinfo. Contar ela como
    # credencial faria a verificacao acusar vazamento exatamente no texto ja
    # limpo -- e um teste que nunca fica verde deixa de ser lido.
    return any(m.group("userinfo") != MASK_USERINFO
               for m in _USERINFO.finditer(text))
