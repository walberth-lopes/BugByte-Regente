# -*- coding: utf-8 -*-
"""Superficie de linha de comando.

A tela responde quatro perguntas, nessa ordem de importancia: o que precisa de
mim, o que esta acontecendo, o que terminou, e ha algum problema. Complexidade
interna -- lease, run, grafo, policy -- so aparece quando alguem pede.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .app import container
from .app.config import Config, load
from .core.states import TaskState
from .app.container import _stable_id
from .core import ids, redaction
from .engine import chain, escalation, shadow
from .ports import AdapterError
from .ports.support import CredentialDenied
from .engine.store_sqlite import SqliteStore

DEFAULT_CONFIG_FILE = "regente.yaml"


def _force_utf8() -> None:
    # Sem isto, um titulo com acento derruba o comando no console do Windows.
    for fluxo in (sys.stdout, sys.stderr):
        try:
            fluxo.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


#: Codigos de saida. Um script que orquestra o Regente precisa distinguir
#: "voce nao tem autoridade" de "o arquivo esta errado" de "nao deu para
#: perguntar" -- e um unico `2` para tudo obriga quem chama a ler texto e
#: adivinhar. Os nomes sao os do motor: a CLI nao inventa vocabulario proprio.
EXIT_OK = 0
EXIT_REFUSED = 1            # o motor decidiu nao; a decisao esta impressa
EXIT_INVALID_ARGUMENT = 2   # tambem o que o argparse usa; mantido de proposito
EXIT_NOT_FOUND = 3
EXIT_POLICY_DENIED = 4
EXIT_AUTHENTICATION = 5
EXIT_CREDENTIAL = 6         # ausente, expirada ou revogada
EXIT_CONFLICT = 7
EXIT_BLOCKED = 8            # nao deu para perguntar; ausencia de resposta
EXIT_UNKNOWN = 9

#: Recusa do motor -> codigo. A CLI TRADUZ; ela nao classifica. Se o motor
#: ganhar uma recusa nova e ela nao estiver aqui, cai em `EXIT_UNKNOWN`, que e
#: a resposta honesta -- nunca `0`.
EXIT_POR_RECUSA = {
    "NOT_FOUND": EXIT_NOT_FOUND,
    "POLICY_DENIED": EXIT_POLICY_DENIED,
    "UNAUTHENTICATED": EXIT_AUTHENTICATION,
    "FORBIDDEN": EXIT_POLICY_DENIED,
    "INVALID": EXIT_INVALID_ARGUMENT,
    "CONFLICT": EXIT_CONFLICT,
    "EXPIRED": EXIT_CREDENTIAL,
    "REVOKED": EXIT_CREDENTIAL,
    "NO_CAPABILITY": EXIT_CREDENTIAL,
    "SOURCE_UNAVAILABLE": EXIT_BLOCKED,
}


def _exit_for(refusal: object) -> int:
    """O codigo desta recusa. Desconhecida vira UNKNOWN, nunca sucesso."""
    return EXIT_POR_RECUSA.get(str(getattr(refusal, "value", refusal) or ""),
                               EXIT_UNKNOWN)


def _load_config(args) -> Config:
    return load(args.config)


# ---- comandos ------------------------------------------------------------

#: O que um nome pode ter.
#:
#: Ele nao e enfeite: junto com a organizacao e o cliente, ele DERIVA o id do
#: workspace. Um espaco a mais no fim produziria um id diferente do que a pessoa
#: pensa ter digitado -- e um workspace novo, vazio, sem nenhuma pista.
_NOME_VALIDO = __import__("re").compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,48}$")


def _pergunte(rotulo: str, padrao: str) -> str:
    """Pergunta um nome, e aceita o padrao quando ninguem responde.

    Entrada fechada levanta `EOFError`, e ai o padrao vale -- e por isso isto
    nunca fica esperando uma tecla que nao vem.
    """
    while True:
        try:
            dito = input(f"  {rotulo} [{padrao}]: ").strip()
        except (EOFError, OSError):
            print(padrao)
            return padrao
        escolhido = dito or padrao
        if _NOME_VALIDO.match(escolhido):
            return escolhido
        print("    use letras, numeros, espaco, ponto, hifen ou sublinhado "
              "(ate 49 caracteres)")


def _confirme(pergunta: str) -> bool:
    """Sim ou nao, com NAO como resposta de quem nao respondeu.

    Abrir a Mission Control prende o terminal ate alguem interromper. Isso e o
    que a pessoa quer quando pediu; e uma armadilha quando ninguem pediu. Um
    padrao negativo torna impossivel um script ficar preso servindo HTTP -- que
    foi exatamente o que travou a suite inteira quando a tela abria sozinha.
    """
    try:
        return input(f"  {pergunta} [s/N]: ").strip().lower() in ("s", "sim", "y")
    except (EOFError, OSError):
        print("n")
        return False


def cmd_init(args) -> int:
    """Cria uma pasta de trabalho que REALMENTE sobe -- e ja utilizavel.

    Ate o marco 6 este comando escrevia um `regente.yaml` apontando para
    `../policies/default.yaml` -- um caminho que so resolve dentro da arvore do
    codigo-fonte. Quem seguia o tutorial recebia, no comando seguinte, um
    `ValueError` cru na cara.

    Depois disso ele passou a escrever um arquivo que sobe, e sobrou o atrito
    seguinte: o arquivo nascia com `my-org / first-client / main`, e quem
    quisesse o proprio nome tinha de editar YAML -- ANTES do primeiro ciclo,
    porque o id do workspace e derivado dos tres nomes e renomear depois cria
    outro workspace, vazio, sem aviso. Depois disso ainda faltavam dois
    comandos de acesso para a tela sair do lugar.

    Agora o comando PERGUNTA os nomes, cria o workspace, concede o acesso e
    abre a Mission Control. O que ele NAO faz e inventar autoridade: as duas
    concessoes passam pelo `AccessService`, com a mesma auditoria de sempre.
    """
    import sys

    destination = Path(args.config)
    recursos = Path(__file__).parent / "resources"
    if destination.exists() and not args.force:
        print(f"{destination} ja existe. Use --force para sobrescrever.")
        return EXIT_CONFLICT

    # QUEM DECIDE SE HA PERGUNTAS E UM ARGUMENTO, e nao o ambiente.
    #
    # A versao anterior olhava `sys.stdin.isatty()`, e isso se mostrou uma base
    # ruim: no Git Bash do Windows, `< /dev/null` responde que E um terminal, e
    # um cano responde que NAO e -- entao ora o comando travava esperando uma
    # tecla, ora descartava em silencio respostas que alguem tinha enviado.
    #
    # Agora e explicito. Sem `--perguntar`, o comando nunca le a entrada, nunca
    # bloqueia, e IMPRIME os nomes que usou.
    perguntar = args.perguntar and not (
        args.organizacao or args.cliente or args.workspace)

    if perguntar:
        print()
        print("  Regente -- configuracao inicial")
        print("  Enter aceita o valor entre colchetes.")
        print()
        organizacao = _pergunte("Nome da organizacao", "my-org")
        # O cliente costuma ser a propria organizacao em quem esta comecando: o
        # padrao segue o que a pessoa acabou de dizer, e nao um nome de exemplo.
        cliente = _pergunte("Nome do cliente", organizacao)
        workspace = _pergunte("Nome do workspace", "main")
        print()
    else:
        organizacao = args.organizacao or "my-org"
        cliente = args.cliente or organizacao
        workspace = args.workspace or "main"
        # Dito em voz alta. Um comando que escolhe nomes por voce e nao conta
        # produz um workspace com um nome que ninguem reconhece -- e renomear
        # depois cria OUTRO workspace, porque o id e derivado dos tres nomes.
        if not (args.organizacao or args.cliente or args.workspace):
            print(f"usando os nomes padrao: {organizacao} / {cliente} / "
                  f"{workspace}")
            print("  (para escolher: regente init --perguntar, ou "
                  "--organizacao X --cliente Y --workspace Z)")

    modelo = (recursos / "regente.yaml.example").read_text(encoding="utf-8")
    for chave, valor in (("organization", organizacao), ("client", cliente),
                         ("workspace", workspace)):
        modelo = __import__("re").sub(
            rf"^{chave}: .*$", f"{chave}: {valor}", modelo, count=1,
            flags=__import__("re").M)
    destination.write_text(modelo, encoding="utf-8")

    policies = destination.parent / "policies.yaml"
    if not policies.exists() or args.force:
        policies.write_text(
            (recursos / "policies.yaml.example").read_text(encoding="utf-8"),
            encoding="utf-8")
    tasks = destination.parent / "tasks"
    tasks.mkdir(exist_ok=True)

    print(f"criado {destination}")
    print(f"criado {policies} -- o que o motor pode fazer, e o que nao pode")
    print(f"criado {tasks}/ -- descreva trabalho em YAML aqui")

    concedido = _abrir_o_workspace(args, cliente, workspace)
    if concedido != EXIT_OK:
        return concedido

    # ABRIR A TELA E UMA RESPOSTA, e nao um palpite sobre o ambiente.
    #
    # `cmd_ui` sobe um servidor e NAO RETORNA. Isso e o que a pessoa quer quando
    # pediu, e uma armadilha quando ninguem pediu: a suite inteira parou uma vez
    # num `regente init` de subprocesso servindo HTTP na porta 8787, sem erro e
    # sem pista.
    #
    # `--ui` abre; `--perguntar` pergunta; qualquer outro caminho so diz o
    # proximo comando. Nao ha combinacao em que um script fique preso.
    abrir = args.ui or (perguntar and _confirme("Abrir a Mission Control agora?"))
    if not abrir:
        print()
        print("proximo: regente ui")
        return EXIT_OK

    print()
    print("abrindo a Mission Control...")
    return cmd_ui(args)


def _abrir_o_workspace(args, cliente: str, workspace: str) -> int:
    """Cria o workspace e concede o acesso a quem esta rodando o comando.

    DUAS concessoes, e nao uma, porque sao duas identidades. O terminal
    autentica pela conta do sistema operacional; a Mission Control desta versao
    autentica por um token local nomeado pelo cliente. Conceder so a primeira
    era o que fazia a tela abrir autenticada e sem poder fazer nada -- e o que
    obrigava a um segundo comando que ninguem tinha como adivinhar.

    Isto nao afrouxa nada. As duas passam pelo `AccessService`, com policy e
    auditoria; e quem roda `regente init` ja controla o banco e o arquivo de
    configuracao, entao nao ha autoridade nova sendo criada aqui -- so a que
    existia deixando de ser trabalho manual.
    """
    from .core.access import PrincipalRef

    cfg = _load_config(args)
    motor = container.build(cfg)
    try:
        service = motor.access()
        eu = motor.terminal_principal()
        ws = motor.workspace.id
        print(f"criado workspace {cliente} / {workspace}")

        inicial = service.bootstrap(eu, ws, note="regente init")
        if not inicial.accepted:
            # CONFLICT aqui significa que a pasta ja tinha dono. Nao e erro do
            # comando, e refazer o `init` nao deve tirar acesso de ninguem.
            if inicial.refusal.value == "CONFLICT":
                print("este workspace ja tem dono; nada foi alterado no acesso")
                return EXIT_OK
            print(f"{inicial.refusal.value}: {inicial.reason}", file=sys.stderr)
            return _exit_for(inicial.refusal)
        print(f"voce e o dono: {inicial.target}")

        # RELER a identidade, e nao reusar a de cima.
        #
        # As capacidades de um principal sao um RETRATO do momento em que ele
        # foi lido -- e o de cima foi lido antes da concessao existir. Reusa-lo
        # fazia a segunda concessao ser recusada com `NOT_FOUND`, que e como o
        # motor diz "voce nao pode conceder aqui" sem confirmar o que existe.
        eu = motor.terminal_principal()

        # A identidade que a Mission Control usa nesta maquina.
        from dataclasses import fields

        from .adapters.identity.dev_token import DevTokenIdentity

        # `DevTokenIdentity.name` num dataclass com `slots` devolve o descritor
        # do slot, e nao o valor -- foi o que imprimiu
        # `<member 'name' of 'DevTokenIdentity' objects>` na cara de quem rodou.
        # O padrao do campo e o valor de verdade, e le-lo nao constroi nada.
        provedor = next(c.default for c in fields(DevTokenIdentity)
                        if c.name == "name")
        da_tela = f"{provedor}:{cfg.client}"
        segunda = service.grant(eu, ws, PrincipalRef.parse(da_tela), "owner",
                                note="Mission Control desta maquina")
        if segunda.accepted:
            print(f"a Mission Control tambem: {da_tela}")
        else:
            # Nao e fatal: o workspace ja e utilizavel pelo terminal, e dizer o
            # comando exato e melhor que falhar tudo por causa da segunda.
            print(f"a tela ainda nao tem acesso ({segunda.refusal.value}). "
                  f"Conceda com: regente access conceder {da_tela} --papel owner")
        return EXIT_OK
    finally:
        motor.close()


def cmd_doctor(args) -> int:
    cfg = _load_config(args)
    problemas = 0
    for name, ok, detalhe in container.diagnose(cfg):
        mark = "ok  " if ok else "FALHA"
        print(f"  {mark}  {name:<28} {detalhe}")
        problemas += 0 if ok else 1
    print()
    print("tudo pronto" if not problemas else f"{problemas} problema(s) -- o motor nao vai rodar assim")
    return 0 if not problemas else 2


def cmd_tick(args) -> int:
    cfg = _load_config(args)
    motor = container.build(cfg)
    try:
        rel = motor.orchestrator.tick()
        print(rel.summary())
        if rel.dispatched:
            print("  despachadas:", ", ".join(rel.dispatched))
        if rel.completed:
            print("  concluidas: ", ", ".join(rel.completed))
        if rel.recovered:
            print("  recuperadas:", ", ".join(rel.recovered))
        if rel.cycles:
            print("  em ciclo:   ", ", ".join(rel.cycles))
        if args.verbose and rel.deferred:
            for key, reason in rel.deferred:
                print(f"  adiada {key}: {reason}")
        for e in rel.errors:
            print("  error:", e)
        if rel.escalated:
            print()
            print(f"  {len(rel.escalated)} precisam de voce: regente needs-me")
        return 0
    finally:
        motor.close()


def cmd_status(args) -> int:
    cfg = _load_config(args)
    motor = container.build(cfg)
    try:
        store, ws = motor.store, motor.workspace
        tasks = store.tasks(ws.id)
        by_state: dict[str, int] = {}
        for t in tasks:
            by_state[t.state.value] = by_state.get(t.state.value, 0) + 1

        rodando = [t for t in tasks if t.state.value in
                   {"ASSIGNED", "IMPLEMENTING", "TESTING", "CI_RUNNING", "AI_REVIEW",
                    "MERGING", "DEPLOYING"}]
        open_items = store.open_approvals(ws.id)
        blocked = [t for t in tasks if t.state in (TaskState.BLOCKED, TaskState.FAILED)]
        ready = [t for t in tasks if t.state is TaskState.DONE]

        print(f"REGENTE -- {ws.name}  [{'sombra' if cfg.shadow else 'VALENDO'}]")
        print()
        print(f"  Rodando     {len(rodando)}")
        print(f"  Precisa de voce  {len(open_items)}" + ("   <-- prioridade" if open_items else ""))
        print(f"  Bloqueadas  {len(blocked)}")
        print(f"  Concluidas  {len(ready)}")

        if rodando:
            print()
            print("  TRABALHO ATIVO")
            for t in rodando:
                print(f"    {t.key:<16} {t.state.value}")
        if open_items:
            print()
            print("  PRECISA DE VOCE")
            for a in open_items:
                t = store.task(a.task_id)
                print(f"    [{a.risk.name}] {t.key:<16} {a.what_happened[:60]}")
            print()
            print("    regente needs-me   para ver e decidir")
        if args.verbose:
            print()
            print("  POR ESTADO")
            for state, n in sorted(by_state.items()):
                print(f"    {state:<16} {n}")
        return 0
    finally:
        motor.close()


def cmd_needs_me(args) -> int:
    cfg = _load_config(args)
    motor = container.build(cfg)
    try:
        open_items = motor.store.open_approvals(motor.workspace.id)
        if not open_items:
            print("nada precisa de voce now.")
            return 0
        for a in open_items:
            t = motor.store.task(a.task_id)
            print(escalation.render(escalation.briefing(a, t)))
            print(f"\n  regente decide {a.id} <opcao>")
            print("-" * 62)
        return 0
    finally:
        motor.close()


def cmd_decide(args) -> int:
    """Decide uma escalada -- pelo mesmo caminho que a Mission Control usa.

    Este comando estava QUEBRADO: o parser recebia `opcao` e `--por`, o handler
    lia `args.option` e `args.per`, e a chamada morria com `AttributeError`
    antes de tocar no store. Ninguem viu porque todo teste chamava
    `store.decide_approval` diretamente -- a fiacao de argumentos do CLI nao
    tinha teste nenhum, e e justamente onde uma renomeacao deixa restos.

    `--por` tambem foi embora, e essa parte e de propósito. Identidade digitada
    nao e identidade: gravava na auditoria o texto que a pessoa quisesse. Quem
    assina agora e a conta que roda o processo.
    """
    cfg = _load_config(args)
    motor = container.build(cfg)
    try:
        who = motor.terminal_principal()
        outcome = motor.decisions().decide(
            who, motor.workspace.id, args.approval_id, args.opcao,
            note=args.nota or "")
        if not outcome.accepted:
            print(f"{outcome.denial.value}: {outcome.reason}", file=sys.stderr)
            return _exit_for(outcome.denial)
        print(f"{outcome.task_key or outcome.task_id}: "
              f"registrado '{outcome.choice}' por {outcome.decided_by}.")
        print("O proximo tick retoma a task a partir daqui.")
        return 0
    finally:
        motor.close()


def cmd_health(args) -> int:
    """What is running, what is stuck, for how long and why.

    Reads persisted state only. It answers after a crash, which is the moment it
    matters -- a report assembled from a live process's memory would be empty
    exactly then, and an empty report reads like a healthy one.
    """
    from .engine import health as health_module

    cfg = load(args.config)
    store = SqliteStore(cfg.banco)
    store.migrate()
    try:
        ws_id = _stable_id(ids.WORKSPACE, cfg.organization, cfg.client, cfg.workspace)
        report = health_module.inspect(
            store, ws_id, cfg.workspace,
            areas_root=cfg.areas,
            budget_usd=cfg.budget.max_cost_usd,
            max_dispatches=cfg.limits.max_dispatches_per_day)
        print(report.render())
    finally:
        store.close()
    # Exit code carries the verdict so a cron job can act on it without parsing
    # prose: 0 healthy, 1 needs attention or is unexamined, 2 stuck.
    return {health_module.Level.OK: 0,
            health_module.Level.ATTENTION: 1,
            health_module.Level.UNKNOWN: 1,
            health_module.Level.STUCK: 2}[report.level]


def cmd_credentials(args) -> int:
    """Administra as credenciais deste workspace.

    Nenhum subcomando imprime material secreto, e nao existe um que imprima:
    `mostrar` exibe endereco, capacidades, validade e quem concedeu. Um
    comando de diagnostico que revelasse valor viraria, no primeiro incidente,
    a forma mais rapida de copiar um token -- e ficaria.
    """
    from datetime import datetime, timedelta, timezone

    from .engine.credentials import Reach, Use

    cfg = _load_config(args)
    motor = container.build(cfg)
    try:
        service = motor.credentials()
        who = motor.terminal_principal()
        workspace = motor.workspace.id

        if args.acao == "listar":
            saida = service.listing(who, workspace)
            if not isinstance(saida, list):
                print(f"{saida.refusal.value}: {saida.reason}", file=sys.stderr)
                return _exit_for(saida.refusal)
            if not saida:
                print("nenhuma credencial registrada neste workspace")
                return 0
            agora = datetime.now(timezone.utc)
            for c in saida:
                print(f"  {c.provider}/{c.name}  [{c.id}]")
                print(f"    estado      : {c.status(agora).value}")
                print(f"    referencia  : {c.secret_ref}")
                print(f"    capacidades : "
                      f"{', '.join(sorted(u.value for u in c.capabilities))}")
                print(f"    concedida   : {c.granted_at} por {c.granted_by}")
                print(f"    vence       : {c.expires_at or 'sem validade'}")
                if c.revoked_at:
                    print(f"    revogada    : {c.revoked_at} por {c.revoked_by}")
            return 0

        if args.acao == "registrar":
            vence = None
            if args.dias:
                vence = datetime.now(timezone.utc) + timedelta(days=args.dias)
            saida = service.register(
                who, workspace, name=args.nome, provider=args.provider,
                secret_ref=args.referencia,
                capabilities=[c.strip() for c in args.capacidades.split(",")],
                kind=args.tipo, expires_at=vence, note=args.nota or "")
        elif args.acao == "revogar":
            saida = service.revoke(who, workspace, args.nome,
                                   reason=args.nota or "")
        else:                                              # testar
            from .adapters.probe import probe_for

            uso = Use(args.uso)
            resultado = service.test_connection(
                who, workspace, args.provider, uso,
                probe_for(args.provider, cfg))
            print(f"  autorizado pelo Regente : {resultado.authorized}")
            print(f"  resposta do provedor    : {resultado.reach.value}")
            print(f"  capacidade suportada    : {resultado.capability_supported}")
            print(f"  utilizavel              : {resultado.usable}")
            print(f"  detalhe                 : {resultado.detail}")
            return 0 if resultado.usable else 1

        if not saida.accepted:
            print(f"{saida.refusal.value}: {saida.reason}", file=sys.stderr)
            return _exit_for(saida.refusal)
        print(saida.reason)
        print(f"  ator : {saida.actor}")
        if saida.credential:
            print(f"  id   : {saida.credential.id}")
        return 0
    finally:
        motor.close()


def cmd_access(args) -> int:
    """Administra o acesso deste workspace, pelo mesmo caminho que a tela usa.

    Nao existe um `access` de terminal e outro de navegador: os dois chamam
    `AccessService`. Duas administracoes de acesso divergem, e a que diverge e
    sempre a que esquece de conferir alguma coisa.
    """
    from .core.access import PrincipalRef, ROLES

    cfg = _load_config(args)
    motor = container.build(cfg)
    try:
        service = motor.access()
        who = motor.terminal_principal()
        workspace = motor.workspace.id

        if args.acao == "quem-sou-eu":
            print(f"identidade : {who.label}")
            print(f"emissor    : {who.issuer or '(nao informado)'}")
            print(f"provado em : {who.authenticated_at}")
            capacidades = sorted(a.value for a in
                                 who.abilities.get(workspace, ()))
            print(f"pode aqui  : {', '.join(capacidades) or 'nada'}")

            # O motor tambem e um principal (marco 16), e alguem precisa
            # CONCEDER a ele para que um tick de madrugada consiga usar
            # credencial. Sem isto impresso aqui, descobrir a identidade dele
            # exigia escrever Python -- e um passo obrigatorio do fluxo que so
            # existe fora da CLI e um passo que ninguem da.
            motor_ = motor.engine_principal()
            do_motor = sorted(a.value for a in
                              motor_.abilities.get(workspace, ()))
            print()
            print(f"o motor    : {motor_.label}")
            print(f"pode aqui  : {', '.join(do_motor) or 'nada'}")
            if not do_motor:
                print(f"             (para um tick usar credencial: "
                      f"regente access conceder {motor_.label} --papel service)")
            return EXIT_OK

        if args.acao == "inicial":
            saida = service.bootstrap(who, workspace, note=args.nota or "")
        elif args.acao == "conceder":
            saida = service.grant(who, workspace,
                                  PrincipalRef.parse(args.principal),
                                  role=args.papel, note=args.nota or "")
        elif args.acao == "revogar":
            saida = service.revoke(who, workspace,
                                   PrincipalRef.parse(args.principal))
        else:                                    # listar
            saida = service.listing(who, workspace)
            if isinstance(saida, list):
                if not saida:
                    print("nenhuma concessao registrada neste workspace")
                    return 0
                for g in saida:
                    estado = ("VIVA" if g.active
                              else f"revogada em {g.revoked_at} por {g.revoked_by}")
                    print(f"  {g.principal.key}")
                    print(f"    capacidades : "
                          f"{', '.join(sorted(a.value for a in g.abilities))}")
                    print(f"    concedida   : {g.granted_at} por {g.granted_by}")
                    print(f"    estado      : {estado}")
                return 0

        if not saida.accepted:
            print(f"{saida.refusal.value}: {saida.reason}", file=sys.stderr)
            return _exit_for(saida.refusal)
        print(saida.reason)
        if args.acao in ("inicial", "conceder"):
            print(f"  ator  : {saida.actor}")
            print(f"  alvo  : {saida.target}")
        return 0
    finally:
        motor.close()


def cmd_run(args) -> int:
    """Roda ciclos ate mandarem parar. O processo que faz o Regente trabalhar.

    Ate o marco de operacao, o motor so avancava quando alguem digitava
    `regente tick`. Tudo o mais estava pronto -- lease, orcamento, recuperacao,
    entrega -- e faltava alguem chamar de novo.

    Este comando NAO decide nada. Ele le a intencao gravada a cada volta e
    obedece: `Pausar` na tela para de despachar em segundos, `Parar` encerra
    limpo, e Ctrl+C tambem. Toda decisao continua onde sempre esteve.
    """
    import signal
    import socket
    import os

    from .core.operation import Intent
    from .engine.loop import ContinuousLoop

    cfg = _load_config(args)
    motor = container.build(cfg)
    ws = motor.workspace.id
    ops = motor.operations()

    # `--iniciar` grava a intencao antes de comecar, pelo mesmo caminho da tela.
    # Sem ele, `run` obedece o que ja estava gravado -- que e o que faz um
    # reinicio depois de queda voltar ao estado em que a pessoa deixou.
    if args.iniciar:
        saida = ops.set_intent(motor.terminal_principal(), ws, Intent.RUNNING,
                               note="regente run --iniciar",
                               interval_seconds=args.intervalo)
        if not saida.accepted:
            print(f"{saida.refusal}: {saida.reason}", file=sys.stderr)
            motor.close()
            return _exit_for(saida.refusal)

    op, _, fase = ops.state(ws)
    if op.intent is Intent.STOPPED and not args.iniciar:
        print("o processamento deste workspace esta PARADO.")
        print("  ligue com: regente run --iniciar")
        print("  ou pela tela: regente ui")
        motor.close()
        return EXIT_OK

    laco = ContinuousLoop(
        read_intent=lambda: (lambda o: (o.intent, o.interval_seconds))(
            motor.store.operation(ws)),
        tick=lambda: len(motor.orchestrator.tick().dispatched),
        beat=lambda ticks, detalhe: ops.beat(
            ws, pid=os.getpid(), host=socket.gethostname(), ticks=ticks,
            detail=detalhe),
        stand_down=lambda: ops.stood_down(ws),
        on_error=lambda e: print(f"  ciclo falhou: {type(e).__name__}: "
                                 f"{redaction.redact_url(str(e))[:200]}",
                                 file=sys.stderr))

    # Ctrl+C e SIGTERM viram PEDIDO de parada, e nao morte. O laco termina o
    # que comecou, apaga o sinal de vida e sai -- sem isso, um processo morto
    # continua parecendo vivo ate o prazo de graca passar.
    def parar(signum, frame):
        print("\n  parada pedida; terminando o ciclo atual...", file=sys.stderr)
        laco.request_stop("sinal do terminal")

    signal.signal(signal.SIGINT, parar)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, parar)

    print(f"{motor.workspace.name}: processando a cada {op.interval_seconds}s "
          f"(Ctrl+C para parar)")
    try:
        rel = laco.run(max_cycles=args.ciclos)
    finally:
        motor.close()
    print(f"  {rel.ticks} ciclo(s), {rel.dispatched} despacho(s), "
          f"{rel.failures} falha(s) -- {rel.stopped_because}")
    return EXIT_OK


def cmd_engine(args) -> int:
    """Liga, pausa, retoma e para o processamento -- e diz o que esta havendo.

    Grava INTENCAO. Nao cria nem mata processo: quem executa e um `regente run`,
    e se nao houver nenhum o estado mostra isso em vez de fingir.
    """
    from .core.operation import Intent

    cfg = _load_config(args)
    motor = container.build(cfg)
    try:
        ops = motor.operations()
        ws = motor.workspace.id

        if args.acao == "estado":
            from .core.operation import explain

            op, beat, fase = ops.state(ws)
            print(f"fase       : {fase.value}")
            print(f"o que e    : {explain(fase, beat)}")
            print(f"intencao   : {op.intent.value}"
                  + (f" (por {op.changed_by})" if op.changed_by else ""))
            print(f"intervalo  : {op.interval_seconds}s")
            if beat:
                print(f"processo   : pid {beat.pid} em {beat.host}, "
                      f"{beat.ticks} ciclo(s), sinal de {beat.at}")
            else:
                print("processo   : nenhum sinal de vida")
            return EXIT_OK if not fase.needs_attention else EXIT_BLOCKED

        alvo = {"iniciar": Intent.RUNNING, "retomar": Intent.RUNNING,
                "pausar": Intent.PAUSED, "parar": Intent.STOPPED}[args.acao]
        saida = ops.set_intent(motor.terminal_principal(), ws, alvo,
                               note=args.nota or "",
                               interval_seconds=args.intervalo)
        if not saida.accepted:
            print(f"{saida.refusal}: {saida.reason}", file=sys.stderr)
            return _exit_for(saida.refusal)
        print(f"{motor.workspace.name}: {saida.detail}")
        if alvo is Intent.RUNNING:
            _, beat, fase = ops.state(ws)
            if beat is None:
                print("  nenhum processo esta rodando ainda. "
                      "Inicie com: regente run")
        return EXIT_OK
    finally:
        motor.close()


def cmd_config(args) -> int:
    """Le e escreve a configuracao do workspace -- o MESMO servico da tela.

    Existe para a CLI continuar oficial: tudo o que a Mission Control configura
    tem de ser configuravel daqui, e pelo mesmo caminho. Duas implementacoes da
    mesma escrita divergem, e a que diverge e a que esquece uma barreira.
    """
    import json as _json

    from .app.config import load_policies
    from .core.policy import PolicyEngine
    from .core.settings import OVERRIDABLE, describe, effective
    from .engine.settings import SettingsService

    cfg = _load_config(args)
    motor = container.build(cfg)
    try:
        servico = SettingsService(
            store=motor.store,
            policy=PolicyEngine.from_config(load_policies(cfg.policies)),
            organization=cfg.organization, client=cfg.client,
            workspace_name=cfg.workspace,
            environment=(cfg.projects[0].default_environment
                         if cfg.projects else "staging"))
        ws = motor.workspace.id

        if args.acao == "mostrar":
            overlay = servico.overlay(ws)
            do_arquivo = _config_do_arquivo(cfg)
            for chave in OVERRIDABLE:
                campo = effective(chave, do_arquivo.get(chave), overlay)
                print(f"{chave}")
                print(f"  fonte  : {campo.source.value}"
                      + ("  (CONFLITO com o arquivo)" if campo.conflicts else ""))
                print(f"  {describe(campo, chave)}")
                if args.verbose and campo.value is not None:
                    for linha in _json.dumps(campo.value, indent=2,
                                             ensure_ascii=False).splitlines():
                        print(f"    {linha}")
                print()
            return EXIT_OK

        who = motor.terminal_principal()
        if args.acao == "remover":
            saida = servico.clear(who, ws, args.chave)
        else:                                                  # definir
            origem = Path(args.de).read_text(encoding="utf-8") if args.de else args.valor
            if not origem:
                print("informe --valor '<json>' ou --de arquivo.json",
                      file=sys.stderr)
                return EXIT_INVALID_ARGUMENT
            try:
                valor = _json.loads(origem)
            except ValueError as e:
                print(f"INVALID_ARGUMENT: isto nao e JSON valido: {e}",
                      file=sys.stderr)
                return EXIT_INVALID_ARGUMENT
            saida = servico.put(who, ws, args.chave, valor)

        if not saida.accepted:
            print(f"{saida.refusal}: {saida.reason}", file=sys.stderr)
            return _exit_for(saida.refusal)
        print(f"{args.chave}: {saida.detail}")
        return EXIT_OK
    finally:
        motor.close()


def _config_do_arquivo(cfg) -> dict:
    """O que o ARQUIVO diz nas chaves sobreponiveis. Espelha o da API."""
    provedores = {k: {"name": v.name, **dict(v.options)}
                  for k, v in cfg.providers.items()}
    tasks = cfg.providers.get("tasks")
    return {
        "providers": provedores,
        "status_map": dict(tasks.options.get("status_map") or {}) if tasks else {},
        "selection": [
            {"name": r.name, "field": r.field_name, "match": r.match.value,
             "value": r.value, "effect": r.effect.value, "delta": r.delta}
            for r in cfg.selection.rules],
    }


def cmd_ui(args) -> int:
    """Sobe a Mission Control sobre o estado deste workspace.

    Um processo, uma porta, loopback. A tela e servida pelo mesmo servidor que
    responde a API para nao existir configuracao de origem cruzada -- e para nao
    existir a tentacao de abrir CORS "so para desenvolver".

    Nao ha autenticacao nesta versao, e por isso o default nao escuta na rede.
    Quem precisar expor tem de trocar o `Principal` por um vindo de identidade
    real; a fronteira ja existe, vazia de proposito.
    """
    from .adapters.identity.dev_token import DevTokenIdentity
    from .adapters.registry import catalogo as catalogo_de_provedores
    from .app.api import serve
    from .engine.access import AccessService
    from .app.config import load_policies
    from .core.policy import PolicyEngine
    from .engine.decision import DecisionService
    from .engine.readmodel import ReadModel

    cfg = _load_config(args)
    local = args.host in ("127.0.0.1", "::1", "localhost")
    if not local and not args.i_know_this_is_not_authenticated:
        # Recusa no codigo, e nao conselho no README. O unico mecanismo de
        # identidade desta versao e de desenvolvimento; servi-lo na rede
        # entregaria o estado de todos os clientes visiveis a quem alcancar a
        # porta -- e a escrita junto.
        print(f"recusando escutar em {args.host}: o mecanismo de identidade "
              f"desta versao e SOMENTE DESENVOLVIMENTO e nao serve para "
              f"exposicao em rede.\nUse --host 127.0.0.1, ou ligue um provedor "
              f"de identidade real antes de expor.", file=sys.stderr)
        return 2

    store = SqliteStore(cfg.banco)
    store.migrate()
    # Os nomes de organizacao e cliente so existem no arquivo de configuracao, e
    # este comando e um dos poucos lugares que o le. Sem esta linha a tela mostra
    # um id opaco para quem precisa saber de quem e o trabalho -- que foi
    # exatamente o que a primeira execucao real mostrou.
    store.save_client(_stable_id(ids.CLIENT, cfg.organization, cfg.client),
                      cfg.organization, cfg.client)
    read = ReadModel(store=store, areas_root=str(cfg.areas),
                     organization=cfg.organization,
                     budget_usd=cfg.budget.max_cost_usd,
                     max_dispatches=cfg.limits.max_dispatches_per_day)

    # Escopo do operador local. `None` seria "todos os workspaces do banco";
    # nomear os do proprio arquivo de configuracao e mais estreito e continua
    # sendo verdade -- e o dia em que houver identidade real, so este ponto muda.
    configured = _stable_id(ids.WORKSPACE, cfg.organization, cfg.client,
                            cfg.workspace)
    visible = frozenset({configured})
    if args.all_workspaces:
        visible = None

    identity = DevTokenIdentity(
        operator=args.as_operator or cfg.client,
        reads=visible, bind_is_local=local)

    decisions = DecisionService(
        store=store, policy=PolicyEngine.from_config(load_policies(cfg.policies)),
        organization=cfg.organization, client=cfg.client,
        workspace_name=cfg.workspace,
        environment=(cfg.projects[0].default_environment
                     if cfg.projects else "staging"))

    access = AccessService(
        store=store, policy=PolicyEngine.from_config(load_policies(cfg.policies)),
        organization=cfg.organization, client=cfg.client,
        workspace_name=cfg.workspace,
        environment=(cfg.projects[0].default_environment
                     if cfg.projects else "staging"))

    from .adapters.secrets import ScopedSecrets
    from .engine.credentials import CredentialService

    credentials = CredentialService(
        store=store, policy=PolicyEngine.from_config(load_policies(cfg.policies)),
        secrets=ScopedSecrets(workspace=cfg.workspace, allow_any=True,
                              helpers=dict(cfg.helpers)),
        organization=cfg.organization, client=cfg.client,
        workspace_name=cfg.workspace,
        environment=(cfg.projects[0].default_environment
                     if cfg.projects else "staging"))

    from .engine.operation import OperationService

    operations = OperationService(
        store=store, policy=PolicyEngine.from_config(load_policies(cfg.policies)),
        organization=cfg.organization, client=cfg.client,
        workspace_name=cfg.workspace,
        environment=(cfg.projects[0].default_environment
                     if cfg.projects else "staging"))

    from .engine.settings import SettingsService

    settings = SettingsService(
        store=store, policy=PolicyEngine.from_config(load_policies(cfg.policies)),
        organization=cfg.organization, client=cfg.client,
        workspace_name=cfg.workspace,
        environment=(cfg.projects[0].default_environment
                     if cfg.projects else "staging"))

    # A SONDA vem da composicao, e nao da API. Escolher qual usar exigiria a
    # API saber o que e um Jira -- a recusa do marco 15 continua valendo, e o
    # que muda e que agora alguem entrega a sonda pronta.
    from .adapters.probe import probe_for as _probe
    from .adapters.registry import needs_credential as _needs_credential

    httpd = serve(read, host=args.host, port=args.port, identity=identity,
                  decisions=decisions, access=access, credentials=credentials,
                  operations=operations, settings=settings, config=cfg,
                  probe_for=lambda provider: _probe(provider, cfg),
                  needs_credential=_needs_credential,
                  catalog=catalogo_de_provedores(),
                  session_token=identity.token,
                  read_only=args.read_only)
    where = f"http://{args.host}:{args.port}/"
    print(f"Mission Control em {where}")
    print(f"identidade: {identity.describe()}")
    if identity.development_only:
        print("ATENCAO: mecanismo de identidade SOMENTE DESENVOLVIMENTO")
    concedido = access.abilities_for(
        identity.principal(identity.authenticate(identity.token)).ref)
    capacidades = sorted(a.value for a in concedido.get(configured, ()))
    print(f"autoridade desta identidade: {', '.join(capacidades) or 'nenhuma'}"
          + (" (sessao marcada como somente leitura)" if args.read_only else ""))
    if not capacidades:
        print("  conceda com: regente access conceder "
              f"{identity.name}:{args.as_operator or cfg.client} --papel operator")
    print("ctrl-c para parar")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print()
    finally:
        httpd.server_close()
        identity.close()
        store.close()
    return 0


def cmd_log(args) -> int:
    cfg = _load_config(args)
    motor = container.build(cfg)
    try:
        target = None
        if args.task:
            for t in motor.store.tasks(motor.workspace.id):
                if t.key == args.task or t.id == args.task:
                    target = t.id
                    break
            if target is None:
                print(f"task '{args.task}' nao encontrada")
                return EXIT_NOT_FOUND
        events = motor.store.events(motor.workspace.id, task_id=target, limit=args.n)
        for e in reversed(events):
            hora = e.ts.strftime("%d/%m %H:%M")
            key = ""
            if e.task_id and not target:
                t = motor.store.task(e.task_id)
                key = f"{t.key} " if t else ""
            print(f"{hora}  {key}{e.kind:<14} {e.summary}")
        return 0
    finally:
        motor.close()


def cmd_plan(args) -> int:
    """Mostra a decisao do scheduler sem executar nada."""
    cfg = _load_config(args)
    motor = container.build(cfg)
    try:
        p = motor.orchestrator.plan()
        if p.dispatch:
            print("DESPACHARIA EM PARALELO")
            for i in p.dispatch:
                t = motor.store.task(i)
                print(f"  {t.key:<16} {', '.join(t.resources)}")
        else:
            print("nada pronto para despachar")
        if p.deferred:
            print()
            print("ADIADAS")
            for a in p.deferred:
                t = motor.store.task(a.task_id)
                print(f"  {t.key:<16} {a.reason}")
        if p.in_cycle:
            print()
            print("EM CICLO (ninguem pode comecar)")
            for i in p.in_cycle:
                print(f"  {motor.store.task(i).key}")
        return 0
    finally:
        motor.close()


def cmd_sombra(args) -> int:
    """Descobre e planeja contra o provedor real, sem mutar nada."""
    cfg = _load_config(args)
    motor = container.build(cfg)
    try:
        r = shadow.execute(
            provider=motor.orchestrator.tasks_provider,
            limits=cfg.limits,
            # `args.minhas` e `args.saida` sao os nomes que o PARSER define.
            # Ate o marco 6 este handler lia `args.mine` e `args.output`,
            # sobras de uma renomeacao -- e `regente sombra`, que o README
            # anuncia como o comando de entrada, morria com `AttributeError`
            # antes de tocar em coisa nenhuma. O mesmo defeito que `decide`
            # teve no marco 13, e `cadeia` tinha na linha de baixo.
            filtro={"apenas_minhas": True} if args.minhas else None,
            eu=args.eu)
        print(shadow.render(r))
        if args.saida:
            Path(args.saida).write_text(shadow.render(r), encoding="utf-8")
            print()
            print(f"  gravado em {args.saida}")
        return 0 if not r.provider_errors else 2
    finally:
        motor.close()


def cmd_repos(args) -> int:
    """Repositorios visiveis, como o motor os enxerga."""
    cfg = _load_config(args)
    motor = container.build(cfg)
    try:
        if motor.repos is None:
            print("nenhum provedor de repositorio configurado")
            return EXIT_INVALID_ARGUMENT
        items = motor.repos.list_repositories()
        print(f"{len(items)} repositorio(s) via {motor.repos.name}")
        print()
        for r in sorted(items, key=lambda x: x.ref.key):
            mark = "!" if r.anomalies else " "
            print(f" {mark} {r.ref.key:<46} base={r.base_branch or '(nao lida)':<10}")
            if args.verbose:
                print(f"     recurso: {r.ref.resource(motor.workspace.id)}")
                if r.anomalies:
                    print(f"     anomalias: {'; '.join(r.anomalies)}")
        return 0
    finally:
        motor.close()


def cmd_cadeia(args) -> int:
    """task -> repositorio -> base -> recursos -> risco/policy -> candidato."""
    cfg = _load_config(args)
    motor = container.build(cfg)
    try:
        if motor.repos is None:
            print("nenhum provedor de repositorio configurado")
            return EXIT_INVALID_ARGUMENT
        items = motor.orchestrator.tasks_provider.list_tasks()
        repositories = motor.repos.list_repositories()
        branches = {}
        if not args.sem_branches:
            for r in repositories:
                try:
                    branches[r.ref.key] = motor.repos.list_branches(r.ref.key)
                except Exception:
                    branches[r.ref.key] = []
        rel = chain.build(
            workspace_nome=motor.workspace.name, workspace_id=motor.workspace.id,
            tasks=items, repos=repositories, resolvedor=motor.resolvedor,
            policy=motor.policy, risk=motor.risk,
            autonomy=motor.workspace.max_autonomy, branches=branches,
            organization=cfg.organization, client=cfg.client)
        print(chain.render(rel, limit=args.limite))
        if args.saida:
            Path(args.saida).write_text(chain.render(rel, limit=200), encoding="utf-8")
            print()
            print(f"  gravado em {args.saida}")
        return 0
    finally:
        motor.close()


def cmd_mission(args) -> int:
    """Select one task and show the briefing. Executes only with --run."""
    cfg = _load_config(args)
    engine = container.build(cfg)
    try:
        if engine.repos is None:
            print("no repository provider configured")
            return EXIT_INVALID_ARGUMENT
        outcome = engine.run_mission(execute=args.run, only=args.task)
        if outcome.refused:
            print(outcome.refusal)
            return 3
        print(outcome.briefing.render())
        if not args.run:
            print()
            print("  DRY: nothing was executed. Add --run to execute.")
            return 0
        print()
        print(f"VERDICT  {outcome.verdict.value}")
        print(f"  {outcome.loop.reason}")
        if outcome.loop.changed_files:
            print(f"  changed: {', '.join(outcome.loop.changed_files[:8])}")
        print()
        print(outcome.measurements.render())
        if args.output:
            report = outcome.briefing.render() + "\n\n" + outcome.measurements.render()
            Path(args.output).write_text(report, encoding="utf-8")
        return 0
    finally:
        engine.close()


def cmd_rules(args) -> int:
    cfg = _load_config(args)
    from .app.config import load_policies
    from .adapters import registry
    print(f"autonomia maxima: {cfg.autonomy.name}")
    print(f"modo: {'sombra' if cfg.shadow else 'VALENDO'}")
    print(f"limites: {cfg.limits.max_workers} workers, "
          f"{cfg.limits.max_dispatches_per_day} despachos/dia")
    print()
    print("REGRAS")
    for r in load_policies(cfg.policies):
        criteria = ", ".join(f"{k}={v}" for k, v in (r.get("match") or {}).items())
        print(f"  {r['effect']:<15} {r.get('name', '?'):<26} {criteria}")
    print()
    print("ADAPTERS DISPONIVEIS")
    for cap, nomes in registry.available().items():
        print(f"  {cap:<14} {', '.join(nomes)}")
    return 0


# ---- entrada -------------------------------------------------------------

def _versao_instalada() -> str:
    """A versao deste pacote, perguntada a quem o instalou.

    `desenvolvimento` quando o pacote nao esta instalado -- rodar direto do
    repositorio e legitimo, e dizer isso e mais util que inventar um numero.
    """
    from importlib import metadata

    try:
        return metadata.version("regente")
    except metadata.PackageNotFoundError:
        return "desenvolvimento (rodando do repositorio)"


def _argumentos_da_ui(p: "argparse.ArgumentParser") -> None:
    """Os argumentos que `cmd_ui` LE, num lugar so.

    `init` termina abrindo a Mission Control, entao ele chama `cmd_ui` -- e
    `cmd_ui` le `args.host`, `args.port` e mais quatro. Declarar essa lista duas
    vezes seria pedir para as duas divergirem, e a divergencia entre o que o
    parser define e o que o handler le ja atravessou esta suite tres vezes: e
    exatamente a classe de defeito que `test_cli.py` passou a guardar no marco 6.
    """
    p.add_argument("--host", default="127.0.0.1",
                   help="loopback por padrao: esta versao nao autentica ninguem")
    p.add_argument("--port", type=int, default=8787)
    p.add_argument("--all-workspaces", action="store_true",
                   help="mostra todo workspace do banco, nao so o configurado")
    p.add_argument("--read-only", action="store_true",
                   help="nao concede autoridade de decisao a esta sessao")
    p.add_argument("--as-operator", default="",
                   help="como esta sessao assina na auditoria; o default vem "
                        "da configuracao, nunca do navegador")
    p.add_argument("--i-know-this-is-not-authenticated", action="store_true",
                   help="permite escutar fora do loopback; o provedor de "
                        "identidade de desenvolvimento ainda recusa autenticar")


def build_parser() -> argparse.ArgumentParser:
    """O parser inteiro, montado e devolvido sem rodar nada.

    Separado de `main` para que um teste possa comparar, comando a comando, os
    `dest` que o parser define com os `args.X` que cada handler le. Duas vezes
    esta divergencia atravessou uma suite verde -- `decide` no marco 13,
    `sombra` e `cadeia` neste -- porque a fiacao de argumentos nao tinha teste,
    e ela e exatamente onde uma renomeacao deixa restos.
    """
    ap = argparse.ArgumentParser(prog="regente",
                                 description="Sistema operacional para agentes de engenharia.")
    ap.add_argument("-c", "--config", default=DEFAULT_CONFIG_FILE)
    # A versao vem do PACOTE INSTALADO, e nao de uma constante escrita aqui.
    #
    # Uma constante e uma segunda fonte: ela envelhece a cada `version =` no
    # pyproject que ninguem lembra de copiar, e o que ela informa e sempre a
    # versao do codigo-fonte, e nunca a do que a pessoa de fato instalou. Quem
    # sobe um bug quer saber qual build esta na maquina.
    ap.add_argument("--version", action="version",
                    version=f"regente {_versao_instalada()}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser(
        "init",
        help="cria a configuracao, concede o acesso e abre a Mission Control")
    p.add_argument("--force", action="store_true")
    # Os tres nomes derivam o id do workspace. Passa-los por argumento e o que
    # torna o comando usavel num script sem ninguem para responder as perguntas.
    p.add_argument("--organizacao", default="")
    p.add_argument("--cliente", default="")
    p.add_argument("--workspace", default="")
    # Perguntar e ABRIR A TELA sao os dois comportamentos que prendem o
    # terminal. Os dois sao opt-in, e por isso `regente init` num script nunca
    # trava -- ele escolhe os padroes, imprime quais foram, e retorna.
    p.add_argument("--perguntar", action="store_true",
                   help="pergunta os nomes e oferece abrir a Mission Control")
    p.add_argument("--ui", action="store_true",
                   help="abre a Mission Control ao terminar")
    # `init` termina abrindo a tela, entao ele precisa dos mesmos argumentos que
    # `ui` le. Sem isto, `cmd_ui` estouraria num `args.X` que o parser do `init`
    # nunca definiu -- a classe de defeito que o marco 6 fechou.
    _argumentos_da_ui(p)
    p.set_defaults(fn=cmd_init)

    p = sub.add_parser("doctor", help="prova que o motor sobe")
    p.set_defaults(fn=cmd_doctor)

    p = sub.add_parser("tick", help="roda um ciclo")
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(fn=cmd_tick)

    p = sub.add_parser("status", help="o que esta acontecendo")
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(fn=cmd_status)

    p = sub.add_parser("plan", help="o que o scheduler faria now")
    p.set_defaults(fn=cmd_plan)

    p = sub.add_parser("health", help="o que esta rodando, o que travou e ha quanto tempo")
    p.add_argument("--config", default="regente.yaml")
    p.set_defaults(fn=cmd_health)

    p = sub.add_parser("needs-me", help="a fila de decisoes humanas")
    p.set_defaults(fn=cmd_needs_me)

    p = sub.add_parser("decide", help="decide um item da fila")
    p.add_argument("approval_id")
    p.add_argument("opcao")
    # Sem `--por`: quem assina e a conta que roda o processo, e nao um texto
    # que quem decide escolhe. Identidade digitada nao e identidade.
    p.add_argument("--nota", default="")
    p.set_defaults(fn=cmd_decide)

    p = sub.add_parser("log", help="a trilha do que o motor fez")
    p.add_argument("-n", type=int, default=40)
    p.add_argument("--task", help="filtra por chave de task")
    p.set_defaults(fn=cmd_log)

    p = sub.add_parser("sombra", help="ve o trabalho real sem tocar em nada")
    p.add_argument("--minhas", action="store_true", help="so o que esta comigo")
    p.add_argument("--eu", help="nome do responsavel a contar como 'minhas'")
    p.add_argument("--saida", help="grava o relatorio neste arquivo")
    p.set_defaults(fn=cmd_sombra)

    p = sub.add_parser("repos", help="repositorios visiveis, sem tocar em nada")
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(fn=cmd_repos)

    p = sub.add_parser("cadeia", help="da task real ao candidato a execucao, em sombra")
    p.add_argument("--limite", type=int, default=10)
    p.add_argument("--sem-branches", action="store_true",
                   help="pula a leitura de branches (mais rapido, menos evidencia)")
    p.add_argument("--saida", help="grava o relatorio neste arquivo")
    p.set_defaults(fn=cmd_cadeia)

    p = sub.add_parser("mission", help="select one task, show the briefing, optionally run")
    p.add_argument("--run", action="store_true", help="execute; without it, nothing runs")
    p.add_argument("--task", help="restrict selection to this task key")
    p.add_argument("--output", help="write briefing and metrics to this file")
    p.set_defaults(fn=cmd_mission)

    p = sub.add_parser("rules", help="regras, limites e adapters em vigor")
    p.set_defaults(fn=cmd_rules)

    p = sub.add_parser("credentials",
                       help="credenciais de provider deste workspace")
    p.add_argument("acao", choices=["listar", "registrar", "revogar", "testar"])
    p.add_argument("nome", nargs="?", default="",
                   help="nome da credencial; na revogacao, o id")
    p.add_argument("--provider", default="repository")
    p.add_argument("--referencia", default="",
                   help="onde o segredo vive: env:NOME, arquivo:CAMINHO "
                        "ou helper:AJUDANTE")
    p.add_argument("--capacidades", default="repo.read",
                   help="lista separada por virgula")
    p.add_argument("--tipo", default="token")
    p.add_argument("--dias", type=int, default=0,
                   help="validade em dias; 0 = sem validade")
    p.add_argument("--uso", default="repo.read",
                   help="capacidade a testar em `testar`")
    p.add_argument("--nota", default="")
    p.add_argument("--config", default="regente.yaml")
    p.set_defaults(fn=cmd_credentials)

    p = sub.add_parser("access", help="quem pode agir neste workspace")
    p.add_argument("acao", choices=["listar", "conceder", "revogar", "inicial",
                                    "quem-sou-eu"])
    p.add_argument("principal", nargs="?", default="",
                   help="identidade alvo, na forma provedor:sujeito")
    p.add_argument("--papel", default="operator",
                   help="operator, admin ou owner")
    p.add_argument("--nota", default="")
    p.add_argument("--config", default="regente.yaml")
    p.set_defaults(fn=cmd_access)

    p = sub.add_parser("run", help="roda ciclos continuamente ate mandarem parar")
    p.add_argument("--iniciar", action="store_true",
                   help="grava a intencao RUNNING antes de comecar")
    p.add_argument("--intervalo", type=int, default=None,
                   help="segundos entre ciclos (padrao: o do workspace)")
    p.add_argument("--ciclos", type=int, default=None,
                   help="para depois de N ciclos (diagnostico)")
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("config",
                       help="configuracao do workspace: providers, status, prioridade")
    p.add_argument("acao", choices=["mostrar", "definir", "remover"])
    p.add_argument("chave", nargs="?", default="",
                   help="providers | status_map | selection")
    p.add_argument("--valor", default="", help="o JSON a gravar")
    p.add_argument("--de", default="", help="arquivo com o JSON a gravar")
    p.add_argument("-v", "--verbose", action="store_true",
                   help="mostra tambem o valor efetivo de cada chave")
    p.set_defaults(fn=cmd_config)

    p = sub.add_parser("engine", help="liga, pausa, retoma e para o processamento")
    p.add_argument("acao", choices=["estado", "iniciar", "pausar", "retomar",
                                    "parar"])
    p.add_argument("--intervalo", type=int, default=None,
                   help="segundos entre ciclos")
    p.add_argument("--nota", default="")
    p.set_defaults(fn=cmd_engine)

    p = sub.add_parser("ui", help="Mission Control: o estado do motor numa tela")
    p.add_argument("--config", default="regente.yaml")
    _argumentos_da_ui(p)
    p.set_defaults(fn=cmd_ui)

    return ap


def main(argv: list[str] | None = None) -> int:
    _force_utf8()
    ap = build_parser()
    args = ap.parse_args(argv)
    try:
        return args.fn(args)
    except FileNotFoundError as e:
        print(f"{e}\nRode `regente init` para comecar.", file=sys.stderr)
        return EXIT_NOT_FOUND
    except CredentialDenied as e:
        # A recusa do caminho governado chega aqui com o motivo do MOTOR. A CLI
        # nao reclassifica: transformar "voce nao tem autoridade" num erro
        # generico manda a pessoa procurar o problema no lugar errado.
        print(f"{e.refusal}: {e.reason}", file=sys.stderr)
        return _exit_for(e.refusal)
    except PermissionError as e:
        print(f"AUTHENTICATION: {e}", file=sys.stderr)
        return EXIT_AUTHENTICATION
    except (ValueError, KeyError) as e:
        # Configuracao errada, nome inexistente, argumento impossivel. Nao e
        # falta de autoridade, e a pessoa conserta um arquivo -- nao um acesso.
        print(f"INVALID_ARGUMENT: {type(e).__name__}: {e}", file=sys.stderr)
        return EXIT_INVALID_ARGUMENT
    except AdapterError as e:
        # O mundo externo nao respondeu, ou respondeu nao. Nunca sucesso, e
        # tambem nunca "sua culpa": e a categoria de "nao deu para saber".
        print(f"BLOCKED: {redaction.redact_url(str(e))}", file=sys.stderr)
        return EXIT_BLOCKED
    except Exception as e:  # noqa: BLE001
        # O texto de uma excecao inesperada pode citar uma URL com credencial
        # embutida -- e um traceback na tela e o primeiro lugar onde isso vaza.
        print(f"UNKNOWN: {type(e).__name__}: {redaction.redact_url(str(e))}",
              file=sys.stderr)
        return EXIT_UNKNOWN


if __name__ == "__main__":
    raise SystemExit(main())
