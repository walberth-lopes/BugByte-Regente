# -*- coding: utf-8 -*-
"""Como a CLI de hospedagem e disparada: ambiente, isolamento e credencial.

Um so lugar para os tres adapters que invocam esta ferramenta -- leitura de
repositorio, escrita de pull request e observacao de CI. Tres montagens do mesmo
ambiente divergiriam, e a que divergisse seria a que esqueceu do isolamento.

**O defeito que este modulo existe para fechar foi medido, nao deduzido.** Ate o
marco 6.1 os tres adapters chamavam `subprocess.run` sem `env=`, e o filho
herdava o ambiente inteiro do motor. A ferramenta entao se autenticava sozinha,
pelo chaveiro do sistema operacional:

    (ambiente herdado, sem token nenhum)    -> aceita, como o dono da maquina
    (ambiente isolado, sem token)           -> recusa, e diz como autenticar
    (ambiente isolado, token invalido)      -> 401

A primeira linha era o motor agindo com uma autoridade que ninguem lhe deu, que
ninguem conseguia revogar, e cujo alcance nao tinha relacao com a capacidade da
credencial registrada. As outras duas provam que da para fechar: sem a
configuracao propria da ferramenta, ela obedece a variavel de ambiente.
"""

from __future__ import annotations

import os
import tempfile

from ...core import childenv
from ...ports import AdapterError
from ..childproc import ChildEnvironment

#: Onde a ferramenta NAO vai encontrar a configuracao dela.
#:
#: Um caminho que deliberadamente nao existe. Nao e criado: um diretorio ausente
#: e mais isolado que um vazio, e criar um seria uma escrita em disco feita por
#: um adapter de leitura.
NO_CONFIG_DIR = os.path.join(tempfile.gettempdir(), "regente-sem-config-de-cli")

#: Arquivos que, se aparecerem no diretorio de isolamento, devolveriam a
#: ferramenta a uma credencial propria. Nomeados para que `verify()` possa
#: recusar em vez de descobrir tarde.
CONFIG_FILES = ("hosts.yml", "hosts.yaml", "config.yml", "config.yaml")

#: Nome da variavel sob a qual ESTA ferramenta le um token. Conhecimento de
#: fornecedor, e por isso mora aqui e nao no motor. E um default: a configuracao
#: do workspace pode dizer outro, e um provedor diferente dira outro.
DEFAULT_CREDENTIAL_ENV: tuple[str, ...] = ("GH_TOKEN",)


def fixed_variables(config_dir: str = "") -> tuple[tuple[str, str], ...]:
    """Variaveis nao secretas de que a ferramenta precisa para ser previsivel."""
    return (
        # Sem isto ela procura a propria configuracao e usa o que achar.
        ("GH_CONFIG_DIR", config_dir or NO_CONFIG_DIR),
        # Nunca perguntar nada: nao ha ninguem para responder, e um processo
        # esperando resposta so aparece como um timeout sem explicacao.
        ("GH_PROMPT_DISABLED", "1"),
        ("GH_NO_UPDATE_NOTIFIER", "1"),
        ("GH_PAGER", ""),
    )


def refuse_if_config_reachable(config_dir: str = "") -> None:
    """Recusa se a ferramenta ainda tiver de onde tirar credencial propria.

    Chamado por `verify()`. Um diretorio de isolamento que contenha a
    configuracao da ferramenta reabre exatamente o caminho que este modulo
    fecha -- e reabre em silencio, porque tudo continua funcionando.
    """
    root = config_dir or NO_CONFIG_DIR
    achados = [n for n in CONFIG_FILES if os.path.exists(os.path.join(root, n))]
    if achados:
        raise AdapterError(
            f"o diretorio de isolamento '{root}' contem {', '.join(achados)}: a "
            f"ferramenta voltaria a se autenticar por conta propria, sem passar "
            f"pelo caminho governado")


def child_environment(names: tuple[str, ...], broker,
                      config_dir: str = "") -> ChildEnvironment:
    """A receita do ambiente do filho. Sem material dentro."""
    return ChildEnvironment(
        names=tuple(names),
        broker=broker,
        allow=childenv.BASE_ALLOWLIST + childenv.NETWORK_ALLOWLIST,
        fixed=fixed_variables(config_dir))
