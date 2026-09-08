# -*- coding: utf-8 -*-
"""Operacao continua: ligar, pausar, parar -- e o que pode dar errado calado.

Os defeitos que este arquivo procura sao os que uma suite verde esconde bem:
uma pausa que nao pausa, um processo morto que continua parecendo vivo, uma
regra de prioridade que some com trabalho, um status que ninguem mapeou virando
o vizinho mais conveniente.

A regra que organiza tudo aqui: **ausencia de evidencia nunca vira evidencia de
sucesso.** Intencao gravada sem processo nao e `RUNNING`; status nao mapeado nao
e `NOT_STARTED`; task inelegivel nao desaparece.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

import pytest

from regente.core.access import PrincipalRef, abilities_of
from regente.core.model import Workspace
from regente.core.operation import (DEFAULT_GRACE, Heartbeat, Intent, Operation,
                                    Phase, explain)
from regente.core.policy import PolicyEngine
from regente.core.principal import Principal
from regente.core.selection import (Effect, Match, Rule, Selectable, Selection,
                                    rules_from)
from regente.engine.loop import ContinuousLoop
from regente.engine.operation import OperationService
from regente.engine.store_sqlite import SqliteStore
from regente.ports.tasks import ExternalStatus, status_map_from

T0 = datetime(2026, 9, 11, 9, 0, tzinfo=timezone.utc)
ALICE = PrincipalRef("os-account", "S-1-5-21-1")

REGRAS = [{"name": "opera", "effect": "ALLOW",
           "match": {"action": ["workspace.engine.control"]}}]


@pytest.fixture
def bench(tmp_path):
    store = SqliteStore(tmp_path / "o.db", clock=lambda: T0)
    store.migrate()
    for wid, client in (("wks_a", "cli_a"), ("wks_b", "cli_b")):
        store.save_client(client, "org", wid)
        store.save_workspace(Workspace(id=wid, client_id=client, name="main"))
    yield store
    store.close()


def quem(workspace="wks_a", role="operator") -> Principal:
    return Principal(subject=ALICE.subject, display="alice", method="os-account",
                     provider="os-account", issuer="maquina", authenticated_at=T0,
                     abilities={workspace: abilities_of(role)})


def servico(store, rules=None, at=T0) -> OperationService:
    return OperationService(
        store=store,
        policy=PolicyEngine.from_config(REGRAS if rules is None else rules),
        clock=lambda: at, organization="org", client="Acme",
        workspace_name="main")


# ===========================================================================
# A FASE -- derivada, e nunca gravada
# ===========================================================================

def test_a_recorded_intent_without_a_process_is_never_running(bench):
    """O defeito mais caro deste marco seria este.

    Uma tela que mostrasse `RUNNING` porque alguem clicou em Iniciar estaria
    afirmando algo sobre o mundo com base num pedido. Quem confia nela deixaria
    o motor "trabalhando" a noite inteira sem nenhum processo vivo.
    """
    op = Operation("wks_a", Intent.RUNNING, changed_at=T0)

    assert op.phase(None, T0 + timedelta(seconds=10)) is Phase.STARTING
    assert op.phase(None, T0 + timedelta(minutes=5)) is Phase.DEGRADED
    assert op.phase(None, T0 + timedelta(minutes=5)).needs_attention

    vivo = Heartbeat(at=T0 + timedelta(minutes=5), pid=1, host="h")
    assert op.phase(vivo, T0 + timedelta(minutes=5)) is Phase.RUNNING


def test_a_stale_heartbeat_is_not_a_live_process(bench):
    """Um processo morto deixa o ultimo sinal para tras. Ele nao prova nada."""
    op = Operation("wks_a", Intent.RUNNING, changed_at=T0)
    velho = Heartbeat(at=T0, pid=1, host="h")

    dentro = T0 + DEFAULT_GRACE - timedelta(seconds=1)
    fora = T0 + DEFAULT_GRACE + timedelta(seconds=1)
    assert op.phase(velho, dentro) is Phase.RUNNING
    assert op.phase(velho, fora + timedelta(minutes=5)) is Phase.DEGRADED


def test_a_process_still_alive_after_stop_is_stopping_not_stopped(bench):
    """Parada pedida com processo terminando nao e "parado": e "parando".

    Dizer `STOPPED` ali faria alguem concluir que pode desligar a maquina.
    """
    op = Operation("wks_a", Intent.STOPPED, changed_at=T0)
    vivo = Heartbeat(at=T0, pid=1, host="h")
    assert op.phase(vivo, T0) is Phase.STOPPING
    assert op.phase(None, T0) is Phase.STOPPED


def test_every_phase_says_what_to_do(bench):
    """Estado sem acao vira enfeite. Cada fase explica o proximo passo."""
    for fase in Phase:
        frase = explain(fase, None)
        assert frase and len(frase) > 15, fase
    assert "regente run" in explain(Phase.DEGRADED, None)


# ===========================================================================
# A AUTORIDADE -- a tela nao tem uma paralela
# ===========================================================================

def test_without_the_ability_nothing_is_recorded(bench):
    sem = Principal(subject=ALICE.subject, display="alice", method="os-account",
                    provider="os-account", issuer="m", authenticated_at=T0)
    saida = servico(bench).set_intent(sem, "wks_a", Intent.RUNNING)

    assert not saida.accepted and saida.refusal == "NOT_FOUND"
    assert bench.operation("wks_a").intent is Intent.STOPPED, \
        "a intencao mudou apesar da recusa"


def test_deciding_escalations_is_not_operating_the_engine(bench):
    """Duas autoridades diferentes. `keeper` cuida de segredo e nao liga motor."""
    keeper = quem(role="keeper")
    saida = servico(bench).set_intent(keeper, "wks_a", Intent.RUNNING)
    assert not saida.accepted
    assert bench.operation("wks_a").intent is Intent.STOPPED


def test_policy_is_a_separate_barrier(bench):
    saida = servico(bench, rules=[{"name": "nada", "effect": "ALLOW",
                                   "match": {"action": ["outra.coisa"]}}]
                    ).set_intent(quem(), "wks_a", Intent.RUNNING)
    assert saida.refusal == "POLICY_DENIED"
    assert bench.operation("wks_a").intent is Intent.STOPPED


def test_one_workspace_cannot_operate_another(bench):
    saida = servico(bench).set_intent(quem(workspace="wks_a"), "wks_b",
                                      Intent.RUNNING)
    assert saida.refusal == "NOT_FOUND"
    assert bench.operation("wks_b").intent is Intent.STOPPED


def test_an_absurd_interval_is_refused_not_clamped(bench):
    """Zero vira laco quente que queima o teto diario em segundos."""
    for ruim in (0, 1, 100000):
        saida = servico(bench).set_intent(quem(), "wks_a", Intent.RUNNING,
                                          interval_seconds=ruim)
        assert not saida.accepted and saida.refusal == "INVALID", ruim


def test_every_change_is_audited_even_when_nothing_changes(bench):
    """"Pediram de novo" e um fato que a investigacao de um incidente quer ver."""
    s = servico(bench)
    s.set_intent(quem(), "wks_a", Intent.RUNNING)
    s.set_intent(quem(), "wks_a", Intent.RUNNING)

    trilha = [e for e in bench.events("wks_a", limit=20) if e.kind == "operacao"]
    assert len(trilha) == 2
    assert all(e.actor == ALICE.key for e in trilha)


# ===========================================================================
# O LACO -- o que ele obedece, e o que ele sobrevive
# ===========================================================================

def _laco(intents, tick, intervalo=0, **kw):
    """Um laco com intencoes roteirizadas. Nao dorme de verdade.

    `intervalo=0` por padrao para os testes que nao ligam para o sono. Quem
    exercita a espera passa um valor real -- com zero, `_wait` nao tem o que
    esperar e nao chega a chamar `sleep`, que e correto e nao prova nada.
    """
    passos = list(intents)

    def ler():
        return (passos.pop(0) if passos else Intent.STOPPED), intervalo

    batidas: list = []
    return ContinuousLoop(
        read_intent=ler, tick=tick,
        beat=lambda t, d: batidas.append((t, d)),
        stand_down=lambda: batidas.append(("desligou", "")),
        sleep=lambda s: None, **kw), batidas


def test_pause_stops_dispatching_without_killing_the_process(bench):
    """A propriedade central: pausado NAO despacha, e continua vivo.

    Um laco que continuasse despachando com a tela dizendo "pausado" seria a
    pior mentira que este marco poderia contar.
    """
    despachos = []
    laco, batidas = _laco(
        [Intent.RUNNING, Intent.PAUSED, Intent.PAUSED, Intent.RUNNING],
        lambda: despachos.append(1) or 1)
    laco.run(max_cycles=10)

    assert len(despachos) == 2, "despachou enquanto pausado"
    # E continuou batendo o ponto durante a pausa: vivo e visivel.
    pausados = [d for _, d in batidas if "PAUSED" in str(d)]
    assert len(pausados) == 2


def test_stop_is_obeyed_by_a_process_that_was_already_running(bench):
    laco, batidas = _laco([Intent.RUNNING, Intent.STOPPED], lambda: 1)
    rel = laco.run(max_cycles=10)

    assert rel.ticks == 1
    assert rel.stopped_because == "parada pedida"
    assert batidas[-1] == ("desligou", ""), "o sinal de vida nao foi apagado"


def test_the_heartbeat_is_cleared_even_when_the_loop_explodes(bench):
    """Sair sem apagar o sinal faz um processo morto parecer vivo."""
    def explode():
        raise KeyboardInterrupt("Ctrl+C no meio")

    laco, batidas = _laco([Intent.RUNNING], explode)
    with pytest.raises(KeyboardInterrupt):
        laco.run(max_cycles=3)
    assert ("desligou", "") in batidas


def test_a_failing_provider_does_not_kill_the_loop(bench):
    """Trinta segundos de rede ruim nao podem virar uma parada ate alguem ver."""
    n = {"i": 0}

    def as_vezes():
        n["i"] += 1
        if n["i"] <= 3:
            raise RuntimeError("provedor fora do ar")
        return 1

    laco, _ = _laco([Intent.RUNNING] * 6, as_vezes)
    rel = laco.run(max_cycles=6)

    assert rel.failures == 3
    assert rel.dispatched >= 1, "nunca voltou a trabalhar depois da falha"


def test_consecutive_failures_back_off(bench):
    """Falhar sempre nao pode virar consultar o provedor mil vezes por minuto."""
    esperas: list[float] = []
    laco, _ = _laco([Intent.RUNNING] * 5,
                    lambda: (_ for _ in ()).throw(RuntimeError("nao")),
                    intervalo=10)
    laco.sleep = lambda s: esperas.append(s)
    laco.run(max_cycles=5)

    assert esperas, "nao esperou entre tentativas"
    assert esperas[-1] >= esperas[0], "o intervalo nao cresceu com as falhas"


def test_an_unreadable_intent_neither_dispatches_nor_shuts_down(bench):
    """Sem saber a intencao, o laco nao pode assumir nenhuma das duas.

    Assumir RUNNING despacharia trabalho que ninguem pediu; assumir STOPPED
    desligaria o motor por causa de um erro de leitura.
    """
    despachos = []
    tentativas = {"n": 0}

    def ler():
        tentativas["n"] += 1
        if tentativas["n"] <= 2:
            raise RuntimeError("banco travado")
        return Intent.STOPPED, 0

    laco = ContinuousLoop(read_intent=ler,
                          tick=lambda: despachos.append(1) or 1,
                          beat=lambda t, d: None, stand_down=lambda: None,
                          sleep=lambda s: None)
    rel = laco.run(max_cycles=10)

    assert despachos == [], "despachou sem saber se devia"
    assert rel.failures == 2


def test_a_stop_signal_interrupts_the_sleep(bench):
    """Parar nao pode esperar o intervalo inteiro para ter efeito."""
    laco, _ = _laco([Intent.RUNNING] * 50, lambda: 1, intervalo=10)
    dormidas = {"n": 0}

    def dormir(s):
        dormidas["n"] += 1
        if dormidas["n"] == 1:
            laco.request_stop("teste")

    laco.sleep = dormir
    rel = laco.run(max_cycles=50)
    assert rel.ticks == 1
    assert dormidas["n"] <= 2, "continuou dormindo depois do pedido de parada"


# ===========================================================================
# STATUS DO BOARD -- a fronteira traduz, o motor nao adivinha
# ===========================================================================

def test_a_workspace_declares_what_its_board_statuses_mean():
    m = status_map_from({"available": ["Refinamento"], "andamento": ["Em Dev"],
                         "done": ["Pronto"]})
    assert m["REFINAMENTO"] is ExternalStatus.NOT_STARTED
    assert m["EM DEV"] is ExternalStatus.IN_PROGRESS
    assert m["PRONTO"] is ExternalStatus.COMPLETED


def test_a_misspelled_bucket_raises_instead_of_emptying_the_board():
    """Ignorar em silencio faria o board inteiro cair em UNKNOWN sem explicacao."""
    with pytest.raises(ValueError, match="nao e um estado"):
        status_map_from({"disponivell": ["To Do"]})


def test_an_unmapped_status_stays_unknown_and_is_never_coerced():
    """Um status novo significa que alguem mudou o processo.

    Coagi-lo para o vizinho mais conveniente faz o motor pegar trabalho que a
    equipe tirou da fila -- e ninguem descobre olhando a tela.
    """
    from regente.adapters.tasks.jira import JiraTasks

    a = JiraTasks(transport=None, status_overrides=status_map_from(
        {"available": ["Refinamento"]}))
    assert a.status_overrides.get("INVENTADO") is None
    assert ExternalStatus.UNKNOWN.available is False
    assert ExternalStatus.UNKNOWN.finished is False


# ===========================================================================
# SELECAO -- elegibilidade e prioridade sao perguntas diferentes
# ===========================================================================

def uma(titulo="t", **kw) -> Selectable:
    return Selectable(title=titulo, **kw)


def test_a_priority_rule_reorders_without_hiding_anything():
    s = rules_from([{"name": "faxina", "field": "title", "match": "contains",
                     "value": "FAXINA", "delta": -100}])
    ordem = s.order([("B-2", uma("comum"), 100),
                     ("A-1", uma("[FAXINA] limpa"), 100)])
    assert [k for k, _ in ordem] == ["A-1", "B-2"]
    # E a comum continua elegivel: despriorizar nao e esconder.
    assert s.evaluate(uma("comum")).eligible


def test_a_tie_is_broken_by_key_the_same_way_every_time():
    """Sem desempate estavel, o mesmo tick escolhe tasks diferentes a cada
    execucao e o motor fica indo e voltando sem terminar nada."""
    s = Selection()
    entradas = [("Z-9", uma(), 100), ("A-1", uma(), 100), ("M-5", uma(), 100)]
    for _ in range(5):
        assert [k for k, _ in s.order(list(entradas))] == ["A-1", "M-5", "Z-9"]


def test_the_boards_own_priority_survives_a_workspace_rule():
    """Com piso em zero, uma critica e uma comum encostariam empatadas -- a
    regra do workspace teria APAGADO a informacao do board."""
    s = rules_from([{"name": "faxina", "field": "title", "match": "contains",
                     "value": "FAXINA", "delta": -100}])
    ordem = s.order([("COMUM", uma("[FAXINA] a"), 100),
                     ("CRITICA", uma("[FAXINA] b"), 10)])
    assert [k for k, _ in ordem] == ["CRITICA", "COMUM"]


def test_exclusion_beats_requirement_because_the_strictest_wins():
    """Regras contraditorias: a mais restritiva vence, como na policy."""
    s = Selection((
        Rule("labels", Match.CONTAINS, "backend", Effect.REQUIRE, name="so back"),
        Rule("title", Match.CONTAINS, "LEGAL", Effect.EXCLUDE, name="nunca legal"),
    ))
    v = s.evaluate(uma("[LEGAL] risco", labels=["backend"]))
    assert not v.eligible and v.excluded_by == "nunca legal"


def test_no_rules_means_everything_is_eligible():
    """Ausencia de filtro nao filtra. O contrario esvaziaria o board por omissao."""
    v = Selection().evaluate(uma("qualquer"), base_priority=42)
    assert v.eligible and v.priority == 42


def test_a_rule_about_a_field_that_does_not_exist_is_refused_at_construction():
    """Um campo errado tem de doer ao escrever a regra, e nao virar `None` em
    silencio no meio de um tick de madrugada."""
    with pytest.raises(ValueError, match="nao existe"):
        Rule("sprint", Match.CONTAINS, "x", Effect.PRIORITY, delta=-1)


def test_a_priority_rule_that_changes_nothing_is_refused():
    with pytest.raises(ValueError, match="nao muda nada"):
        Rule("title", Match.CONTAINS, "x", Effect.PRIORITY, delta=0)


def test_an_unknown_comparison_names_the_available_ones():
    with pytest.raises(ValueError, match="Disponiveis"):
        rules_from([{"name": "r", "field": "title", "match": "parecido",
                     "value": "x"}])


def test_the_reason_for_every_position_is_reported():
    """Uma ordem que ninguem consegue explicar e uma ordem em que ninguem
    confia -- e a primeira pergunta e "por que essa primeiro?"."""
    s = rules_from([
        {"name": "faxina", "field": "title", "match": "contains",
         "value": "FAXINA", "delta": -100},
        {"name": "backend", "field": "labels", "match": "contains",
         "value": "backend", "delta": -10}])
    v = s.evaluate(uma("[FAXINA] x", labels=["backend"]))
    assert v.priority == -10
    assert any("faxina" in r for r in v.reasons)
    assert any("backend" in r for r in v.reasons), \
        "duas razoes para adiantar tem de somar, e as duas tem de aparecer"


def test_the_cycle_limit_bounds_the_loop_even_when_nothing_is_dispatched(bench):
    """`--ciclos N` tem de terminar, inclusive com o motor PAUSADO.

    Este defeito foi encontrado por um sweep de mutacao que TRAVOU por tres
    horas em vez de falhar. O limite contava ciclos de TRABALHO, e uma volta
    pausada nao produz trabalho nenhum -- entao `regente run --ciclos 3` com o
    motor pausado girava para sempre.

    Um limite que nao limita e pior que nenhum: quem o passou acredita que o
    processo termina.
    """
    for intent in (Intent.PAUSED, Intent.RUNNING):
        laco = ContinuousLoop(
            read_intent=lambda i=intent: (i, 0), tick=lambda: 1,
            beat=lambda t, d: None, stand_down=lambda: None,
            sleep=lambda s: None)
        rel = laco.run(max_cycles=3)
        assert rel.stopped_because == "limite de ciclos desta execucao", intent
    # E o caso que deu origem a tudo: sem o `break` de STOPPED, o laco ainda
    # assim termina pelo limite -- uma mutacao ali fica VERMELHA, nao pendurada.
    assert rel.ticks == 3


# ===========================================================================
# OS DOIS BURACOS QUE O SWEEP ACHOU
# ===========================================================================

def test_an_ineligible_task_is_never_dispatched_and_never_disappears(bench,
                                                                    tmp_path):
    """A regra de exclusao tem de valer no DESPACHO, e nao so na avaliacao.

    Encontrado por sweep: trocar o filtro do orquestrador por `list(prontas)`
    passava com a suite inteira verde. Eu havia testado `Selection.evaluate`
    isoladamente -- exclusao vence exigencia, empate estavel, piso negativo --
    e nunca que uma task inelegivel deixa de virar candidata. A regra
    funcionaria na tela e nao no despacho, que e o pior lugar para ela nao
    funcionar.

    E a outra metade: ela continua VISIVEL, como adiada, com o nome da regra.
    Trabalho que some sem explicacao e como um board perde tarefas sem ninguem
    perceber.
    """
    from regente.adapters.notify.console import Console
    from regente.adapters.tasks.filesystem import FilesystemTasks
    from regente.adapters.workspace.local import IsolatedDirectory
    from regente.core.model import Task
    from regente.core.risk import RiskEngine
    from regente.core.scheduling import Limits
    from regente.core.states import TaskState
    from regente.engine.gate import Gate
    from regente.engine.orchestrator import Orchestrator

    ws = bench.workspace("wks_a")
    (tmp_path / "tasks").mkdir()
    orq = Orchestrator(
        store=bench, workspace=ws,
        tasks_provider=FilesystemTasks(tmp_path / "tasks"),
        area_provider=IsolatedDirectory(tmp_path / "areas"),
        runner=None,
        gate=Gate(store=bench, policy=PolicyEngine.from_config([]),
                  risk=RiskEngine.from_config([])),
        risk=RiskEngine.from_config([]),
        limits=Limits(max_workers=4, max_dispatches_per_day=100),
        notificador=Console(journal=tmp_path / "j.log"))

    def uma_task(chave, elegivel, por=""):
        t = Task(id=f"tsk_{chave}", workspace_id="wks_a", project_id="p",
                 title=chave, state=TaskState.READY, priority=100,
                 data={"elegivel": elegivel, "excluida_por": por})
        bench.save_task(t)
        return t

    uma_task("PODE", True)
    uma_task("FORA", False, por="nunca legal")

    p = orq.plan()

    assert "tsk_PODE" in p.dispatch
    assert "tsk_FORA" not in p.dispatch, "task excluida por regra foi despachada"

    adiadas = {d.task_id: d.reason for d in p.deferred}
    assert "tsk_FORA" in adiadas, "a task excluida sumiu do plano sem explicacao"
    assert "nunca legal" in adiadas["tsk_FORA"], \
        "o plano nao diz QUAL regra excluiu"


def test_the_operation_route_checks_scope_before_it_reads_the_body(bench):
    """Escopo primeiro, corpo depois -- e o `404` nao depende do corpo.

    Encontrado por sweep, e a primeira versao deste teste estava ERRADA: eu dei
    capacidade nos dois workspaces para "isolar escopo de capacidade", sem
    perceber que `may_read` e verdadeiro por desenho quando ha capacidade ali
    ("quem recebeu uma capacidade precisa enxergar o workspace"). O escopo
    passava com razao, e o teste acusava o produto de um defeito que era meu.

    A diferenca que a checagem REALMENTE faz e de ordem: sem ela, um corpo
    invalido para um workspace fora do alcance responde `400` -- ou seja, a API
    comenta o corpo de um recurso que quem pergunta nao deveria nem saber que
    existe. Com ela, e `404` seja qual for o corpo.
    """
    from regente.app.api import Api
    from regente.engine.readmodel import ReadModel

    api = Api(read=ReadModel(store=bench, clock=lambda: T0),
              operations=servico(bench))

    de_fora = Principal(
        subject=ALICE.subject, display="alice", method="os-account",
        provider="os-account", issuer="m", authenticated_at=T0,
        workspaces=frozenset({"wks_a"}),
        abilities={"wks_a": abilities_of("operator")})

    # Fora do alcance, o corpo nem chega a ser lido: valido ou lixo, e 404.
    for corpo in ({"intent": "RUNNING"}, {"intent": "TURBO"}, {}, None):
        r = api.resolve("POST", "/api/workspaces/wks_b/operation/intent",
                        {}, de_fora, corpo)
        assert r.status == 404, f"corpo {corpo!r} vazou o estado do recurso: {r.status}"
    assert bench.operation("wks_b").intent is Intent.STOPPED

    # E dentro do alcance a rota funciona -- senao o 404 acima nao provaria nada.
    dentro = api.resolve("POST", "/api/workspaces/wks_a/operation/intent",
                         {}, de_fora, {"intent": "RUNNING"})
    assert dentro.status == 200
    assert bench.operation("wks_a").intent is Intent.RUNNING
