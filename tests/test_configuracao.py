# -*- coding: utf-8 -*-
"""Configurar pela tela -- sem que a tela ganhe autoridade propria.

O `regente.yaml` continua sendo a base. A tela grava uma SOBREPOSICAO, e a
configuracao efetiva e derivada das duas. O defeito que este arquivo procura
antes de todos os outros e o da procedencia: sem ela alguem edita o arquivo,
nada muda, e a conclusao razoavel e que o Regente esta quebrado.

Os outros sao os de sempre nesta base: a tela nao decide, nao escolhe o proprio
principal, nao alcanca outro tenant, e nao transforma ausencia em sucesso.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from regente.core.access import PrincipalRef, abilities_of
from regente.core.model import Workspace
from regente.core.policy import PolicyEngine
from regente.core.principal import Principal
from regente.core.settings import (OVERRIDABLE, Overlay, Source, describe,
                                   effective)
from regente.engine.settings import SettingsService, validate
from regente.engine.store_sqlite import SqliteStore

T0 = datetime(2026, 9, 11, 9, 0, tzinfo=timezone.utc)
ALICE = PrincipalRef("os-account", "S-1-5-21-1")

REGRAS = [{"name": "cfg", "effect": "ALLOW",
           "match": {"action": ["workspace.settings.write"]}}]


@pytest.fixture
def bench(tmp_path):
    store = SqliteStore(tmp_path / "c.db", clock=lambda: T0)
    store.migrate()
    for wid, client in (("wks_a", "cli_a"), ("wks_b", "cli_b")):
        store.save_client(client, "org", wid)
        store.save_workspace(Workspace(id=wid, client_id=client, name="main"))
    yield store
    store.close()


def quem(workspace="wks_a", role="admin") -> Principal:
    return Principal(subject=ALICE.subject, display="alice", method="os-account",
                     provider="os-account", issuer="maquina", authenticated_at=T0,
                     workspaces=frozenset({workspace}),
                     abilities={workspace: abilities_of(role)})


def servico(store, rules=None) -> SettingsService:
    return SettingsService(
        store=store,
        policy=PolicyEngine.from_config(REGRAS if rules is None else rules),
        clock=lambda: T0, organization="org", client="Acme",
        workspace_name="main")


# ===========================================================================
# PROCEDENCIA -- o defeito mais caro deste marco
# ===========================================================================

def test_the_screen_wins_and_says_so(bench):
    """Duas fontes sem procedencia e a receita para "editei e nada mudou"."""
    o = Overlay("wks_a", {"status_map": {"available": ["Refinamento"]}})
    campo = effective("status_map", {"available": ["To Do"]}, o)

    assert campo.source is Source.OVERLAY
    assert campo.conflicts, "a tela venceu o arquivo e ninguem foi avisado"
    assert campo.shadowed == {"available": ["To Do"]}
    frase = describe(campo, "status_map")
    assert "Editar o arquivo nao muda nada" in frase
    assert "remova-a" in frase


def test_without_an_overlay_the_file_is_the_source(bench):
    campo = effective("providers", {"tasks": {"name": "jira"}}, Overlay("wks_a"))
    assert campo.source is Source.FILE and not campo.overridden


def test_absence_is_named_never_blank(bench):
    campo = effective("selection", None, Overlay("wks_a"))
    assert campo.source is Source.ABSENT
    assert "nao esta configurado" in describe(campo, "selection")


def test_only_a_closed_list_can_be_overridden(bench):
    """Sobrepor qualquer chave viraria um segundo formato de configuracao --
    e o primeiro uso seria sobrepor `policies` pela tela."""
    with pytest.raises(ValueError, match="nao e sobreponivel"):
        effective("policies", "x", Overlay("wks_a"))
    assert "policies" not in OVERRIDABLE


# ===========================================================================
# AUTORIDADE -- a tela nao tem uma paralela
# ===========================================================================

def test_configuring_needs_its_own_capability(bench):
    """Operar o motor nao e apontar o motor.

    Quem pausa numa emergencia nao precisa, por tabela, do direito de apontar o
    Regente para outro board.
    """
    operador = quem(role="operator")
    saida = servico(bench).put(operador, "wks_a", "selection", [])

    assert not saida.accepted and saida.refusal == "NOT_FOUND"
    assert bench.settings("wks_a").values == {}


def test_policy_is_a_separate_barrier(bench):
    saida = servico(bench, rules=[{"name": "n", "effect": "ALLOW",
                                   "match": {"action": ["outra"]}}]
                    ).put(quem(), "wks_a", "selection", [])
    assert saida.refusal == "POLICY_DENIED"
    assert bench.settings("wks_a").values == {}


def test_one_tenant_cannot_configure_another(bench):
    saida = servico(bench).put(quem(workspace="wks_a"), "wks_b", "selection", [])
    assert saida.refusal == "NOT_FOUND"
    assert bench.settings("wks_b").values == {}


def test_a_tenant_cannot_read_anothers_overlay(bench):
    servico(bench).put(quem(), "wks_a", "selection", [])
    assert bench.settings("wks_a").values, "nada foi gravado em A"
    assert bench.settings("wks_b").values == {}, "B enxergou a configuracao de A"


def test_every_change_is_audited(bench):
    s = servico(bench)
    s.put(quem(), "wks_a", "selection", [])
    s.clear(quem(), "wks_a", "selection")

    trilha = [e for e in bench.events("wks_a", limit=20)
              if e.kind == "configuracao"]
    assert len(trilha) == 2
    assert all(e.actor == ALICE.key for e in trilha)


# ===========================================================================
# VALIDACAO -- recusada agora, e nao no proximo tick
# ===========================================================================

def test_an_invalid_status_bucket_is_refused_at_write_time(bench):
    """Gravar e quebrar depois faria o motor falhar longe de quem escreveu."""
    saida = servico(bench).put(quem(), "wks_a", "status_map",
                               {"disponivell": ["To Do"]})
    assert saida.refusal == "INVALID"
    assert "nao e um estado" in saida.reason
    assert bench.settings("wks_a").values == {}


def test_a_rule_about_a_field_that_does_not_exist_is_refused(bench):
    saida = servico(bench).put(quem(), "wks_a", "selection",
                               [{"name": "x", "field": "sprint",
                                 "match": "contains", "value": "1", "delta": -1}])
    assert saida.refusal == "INVALID"
    assert "sprint" in saida.reason and "Campos disponiveis" in saida.reason


def test_a_contradictory_rule_is_refused_with_the_reason(bench):
    """Uma regra de prioridade que nao muda nada e ruido que parece protecao."""
    saida = servico(bench).put(quem(), "wks_a", "selection",
                               [{"name": "vazia", "field": "title",
                                 "match": "contains", "value": "x", "delta": 0}])
    assert saida.refusal == "INVALID" and "nao muda nada" in saida.reason


def test_the_screen_cannot_inject_what_the_composition_owns(bench):
    """`credentials` e `observer` sao injetados pela composicao.

    Aceita-los na configuracao deixaria a tela entregar ao adapter uma porta de
    credencial escolhida por ela -- a autoridade paralela por outro caminho.
    """
    for proibido in ("credentials", "observer", "policies", "actor"):
        saida = servico(bench).put(
            quem(), "wks_a", "providers",
            {"tasks": {"name": "jira", proibido: "x"}})
        assert saida.refusal == "INVALID", proibido
        assert proibido in saida.reason


def test_validation_uses_the_same_code_the_engine_reads_with():
    """Uma segunda validacao divergiria da leitura, e a que diverge aceita o
    que quebra depois."""
    assert validate("status_map", {"available": ["To Do"]}) == ""
    assert validate("status_map", {"nope": []}) != ""
    assert validate("selection", []) == ""
    assert validate("providers", {}) != ""


# ===========================================================================
# REMOVER A SOBREPOSICAO
# ===========================================================================

def test_clearing_gives_the_file_back(bench):
    s = servico(bench)
    s.put(quem(), "wks_a", "selection", [])
    saida = s.clear(quem(), "wks_a", "selection")
    assert saida.accepted and "arquivo volta a valer" in saida.detail
    assert bench.settings("wks_a").values == {}


def test_clearing_something_that_was_never_set_says_so(bench):
    """"Removido" para algo que nao existia faria alguem achar que resolveu."""
    saida = servico(bench).clear(quem(), "wks_a", "status_map")
    assert not saida.accepted and saida.refusal == "NOT_FOUND"
    assert "ja era quem mandava" in saida.reason


def test_two_sessions_do_not_corrupt_each_other(bench):
    """Duas telas abertas escrevem chaves diferentes sem se atropelar.

    Uma linha por (workspace, chave) e o que garante isso -- com um documento
    unico, a segunda gravacao levaria junto o que a primeira acabou de escrever.
    """
    s = servico(bench)
    s.put(quem(), "wks_a", "selection", [])
    s.put(quem(), "wks_a", "status_map", {"done": ["DONE"]})

    guardado = bench.settings("wks_a").values
    assert set(guardado) == {"selection", "status_map"}


# ===========================================================================
# A COMPOSICAO -- a sobreposicao chega ao MOTOR
# ===========================================================================

def test_the_overlay_reaches_the_engine(tmp_path, monkeypatch):
    """Configurar sem efeito no tick seria uma tela decorativa."""
    from regente.app import config as appconfig
    from regente.app.container import apply_overlay, build
    from regente.cli import cmd_init

    monkeypatch.chdir(tmp_path)
    cmd_init(type("A", (), {"config": "regente.yaml", "force": False})())
    cfg = appconfig.load("regente.yaml")
    motor = build(cfg)
    ws = motor.workspace.id
    assert len(cfg.selection.rules) == 0

    motor.store.save_setting(ws, "selection", [
        {"name": "faxina", "field": "title", "match": "contains",
         "value": "FAXINA", "delta": -100}], "alice")
    efetiva = apply_overlay(appconfig.load("regente.yaml"), motor.store)
    motor.close()

    assert [r.name for r in efetiva.selection.rules] == ["faxina"]


def test_an_overlaid_provider_does_not_erase_the_others(tmp_path, monkeypatch):
    """Configurar UM provider pela tela nao pode remover os outros em silencio."""
    from regente.app import config as appconfig
    from regente.app.container import apply_overlay, build
    from regente.cli import cmd_init

    monkeypatch.chdir(tmp_path)
    cmd_init(type("A", (), {"config": "regente.yaml", "force": False})())
    motor = build(appconfig.load("regente.yaml"))
    ws = motor.workspace.id
    antes = set(appconfig.load("regente.yaml").providers)

    motor.store.save_setting(ws, "providers",
                             {"tasks": {"name": "jira", "site": "https://x"}},
                             "alice")
    efetiva = apply_overlay(appconfig.load("regente.yaml"), motor.store)
    motor.close()

    assert efetiva.providers["tasks"].name == "jira"
    assert antes <= set(efetiva.providers), \
        "sobrepor um provider apagou os que o arquivo declarava"


def test_a_corrupt_overlay_falls_back_to_the_file_instead_of_dying(
        tmp_path, monkeypatch):
    """Uma linha ruim no banco nao pode virar uma parada total.

    Ela ja foi validada ao ser gravada; chegar quebrada significa edicao por
    fora, e derrubar o motor por isso troca um problema pequeno por um grande.
    """
    from regente.app import config as appconfig
    from regente.app.container import apply_overlay, build
    from regente.cli import cmd_init

    monkeypatch.chdir(tmp_path)
    cmd_init(type("A", (), {"config": "regente.yaml", "force": False})())
    motor = build(appconfig.load("regente.yaml"))
    motor.store.save_setting(motor.workspace.id, "selection",
                             [{"field": "inexistente", "match": "contains",
                               "value": "x", "delta": -1}], "por fora")
    efetiva = apply_overlay(appconfig.load("regente.yaml"), motor.store)
    motor.close()
    assert len(efetiva.selection.rules) == 0


# ===========================================================================
# A TELA NAO E AUTORIDADE -- provas estruturais
# ===========================================================================

def test_the_ui_never_talks_to_the_database():
    """A tela chama a API. A API chama o servico. O servico chama o Core."""
    from pathlib import Path

    js = Path("regente/app/ui/app.js").read_text(encoding="utf-8")
    for proibido in ("sqlite", "SqliteStore", "store.", "SELECT ", "INSERT "):
        assert proibido not in js, f"a tela fala com o banco: {proibido}"


def test_the_ui_never_receives_secret_material():
    """Nao ha rota que devolva material, e a tela nao pede uma.

    Procura a FORMA -- ler um campo de segredo de um payload, ou buscar uma
    rota de segredo -- e nao a palavra. A primeira versao deste teste acusava a
    propria frase que explica que o Regente nao guarda material secreto; um
    guard que acusa a explicacao ensina a ignorar o guard.
    """
    import re
    from pathlib import Path

    js = Path("regente/app/ui/app.js").read_text(encoding="utf-8")
    formas = {
        r"\.\s*(secret|material|token|password)": "le um campo de segredo",
        r"""["'`][^"'`]*/(secret|material|reveal)[^"'`]*["'`]""":
            "busca uma rota de segredo",
    }
    culpados = [f"{motivo}: {m.group(0)[:40]}"
                for padrao, motivo in formas.items()
                for m in re.finditer(padrao, js)]
    assert not culpados, ("a tela alcanca material:\n  "
                          + "\n  ".join(culpados))

    # E a contraprova: a rota que devolveria material NAO existe na API.
    api = Path("regente/app/api.py").read_text(encoding="utf-8")
    assert "secret_ref" in api, "a API deixou de expor o ENDERECO"
    assert re.search(r'"secret"\s*:', api) is None, \
        "a API passou a devolver um campo `secret`"


def test_the_api_refuses_to_override_anything_outside_the_closed_list():
    """A lista fechada e a barreira; a rota nao inventa chave nova."""
    assert set(OVERRIDABLE) == {"providers", "status_map", "selection"}


# ===========================================================================
# O QUE O SWEEP ACHOU: eu provei a mao, e nao converti em teste
# ===========================================================================
#
# Seis mutacoes escaparam da primeira rodada, e nenhuma era ruido: eu havia
# exercitado as seis pelo navegador e pelo HTTP real, e parei ali. Prova manual
# nao protege ninguem do proximo commit.

def _bancada(tmp_path, monkeypatch):
    """Um workspace de verdade, com a API montada como a composicao monta."""
    from regente.app import config as appconfig
    from regente.app.api import Api
    from regente.app.container import build
    from regente.cli import cmd_init
    from regente.engine.readmodel import ReadModel

    monkeypatch.chdir(tmp_path)
    cmd_init(type("A", (), {"config": "regente.yaml", "force": False})())
    cfg = appconfig.load("regente.yaml")
    motor = build(cfg)
    locais = {"filesystem", "directory", "script", "console", "clone"}
    api = Api(read=ReadModel(store=motor.store, clock=lambda: T0),
              settings=servico(motor.store), config=cfg,
              needs_credential=lambda nome: nome not in locais)
    return motor, api


def _quem_pode(ws: str) -> Principal:
    return Principal(subject=ALICE.subject, display="alice", method="os-account",
                     provider="os-account", issuer="m", authenticated_at=T0,
                     workspaces=frozenset({ws}),
                     abilities={ws: abilities_of("admin")})


def test_building_the_engine_applies_the_overlay(tmp_path, monkeypatch):
    """`apply_overlay` existir nao basta: `build` tem de CHAMA-LO.

    O teste anterior chamava `apply_overlay` direto, entao remover a chamada da
    composicao passava verde -- a tela configuraria e o tick ignoraria.
    """
    from regente.app import config as appconfig
    from regente.app.container import build

    motor, _ = _bancada(tmp_path, monkeypatch)
    motor.store.save_setting(motor.workspace.id, "selection", [
        {"name": "faxina", "field": "title", "match": "contains",
         "value": "FAXINA", "delta": -100}], "alice")
    motor.close()

    outro = build(appconfig.load("regente.yaml"))
    nomes = [r.name for r in outro.orchestrator.selection.rules]
    outro.close()
    assert nomes == ["faxina"], \
        "a composicao nao aplicou a sobreposicao: a tela configura e o tick ignora"


def test_the_settings_route_checks_scope_before_the_body(tmp_path, monkeypatch):
    """Escopo antes do corpo. Um 404 que depende do corpo comenta um recurso
    que quem pergunta nao deveria saber que existe."""
    motor, api = _bancada(tmp_path, monkeypatch)
    ws = motor.workspace.id
    try:
        quem_ = _quem_pode(ws)
        for corpo in ({"value": []}, {"value": "lixo"}, {}, None):
            r = api.resolve("POST",
                            "/api/workspaces/wks_outro/settings/selection",
                            {}, quem_, corpo)
            assert r.status == 404, f"corpo {corpo!r} vazou o recurso: {r.status}"
        dentro = api.resolve("POST", f"/api/workspaces/{ws}/settings/selection",
                             {}, quem_, {"value": []})
        assert dentro.status == 200, "a rota nao funciona nem dentro do escopo"
    finally:
        motor.close()


def test_the_preview_uses_the_rules_of_now(tmp_path, monkeypatch):
    """Editar uma regra e nao ver nada mudar e a confusao que a pagina evita."""
    from regente.core.model import Task
    from regente.core.states import TaskState

    motor, api = _bancada(tmp_path, monkeypatch)
    ws = motor.workspace.id
    try:
        motor.store.save_task(Task(
            id="tsk_1", workspace_id=ws, project_id="p",
            title="[FAXINA] limpar", state=TaskState.READY, priority=100,
            data={"prioridade_origem": 100, "elegivel": True, "selecao": []}))

        antes = api.resolve("GET", f"/api/workspaces/{ws}/queue", {},
                            _quem_pode(ws)).payload["queue"]
        assert antes[0]["priority"] == 100

        api.resolve("POST", f"/api/workspaces/{ws}/settings/selection", {},
                    _quem_pode(ws), {"value": [
                        {"name": "faxina", "field": "title",
                         "match": "contains", "value": "FAXINA",
                         "delta": -100}]})

        depois = api.resolve("GET", f"/api/workspaces/{ws}/queue", {},
                             _quem_pode(ws)).payload["queue"]
        assert depois[0]["priority"] == 0, \
            "a previa mostrou o veredito do ultimo tick, e nao a regra de agora"
        assert depois[0]["why"], "a previa nao explica a ordem"
        assert depois[0]["applied"] is False, \
            "a previa nao avisa que o motor ainda nao reavaliou"
    finally:
        motor.close()


def test_a_configured_provider_without_a_credential_is_never_ready(
        tmp_path, monkeypatch):
    """"Conectado" por ter configuracao manda procurar no lugar errado."""
    motor, api = _bancada(tmp_path, monkeypatch)
    ws = motor.workspace.id
    try:
        api.resolve("POST", f"/api/workspaces/{ws}/settings/providers", {},
                    _quem_pode(ws),
                    {"value": {"tasks": {"name": "jira", "site": "https://x"}}})
        conexoes = api.resolve("GET", f"/api/workspaces/{ws}/connections", {},
                               _quem_pode(ws)).payload["connections"]
        tasks = next(c for c in conexoes if c["role"] == "tasks")
        assert tasks["adapter"] == "jira"
        assert tasks["needs_credential"] is True
        assert tasks["state"] == "SEM_CREDENCIAL", \
            f"provider sem credencial apareceu como {tasks['state']}"
    finally:
        motor.close()


def test_a_local_adapter_is_never_asked_for_a_credential(tmp_path, monkeypatch):
    """O erro invertido: cobrar credencial de quem nao alcanca nada de fora."""
    motor, api = _bancada(tmp_path, monkeypatch)
    ws = motor.workspace.id
    try:
        conexoes = api.resolve("GET", f"/api/workspaces/{ws}/connections", {},
                               _quem_pode(ws)).payload["connections"]
        tasks = next(c for c in conexoes if c["role"] == "tasks")
        assert tasks["adapter"] == "filesystem"
        assert tasks["needs_credential"] is False
        assert tasks["state"] == "PRONTO"
    finally:
        motor.close()


def test_a_rule_survives_the_next_tick(tmp_path, monkeypatch):
    """O defeito: `_refresh` reescrevia a prioridade com a da origem.

    A regra valia na descoberta e era desfeita no ciclo seguinte. A ordem
    voltava sozinha, sem nada nos eventos, e a unica pista era a prioridade nao
    bater com o motivo gravado ao lado dela.
    """
    from pathlib import Path

    from regente.app import config as appconfig
    from regente.app.container import build

    motor, _ = _bancada(tmp_path, monkeypatch)
    motor.store.save_setting(motor.workspace.id, "selection", [
        {"name": "faxina", "field": "title", "match": "contains",
         "value": "FAXINA", "delta": -100}], "alice")
    motor.close()
    Path("tasks/SG-1.yaml").write_text(
        'key: SG-1\ntitle: "[FAXINA] limpar"\nstatus: TO DO\n',
        encoding="utf-8")

    vistos = []
    for _ in range(3):                 # baseline, descoberta, refresh
        m = build(appconfig.load("regente.yaml"))
        m.orchestrator.tick()
        achadas = [x for x in m.store.tasks(m.workspace.id) if x.key == "SG-1"]
        if achadas:
            vistos.append((achadas[0].priority,
                           achadas[0].data.get("prioridade_origem")))
        m.close()

    assert vistos, "a task nunca foi descoberta"
    assert all(p == 0 for p, _ in vistos), \
        f"a regra foi desfeita entre ciclos: {vistos}"
    assert all(o == 100 for _, o in vistos), \
        "a prioridade da origem nao foi guardada"


def test_the_deltas_do_not_compound_across_ticks(tmp_path, monkeypatch):
    """Reavaliar sobre a prioridade JA ajustada afundaria a task a cada ciclo."""
    from pathlib import Path

    from regente.app import config as appconfig
    from regente.app.container import build

    motor, _ = _bancada(tmp_path, monkeypatch)
    motor.store.save_setting(motor.workspace.id, "selection", [
        {"name": "faxina", "field": "title", "match": "contains",
         "value": "FAXINA", "delta": -50}], "alice")
    motor.close()
    Path("tasks/SG-9.yaml").write_text(
        'key: SG-9\ntitle: "[FAXINA] x"\nstatus: TO DO\n', encoding="utf-8")

    vistos = []
    for _ in range(4):
        m = build(appconfig.load("regente.yaml"))
        m.orchestrator.tick()
        achadas = [x for x in m.store.tasks(m.workspace.id) if x.key == "SG-9"]
        if achadas:
            vistos.append(achadas[0].priority)
        m.close()

    assert vistos and len(set(vistos)) == 1, \
        f"a prioridade mudou a cada ciclo -- os deltas se compuseram: {vistos}"
    assert vistos[0] == 50


def test_the_preview_never_compounds_the_delta_on_an_adjusted_priority(
        tmp_path, monkeypatch):
    """A previa reavalia a partir da prioridade DA ORIGEM, nunca da ajustada.

    O teste anterior da previa criava a task com `priority` e
    `prioridade_origem` IGUAIS -- e nesse caso ler uma ou outra da no mesmo, e
    a mutacao que troca as duas passava despercebida.

    Aqui elas sao diferentes de proposito: a task ja esta com a prioridade
    ajustada de um ciclo anterior. Reavaliar sobre ela somaria o delta de novo,
    e a task afundaria um pouco mais a cada leitura da tela -- ate encostar no
    piso e trocar de posicao com quem nunca casou regra nenhuma.
    """
    from regente.core.model import Task
    from regente.core.states import TaskState

    motor, api = _bancada(tmp_path, monkeypatch)
    ws = motor.workspace.id
    try:
        api.resolve("POST", f"/api/workspaces/{ws}/settings/selection", {},
                    _quem_pode(ws), {"value": [
                        {"name": "faxina", "field": "title",
                         "match": "contains", "value": "FAXINA",
                         "delta": -100}]})

        # Ja ajustada num ciclo anterior: 100 (origem) - 100 (regra) = 0.
        motor.store.save_task(Task(
            id="tsk_ja", workspace_id=ws, project_id="p",
            title="[FAXINA] ja avaliada", state=TaskState.READY, priority=0,
            data={"prioridade_origem": 100, "elegivel": True,
                  "selecao": ["faxina: -100"]}))

        fila = api.resolve("GET", f"/api/workspaces/{ws}/queue", {},
                           _quem_pode(ws)).payload["queue"]
        linha = next(t for t in fila if t["key"] == "tsk_ja")

        assert linha["priority"] == 0, (
            f"a previa somou o delta de novo sobre a prioridade ja ajustada: "
            f"{linha['priority']}")
        assert linha["applied"] is True, \
            "a previa deveria concordar com o motor neste caso"
    finally:
        motor.close()
