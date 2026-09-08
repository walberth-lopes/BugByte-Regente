# -*- coding: utf-8 -*-
"""Integracoes autodescobriveis: descobrir, escolher, e o que separa as duas.

Este arquivo guarda uma fronteira, e vale dizer qual antes dos testes.

Uma credencial que alcanca 47 repositorios NAO autoriza o motor a trabalhar em
47. Ela autoriza PERGUNTAR. Entre "o provedor tem" e "o motor pode tocar" existe
uma escolha humana, gravada e atribuivel -- e sem essa etapa do meio, conectar
um provedor entregaria a ele tudo o que a credencial alcanca.

Os defeitos que estes testes existem para impedir sao todos silenciosos:

* uma descoberta que falha virando lista vazia, e a pessoa concluindo que a
  conta esvaziou;
* uma selecao boa sendo apagada porque a leitura seguinte veio incompleta;
* o mesmo `org/backend` de dois clientes virando um recurso so;
* conectar um provedor concedendo, por tabela, leitura de tudo.

Nenhum deles falha alto. Todos parecem funcionar ate o dia em que custam caro.
"""

from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
import yaml

from regente.core.access import Ability, AccessGrant, PrincipalRef, abilities_of
from regente.core.model import Workspace
from regente.core.policy import PolicyEngine
from regente.core.principal import Principal
from regente.core.resource import (Falha, Inventario, Kind, Resource,
                                   ResourceRef, Selecionado, Situacao, situacao)
from regente.engine.resources import (ResourceService, SomenteSelecionados)
from regente.engine.store_sqlite import SqliteStore

T0 = datetime(2026, 9, 12, 9, 0, tzinfo=timezone.utc)

#: A policy REAL, a mesma que `regente init` escreve num workspace novo.
#:
#: A bancada nao inventa uma policy permissiva. Se estas acoes nao estiverem
#: declaradas no arquivo que a pessoa recebe, descobrir e escolher nascem mortos
#: em todo workspace do mundo -- e um teste com policy propria passaria verde
#: enquanto o produto instalado nao funciona. E o unico jeito de a suite provar
#: que o caminho existe de verdade e ela usar o mesmo arquivo.
POLICY = PolicyEngine.from_config(
    yaml.safe_load(
        Path("regente/resources/policies.yaml.example").read_text(
            encoding="utf-8"))["rules"])


# ===========================================================================
# bancada
# ===========================================================================

@dataclass
class ProvedorFake:
    """Um provedor determinístico. Nao fala com rede, e responde o combinado.

    Existe para exercitar o COMPORTAMENTO das barreiras sem inventar credencial
    nenhuma. O que ele nao prova -- que o GitHub de verdade responde assim --
    nenhum teste com provedor falso prova, e por isso isso esta marcado no
    relatorio, e nao escondido aqui.
    """
    itens: list[Resource] = field(default_factory=list)
    falha: Falha | None = None
    detalhe: str = ""
    tipos: tuple[str, ...] = ("repository",)
    chamadas: int = 0

    def discovers(self) -> tuple[str, ...]:
        return self.tipos

    def discover(self, kind: str, parent: ResourceRef | None = None) -> Inventario:
        self.chamadas += 1
        if kind not in self.tipos:
            return Inventario.nao_deu("fake", kind, Falha.NAO_SUPORTADO,
                                      "tipo desconhecido", at=T0)
        if self.falha is not None:
            return Inventario.nao_deu("fake", kind, self.falha, self.detalhe, at=T0)
        filhos = [r for r in self.itens
                  if parent is None or r.parent == parent]
        return Inventario.achou("fake", kind, filhos, at=T0)


def _repo(ident: str, parent: ResourceRef | None = None) -> Resource:
    return Resource(
        ref=ResourceRef("fake", "repository", ident), name=ident,
        role=Kind.CODE, parent=parent, path=ident)


@dataclass
class Bancada:
    store: SqliteStore
    service: ResourceService
    workspace: str
    provedor: ProvedorFake


def _bancada(tmp_path: Path, provedor: ProvedorFake | None = None,
             workspace: str = "wks_A") -> Bancada:
    store = SqliteStore(tmp_path / f"{workspace}.db")
    store.migrate()
    store.save_client("cli_1", "acme", "acme")
    store.save_workspace(Workspace(id=workspace, client_id="cli_1", name="main",
                                   root=str(tmp_path)))
    p = provedor or ProvedorFake(itens=[_repo("org/a"), _repo("org/b"),
                                        _repo("org/c")])
    return Bancada(
        store=store, workspace=workspace, provedor=p,
        service=ResourceService(
            store=store, policy=POLICY,
            organization="acme", client="acme", workspace_name="main",
            discovery_for=lambda _prov, _ator=None: p, clock=lambda: T0))


def _quem(workspace: str, *abilities: Ability, sujeito: str = "os:1") -> Principal:
    # `authenticated` e DERIVADO de `method`, e nao um campo: uma identidade
    # que se declara autenticada sem dizer como foi provada e o que o marco 14
    # removeu.
    return Principal(
        subject=sujeito, display=sujeito, method="teste", provider="os",
        authenticated_at=T0, workspaces=frozenset({workspace}),
        abilities={workspace: frozenset(abilities)})


def _dono(workspace: str, sujeito: str = "os:1") -> Principal:
    return _quem(workspace, *abilities_of("owner"), sujeito=sujeito)


# ===========================================================================
# 1. DESCOBRIR NAO E AUTORIZAR
# ===========================================================================

def test_a_discovered_resource_is_not_a_resource_the_engine_may_touch(tmp_path):
    """O coracao do marco: descobrir tres nao autoriza a trabalhar em tres."""
    b = _bancada(tmp_path)
    achado = b.service.discover(_dono(b.workspace), b.workspace, "fake",
                                "repository")

    assert achado.ok
    assert len(achado.itens) == 3
    assert all(i.status is Situacao.DISPONIVEL for i in achado.itens), (
        "a descoberta marcou algo como selecionado sem ninguem ter escolhido")
    assert b.store.resources(b.workspace) == [], (
        "descobrir gravou selecao: conectar passaria a conceder tudo")


def test_selecting_is_what_makes_a_resource_usable(tmp_path):
    b = _bancada(tmp_path)
    achado = b.service.discover(_dono(b.workspace), b.workspace, "fake",
                                "repository")
    escolhido = [r for r in b.provedor.itens if r.ref.id == "org/b"]

    saida = b.service.select(_dono(b.workspace), b.workspace, escolhido)
    assert saida.accepted, saida.reason

    depois = b.service.discover(_dono(b.workspace), b.workspace, "fake",
                                "repository")
    porref = {i.ref.id: i.status for i in depois.itens}
    assert porref["org/b"] is Situacao.SELECIONADO
    assert porref["org/a"] is Situacao.DISPONIVEL
    assert porref["org/c"] is Situacao.DISPONIVEL


def test_connecting_a_provider_grants_nothing_by_itself(tmp_path):
    """Conectar != autorizar. Sem selecionar, o motor nao ganha recurso nenhum."""
    b = _bancada(tmp_path)
    b.service.discover(_dono(b.workspace), b.workspace, "fake", "repository")
    assert b.service.selected(_dono(b.workspace), b.workspace) == []


# ===========================================================================
# 2. FALHAR NAO E NAO ACHAR NADA
# ===========================================================================

@pytest.mark.parametrize("falha", [
    Falha.PROVEDOR_INDISPONIVEL, Falha.CREDENCIAL_EXPIRADA,
    Falha.CREDENCIAL_REVOGADA, Falha.SEM_CREDENCIAL, Falha.SEM_CAPACIDADE,
])
def test_a_discovery_that_failed_is_never_an_empty_list(tmp_path, falha):
    """"Nao consegui perguntar" e "nao ha nada" mandam fazer coisas opostas.

    Se as duas chegassem como `[]`, a pessoa removeria a selecao de um
    repositorio que continua existindo -- e o motor concluiria que nao ha
    trabalho num dia em que so a rede caiu.
    """
    b = _bancada(tmp_path, ProvedorFake(falha=falha, detalhe="algo deu errado"))
    achado = b.service.discover(_dono(b.workspace), b.workspace, "fake",
                                "repository")

    assert not achado.ok
    assert achado.falha is falha
    assert achado.detalhe, "a falha nao disse o que houve"


def test_the_type_itself_forbids_saying_both_things(tmp_path):
    """Um inventario nao pode ter falha E itens: um dos dois estaria mentindo."""
    with pytest.raises(ValueError) as erro:
        Inventario(provider="fake", kind="repository",
                   itens=(_repo("org/a"),), falha=Falha.PROVEDOR_INDISPONIVEL)
    assert "mentindo" in str(erro.value)


def test_a_failed_discovery_never_erases_a_good_selection(tmp_path):
    """O provedor cai, e o que a pessoa escolheu continua la.

    Este e o defeito que mais custa: uma leitura ruim virando uma remocao.
    """
    b = _bancada(tmp_path)
    b.service.select(_dono(b.workspace), b.workspace,
                     [r for r in b.provedor.itens if r.ref.id == "org/b"])

    b.provedor.falha = Falha.PROVEDOR_INDISPONIVEL
    achado = b.service.discover(_dono(b.workspace), b.workspace, "fake",
                                "repository")

    assert not achado.ok
    ainda = b.service.selected(_dono(b.workspace), b.workspace)
    assert [s.ref.id for s in ainda] == ["org/b"], (
        "a falha de leitura apagou a selecao")
    assert [i.ref.id for i in achado.itens] == ["org/b"], (
        "a tela perderia de vista o que o workspace usa")


def test_a_selected_resource_missing_from_a_good_discovery_is_named(tmp_path):
    """Sumiu de uma leitura BEM-SUCEDIDA e outra coisa, e precisa aparecer."""
    b = _bancada(tmp_path)
    b.service.select(_dono(b.workspace), b.workspace,
                     [r for r in b.provedor.itens if r.ref.id == "org/b"])

    b.provedor.itens = [_repo("org/a"), _repo("org/c")]     # `b` saiu do provedor
    achado = b.service.discover(_dono(b.workspace), b.workspace, "fake",
                                "repository")

    assert achado.ok
    situacoes = {i.ref.id: i.status for i in achado.itens}
    assert situacoes["org/b"] is Situacao.NAO_ENCONTRADO
    assert b.service.selected(_dono(b.workspace), b.workspace), (
        "identificar que sumiu nao e o mesmo que remover sozinho")


def test_not_looked_for_is_not_missing():
    """A funcao pura: sem descoberta, nada e marcado como ausente."""
    ref = ResourceRef("fake", "repository", "org/a")
    assert situacao(ref, {ref}, None) is Situacao.SELECIONADO
    assert situacao(ref, {ref}, set()) is Situacao.NAO_ENCONTRADO


# ===========================================================================
# 3. O MOTOR SO ALCANCA O QUE FOI ESCOLHIDO
# ===========================================================================

@dataclass
class ReposFake:
    """Um provider de repositorio que devolve tudo o que a credencial alcanca."""
    itens: list[Any]

    def list_repositories(self, filtro=None):
        return list(self.itens)

    def qualquer_outra_coisa(self):
        return "passou direto"


@dataclass(frozen=True)
class _Ref:
    key: str


@dataclass(frozen=True)
class _Repo:
    ref: _Ref


def test_the_engine_only_sees_the_selected_resources(tmp_path):
    """47 alcancaveis, 2 escolhidos: o motor enxerga 2."""
    b = _bancada(tmp_path)
    todos = [_Repo(_Ref(f"org/r{i}")) for i in range(47)]
    b.service.select(_dono(b.workspace), b.workspace, [
        _repo("org/r3"), _repo("org/r9")])

    filtrado = SomenteSelecionados(inner=ReposFake(todos),
                                   workspace_id=b.workspace, store=b.store)
    visiveis = [r.ref.key for r in filtrado.list_repositories()]
    assert sorted(visiveis) == ["org/r3", "org/r9"]


def test_without_any_selection_the_engine_keeps_working(tmp_path):
    """Um banco que migra nao pode acordar com o motor parado.

    A tabela nasce vazia. Cortar tudo enquanto ninguem escolheu nada seria
    aplicar uma decisao que ninguem tomou -- e derrubaria todo workspace
    existente na atualizacao.
    """
    b = _bancada(tmp_path)
    todos = [_Repo(_Ref("org/a")), _Repo(_Ref("org/b"))]
    filtrado = SomenteSelecionados(inner=ReposFake(todos),
                                   workspace_id=b.workspace, store=b.store)
    assert len(filtrado.list_repositories()) == 2


def test_the_filter_does_not_get_in_the_way_of_anything_else(tmp_path):
    """Envelopar a listagem nao pode quebrar o resto do provider."""
    b = _bancada(tmp_path)
    filtrado = SomenteSelecionados(inner=ReposFake([]),
                                   workspace_id=b.workspace, store=b.store)
    assert filtrado.qualquer_outra_coisa() == "passou direto"


def test_removing_a_resource_takes_it_from_the_engine_at_once(tmp_path):
    b = _bancada(tmp_path)
    b.service.select(_dono(b.workspace), b.workspace,
                     [_repo("org/a"), _repo("org/b")])
    filtrado = SomenteSelecionados(inner=ReposFake([_Repo(_Ref("org/a")),
                                                    _Repo(_Ref("org/b"))]),
                                   workspace_id=b.workspace, store=b.store)
    assert len(filtrado.list_repositories()) == 2

    saida = b.service.unselect(_dono(b.workspace), b.workspace,
                               ResourceRef("fake", "repository", "org/a"))
    assert saida.accepted
    assert [r.ref.key for r in filtrado.list_repositories()] == ["org/b"]


# ===========================================================================
# 3b. O CORTE DO LADO DO TRABALHO
# ===========================================================================
#
# Um repositorio nao escolhido nao pode ser tocado; uma fonte de trabalho nao
# escolhida nao pode entrar na fila. As duas metades do mesmo corte, e a segunda
# tem um risco que a primeira nao tem: cortar demais aqui produz SILENCIO, e
# ninguem revisa silencio.

@dataclass(frozen=True)
class _Task:
    key: str
    sources: tuple = ()


@dataclass
class TasksFake:
    itens: list

    def list_tasks(self, filtro=None):
        return list(self.itens)

    def get_task(self, key):
        return next(t for t in self.itens if t.key == key)


def _fonte(ident: str, nome: str = "") -> Resource:
    return Resource(
        ref=ResourceRef("fake", "project", ident), name=nome or ident,
        role=Kind.TASK_SOURCE, path=ident)


def _bancada_de_trabalho(tmp_path, escolhidas: list[Resource]):
    b = _bancada(tmp_path, ProvedorFake(itens=escolhidas, tipos=("project",)))
    if escolhidas:
        assert b.service.select(_dono(b.workspace), b.workspace,
                                escolhidas).accepted
    return b


def test_work_from_a_source_nobody_chose_never_enters_the_queue(tmp_path):
    b = _bancada_de_trabalho(tmp_path, [_fonte("10001", "SG")])
    fila = SomenteSelecionados(
        inner=TasksFake([_Task("SG-1", ("10001", "SG")),
                         _Task("OUTRO-9", ("10777", "OUTRO"))]),
        workspace_id=b.workspace, store=b.store, role=Kind.TASK_SOURCE)
    assert [t.key for t in fila.list_tasks()] == ["SG-1"]


def test_a_source_chosen_by_key_matches_a_task_that_reports_the_id(tmp_path):
    """Id e nome sao a mesma escolha vista de dois lugares.

    A descoberta identifica um projeto por um numero; quem configurou o board a
    mao escreveu a sigla. Aceitar so um dos dois faria o cruzamento falhar
    conforme o caminho por onde a selecao entrou -- e falhar calado, esvaziando
    a fila de quem nao fez nada de errado.
    """
    b = _bancada_de_trabalho(tmp_path, [_fonte("10001", "SG")])
    fila = SomenteSelecionados(
        inner=TasksFake([_Task("SG-1", ("SG",)), _Task("SG-2", ("10001",))]),
        workspace_id=b.workspace, store=b.store, role=Kind.TASK_SOURCE)
    assert sorted(t.key for t in fila.list_tasks()) == ["SG-1", "SG-2"]


def test_a_task_that_cannot_say_where_it_came_from_still_passes(tmp_path):
    """"Nao consigo provar que pertence" nao e "nao pertence".

    Um adapter que ainda nao preenche a origem veria a fila inteira sumir. O
    risco dos dois lados nao e simetrico: deixar passar gera trabalho que alguem
    revisa e interrompe; cortar gera silencio.
    """
    b = _bancada_de_trabalho(tmp_path, [_fonte("10001", "SG")])
    fila = SomenteSelecionados(
        inner=TasksFake([_Task("VELHA-1")]),
        workspace_id=b.workspace, store=b.store, role=Kind.TASK_SOURCE)
    assert [t.key for t in fila.list_tasks()] == ["VELHA-1"]


def test_without_a_chosen_source_the_queue_is_untouched(tmp_path):
    b = _bancada_de_trabalho(tmp_path, [])
    fila = SomenteSelecionados(
        inner=TasksFake([_Task("A-1", ("1",)), _Task("B-2", ("2",))]),
        workspace_id=b.workspace, store=b.store, role=Kind.TASK_SOURCE)
    assert len(fila.list_tasks()) == 2


def test_choosing_code_does_not_narrow_work_nor_the_other_way_round(tmp_path):
    """Os dois cortes sao independentes, e cada um le so o proprio papel.

    Sem isto, escolher o primeiro repositorio esvaziaria a fila de tasks -- um
    workspace inteiro parado por uma escolha que nao falava sobre trabalho.
    """
    b = _bancada(tmp_path)
    assert b.service.select(_dono(b.workspace), b.workspace,
                            [_repo("org/a")]).accepted
    fila = SomenteSelecionados(
        inner=TasksFake([_Task("SG-1", ("10001",))]),
        workspace_id=b.workspace, store=b.store, role=Kind.TASK_SOURCE)
    assert [t.key for t in fila.list_tasks()] == ["SG-1"]


# ===========================================================================
# 3c. ESCOLHER POR IDENTIFICADOR
# ===========================================================================
#
# A tela e a CLI so tem ids. Aceitar o recurso inteiro que o chamador mandasse
# faria do CORPO DA REQUISICAO a autoridade -- bastaria enviar
# `selectable: true` para escolher uma organizacao inteira.

def test_choosing_by_id_only_accepts_what_the_provider_just_showed(tmp_path):
    b = _bancada(tmp_path)
    saida = b.service.select_refs(_dono(b.workspace), b.workspace, "fake",
                                  "repository", ["org/a", "org/inventado"])
    assert not saida.accepted
    assert saida.refusal.value == "NOT_FOUND"
    assert b.store.resources(b.workspace) == [], (
        "escolheu o que existia e deixou passar meia selecao")


def test_choosing_by_id_records_the_providers_name_not_the_callers(tmp_path):
    """O nome gravado e o que a auditoria le depois. Ele vem do provedor."""
    b = _bancada(tmp_path, ProvedorFake(itens=[
        Resource(ref=ResourceRef("fake", "repository", "org/a"),
                 name="org/a", display_name="Backend", role=Kind.CODE)]))
    assert b.service.select_refs(_dono(b.workspace), b.workspace, "fake",
                                 "repository", ["org/a"]).accepted
    assert b.store.resources(b.workspace)[0].name == "org/a"


def test_choosing_by_id_refuses_what_exists_only_to_navigate(tmp_path):
    conta = Resource(ref=ResourceRef("fake", "account", "org"), name="org",
                     selectable=False)
    b = _bancada(tmp_path, ProvedorFake(itens=[conta], tipos=("account",)))
    saida = b.service.select_refs(_dono(b.workspace), b.workspace, "fake",
                                  "account", ["org"])
    assert not saida.accepted
    assert "navegar" in saida.reason


def test_a_provider_that_is_down_blocks_choosing_instead_of_guessing(tmp_path):
    """Nao deu para conferir NAO e "o recurso nao existe".

    Gravar a escolha as cegas colocaria no workspace um recurso que ninguem
    confirmou; responder NOT_FOUND mandaria a pessoa procurar um repositorio que
    esta la. As duas leituras erradas vem da mesma confusao.
    """
    b = _bancada(tmp_path, ProvedorFake(falha=Falha.PROVEDOR_INDISPONIVEL,
                                        detalhe="timeout"))
    saida = b.service.select_refs(_dono(b.workspace), b.workspace, "fake",
                                  "repository", ["org/a"])
    assert not saida.accepted
    assert saida.refusal.value == "SOURCE_UNAVAILABLE"
    assert b.store.resources(b.workspace) == []


def test_choosing_by_id_still_needs_the_ability(tmp_path):
    b = _bancada(tmp_path)
    operador = _quem(b.workspace, *abilities_of("operator"))
    saida = b.service.select_refs(operador, b.workspace, "fake", "repository",
                                  ["org/a"])
    assert not saida.accepted
    assert b.provedor.chamadas == 0, (
        "perguntou ao provedor antes de conferir a autoridade")

# ===========================================================================
# 4. TENANCY
# ===========================================================================

def test_two_workspaces_in_the_SAME_database_never_read_each_other(tmp_path):
    """Um banco, dois workspaces. E a forma real de uma instalacao.

    Este teste existe porque a varredura de mutacao passou: tirar o
    `WHERE workspace_id = ?` da leitura de recursos nao quebrou nada, e a razao
    era a bancada -- ela dava um ARQUIVO a cada workspace, e arquivos separados
    nao se contaminam nem com a consulta errada. A tenancy so aparece quando os
    dois dividem o mesmo banco, que e como todo cliente real roda.
    """
    from regente.core.model import Workspace

    store = SqliteStore(tmp_path / "compartilhado.db")
    store.migrate()
    for wid, cid, nome in (("wks_A", "cli_1", "acme"), ("wks_B", "cli_2", "beta")):
        store.save_client(cid, nome, nome)
        store.save_workspace(Workspace(id=wid, client_id=cid, name="main",
                                       root=str(tmp_path)))

    servico = ResourceService(
        store=store, policy=POLICY, organization="acme", client="acme",
        workspace_name="main", discovery_for=lambda _prov, _ator=None: None, clock=lambda: T0)

    # O MESMO recurso, escolhido nos dois. Um `org/backend` no mesmo provedor e
    # dois recursos diferentes quando os clientes sao dois.
    for wid in ("wks_A", "wks_B"):
        assert servico.select(_dono(wid), wid, [_repo("org/backend")]).accepted

    assert [r.workspace_id for r in store.resources("wks_A")] == ["wks_A"]
    assert [r.workspace_id for r in store.resources("wks_B")] == ["wks_B"]

    # Cada um escolhe mais um, e sao coisas diferentes.
    assert servico.select(_dono("wks_A"), "wks_A", [_repo("org/so-do-A")]).accepted
    assert servico.select(_dono("wks_B"), "wks_B", [_repo("org/so-do-B")]).accepted

    # O corte do motor le a MESMA consulta, e tambem nao atravessa.
    catalogo = [_Repo(_Ref("org/backend")), _Repo(_Ref("org/so-do-A")),
                _Repo(_Ref("org/so-do-B"))]
    for wid, esperado in (("wks_A", ["org/backend", "org/so-do-A"]),
                          ("wks_B", ["org/backend", "org/so-do-B"])):
        filtrado = SomenteSelecionados(inner=ReposFake(catalogo),
                                       workspace_id=wid, store=store)
        visiveis = sorted(r.ref.key for r in filtrado.list_repositories())
        assert visiveis == esperado, (
            f"o motor de {wid} alcancou o que o outro cliente escolheu")

    # E remover de um nao toca no outro.
    assert servico.unselect(_dono("wks_A"), "wks_A",
                            ResourceRef("fake", "repository", "org/backend")).accepted
    assert sorted(r.ref.id for r in store.resources("wks_A")) == ["org/so-do-A"]
    assert sorted(r.ref.id for r in store.resources("wks_B")) == [
        "org/backend", "org/so-do-B"]
    store.close()


def test_the_same_resource_id_in_two_clients_never_crosses(tmp_path):
    """`org/backend` em dois clientes sao DOIS recursos, e nao um partilhado.

    E o defeito de tenancy mais silencioso que existe: tudo funciona ate haver
    o segundo cliente.
    """
    a = _bancada(tmp_path, workspace="wks_A")
    bb = _bancada(tmp_path, workspace="wks_B")

    a.service.select(_dono("wks_A"), "wks_A", [_repo("org/backend")])

    assert [s.ref.id for s in a.service.selected(_dono("wks_A"), "wks_A")] \
        == ["org/backend"]
    assert bb.service.selected(_dono("wks_B"), "wks_B") == [], (
        "selecionar num cliente apareceu selecionado no outro")


def test_nobody_selects_into_a_workspace_they_cannot_reach(tmp_path):
    b = _bancada(tmp_path, workspace="wks_A")
    _bancada(tmp_path, workspace="wks_B")

    de_outro = _dono("wks_B", sujeito="os:2")
    saida = b.service.select(de_outro, "wks_A", [_repo("org/a")])

    assert not saida.accepted
    assert saida.refusal.value == "NOT_FOUND", (
        "responder FORBIDDEN confirmaria que o workspace existe")
    assert b.store.resources("wks_A") == []


def test_nobody_reads_the_selection_of_another_tenant(tmp_path):
    b = _bancada(tmp_path, workspace="wks_A")
    b.service.select(_dono("wks_A"), "wks_A", [_repo("org/a")])

    saida = b.service.selected(_dono("wks_B", sujeito="os:2"), "wks_A")
    assert not getattr(saida, "accepted", True)


def test_nobody_discovers_into_a_workspace_they_cannot_reach(tmp_path):
    b = _bancada(tmp_path, workspace="wks_A")
    achado = b.service.discover(_dono("wks_B", sujeito="os:2"), "wks_A",
                                "fake", "repository")
    assert not achado.ok
    assert b.provedor.chamadas == 0, (
        "o provedor foi consultado antes de o escopo ser conferido")


# ===========================================================================
# 5. AUTORIDADE
# ===========================================================================

def test_reading_capabilities_do_not_grant_selection(tmp_path):
    """Ver o que existe nao da o direito de escolher o que o motor usa."""
    b = _bancada(tmp_path)
    so_le = _quem(b.workspace, Ability.CREDENTIAL_LIST)

    saida = b.service.select(so_le, b.workspace, [_repo("org/a")])
    assert not saida.accepted
    assert b.store.resources(b.workspace) == []


def test_operating_the_engine_does_not_grant_selection(tmp_path):
    """Quem pausa o motor numa emergencia nao ganha o direito de mudar o escopo."""
    b = _bancada(tmp_path)
    operador = _quem(b.workspace, *abilities_of("operator"))
    assert not b.service.select(operador, b.workspace, [_repo("org/a")]).accepted


def test_selecting_a_resource_grants_no_write_capability(tmp_path):
    """Escolher um repositorio nao autoriza escrever nele.

    As duas coisas moram em lugares diferentes de proposito: o que o motor pode
    TOCAR e a selecao; o que ele pode FAZER continua sendo a credencial.
    """
    b = _bancada(tmp_path)
    b.service.select(_dono(b.workspace), b.workspace, [_repo("org/a")])
    gravado = b.store.resources(b.workspace)[0]
    assert not hasattr(gravado, "capabilities"), (
        "a selecao passou a carregar capacidade: seria uma segunda autoridade")


def test_an_unauthenticated_request_selects_nothing(tmp_path):
    b = _bancada(tmp_path)
    anonimo = Principal(subject="anonimo", display="nao autenticado")
    saida = b.service.select(anonimo, b.workspace, [_repo("org/a")])
    assert not saida.accepted
    assert saida.refusal.value == "UNAUTHENTICATED"


def _policy_que_libera_tudo_menos(acao: str) -> PolicyEngine:
    """ALLOW geral, DENY na acao em questao.

    Escrita assim de proposito. Uma policy VAZIA tambem barraria -- mas barraria
    por ausencia de regra, e o teste passaria mesmo que o servico nunca
    consultasse a policy. Com ALLOW amplo, so o DENY especifico explica a
    recusa: e o mais-restritivo-vence que precisa estar funcionando.
    """
    return PolicyEngine.from_config([
        {"name": "tudo", "effect": "ALLOW", "match": {"action": "*"}},
        {"name": "menos_esta", "effect": "DENY", "match": {"action": acao}},
    ])


def test_policy_denial_stops_selection(tmp_path):
    b = _bancada(tmp_path)
    b.service.policy = _policy_que_libera_tudo_menos("workspace.resource.select")
    saida = b.service.select(_dono(b.workspace), b.workspace, [_repo("org/a")])
    assert not saida.accepted
    assert saida.refusal.value == "POLICY_DENIED"
    assert b.store.resources(b.workspace) == [],         "a policy recusou e mesmo assim ficou gravado"


def test_policy_denial_stops_discovery(tmp_path):
    b = _bancada(tmp_path)
    b.service.policy = _policy_que_libera_tudo_menos("fake.resource.discover")
    achado = b.service.discover(_dono(b.workspace), b.workspace, "fake",
                                "repository")
    assert not achado.ok
    assert achado.falha is Falha.POLICY_RECUSOU
    assert b.provedor.chamadas == 0, "o provedor foi consultado apesar do DENY"


def test_every_credential_capability_is_an_action_the_shipped_policy_knows():
    """Uma capacidade que a policy nunca ouviu falar nao existe na pratica.

    Este teste existe por causa de um defeito que passou pela suite inteira e
    pela varredura de mutacao: `repo.discover` foi criada como capacidade de
    credencial e NUNCA foi declarada na policy. Toda a descoberta contra um
    provedor real recusava com "nenhuma regra permite 'repo.discover'" -- e
    nenhum teste via, porque o provedor de teste nao resolve credencial.

    O guard e geral: para cada valor de `Use`, a policy enviada precisa
    permitir a acao de mesmo nome, e o teto de autonomia dela nao pode ser o
    maximo por desconhecimento.
    """
    from regente.core.credential import Use
    from regente.core.policy import (Action, AutonomyLevel, Effect,
                                     PolicyContext, required_level)

    mudos, altos = [], []
    for u in Use:
        d = POLICY.decide(PolicyContext(
            action=Action(kind=u.value, resource="x", environment="staging"),
            organization="o", client="c", workspace="w", agent="a",
            autonomy=AutonomyLevel.L4))
        if d.effect is not Effect.ALLOW:
            mudos.append(f"{u.value} -> {d.effect.value}")
        if u.value not in ("task.write", "repo.push", "repo.pr", "agent.run"):
            # As de leitura precisam de teto declarado: sem entrada em
            # REQUIRED_LEVEL o teto cai em L4, e mostrar uma lista passaria a
            # pedir aprovacao humana num workspace comum.
            if required_level(u.value) is not AutonomyLevel.L0:
                altos.append(f"{u.value} -> {required_level(u.value).name}")

    assert not mudos, ("capacidades que a policy enviada nao permite: "
                       + ", ".join(mudos))
    assert not altos, ("capacidades de leitura sem teto declarado: "
                       + ", ".join(altos))


def test_the_shipped_policy_lets_a_new_workspace_discover_and_choose(tmp_path):
    """O arquivo que a pessoa recebe precisa declarar as duas acoes.

    Sem isto o marco inteiro nasce morto na instalacao: a tela mostraria
    "policy recusou" em todo workspace novo, e ninguem entenderia por que --
    a policy nao nega nada explicitamente, ela apenas nunca ouviu falar da
    acao. Um DENY por omissao e o mais dificil de diagnosticar que existe.
    """
    b = _bancada(tmp_path)                      # usa a POLICY enviada
    assert b.service.discover(_dono(b.workspace), b.workspace, "fake",
                              "repository").ok
    assert b.service.select(_dono(b.workspace), b.workspace,
                            [_repo("org/a")]).accepted


# ===========================================================================
# 6. O QUE E SO NAVEGACAO NAO E ESCOLHA
# ===========================================================================

def test_a_resource_that_cannot_be_chosen_says_why(tmp_path):
    """Uma caixa desabilitada sem explicacao le-se como defeito.

    E os motivos sao diferentes entre si: um site existe para atravessar, e um
    board pode ser um recurso legitimo que ESTE adapter ainda nao sabe usar para
    recortar trabalho. Quem le precisa saber com qual dos dois esta lidando --
    o primeiro nunca vai mudar, o segundo e uma limitacao com prazo.
    """
    from regente.adapters.discovery import JiraDiscovery

    class Transporte:
        def get(self, rota, params=None):
            return {"values": [{"id": "7", "name": "Sprint Board"}]}

    inv = JiraDiscovery(transport=Transporte(), site="https://x").discover("board")
    board = inv.itens[0]
    assert not board.selectable
    assert board.note, "recusou a escolha e nao disse por que"

    b = _bancada(tmp_path, ProvedorFake(itens=[board], tipos=("board",)))
    achado = b.service.discover(_dono(b.workspace), b.workspace, "fake", "board")
    assert achado.itens[0].as_dict()["note"] == board.note, (
        "o motivo morreu antes de chegar a tela")


def test_a_container_that_exists_only_to_navigate_cannot_be_selected(tmp_path):
    """Selecionar uma organizacao inteira daria ao motor o que ela ganhar amanha."""
    b = _bancada(tmp_path)
    conta = Resource(ref=ResourceRef("fake", "account", "org"), name="org",
                     selectable=False)
    saida = b.service.select(_dono(b.workspace), b.workspace, [conta])
    assert not saida.accepted
    assert "navegar" in saida.reason


# ===========================================================================
# 7. HIERARQUIAS DIFERENTES
# ===========================================================================

def test_two_providers_with_different_trees_both_fit(tmp_path):
    """GitHub e conta->repo; Jira e projeto->board. A abstracao aguenta os dois.

    Este teste existe porque uma abstracao que so coubesse numa arvore
    pareceria certa com um provedor e quebraria no segundo -- e o objetivo do
    marco e o TERCEIRO caber sem redesenho.
    """
    conta = ResourceRef("github", "account", "acme")
    repo = Resource(ref=ResourceRef("github", "repository", "acme/api"),
                    name="acme/api", role=Kind.CODE, parent=conta)

    projeto = ResourceRef("jira", "project", "10001")
    board = Resource(ref=ResourceRef("jira", "board", "42"), name="Sprint",
                     role=Kind.TASK_SOURCE, parent=projeto)

    assert repo.parent.kind == "account"
    assert board.parent.kind == "project"
    # A mesma consulta funciona nos dois sem conhecer nenhum dos dois.
    for r in (repo, board):
        assert r.ref.scoped_to("wks_A").startswith("wks_A/")


def test_the_discovery_port_is_built_for_WHOEVER_asked(tmp_path):
    """Quem descobre e a pessoa que clicou, e nao o motor agindo por ela.

    Este teste existe por causa de um defeito real. A porta de descoberta era
    construida UMA VEZ na composicao, com a identidade de servico do motor.
    Resultado: conectar o GitHub funcionava e descobrir recusava, porque quem
    chegava ao broker nao era quem tinha autoridade.

    E o erro apontava para os dois lados. Se o motor TIVESSE concessao, uma
    pessoa sem `workspace.credential.use` faria o motor usar credencial por ela
    -- um emprestimo de autoridade, que e o contorno que tudo aqui recusa.
    """
    pedidos = []

    def fabrica(provedor, ator=None):
        pedidos.append((provedor, ator.subject if ator else None))
        return ProvedorFake(itens=[_repo("org/a")])

    b = _bancada(tmp_path)
    b.service.discovery_for = fabrica
    quem = _dono(b.workspace, sujeito="os:pessoa")
    b.service.discover(quem, b.workspace, "fake", "repository")

    assert pedidos == [("fake", "os:pessoa")], (
        "a porta foi construida sem saber quem estava pedindo")


def test_a_provider_that_discovers_nothing_says_so(tmp_path):
    b = _bancada(tmp_path, ProvedorFake(tipos=()))
    achado = b.service.discover(_dono(b.workspace), b.workspace, "fake",
                                "repository")
    assert achado.falha is Falha.NAO_SUPORTADO


def test_a_workspace_with_no_discovery_port_is_not_an_empty_workspace(tmp_path):
    b = _bancada(tmp_path)
    b.service.discovery_for = lambda _prov, _ator=None: None
    achado = b.service.discover(_dono(b.workspace), b.workspace, "outro",
                                "repository")
    assert achado.falha is Falha.NAO_SUPORTADO
    assert "nao descobre" in achado.detalhe


# ===========================================================================
# 8. IDENTIDADE DO RECURSO
# ===========================================================================

def test_a_resource_reference_never_travels_without_its_scope():
    ref = ResourceRef("github", "repository", "org/backend")
    assert ref.scoped_to("wks_A") != ref.scoped_to("wks_B")
    with pytest.raises(ValueError) as erro:
        ref.scoped_to("")
    assert "nao pertence a ninguem" in str(erro.value)


def test_an_incomplete_reference_is_refused_at_birth():
    for kwargs in ({"provider": "", "kind": "repository", "id": "x"},
                   {"provider": "github", "kind": "", "id": "x"},
                   {"provider": "github", "kind": "repository", "id": ""}):
        with pytest.raises(ValueError):
            ResourceRef(**kwargs)


def test_the_kind_is_part_of_the_identity():
    """Um projeto `SG` e um board `SG` sao coisas diferentes no mesmo provedor."""
    a = ResourceRef("jira", "project", "SG")
    b = ResourceRef("jira", "board", "SG")
    assert a != b
    assert a.scoped_to("wks") != b.scoped_to("wks")


# ===========================================================================
# 9. A TRILHA
# ===========================================================================

def test_selection_is_audited_and_carries_no_secret(tmp_path):
    b = _bancada(tmp_path)
    b.service.select(_dono(b.workspace), b.workspace, [_repo("org/a")])

    eventos = [e for e in b.store.events(b.workspace)
               if e.kind == "recurso_selecionado"]
    assert eventos, "escolher o que o motor usa nao deixou rastro"
    assert "os:1" in eventos[0].actor
    texto = f"{eventos[0].summary} {eventos[0].data}"
    for proibido in ("token", "secret", "ghp_", "Bearer"):
        assert proibido not in texto


def test_a_failed_discovery_is_audited_too(tmp_path):
    """O evento so quando da certo esconde o que se quer investigar depois."""
    b = _bancada(tmp_path, ProvedorFake(falha=Falha.PROVEDOR_INDISPONIVEL,
                                        detalhe="rede fora"))
    b.service.discover(_dono(b.workspace), b.workspace, "fake", "repository")

    eventos = [e for e in b.store.events(b.workspace)
               if e.kind == "recursos_descobertos"]
    assert eventos
    assert eventos[0].data["ok"] is False
    assert eventos[0].data["failure"] == "PROVEDOR_INDISPONIVEL"


def test_removal_is_audited(tmp_path):
    b = _bancada(tmp_path)
    b.service.select(_dono(b.workspace), b.workspace, [_repo("org/a")])
    b.service.unselect(_dono(b.workspace), b.workspace,
                       ResourceRef("fake", "repository", "org/a"))
    assert [e for e in b.store.events(b.workspace)
            if e.kind == "recurso_removido"]


# ===========================================================================
# 10. REFRESH
# ===========================================================================

def test_refreshing_keeps_a_selection_that_is_still_there(tmp_path):
    b = _bancada(tmp_path)
    b.service.select(_dono(b.workspace), b.workspace, [_repo("org/b")])
    for _ in range(3):
        b.service.discover(_dono(b.workspace), b.workspace, "fake", "repository")
    assert [s.ref.id for s in b.service.selected(_dono(b.workspace),
                                                 b.workspace)] == ["org/b"]


def test_selecting_the_same_resource_twice_does_not_duplicate_it(tmp_path):
    b = _bancada(tmp_path)
    b.service.select(_dono(b.workspace), b.workspace, [_repo("org/a")])
    b.service.select(_dono(b.workspace), b.workspace, [_repo("org/a")])
    assert len(b.store.resources(b.workspace)) == 1


def test_removing_something_that_was_never_selected_says_so(tmp_path):
    b = _bancada(tmp_path)
    saida = b.service.unselect(_dono(b.workspace), b.workspace,
                               ResourceRef("fake", "repository", "org/z"))
    assert not saida.accepted
    assert saida.refusal.value == "NOT_FOUND"


# ===========================================================================
# 11. A ABSTRACAO NAO CONHECE FORNECEDOR
# ===========================================================================


# ===========================================================================
# 8. A API
# ===========================================================================
#
# A tela nao alcanca o banco. Ela fala com estas rotas, e cada uma passa pelo
# mesmo servico que o terminal usa -- e por isso nao existe barreira que valha
# no terminal e nao valha aqui.

def _api(b, discovery=True):
    from regente.app.api import Api
    from regente.engine.readmodel import ReadModel

    return Api(read=ReadModel(store=b.store, clock=lambda: T0),
               resources=b.service,
               discovery_trees={"fake": ("account", "repository")}
                               if discovery else {})


def _pede(b, metodo, caminho, corpo=None, quem=None, discovery=True):
    return _api(b, discovery).resolve(
        metodo, caminho, {}, quem if quem is not None else _dono(b.workspace),
        corpo)


def test_the_screen_reads_what_the_workspace_chose(tmp_path):
    b = _bancada(tmp_path)
    b.service.select(_dono(b.workspace), b.workspace, [_repo("org/a")])
    r = _pede(b, "GET", f"/api/workspaces/{b.workspace}/resources")
    assert r.status == 200
    assert [x["ref"] for x in r.payload["resources"]] == ["fake:repository:org/a"]
    assert r.payload["editable"] is True


def test_the_screen_learns_the_tree_without_knowing_any_vendor(tmp_path):
    """A tela navega por niveis com nomes, e nao por um `if provider ===`."""
    b = _bancada(tmp_path)
    r = _pede(b, "GET", f"/api/workspaces/{b.workspace}/resources/providers")
    assert r.status == 200
    assert r.payload["providers"] == [
        {"provider": "fake", "tree": ["account", "repository"],
         "root": "account"}]


def test_discovering_is_a_post_because_it_costs_and_leaves_a_trail(tmp_path):
    """GET seria disparado por qualquer recarga, e a trilha viraria ruido."""
    b = _bancada(tmp_path)
    r = _pede(b, "GET", f"/api/workspaces/{b.workspace}/resources/discover")
    assert r.status == 404
    assert b.provedor.chamadas == 0


def test_a_provider_that_is_down_answers_200_carrying_the_failure(tmp_path):
    """A falha vem JUNTO do que ja estava escolhido, e nao no lugar dele.

    Um 503 aqui apagaria a lista, e a tela mostraria um workspace vazio quando
    o que houve foi o provedor fora do ar -- que e a leitura mais cara que esta
    tela pode induzir.
    """
    b = _bancada(tmp_path, ProvedorFake(falha=Falha.PROVEDOR_INDISPONIVEL,
                                        detalhe="timeout"))
    b.service.select(_dono(b.workspace), b.workspace, [_repo("org/a")])
    r = _pede(b, "POST", f"/api/workspaces/{b.workspace}/resources/discover",
              {"provider": "fake", "kind": "repository"})
    assert r.status == 200
    assert r.payload["ok"] is False
    assert r.payload["failure"] == "PROVEDOR_INDISPONIVEL"
    assert [x["ref"] for x in r.payload["resources"]] == ["fake:repository:org/a"]


def test_the_body_never_becomes_the_authority(tmp_path):
    """Ator e escopo nao vem da requisicao. Nunca vieram, e aqui tambem nao."""
    b = _bancada(tmp_path)
    for proibido in ("actor", "selected_by", "workspace_id", "client_id"):
        r = _pede(b, "POST", f"/api/workspaces/{b.workspace}/resources/select",
                  {"provider": "fake", "kind": "repository", "ids": ["org/a"],
                   proibido: "outro"})
        assert r.status == 400, proibido
        assert proibido in r.payload["detail"]


def test_the_api_cannot_choose_what_the_provider_never_showed(tmp_path):
    """O corpo da requisicao nao inventa recurso.

    Este teste existe porque a varredura de mutacao passou: trocar
    `select_refs` por `select` com recursos montados a partir do corpo nao
    quebrou nada. Com essa mutacao, `{"ids": ["org/inventado"], "selectable":
    true}` bastaria para colocar no workspace algo que ninguem confirmou -- e o
    proximo passo seria enviar `role` e fazer o motor tratar qualquer coisa como
    fonte de trabalho.
    """
    b = _bancada(tmp_path)
    r = _pede(b, "POST", f"/api/workspaces/{b.workspace}/resources/select",
              {"provider": "fake", "kind": "repository",
               "ids": ["org/inventado"], "selectable": True, "role": "code"})
    assert r.status == 404
    assert b.store.resources(b.workspace) == []

    # E o que so navega continua nao entrando, mesmo com o corpo insistindo.
    conta = Resource(ref=ResourceRef("fake", "account", "org"), name="org",
                     selectable=False)
    c = _bancada(tmp_path / "conta", ProvedorFake(itens=[conta],
                                                  tipos=("account",)))
    r = _pede(c, "POST", f"/api/workspaces/{c.workspace}/resources/select",
              {"provider": "fake", "kind": "account", "ids": ["org"],
               "selectable": True})
    assert r.status == 403
    assert c.store.resources(c.workspace) == []


def test_choosing_through_the_api_needs_the_same_ability(tmp_path):
    b = _bancada(tmp_path)
    operador = _quem(b.workspace, *abilities_of("operator"))
    r = _pede(b, "POST", f"/api/workspaces/{b.workspace}/resources/select",
              {"provider": "fake", "kind": "repository", "ids": ["org/a"]},
              quem=operador)
    assert r.status == 404, "confirmou a existencia do workspace a quem nao pode"
    assert b.store.resources(b.workspace) == []


def test_an_unauthenticated_request_chooses_nothing_through_the_api(tmp_path):
    b = _bancada(tmp_path)
    anonimo = Principal(subject="anonimo", display="nao autenticado")
    r = _pede(b, "POST", f"/api/workspaces/{b.workspace}/resources/select",
              {"provider": "fake", "kind": "repository", "ids": ["org/a"]},
              quem=anonimo)
    assert r.status in (401, 404)
    assert b.store.resources(b.workspace) == []


def test_the_api_can_remove_and_says_when_there_was_nothing(tmp_path):
    b = _bancada(tmp_path)
    b.service.select(_dono(b.workspace), b.workspace, [_repo("org/a")])
    rota = (f"/api/workspaces/{b.workspace}/resources/"
            "fake:repository:org%2Fa")

    r = _pede(b, "DELETE", rota)
    assert r.status == 200
    assert b.store.resources(b.workspace) == []

    # De novo: "removi" e "nao estava la" nao podem responder a mesma coisa,
    # senao quem remove o recurso errado nunca descobre.
    assert _pede(b, "DELETE", rota).status == 404


def test_a_reference_the_api_cannot_read_is_refused_not_guessed(tmp_path):
    b = _bancada(tmp_path)
    r = _pede(b, "DELETE",
              f"/api/workspaces/{b.workspace}/resources/isto-nao-e-uma-referencia")
    assert r.status == 400
    assert "provedor:tipo:id" in r.payload["detail"]


def test_a_composition_without_the_service_says_so_and_reads_empty(tmp_path):
    """Sem servico, a leitura responde vazio com aviso -- e nao um erro.

    Um workspace que nao administra integracoes por aqui continua sendo um
    workspace valido, e a tela precisa poder desenhar a pagina.
    """
    from regente.app.api import Api
    from regente.engine.readmodel import ReadModel

    b = _bancada(tmp_path)
    api = Api(read=ReadModel(store=b.store, clock=lambda: T0))
    leitura = api.resolve("GET", f"/api/workspaces/{b.workspace}/resources", {},
                          _dono(b.workspace))
    assert leitura.status == 200
    assert leitura.payload["editable"] is False

    escrita = api.resolve(
        "POST", f"/api/workspaces/{b.workspace}/resources/select", {},
        _dono(b.workspace), {"provider": "fake", "kind": "repository",
                             "ids": ["org/a"]})
    assert escrita.status == 403


def test_every_answer_actually_renders(tmp_path):
    """`resolve()` devolver o objeto certo nao prova que ele vira HTTP.

    Os outros testes leem `r.payload`, que e um dicionario Python -- e um
    dicionario com um `datetime` ou um `Enum` dentro passa por eles e explode
    no servidor, virando 500 sem nenhum teste vermelho. Este chama `rendered()`,
    que e o que o socket recebe.
    """
    b = _bancada(tmp_path)
    b.service.select(_dono(b.workspace), b.workspace, [_repo("org/a")])
    ws = b.workspace
    respostas = [
        _pede(b, "GET", f"/api/workspaces/{ws}/resources"),
        _pede(b, "GET", f"/api/workspaces/{ws}/resources/providers"),
        _pede(b, "POST", f"/api/workspaces/{ws}/resources/discover",
              {"provider": "fake", "kind": "repository"}),
        _pede(b, "POST", f"/api/workspaces/{ws}/resources/select",
              {"provider": "fake", "kind": "repository", "ids": ["org/b"]}),
        _pede(b, "DELETE", f"/api/workspaces/{ws}/resources/fake:repository:org%2Fb"),
    ]
    for r in respostas:
        corpo = r.rendered()
        assert corpo, "resposta vazia"
        json.loads(corpo.decode("utf-8"))


def test_the_api_never_answers_a_secret(tmp_path):
    """Nenhuma rota daqui carrega material. A ausencia e o desenho."""
    b = _bancada(tmp_path)
    b.service.select(_dono(b.workspace), b.workspace, [_repo("org/a")])
    corpo = json.dumps(
        _pede(b, "GET", f"/api/workspaces/{b.workspace}/resources").payload,
        default=str).lower()
    for palavra in ("secret", "token", "password", "senha", "material",
                    "authorization", "bearer"):
        assert palavra not in corpo, f"a resposta carregou '{palavra}'"


# ===========================================================================
# 7b. OS ENVELOPES DE FORNECEDOR
# ===========================================================================
#
# As secoes acima exercitam as barreiras com um provedor falso, o que prova o
# COMPORTAMENTO e nao a traducao. Estas duas provam a traducao: e nela que uma
# falha de leitura vira -- ou deixa de virar -- uma lista vazia.

class _Explode:
    """Um provider que so sabe levantar. Cada teste escolhe o que."""

    def __init__(self, erro):
        self.erro = erro

    def discover_repositories(self, filtro=None):
        raise self.erro


@pytest.mark.parametrize("erro, esperado", [
    (RuntimeError("boom"), Falha.PROVEDOR_INDISPONIVEL),
    (TimeoutError("demorou"), Falha.PROVEDOR_INDISPONIVEL),
    (ValueError("resposta torta"), Falha.PROVEDOR_INDISPONIVEL),
])
def test_no_provider_failure_ever_becomes_an_empty_account(erro, esperado):
    """Nenhuma excecao vira `[]`. Esta e a razao de o tipo existir.

    Um provedor fora do ar devolvendo lista vazia le-se como "sua conta
    esvaziou" -- e a reacao razoavel de quem le e remover a selecao que ainda
    estava certa. O dano vem de uma FALHA DE LEITURA tratada como um FATO.
    """
    from regente.adapters.discovery import GitHubDiscovery

    inv = GitHubDiscovery(repos=_Explode(erro), org="acme").discover("repository")
    assert not inv.ok
    assert inv.falha is esperado
    assert inv.itens == ()


def test_a_refusal_from_the_governed_path_keeps_its_own_word():
    """"O Regente recusou" e "o provedor recusou" mandam a lugares diferentes.

    Traduzir por TIPO de excecao, e nunca por texto: bastaria um provedor mudar
    a redacao para uma credencial revogada ser reportada como instabilidade, e a
    pessoa iria investigar a rede.
    """
    from regente.adapters.discovery import GitHubDiscovery
    from regente.ports.support import CredentialDenied

    for recusa, esperado in (("NO_CAPABILITY", Falha.SEM_CAPACIDADE),
                             ("EXPIRED", Falha.CREDENCIAL_EXPIRADA),
                             ("REVOKED", Falha.CREDENCIAL_REVOGADA),
                             ("NOT_FOUND", Falha.SEM_CREDENCIAL),
                             ("POLICY_DENIED", Falha.POLICY_RECUSOU)):
        inv = GitHubDiscovery(
            repos=_Explode(CredentialDenied(recusa, "sem material")),
            org="acme").discover("repository")
        assert inv.falha is esperado, recusa
        assert inv.itens == ()


def test_a_provider_that_cannot_discover_says_so_instead_of_reading():
    """Sem o metodo de DESCOBERTA nao ha descoberta.

    Cair para `list_repositories` funcionaria, e usaria a credencial de LEITURA
    para varrer a organizacao inteira -- a porta dos fundos que este marco
    existe para fechar.
    """
    from regente.adapters.discovery import GitHubDiscovery

    class SoLeitura:
        def list_repositories(self, filtro=None):
            raise AssertionError("a descoberta caiu na credencial de leitura")

    inv = GitHubDiscovery(repos=SoLeitura(), org="acme").discover("repository")
    assert inv.falha is Falha.NAO_SUPORTADO


def test_discovering_asks_for_a_different_credential_than_reading():
    """Listar e ler sao capacidades separadas, e o adapter pede a certa.

    Sem esta separacao, conectar um provedor concederia leitura de tudo o que a
    credencial alcanca. O teste olha a CAPACIDADE PEDIDA, e nao o resultado:
    e ela que o caminho governado avalia.
    """
    from regente.adapters.repos.github import GitHubRepos
    from regente.core.credential import Use

    import inspect

    # O padrao vem da assinatura REAL. Ler nao passa `use` -- ele conta com o
    # default -- e um teste que inventasse o proprio default deixaria de notar
    # se alguem trocasse o de producao por `repo.discover`.
    padrao = inspect.signature(GitHubRepos._cli).parameters["use"].default
    assert padrao is Use.REPO_READ, (
        f"o padrao de uma chamada ao provedor virou {padrao!r}")

    pedidas = []
    repos = GitHubRepos(org="acme")
    repos._cli = lambda args, json_esperado=True, use=padrao: (
        pedidas.append(use) or [])

    repos.list_repositories()
    repos.discover_repositories()
    assert pedidas == [Use.REPO_READ, Use.REPO_DISCOVER], (
        "descobrir e ler pediram a mesma capacidade")


def test_a_line_the_provider_sent_without_identity_does_not_sink_the_rest():
    """Uma linha sem id nao pode derrubar as outras trinta que serviam."""
    from regente.adapters.discovery import JiraDiscovery

    class Transporte:
        def get(self, rota, params=None):
            return {"values": [{"name": "sem id"},
                               {"id": "10001", "key": "SG", "name": "Silverguard"}]}

    inv = JiraDiscovery(transport=Transporte(), site="https://x").discover("project")
    assert inv.ok
    assert [r.ref.id for r in inv.itens] == ["10001"]


def test_two_providers_with_different_trees_are_both_real_adapters():
    """A prova de que a abstracao aguenta duas formas nao e um falso.

    GitHub e conta -> repositorio, com a folha selecionavel. Jira e projeto ->
    board, com a RAIZ selecionavel. Se a porta so coubesse numa das duas, a
    segunda teria de mentir sobre a propria arvore.
    """
    from regente.adapters.discovery import GitHubDiscovery, JiraDiscovery

    gh = GitHubDiscovery(repos=_Explode(RuntimeError()), org="acme")
    jira = JiraDiscovery(transport=None, site="https://x")
    assert gh.discovers() == ("account", "repository")
    assert jira.discovers() == ("project", "board")

    raiz_gh = gh.discover("account").itens[0]
    assert not raiz_gh.selectable, "selecionar a organizacao daria tudo de amanha"

def test_no_public_way_into_the_provider_skips_a_barrier():
    """Lido da ARVORE: nenhuma entrada publica alcanca o provedor sem guarda.

    A varredura de mutacao prova que as barreiras ESTAO funcionando hoje. Este
    guard prova outra coisa: que uma entrada NOVA nao pode aparecer sem uma.
    Sao perguntas diferentes, e a segunda e a que envelhece pior sem teste --
    um metodo acrescentado daqui a seis meses nao quebra nenhuma mutacao
    existente, porque nenhuma mutacao fala dele.

    "Guarda" aqui e uma destas: `_may` (capacidade + policy + escopo),
    `may_read` (escopo), ou delegar a outro metodo que ja tenha uma.
    """
    import ast
    from pathlib import Path as _P

    fonte = (_P(__file__).resolve().parents[1]
             / "regente" / "engine" / "resources.py").read_text(encoding="utf-8")
    arvore = ast.parse(fonte)

    servico = next(n for n in arvore.body
                   if isinstance(n, ast.ClassDef) and n.name == "ResourceService")
    metodos = {m.name: m for m in servico.body
               if isinstance(m, ast.FunctionDef)}

    #: O que caracteriza "alcancou o mundo ou o banco".
    PERIGO = {"save_resource", "drop_resource", "mark_resources_seen",
              "discovery_for", "_perguntar"}
    GUARDAS = ("_may", "may_read")

    desprotegidos = []
    for nome, metodo in metodos.items():
        if nome.startswith("_"):
            continue                       # privado: quem o chama e que responde
        corpo = ast.unparse(metodo)
        alcanca = any(p in corpo for p in PERIGO)
        if not alcanca:
            continue
        # Delegar a um irmao publico ja guardado tambem vale.
        delega = any(f"self.{outro}(" in corpo
                     for outro in metodos if outro != nome and
                     any(g in ast.unparse(metodos[outro]) for g in GUARDAS))
        if not any(g in corpo for g in GUARDAS) and not delega:
            desprotegidos.append(nome)

    assert not desprotegidos, (
        "entrada publica que alcanca provedor ou banco sem barreira: "
        + ", ".join(sorted(desprotegidos)))


def test_no_vendor_name_leaks_into_the_domain_or_the_engine():
    """Um `if provider == "github"` no dominio e a abstracao falhando.

    O criterio arquitetural do marco: `adapters/clickup/` deve custar um arquivo
    de adapter, e nao alteracoes espalhadas por dominio, motor e porta.

    O guard le a ARVORE, e nao o texto. A primeira versao filtrava linhas que
    "pareciam" comentario e acusou uma frase de docstring que citava o GitHub
    como exemplo -- exatamente o uso legitimo. Um guard que da alarme falso e
    desligado, e um guard desligado nao guarda nada.
    """
    import ast
    from pathlib import Path as _P

    VENDEDORES = ("github", "jira", "clickup", "gitlab", "linear", "azure",
                  "bitbucket", "trello", "asana", "monday")

    raiz = _P(__file__).resolve().parents[1] / "regente"
    for arquivo in (raiz / "core" / "resource.py",
                    raiz / "engine" / "resources.py",
                    raiz / "ports" / "discovery.py"):
        arvore = ast.parse(arquivo.read_text(encoding="utf-8"))

        # Docstring e comentario podem citar fornecedor -- e devem, quando
        # explicam por que a forma e essa. O que nao pode e o CODIGO decidir por
        # nome de fornecedor: um identificador, uma constante, um literal.
        # Docstrings sao identificadas por IDENTIDADE do no, e nao pelo texto:
        # `ast.get_docstring` devolve o texto ja limpo, que nunca e igual ao
        # literal cru -- comparar os dois deixaria toda docstring passar pelo
        # filtro como se fosse codigo.
        docstrings = set()
        for no in ast.walk(arvore):
            if isinstance(no, (ast.Module, ast.ClassDef, ast.FunctionDef,
                               ast.AsyncFunctionDef)) and no.body:
                primeiro = no.body[0]
                if (isinstance(primeiro, ast.Expr)
                        and isinstance(primeiro.value, ast.Constant)
                        and isinstance(primeiro.value.value, str)):
                    docstrings.add(id(primeiro.value))

        nomes: list[str] = []
        for no in ast.walk(arvore):
            if isinstance(no, ast.Name):
                nomes.append(no.id)
            elif isinstance(no, ast.Attribute):
                nomes.append(no.attr)
            elif isinstance(no, ast.arg):
                nomes.append(no.arg)
            elif isinstance(no, (ast.FunctionDef, ast.AsyncFunctionDef,
                                 ast.ClassDef)):
                nomes.append(no.name)
            elif (isinstance(no, ast.Constant) and isinstance(no.value, str)
                  and id(no) not in docstrings):
                nomes.append(no.value)

        for texto in nomes:
            baixo = texto.lower()
            for vendedor in VENDEDORES:
                assert vendedor not in baixo, (
                    f"{arquivo.name} conhece fornecedor no codigo: "
                    f"{vendedor!r} aparece em {texto!r}")
