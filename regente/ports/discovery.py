# -*- coding: utf-8 -*-
"""A porta pela qual um adapter diz o que esta identidade alcanca.

Um adapter que implementa isto ganha descoberta na tela, na CLI e na API sem
tocar em nenhuma das tres. E o criterio arquitetural do marco: acrescentar
`adapters/clickup/` deve custar um arquivo de adapter, e nao uma alteracao
espalhada pela aplicacao.

O que esta porta NAO faz, e a ausencia e o ponto:

* nao recebe credencial, nem `SecretProvider`, nem caminho de segredo -- o
  material continua saindo so pelo `CredentialBroker`, que o adapter ja recebe
  vinculado a quem age e a que workspace;
* nao conhece workspace -- quem escopa e o motor, com `ResourceRef.scoped_to`;
* nao sabe o que foi selecionado -- descobrir e responder o que existe, e nao
  o que alguem escolheu.
"""

from __future__ import annotations

from abc import abstractmethod

from . import Port
from ..core.resource import Inventario, ResourceRef


class ResourceDiscovery(Port):
    """Descobre o que a identidade corrente alcanca neste provedor.

    Opcional: um adapter que nao a implementa continua valendo, e a tela mostra
    que aquele provedor nao descobre nada. Um provedor de arquivos numa pasta
    local nao tem o que descobrir, e forcar uma implementacao vazia so criaria
    um metodo que mente.
    """

    @abstractmethod
    def discovers(self) -> tuple[str, ...]:
        """Os tipos de recurso que este adapter sabe listar, na ordem da arvore.

        GitHub responde `("account", "repository")`; Jira, algo como
        `("project", "board")`. A ORDEM importa para a tela: ela e o caminho da
        raiz ate a folha, e e o que permite navegar sem conhecer o fornecedor.

        Uma tupla vazia e uma resposta legitima: "eu nao descubro nada".
        """

    @abstractmethod
    def discover(self, kind: str,
                 parent: ResourceRef | None = None) -> Inventario:
        """O que existe deste tipo, opcionalmente dentro de `parent`.

        Devolve `Inventario`, e nao uma lista, porque falhar e nao achar nada
        sao coisas diferentes -- e o tipo torna impossivel confundi-las. Um
        provedor fora do ar precisa dizer que esta fora do ar; devolver `[]`
        faria alguem concluir que a conta ficou vazia.

        Um `kind` que este adapter nao descobre e `Falha.NAO_SUPORTADO`, e nao
        uma excecao: perguntar o que um provedor sabe fazer e legitimo.
        """
