# -*- coding: utf-8 -*-
"""Recursos de um provedor: o que existe la fora, e o que este workspace usa.

Tres coisas diferentes moram aqui, e mante-las separadas e a razao deste modulo
existir. Confundi-las e o defeito que ele previne:

    O QUE O PROVEDOR TEM       descoberto, e nao autorizado
    O QUE A CREDENCIAL PERMITE `core/credential.py::Use`
    O QUE O WORKSPACE USA      selecionado, e persistido aqui

Uma credencial que alcanca 47 repositorios nao autoriza o motor a trabalhar em
47 repositorios. Ela autoriza o motor a PERGUNTAR. Quem responde o que o motor
pode tocar e uma escolha humana, gravada e auditavel -- e sem essa etapa do
meio, conectar um provedor entregaria a ele tudo o que a credencial alcanca.

Nada aqui faz I/O, conhece fornecedor ou sabe o que e um workspace por dentro.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class Kind(str, Enum):
    """Que TIPO de coisa um recurso e, do ponto de vista do motor.

    A lista e curta de proposito, e a razao importa. Ela nao tenta descrever a
    arvore de nenhum fornecedor -- um provedor declara os tipos dele como texto,
    e `Resource.kind` aceita qualquer um. O que esta enumerado aqui e so o que o
    MOTOR sabe fazer com um recurso depois de selecionado.

    Um `board` do Jira e um `list` do ClickUp sao ambos `TASK_SOURCE` para o
    motor: e de la que ele tira trabalho. A diferenca entre os dois continua
    inteira em `Resource.kind`, que e o nome do fornecedor para aquilo -- ela
    so nao vira uma decisao do motor.
    """
    #: De onde o motor tira trabalho. Board, projeto, lista, milestone.
    TASK_SOURCE = "task_source"
    #: Onde o motor escreve codigo. Repositorio.
    CODE = "code"
    #: Um no de navegacao: conta, site, organizacao, espaco. Nao e selecionavel
    #: por si -- existe para chegar aos filhos.
    CONTAINER = "container"


@dataclass(frozen=True, slots=True)
class ResourceRef:
    """A identidade de um recurso NO PROVEDOR.

    Nao carrega workspace, pelo mesmo motivo que `RepoRef` nao carrega: o
    adapter nao deve precisar conhecer a tenancy para dizer o que sabe. Quem
    compoe a identidade completa e o motor, com `scoped_to()`.

    O `kind` entra na identidade porque um projeto `SG` e um board `SG` sao
    coisas diferentes no mesmo provedor, e um id sozinho os confundiria.
    """
    provider: str
    kind: str
    id: str

    def __post_init__(self) -> None:
        for nome, valor in (("provider", self.provider), ("kind", self.kind),
                            ("id", self.id)):
            if not str(valor).strip():
                raise ValueError(
                    f"ResourceRef sem {nome}: uma identidade incompleta casa "
                    f"com o recurso errado, e ninguem descobre olhando a tela")

    def __str__(self) -> str:
        return f"{self.provider}:{self.kind}:{self.id}"

    def scoped_to(self, workspace_id: str) -> str:
        """A identidade que o motor usa. Unica no universo de um deployment.

        E ela, e nunca a chave nua, que vira linha no banco. Dois clientes com um
        repositorio `backend` no mesmo GitHub tem o mesmo `provider:kind:id` --
        e precisam continuar sendo dois recursos.
        """
        if not str(workspace_id).strip():
            raise ValueError(
                "escopo vazio: um recurso sem workspace nao pertence a ninguem "
                "e alcancaria todo mundo")
        return f"{workspace_id}/{self.provider}/{self.kind}/{self.id}"

    @classmethod
    def parse(cls, texto: str) -> "ResourceRef":
        """Le `provedor:tipo:id`. O id pode conter dois-pontos."""
        partes = str(texto).split(":", 2)
        if len(partes) != 3:
            raise ValueError(
                f"'{texto}' nao tem a forma provedor:tipo:id")
        return cls(provider=partes[0], kind=partes[1], id=partes[2])


@dataclass(frozen=True, slots=True)
class Resource:
    """Um recurso como o PROVEDOR o descreve, normalizado o minimo necessario.

    "O minimo necessario" e a decisao de desenho. Normalizar demais apagaria o
    que distingue um board de um repositorio; normalizar de menos obrigaria a
    tela a conhecer a resposta da API de cada fornecedor. O meio-termo: identidade,
    nome, lugar na arvore, e uma sacola de dados que so o adapter interpreta.
    """
    ref: ResourceRef
    #: Como o provedor chama isto. `silverguard/backend`, `SG`, `Sprint Board`.
    name: str
    #: Como uma PESSOA chama isto. Vazio significa "use o nome".
    display_name: str = ""
    #: O que o motor faz com isto, depois de selecionado.
    role: Kind = Kind.CONTAINER
    #: De quem isto e filho. `None` na raiz do provedor.
    #:
    #: A hierarquia e uma ARESTA, e nao uma arvore fixa: GitHub tem
    #: conta -> repositorio, e Jira tem site -> projeto -> board. Impor a mesma
    #: profundidade aos dois quebraria um dos dois.
    parent: ResourceRef | None = None
    #: Caminho legivel ate aqui, para a tela nao ter de reconstruir a arvore.
    path: str = ""
    #: Onde uma pessoa ve isto no provedor.
    url: str = ""
    #: Se isto pode ser escolhido, ou so atravessado. Um site do Jira existe
    #: para chegar aos projetos; selecionar um site nao significa nada.
    selectable: bool = True
    #: POR QUE nao se escolhe, quando `selectable` e falso.
    #:
    #: Uma caixa desabilitada sem explicacao le-se como defeito. E os motivos
    #: sao diferentes entre si: um site existe para atravessar, e um board pode
    #: ser um recurso legitimo que ESTE adapter ainda nao sabe usar para
    #: recortar trabalho. Quem le precisa saber com qual dos dois esta lidando.
    note: str = ""
    #: O que o adapter consegue fazer com ESTE recurso. Vocabulario do adapter.
    capabilities: frozenset[str] = field(default_factory=frozenset)
    #: Cru do fornecedor. Ninguem acima do adapter interpreta isto.
    data: dict[str, Any] = field(default_factory=dict)

    @property
    def label(self) -> str:
        return self.display_name or self.name

    def __post_init__(self) -> None:
        if not str(self.name).strip():
            raise ValueError(
                f"recurso {self.ref} sem nome: a tela mostraria uma linha em "
                f"branco e a pessoa escolheria no escuro")


class Falha(str, Enum):
    """Por que uma descoberta nao respondeu.

    Cada uma manda a pessoa fazer uma coisa diferente, e reduzi-las a uma so
    apagaria justamente a diferenca. `SEM_CREDENCIAL` pede registrar uma;
    `EXPIRADA` pede renovar; `INDISPONIVEL` pede esperar; `RECUSADA` pede falar
    com quem administra.
    """
    SEM_CREDENCIAL = "SEM_CREDENCIAL"
    CREDENCIAL_EXPIRADA = "CREDENCIAL_EXPIRADA"
    CREDENCIAL_REVOGADA = "CREDENCIAL_REVOGADA"
    SEM_CAPACIDADE = "SEM_CAPACIDADE"
    POLICY_RECUSOU = "POLICY_RECUSOU"
    PROVEDOR_INDISPONIVEL = "PROVEDOR_INDISPONIVEL"
    NAO_SUPORTADO = "NAO_SUPORTADO"


@dataclass(frozen=True, slots=True)
class Inventario:
    """O resultado de uma descoberta: recursos, OU o motivo de nao haver.

    Este tipo existe por um defeito especifico que ele torna impossivel.

    Uma descoberta que devolvesse `list[Resource]` teria de devolver `[]` quando
    o provedor esta fora do ar -- e `[]` le-se como "voce nao tem nada la".
    A pessoa entao remove a selecao, ou o motor conclui que nao ha trabalho. Nos
    dois casos o dano vem de uma FALHA DE LEITURA sendo tratada como um FATO.

    Aqui os dois nao cabem no mesmo valor: ou `ok` e verdadeiro e ha uma lista,
    ou ha uma `falha` com motivo. Nao existe estado em que ambos valham.
    """
    provider: str
    kind: str
    itens: tuple[Resource, ...] = ()
    falha: Falha | None = None
    detalhe: str = ""
    at: datetime | None = None

    @property
    def ok(self) -> bool:
        return self.falha is None

    def __post_init__(self) -> None:
        if self.falha is not None and self.itens:
            raise ValueError(
                "inventario com falha E com itens: um dos dois esta mentindo, "
                "e quem le nao tem como saber qual")

    @classmethod
    def achou(cls, provider: str, kind: str, itens, at=None) -> "Inventario":
        return cls(provider=provider, kind=kind, itens=tuple(itens), at=at)

    @classmethod
    def nao_deu(cls, provider: str, kind: str, falha: Falha, detalhe: str = "",
                at=None) -> "Inventario":
        return cls(provider=provider, kind=kind, falha=falha, detalhe=detalhe,
                   at=at)


class Situacao(str, Enum):
    """Onde um recurso esta no caminho entre existir e ser usavel.

    Derivada, e nunca guardada: ela e uma comparacao entre o que foi
    SELECIONADO e o que a ultima descoberta ENCONTROU, e guardar o resultado
    criaria uma segunda verdade que envelhece sozinha.
    """
    #: O provedor mostra, e o workspace nao escolheu.
    DISPONIVEL = "DISPONIVEL"
    #: O workspace escolheu, e a ultima descoberta confirmou.
    SELECIONADO = "SELECIONADO"
    #: O workspace escolheu, e a ultima descoberta NAO encontrou.
    #:
    #: Nao e o mesmo que ter sumido: pode ter sumido, pode ter perdido o acesso,
    #: e pode ser que a descoberta nem tenha chegado a rodar. Por isso o nome
    #: fala do que se observou, e nao de uma causa que ninguem apurou.
    NAO_ENCONTRADO = "NAO_ENCONTRADO"


@dataclass(frozen=True, slots=True)
class Selecionado:
    """Um recurso que ESTE workspace escolheu usar. Persistido.

    O que fica gravado e o minimo para identificar e mostrar: a referencia, um
    nome legivel, e quem escolheu quando. O resto e relido do provedor -- gravar
    a descricao inteira criaria uma copia que envelhece em silencio.
    """
    workspace_id: str
    ref: ResourceRef
    name: str
    role: Kind = Kind.CONTAINER
    selected_by: str = ""
    selected_at: datetime | None = None
    #: Quando a ultima descoberta bem-sucedida viu este recurso. `None` significa
    #: que nenhuma descoberta o confirmou desde que foi selecionado.
    last_seen_at: datetime | None = None
    data: dict[str, Any] = field(default_factory=dict)

    @property
    def chave(self) -> str:
        return self.ref.scoped_to(self.workspace_id)


def situacao(ref: ResourceRef, selecionados: set[ResourceRef],
             encontrados: set[ResourceRef] | None) -> Situacao:
    """Onde este recurso esta, dadas a selecao e a ULTIMA descoberta.

    `encontrados=None` significa que a descoberta nao respondeu -- e ai nada e
    marcado como ausente. Chamar de "nao encontrado" o que nao chegou a ser
    procurado e a forma mais rapida de alguem apagar uma selecao boa.
    """
    if ref not in selecionados:
        return Situacao.DISPONIVEL
    if encontrados is None:
        return Situacao.SELECIONADO
    return (Situacao.SELECIONADO if ref in encontrados
            else Situacao.NAO_ENCONTRADO)
