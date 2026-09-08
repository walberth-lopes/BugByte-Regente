# -*- coding: utf-8 -*-
"""Descobrir o que existe, e escolher o que este workspace usa.

Sao duas operacoes, e a fronteira entre elas e o assunto deste modulo.

DESCOBRIR pergunta ao provedor o que esta identidade alcanca. Custa I/O, exige
credencial, e pode falhar -- e falhar precisa continuar sendo diferente de nao
achar nada.

SELECIONAR grava que este workspace passa a usar aquilo. Nao custa I/O, exige
autoridade humana, e e o unico caminho pelo qual um recurso vira algo que o
motor pode tocar.

A separacao existe por um motivo concreto: uma credencial que alcanca 47
repositorios nao autoriza o motor a trabalhar em 47. Sem a etapa do meio,
conectar um provedor entregaria a ele tudo -- e ninguem teria decidido isso.

Este modulo NAO importa adapter nenhum. Ele recebe uma porta de descoberta
pronta de quem compos, do mesmo jeito que a API recebe a sonda de credencial.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

from ..core import ids
from ..core.access import Ability
from ..core.model import Event, now
from ..core.policy import Action, AutonomyLevel, Effect, PolicyContext, PolicyEngine
from ..core.principal import Principal
from ..core.resource import (Falha, Inventario, Kind, Resource, ResourceRef,
                             Selecionado, Situacao, situacao)
from .credentials import Refusal


@dataclass(frozen=True, slots=True)
class ResourceOutcome:
    """O que aconteceu numa operacao de recurso. Nunca traz segredo."""
    accepted: bool
    reason: str
    refusal: Refusal | None = None
    actor: str = ""
    #: As referencias afetadas, para a trilha e para a tela relerem.
    refs: tuple[ResourceRef, ...] = ()


def _no(refusal: Refusal, reason: str, **kw) -> ResourceOutcome:
    return ResourceOutcome(accepted=False, refusal=refusal, reason=reason, **kw)


@dataclass(frozen=True, slots=True)
class Visao:
    """Um recurso como a TELA precisa ve-lo: o que e, e onde esta.

    A situacao e DERIVADA aqui, e nunca lida do banco: ela e uma comparacao
    entre o que foi selecionado e o que a ultima descoberta encontrou, e guardar
    o resultado criaria uma segunda verdade que envelhece sozinha.
    """
    ref: ResourceRef
    name: str
    display_name: str = ""
    role: Kind = Kind.CONTAINER
    parent: ResourceRef | None = None
    path: str = ""
    url: str = ""
    selectable: bool = True
    #: Por que nao se escolhe. Vem do adapter, e a tela mostra literalmente --
    #: uma caixa desabilitada sem explicacao le-se como defeito.
    note: str = ""
    status: Situacao = Situacao.DISPONIVEL
    selected_by: str = ""
    selected_at: datetime | None = None
    last_seen_at: datetime | None = None
    capabilities: tuple[str, ...] = ()

    @property
    def label(self) -> str:
        return self.display_name or self.name

    def as_dict(self) -> dict:
        return {
            "provider": self.ref.provider, "kind": self.ref.kind,
            "id": self.ref.id, "ref": str(self.ref),
            "name": self.name, "display_name": self.display_name,
            "label": self.label, "role": self.role.value,
            "parent": str(self.parent) if self.parent else "",
            "path": self.path, "url": self.url,
            "selectable": self.selectable, "note": self.note,
            "status": self.status.value,
            "selected_by": self.selected_by,
            "selected_at": self.selected_at,
            "last_seen_at": self.last_seen_at,
            "capabilities": list(self.capabilities),
        }


@dataclass(frozen=True, slots=True)
class Achado:
    """O resultado de uma descoberta, ja cruzado com o que o workspace usa.

    Carrega a falha junto de proposito. Quem le precisa poder mostrar "nao deu
    para perguntar" SEM perder a lista do que ja estava selecionado -- se a
    falha apagasse a selecao, um provedor fora do ar faria a tela parecer um
    workspace vazio.
    """
    provider: str
    kind: str
    itens: tuple[Visao, ...] = ()
    falha: Falha | None = None
    detalhe: str = ""
    at: datetime | None = None

    @property
    def ok(self) -> bool:
        return self.falha is None

    def as_dict(self) -> dict:
        return {
            "provider": self.provider, "kind": self.kind,
            "ok": self.ok, "failure": self.falha.value if self.falha else "",
            "detail": self.detalhe, "at": self.at,
            "resources": [i.as_dict() for i in self.itens],
        }


@dataclass(slots=True)
class ResourceService:
    """Descoberta e selecao, pelas mesmas barreiras do resto do motor.

    A cadeia e a de sempre, e nao ha caminho que a contorne:

        identidade -> concessao -> capacidade -> policy -> servico -> auditoria

    `discovery_for` vem da COMPOSICAO: uma funcao que, dado um provedor, devolve
    a porta de descoberta ja construida com o broker vinculado -- ou `None` se
    aquele provedor nao descobre nada. O servico nao importa adapter, nao sabe o
    que e um GitHub, e nao resolve credencial.
    """
    store: Any
    policy: PolicyEngine
    organization: str
    client: str
    workspace_name: str
    environment: str = "staging"
    #: `(provider) -> ResourceDiscovery | None`. Entregue pronta.
    discovery_for: Callable[[str], Any] | None = None
    clock: Callable[[], datetime] = now

    # ------------------------------------------------------------------
    # LEITURA
    # ------------------------------------------------------------------

    def selected(self, actor: Principal, workspace_id: str,
                 role: Kind | None = None) -> list[Selecionado] | ResourceOutcome:
        """O que este workspace escolheu usar.

        Ler exige ESCOPO, e nao capacidade: ver e concessao de composicao, e a
        mesma regra que vale no resto da leitura do motor. O que nao pode e
        atravessar tenant -- e por isso `may_read` vem antes de tocar o banco.
        """
        if not actor.may_read(workspace_id):
            return _no(Refusal.NOT_FOUND, "recurso nao encontrado neste escopo",
                       actor=actor.label)
        return self.store.resources(workspace_id,
                                    role.value if role else None)

    def view(self, actor: Principal, workspace_id: str, provider: str,
             kind: str, inventario: Inventario | None = None) -> Achado:
        """Cruza o que o provedor mostrou com o que o workspace escolheu.

        Sem inventario (`None`), responde so o que esta selecionado -- que e o
        que a tela mostra antes de alguem pedir uma descoberta, e sem custar I/O.
        """
        selecionados = {
            s.ref: s for s in self.store.resources(workspace_id)
            if s.ref.provider == provider and s.ref.kind == kind
        }
        vistos: set[ResourceRef] | None = None
        itens: list[Visao] = []

        if inventario is not None and inventario.ok:
            vistos = {r.ref for r in inventario.itens}
            for r in inventario.itens:
                gravado = selecionados.get(r.ref)
                itens.append(Visao(
                    ref=r.ref, name=r.name, display_name=r.display_name,
                    role=r.role, parent=r.parent, path=r.path, url=r.url,
                    selectable=r.selectable, note=r.note,
                    status=situacao(r.ref, set(selecionados), vistos),
                    selected_by=gravado.selected_by if gravado else "",
                    selected_at=gravado.selected_at if gravado else None,
                    last_seen_at=gravado.last_seen_at if gravado else None,
                    capabilities=tuple(sorted(r.capabilities))))

        # Os selecionados que a descoberta NAO trouxe entram assim mesmo.
        #
        # Some-los seria transformar uma leitura incompleta numa remocao
        # silenciosa: a pessoa abriria a tela, nao veria o repositorio que
        # escolheu, e concluiria que alguem o tirou.
        ja = {i.ref for i in itens}
        for ref, s in selecionados.items():
            if ref in ja:
                continue
            itens.append(Visao(
                ref=ref, name=s.name, role=s.role,
                status=situacao(ref, set(selecionados), vistos),
                selected_by=s.selected_by, selected_at=s.selected_at,
                last_seen_at=s.last_seen_at))

        itens.sort(key=lambda i: (i.status is not Situacao.SELECIONADO,
                                  i.label.lower()))
        return Achado(
            provider=provider, kind=kind, itens=tuple(itens),
            falha=inventario.falha if inventario else None,
            detalhe=inventario.detalhe if inventario else "",
            at=inventario.at if inventario else None)

    # ------------------------------------------------------------------
    # DESCOBERTA
    # ------------------------------------------------------------------

    def discover(self, actor: Principal, workspace_id: str, provider: str,
                 kind: str, parent: ResourceRef | None = None) -> Achado:
        """Pergunta ao provedor o que esta identidade alcanca.

        Devolve `Achado` sempre -- inclusive quando nao deu. Levantar aqui
        obrigaria cada chamador a transformar a excecao em alguma coisa, e o
        "alguma coisa" mais provavel seria uma lista vazia.

        Descobrir NAO seleciona. Este metodo nao escreve uma unica linha de
        selecao, e essa ausencia e o desenho.
        """
        agora = self.clock()

        if not actor.may_read(workspace_id):
            return Achado(provider=provider, kind=kind,
                          falha=Falha.POLICY_RECUSOU,
                          detalhe="recurso nao encontrado neste escopo", at=agora)

        # Descobrir e uma acao, e passa pela policy como qualquer outra.
        decisao = self.policy.decide(PolicyContext(
            action=Action(kind=f"{provider}.resource.discover",
                          resource=workspace_id, environment=self.environment),
            organization=self.organization, client=self.client,
            workspace=self.workspace_name, agent=actor.label,
            autonomy=AutonomyLevel.L0))
        if decisao.effect is Effect.DENY:
            return Achado(provider=provider, kind=kind,
                          falha=Falha.POLICY_RECUSOU,
                          detalhe=f"policy DENY: {decisao.reason}", at=agora)

        inv = self._perguntar(actor, workspace_id, provider, kind, parent,
                              agora=agora)
        if inv is None:
            return Achado(provider=provider, kind=kind,
                          falha=Falha.NAO_SUPORTADO,
                          detalhe=f"'{provider}' nao descobre recursos", at=agora)
        return self.view(actor, workspace_id, provider, kind, inv)

    def _perguntar(self, actor: Principal, workspace_id: str, provider: str,
                   kind: str, parent: ResourceRef | None,
                   agora: datetime | None = None) -> Inventario | None:
        """A ida ao provedor, com marcacao e trilha. `None` = nao descobre.

        Compartilhada por `discover` e `select_refs` de proposito: duas idas ao
        provedor escritas separadamente divergiriam, e a que divergisse seria a
        que esquece de auditar.
        """
        agora = agora or self.clock()
        porta = self.discovery_for(provider) if self.discovery_for else None
        if porta is None:
            return None

        inv = porta.discover(kind, parent)
        if inv.ok:
            # Marcar o que foi VISTO, e nunca apagar o que nao veio.
            self.store.mark_resources_seen(
                workspace_id, provider, kind,
                [r.ref.id for r in inv.itens], agora)
        self._audit_discovery(actor, workspace_id, provider, kind, inv, agora)
        return inv

    # ------------------------------------------------------------------
    # SELECAO
    # ------------------------------------------------------------------

    def select(self, actor: Principal, workspace_id: str,
               recursos: list[Resource]) -> ResourceOutcome:
        """Grava que este workspace passa a usar estes recursos.

        Recebe `Resource` -- o que a descoberta devolveu -- e nao apenas ids.
        Um id sozinho obrigaria o servico a acreditar no nome que o chamador
        mandasse junto, e o nome e o que a tela mostra depois.
        """
        guarda = self._may(actor, workspace_id, Ability.RESOURCE_SELECT)
        if guarda is not None:
            return guarda
        if not recursos:
            return _no(Refusal.NOT_FOUND, "nenhum recurso informado",
                       actor=actor.label)

        nao_selecionaveis = [r for r in recursos if not r.selectable]
        if nao_selecionaveis:
            return _no(
                Refusal.NO_CAPABILITY,
                "estes recursos existem para navegar, e nao para escolher: "
                + ", ".join(str(r.ref) for r in nao_selecionaveis),
                actor=actor.label)

        agora = self.clock()
        for r in recursos:
            self.store.save_resource(Selecionado(
                workspace_id=workspace_id, ref=r.ref, name=r.name, role=r.role,
                selected_by=actor.label, selected_at=agora, last_seen_at=agora,
                data=dict(r.data)))
        refs = tuple(r.ref for r in recursos)
        self._audit_selection("recurso_selecionado", actor, workspace_id, refs)
        return ResourceOutcome(
            accepted=True, actor=actor.label, refs=refs,
            reason=f"{len(refs)} recurso(s) passaram a pertencer a este workspace")

    def select_refs(self, actor: Principal, workspace_id: str, provider: str,
                    kind: str, ids: list[str],
                    parent: ResourceRef | None = None) -> ResourceOutcome:
        """Escolhe por IDENTIFICADOR, conferindo contra o provedor primeiro.

        Existe porque a tela e a CLI so tem ids. Aceitar o recurso inteiro que o
        chamador mandasse faria do CORPO DA REQUISICAO a autoridade: bastaria
        enviar `selectable: true` para escolher uma organizacao inteira, ou um
        `role` diferente para o motor tratar um repositorio como fonte de
        trabalho. O nome gravado tambem viria de fora -- e o nome e o que a tela
        mostra depois a quem for auditar.

        Entao a descoberta e refeita, e so o que o PROVEDOR confirmou pode ser
        escolhido. Custa uma ida ao provedor por selecao, e paga: escolher passa
        a ser impossivel sem enxergar.

        Descobrir aqui NAO e um atalho: e a mesma `discover`, com as mesmas
        barreiras -- inclusive a policy de descoberta, que e separada da de
        selecao. Quem so pode escolher e nao pode descobrir e recusado aqui, e
        essa e a resposta certa.
        """
        guarda = self._may(actor, workspace_id, Ability.RESOURCE_SELECT)
        if guarda is not None:
            return guarda

        pedidos = [str(i) for i in (ids or []) if str(i).strip()]
        if not pedidos:
            return _no(Refusal.NOT_FOUND, "nenhum recurso informado",
                       actor=actor.label)

        inv = self._perguntar(actor, workspace_id, provider, kind, parent)
        if inv is None:
            return _no(Refusal.NO_CAPABILITY,
                       f"'{provider}' nao descobre recursos, entao nao ha o que "
                       f"escolher por aqui", actor=actor.label)
        if not inv.ok:
            # Falhar em perguntar NAO vira "nao existe". A recusa diz que a
            # escolha nao pode ser conferida agora -- e nao que o recurso sumiu.
            return _no(Refusal.SOURCE_UNAVAILABLE,
                       f"nao deu para conferir com o provedor ({inv.falha.value}): "
                       f"{inv.detalhe}"[:300], actor=actor.label)

        por_id = {r.ref.id: r for r in inv.itens}
        faltando = [i for i in pedidos if i not in por_id]
        if faltando:
            return _no(Refusal.NOT_FOUND,
                       "o provedor nao mostrou estes recursos agora: "
                       + ", ".join(faltando[:10]), actor=actor.label)
        return self.select(actor, workspace_id, [por_id[i] for i in pedidos])

    def unselect(self, actor: Principal, workspace_id: str,
                 ref: ResourceRef) -> ResourceOutcome:
        """Tira o recurso do workspace. O motor para de alcanca-lo no ato."""
        guarda = self._may(actor, workspace_id, Ability.RESOURCE_SELECT)
        if guarda is not None:
            return guarda

        havia = self.store.drop_resource(workspace_id, ref.provider, ref.kind,
                                         ref.id)
        if not havia:
            return _no(Refusal.NOT_FOUND,
                       "este recurso nao estava selecionado neste workspace",
                       actor=actor.label, refs=(ref,))
        self._audit_selection("recurso_removido", actor, workspace_id, (ref,))
        return ResourceOutcome(
            accepted=True, actor=actor.label, refs=(ref,),
            reason="o motor deixa de alcancar este recurso a partir de agora")

    # ------------------------------------------------------------------
    # barreiras e trilha
    # ------------------------------------------------------------------

    def _may(self, actor: Principal, workspace_id: str,
             ability: Ability) -> ResourceOutcome | None:
        """As barreiras comuns. `None` significa que pode seguir.

        A ordem e a mesma de `AccessService` e `CredentialService`, e ela nao e
        arbitraria: o escopo e conferido ANTES de o workspace ser lido, e a
        recusa por falta de autoridade responde `NOT_FOUND` -- distinguir "nao
        existe" de "existe e nao e seu" confirmaria a existencia de um workspace
        alheio a quem tentou adivinhar.
        """
        if not actor.authenticated:
            return _no(Refusal.UNAUTHENTICATED,
                       "esta requisicao nao foi autenticada")
        if not actor.can(workspace_id, ability):
            return _no(Refusal.NOT_FOUND, "recurso nao encontrado neste escopo",
                       actor=actor.label)
        if self.store.workspace(workspace_id) is None:
            return _no(Refusal.NOT_FOUND, "recurso nao encontrado neste escopo",
                       actor=actor.label)

        decisao = self.policy.decide(PolicyContext(
            action=Action(kind=ability.value, resource=workspace_id,
                          environment=self.environment),
            organization=self.organization, client=self.client,
            workspace=self.workspace_name, agent=actor.label,
            autonomy=AutonomyLevel.L4))
        if decisao.effect is Effect.DENY:
            return _no(Refusal.POLICY_DENIED, f"policy DENY: {decisao.reason}",
                       actor=actor.label)
        return None

    def _audit_selection(self, kind: str, actor: Principal, workspace_id: str,
                         refs: tuple[ResourceRef, ...]) -> None:
        """Quem, quando, onde, o que. Nunca material.

        Nao ha caminho neste metodo que alcance um segredo: `ResourceRef` nao
        carrega credencial, e o que se grava e o endereco do RECURSO -- publico
        por natureza, e o oposto de um token.
        """
        self.store.record_event(Event(
            id=ids.new_id(ids.EVENT), workspace_id=workspace_id, kind=kind,
            actor=actor.label,
            summary=f"{actor.label}: {kind} {', '.join(str(r) for r in refs)}"[:300],
            data={"refs": [str(r) for r in refs],
                  "providers": sorted({r.provider for r in refs})}))

    def _audit_discovery(self, actor: Principal, workspace_id: str,
                         provider: str, kind: str, inv: Inventario,
                         at: datetime) -> None:
        """Descoberta tambem deixa rastro -- e o rastro registra a FALHA.

        Um evento so quando deu certo esconderia justamente o que se quer
        investigar depois: por que a tela ficou vazia naquela tarde.
        """
        self.store.record_event(Event(
            id=ids.new_id(ids.EVENT), workspace_id=workspace_id,
            kind="recursos_descobertos", actor=actor.label,
            summary=(f"{actor.label}: {provider}/{kind} -> "
                     + (f"{len(inv.itens)} encontrado(s)" if inv.ok
                        else f"falhou ({inv.falha.value})"))[:300],
            data={"provider": provider, "kind": kind, "ok": inv.ok,
                  "count": len(inv.itens),
                  "failure": inv.falha.value if inv.falha else "",
                  "detail": inv.detalhe[:500]}))


# ===========================================================================
# O FILTRO
# ===========================================================================

@dataclass(slots=True)
class SomenteSelecionados:
    """Envelope que esconde do motor tudo o que o workspace nao escolheu.

    E aqui que "recurso nao selecionado nao pode ser usado" deixa de ser uma
    intencao e vira uma propriedade. O motor chama `list_repositories()` como
    sempre chamou; o que muda e que a resposta ja vem cortada.

    POR QUE AQUI, e nao no adapter: um adapter que conhecesse a selecao
    precisaria conhecer o workspace, o banco e a tenancy -- e cada adapter novo
    teria de reimplementar isso, com a chance de um deles esquecer. Envelopando
    na composicao, acrescentar `adapters/gitlab/` nao custa uma linha de
    seguranca.

    POR QUE NAO UM FILTRO NO CHAMADOR: porque haveria mais de um chamador, e o
    que esquecesse seria o furo. Aqui a unica forma de alcancar o provedor e
    passando por este objeto.

    COMPATIBILIDADE. Um workspace sem NENHUMA selecao continua vendo tudo, e
    isso e deliberado: a tabela nasce vazia, e quem migra um banco existente nao
    pode acordar com o motor parado por uma decisao que ninguem tomou. A partir
    da primeira selecao, a lista passa a ser exatamente a escolhida.
    """
    inner: Any
    workspace_id: str
    store: Any
    #: Que papel de recurso este envelope filtra.
    role: Kind = Kind.CODE

    def __getattr__(self, nome: str):
        # Todo o resto do provedor continua igual: ler arquivo, abrir PR, listar
        # branch. O envelope corta a LISTAGEM, e nao a operacao -- quem pede um
        # repositorio pelo nome ja passou por `list_repositories` para chegar
        # nele, e cortar duas vezes so esconderia erro.
        return getattr(self.inner, nome)

    def _escolhidos(self) -> set[str] | None:
        """Os ids selecionados, ou `None` quando nao ha selecao nenhuma.

        O NOME entra junto do id de proposito. Um provedor identifica um projeto
        por um numero e uma pessoa o chama pela sigla; qual dos dois ficou
        gravado depende do caminho por onde a escolha entrou. Aceitar so um
        faria o cruzamento falhar em silencio -- e falhar em silencio, aqui,
        significa fila vazia.
        """
        linhas = self.store.resources(self.workspace_id, self.role.value)
        escolhidos = {s.ref.id for s in linhas} | {s.name for s in linhas if s.name}
        return escolhidos or None

    def list_repositories(self, filtro: dict | None = None):
        todos = self.inner.list_repositories(filtro)
        escolhidos = self._escolhidos()
        if escolhidos is None:
            return todos
        return [r for r in todos if r.ref.key in escolhidos]

    def list_tasks(self, filtro: dict | None = None):
        """O mesmo corte, do outro lado: o motor so ve trabalho do que foi escolhido.

        Uma task so e descartada quando ela DIZ de onde veio e aquilo nao esta
        entre os escolhidos. Uma task que nao sabe dizer PASSA -- porque
        "nao consigo provar que pertence" nao e o mesmo que "nao pertence", e
        tratar os dois igual esvaziaria a fila de todo adapter que ainda nao
        preenche a origem.

        O risco dos dois lados nao e simetrico, e por isso a escolha nao e
        arbitraria: deixar passar demais gera trabalho que alguem revisa e
        interrompe; cortar demais gera silencio, e ninguem revisa silencio.
        """
        todas = self.inner.list_tasks(filtro)
        escolhidos = self._escolhidos()
        if escolhidos is None:
            return todas
        return [t for t in todas
                if not getattr(t, "sources", ())
                or any(o in escolhidos for o in t.sources)]
