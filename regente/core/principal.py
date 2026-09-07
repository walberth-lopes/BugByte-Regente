# -*- coding: utf-8 -*-
"""Quem esta agindo, e ate onde.

Quatro perguntas diferentes, e a confusao entre elas e como uma escrita escapa:

    Autenticacao  quem e voce?            -> um `Identity`, provado por alguem
    Autorizacao   voce manda aqui?        -> `Principal.may_*`, concedido pela composicao
    Policy        esta acao e permitida?  -> o arquivo de regras, independente
    Transicao     este estado permite?    -> a maquina de estados

Nenhuma responde pela outra. Um principal autenticado nao esta autorizado; um
principal autorizado nao venceu a policy; e uma policy que permite nao torna
legal uma transicao ilegal.

O campo mais importante deste arquivo e `method`. Um `Principal` sem ele nao foi
autenticado por ninguem -- foi *afirmado*. E a diferenca entre "o servidor
verificou um segredo que ele proprio emitiu" e "o navegador digitou um nome".
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class Principal:
    """Uma identidade ja autenticada, com o alcance que lhe foi concedido.

    Construir isto NAO autentica ninguem. Quem autentica e um `IdentityProvider`;
    esta classe so carrega o resultado. Por isso `method` vazio significa
    anonimo, e nao "autenticado de algum jeito que ninguem anotou".

    Leitura e escrita tem defaults opostos, de proposito:

    * `workspaces=None` significa "todos os que o store guarda" -- o operador
      local lendo a propria maquina;
    * `decides` comeca **vazio**. Ninguem decide sem concessao explicita. Uma
      autoridade de escrita que nasce aberta e uma que ninguem lembra de fechar.
    """
    subject: str = "anonimo"
    display: str = ""
    #: Como esta identidade foi provada. Vazio = nao foi.
    method: str = ""
    #: Workspaces que pode LER. `None` = todos.
    workspaces: frozenset[str] | None = None
    #: Workspaces onde pode DECIDIR. Sempre explicito.
    decides: frozenset[str] = field(default_factory=frozenset)

    @property
    def authenticated(self) -> bool:
        return bool(self.method)

    @property
    def label(self) -> str:
        """Como esta identidade aparece na auditoria.

        Carrega o metodo junto do sujeito porque "quem decidiu" e "como
        provamos que era essa pessoa" sao coisas que um leitor de auditoria
        precisa ver na mesma linha. `dev-token:walberth` e uma frase honesta;
        `walberth` sozinho esconde que o token era de desenvolvimento.
        """
        return f"{self.method}:{self.subject}" if self.method else self.subject

    def may_read(self, workspace_id: str) -> bool:
        return self.workspaces is None or workspace_id in self.workspaces

    def may_decide(self, workspace_id: str) -> bool:
        """Autoridade de escrita, e nunca a de leitura por tabela.

        Ler e decidir sao concessoes separadas. Derivar uma da outra faria de
        todo observador um decisor -- que e precisamente o que a fila de
        escalada existe para nao ser.
        """
        return self.authenticated and workspace_id in self.decides


#: Ninguem. O default de toda requisicao que ainda nao foi autenticada.
ANONYMOUS = Principal(subject="anonimo", display="nao autenticado",
                      workspaces=frozenset(), decides=frozenset())
