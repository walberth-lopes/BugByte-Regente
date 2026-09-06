# -*- coding: utf-8 -*-
"""SecretProvider: resolve REFERENCIAS a segredo, nunca guarda valores.

A configuracao diz `token: env:JIRA_API_TOKEN`. O que trafega pelo YAML, pelo
banco, pelos eventos e pelos prompts e a *referencia* -- o valor so existe no
momento do uso, dentro do adapter que precisa dele.

**A referencia e escopada por tenancy.** Um workspace so alcanca as referencias
que sua propria configuracao declara, e o resolvedor recusa qualquer outra. Sem
isso, um adapter mal configurado do cliente B leria a credencial do cliente A --
e o pior e que funcionaria, silenciosamente, ate o dia em que aparecesse num
log.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from ..ports import AdapterErro
from ..ports.support import SecretProvider


class SegredoAusente(AdapterErro):
    """A referencia existe na configuracao mas nao resolve para nada."""


class SegredoForaDoEscopo(AdapterErro):
    """Pediram uma referencia que este workspace nao declarou. Nunca e engano
    benigno: e a fronteira entre clientes sendo testada."""


@dataclass(slots=True)
class Segredos(SecretProvider):
    """Resolve `env:NOME` e `arquivo:CAMINHO`.

    Nao existe forma `literal:` de proposito. Se ela existisse, o primeiro
    segredo de producao apareceria num YAML versionado dentro de uma semana.
    """
    nome: str = "escopado"
    #: Referencias que ESTE workspace pode resolver. Vazio = nenhuma.
    permitidas: frozenset[str] = field(default_factory=frozenset)
    workspace: str = "?"

    def resolve(self, referencia: str) -> str:
        if referencia not in self.permitidas:
            raise SegredoForaDoEscopo(
                f"workspace '{self.workspace}' nao declarou a referencia "
                f"{referencia!r}; declaradas: {sorted(self.permitidas) or 'nenhuma'}")

        esquema, _, resto = referencia.partition(":")
        if esquema == "env":
            valor = os.environ.get(resto, "")
            if not valor:
                raise SegredoAusente(
                    f"variavel de ambiente {resto} nao esta definida ou esta vazia")
            return valor
        if esquema == "arquivo":
            caminho = Path(resto).expanduser()
            if not caminho.is_file():
                raise SegredoAusente(f"arquivo de segredo nao existe: {caminho}")
            valor = caminho.read_text(encoding="utf-8").strip()
            if not valor:
                raise SegredoAusente(f"arquivo de segredo esta vazio: {caminho}")
            return valor
        raise SegredoAusente(
            f"esquema de referencia desconhecido: {esquema!r}. Use env: ou arquivo:")

    def disponivel(self, referencia: str) -> bool:
        try:
            self.resolve(referencia)
            return True
        except AdapterErro:
            return False
