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

from ..ports import AdapterError
from ..ports.support import SecretProvider


class SecretMissing(AdapterError):
    """A referencia existe na configuracao mas nao resolve para nada."""


class SecretOutOfScope(AdapterError):
    """Pediram uma referencia que este workspace nao declarou. Nunca e engano
    benigno: e a fronteira entre clientes sendo testada."""


@dataclass(slots=True)
class ScopedSecrets(SecretProvider):
    """Resolve `env:NOME` e `arquivo:CAMINHO`.

    Nao existe forma `literal:` de proposito. Se ela existisse, o primeiro
    segredo de producao apareceria num YAML versionado dentro de uma semana.
    """
    name: str = "scoped"
    #: Referencias que ESTE workspace pode resolver. Vazio = nenhuma.
    allowed_from: frozenset[str] = field(default_factory=frozenset)
    workspace: str = "?"

    def resolve(self, reference: str) -> str:
        if reference not in self.allowed_from:
            raise SecretOutOfScope(
                f"workspace '{self.workspace}' nao declarou a referencia "
                f"{reference!r}; declaradas: {sorted(self.allowed_from) or 'nenhuma'}")

        esquema, _, resto = reference.partition(":")
        if esquema == "env":
            value = os.environ.get(resto, "")
            if not value:
                raise SecretMissing(
                    f"variavel de ambiente {resto} nao esta definida ou esta vazia")
            return value
        if esquema == "arquivo":
            path = Path(resto).expanduser()
            if not path.is_file():
                raise SecretMissing(f"arquivo de segredo nao existe: {path}")
            value = path.read_text(encoding="utf-8").strip()
            if not value:
                raise SecretMissing(f"arquivo de segredo esta vazio: {path}")
            return value
        raise SecretMissing(
            f"esquema de referencia desconhecido: {esquema!r}. Use env: ou arquivo:")

    def available(self, reference: str) -> bool:
        try:
            self.resolve(reference)
            return True
        except AdapterError:
            return False
