# -*- coding: utf-8 -*-
"""Como o `git` e disparado: ambiente classificado, isolamento e credencial.

O M6.1 fechou o caminho ambiente da CLI de hospedagem e deixou o `git` de fora,
com um motivo escrito: compor o ambiente dele do vazio remove o que ele precisa
para funcionar. A resposta nao e herdar tudo de volta -- e **classificar cada
variavel**, uma por uma, e dizer por que ela esta na categoria em que esta.

    SAFE_FIXED        valores que o Regente escolhe. Fecham prompt, config do
                      usuario e ajudante de credencial.
    SAFE_ALLOWLISTED  vem do pai, por nome. Nada que identifique ninguem.
    CREDENTIAL        uma variavel, com material vindo do broker.
    FORBIDDEN         autoridade ambiente. Nomeada, para nao voltar por descuido.

**O defeito medido.** Esta maquina tem `credential.helper = manager` no
`.gitconfig` global. Qualquer `git push` do motor se autenticaria por ele, sem
passar pelo Regente -- o mesmo defeito do M6.1, na outra ferramenta. Apontar
`GIT_CONFIG_GLOBAL` e `GIT_CONFIG_SYSTEM` para um caminho que nao existe o fecha,
sem escrever nada em disco e sem tocar na configuracao de ninguem.

**O transporte, provado contra um `git push` real** sobre HTTP com Basic auth:

    sem credencial   -> rc=128, "could not read Username ... prompts disabled"
    com o cabecalho  -> rc=0,   "* [new branch] main -> main"

O material entra por `http.<url>.extraheader`, escrito em `GIT_CONFIG_VALUE_0`.
Nao vai em argv, nao altera o remote, nao vira arquivo e nao sobrevive ao
processo.

**`ssh` e recusado, e nao silenciosamente permitido.** `SSH_AUTH_SOCK` e
`GIT_SSH_COMMAND` sao autoridade ambiente: um agente ssh autentica sem o Regente
saber. Mante-los seria manter o segundo caminho que o M16 removeu. O Regente nao
tem mecanismo governado para chave ssh, entao um alvo `ssh://` para com um
motivo, em vez de funcionar por um caminho que ninguem autorizou.
"""

from __future__ import annotations

import base64
import os
import tempfile
from urllib.parse import urlsplit

from ...core import childenv
from ...ports import AdapterError
from ..childproc import ChildEnvironment

#: Onde o `git` NAO vai encontrar configuracao de ninguem.
#:
#: Um caminho que nao existe. `git` trata arquivo de configuracao ausente como
#: vazio, entao nada precisa ser criado -- e nada e escrito em disco.
NO_CONFIG = os.path.join(tempfile.gettempdir(), "regente-sem-gitconfig")

#: A variavel que carrega material. Uma so, e e a unica da categoria.
CREDENTIAL_ENV: tuple[str, ...] = ("GIT_CONFIG_VALUE_0",)

#: Autoridade ambiente. Nao estao na allowlist -- estao NOMEADAS, porque uma
#: ausencia parece descuido e uma recusa nomeada parece regra. Se alguem
#: ampliar a allowlist um dia, o teste que le esta lista acusa.
FORBIDDEN: tuple[str, ...] = (
    "SSH_AUTH_SOCK", "SSH_AGENT_PID", "GIT_SSH", "GIT_SSH_COMMAND",
    "GIT_ASKPASS", "SSH_ASKPASS", "GIT_CONFIG", "GIT_CONFIG_NOSYSTEM",
    "GH_TOKEN", "GITHUB_TOKEN", "GIT_USERNAME", "GIT_PASSWORD",
)

#: O que o `git` precisa ver do ambiente do pai para funcionar. `HOME` entra:
#: `git` o usa para mais coisas do que o `.gitconfig`, e o `.gitconfig` e
#: neutralizado por `GIT_CONFIG_GLOBAL` -- esconder `HOME` seria fechar a porta
#: certa pelo motivo errado, e quebraria outras coisas junto.
ALLOWLIST: tuple[str, ...] = childenv.BASE_ALLOWLIST + childenv.NETWORK_ALLOWLIST


def fixed_variables(remote: str = "") -> tuple[tuple[str, str], ...]:
    """SAFE_FIXED: o que o Regente escolhe, igual em toda invocacao."""
    fixas: list[tuple[str, str]] = [
        # A configuracao do usuario e a do sistema deixam de existir para este
        # processo. E aqui que `credential.helper` global morre.
        ("GIT_CONFIG_GLOBAL", NO_CONFIG),
        ("GIT_CONFIG_SYSTEM", NO_CONFIG),
        # Nunca perguntar nada: nao ha ninguem para responder, e um `git`
        # esperando resposta aparece como um timeout sem explicacao.
        ("GIT_TERMINAL_PROMPT", "0"),
        # Vazio, e nao ausente: um `GIT_ASKPASS` herdado do pai seria outro
        # caminho ate credencial.
        ("GIT_ASKPASS", ""),
        ("GIT_ADVICE", "0"),
    ]
    if remote:
        # A chave da configuracao injetada. O VALOR (o cabecalho) e a unica
        # variavel com material, e entra pelo broker -- nunca aqui.
        fixas.append(("GIT_CONFIG_COUNT", "1"))
        fixas.append(("GIT_CONFIG_KEY_0", f"http.{_origin(remote)}.extraheader"))
    return tuple(fixas)


def _origin(remote: str) -> str:
    """`https://host/` do alvo, para escopar o cabecalho a ele.

    Sem escopo, o cabecalho acompanharia qualquer requisicao HTTP que o `git`
    fizesse na invocacao -- inclusive uma para onde um redirecionamento
    apontasse. Escopar custa uma linha; nao escopar custa um token entregue a
    quem respondeu o redirect.
    """
    partes = urlsplit(remote)
    return f"{partes.scheme}://{partes.netloc}/"


def authorization(user: str):
    """Como o material e escrito na variavel. Formatacao, nao autoridade.

    `git` nao le um token cru de variavel nenhuma; o que ele aceita por ambiente
    e um cabecalho HTTP. Base64 aqui e codificacao, e nao protecao -- e por isso
    o valor produzido entra em `secrets` junto com o material.
    """
    def escrever(material: str) -> str:
        par = base64.b64encode(f"{user}:{material}".encode()).decode()
        return f"Authorization: Basic {par}"
    return escrever


def refuse_unless_governable(remote: str) -> None:
    """Recusa um alvo cuja autenticacao o Regente nao consegue governar.

    `ssh` funcionaria -- pelo agente do usuario, que e autoridade que ninguem
    concedeu ao motor e que ninguem consegue revogar pelo Regente. Recusar e a
    resposta honesta: nao ha mecanismo governado para chave ssh neste marco.
    """
    baixo = (remote or "").strip().lower()
    if not baixo:
        raise AdapterError("nao ha alvo de push; empurrar e impossivel")
    if baixo.startswith(("https://", "http://")):
        return
    if baixo.startswith(("ssh://", "git://")) or "@" in baixo.split("/", 1)[0]:
        raise AdapterError(
            f"recusado: '{remote}' autenticaria por ssh, e o Regente nao tem "
            f"mecanismo governado para chave ssh. Usar o agente do usuario "
            f"seria agir com autoridade que ninguem concedeu ao motor")
    raise AdapterError(
        f"recusado: nao sei autenticar '{remote}' de forma governada")


def child_environment(remote: str, broker, user: str) -> ChildEnvironment:
    """A receita do ambiente do `git`. Sem material dentro."""
    return ChildEnvironment(
        names=CREDENTIAL_ENV if remote else (),
        broker=broker,
        allow=ALLOWLIST,
        fixed=fixed_variables(remote),
        render=authorization(user))
