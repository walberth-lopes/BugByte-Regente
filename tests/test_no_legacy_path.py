# -*- coding: utf-8 -*-
"""A prova principal do marco 16: nao ha segunda porta.

    ANTES:  adapter -> caminho governado
                    -> caminho legado

    DEPOIS: adapter -> caminho governado

Testes de ausencia sao estranhos de escrever e faceis de escrever mal. Um que
so procure uma string vira decorativo no dia em que alguem renomeia a funcao.
Os daqui procuram a FORMA do caminho antigo -- uma fabrica que aceita um
resolvedor de segredo, um adapter que resolve endereco, uma construcao que
produz material -- e cada um explica que forma e essa.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ADAPTERS = Path("regente/adapters")
CORE = Path("regente/core")
ENGINE = Path("regente/engine")
PORTS = Path("regente/ports")

#: Onde o mecanismo governado PODE tocar material. Em mais nenhum lugar.
FONTE = {"regente/adapters/secrets.py"}
GOVERNADO = {"regente/engine/credentials.py"}


def _arvore(path: Path):
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _somente_codigo(path: Path) -> list[tuple[int, str]]:
    """Linhas de CODIGO, numeradas -- sem comentario e sem docstring.

    A prosa PRECISA poder citar a armadilha: e nela que a regra fica escrita
    para quem vier depois. Um guard que acusa a propria explicacao ensina a
    ignorar o guard -- e este arquivo inteiro e explicacao.
    """
    texto = path.read_text(encoding="utf-8")
    prosa: set[int] = set()
    for n, linha in enumerate(texto.splitlines(), 1):
        if linha.lstrip().startswith("#"):
            prosa.add(n)
    portadores = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
    for no in ast.walk(ast.parse(texto, filename=str(path))):
        if not isinstance(no, portadores) or not no.body:
            continue
        primeiro = no.body[0]
        if (isinstance(primeiro, ast.Expr)
                and isinstance(primeiro.value, ast.Constant)
                and isinstance(primeiro.value.value, str)):
            prosa.update(range(primeiro.lineno,
                               (primeiro.end_lineno or primeiro.lineno) + 1))
    return [(n, l.split("#", 1)[0]) for n, l in
            enumerate(texto.splitlines(), 1) if n not in prosa]


# ---------------------------------------------------------------------------
# 1. A fabrica antiga nao existe mais como porta
# ---------------------------------------------------------------------------

def test_no_adapter_can_be_built_with_a_secret_provider():
    """A forma do caminho legado: uma fabrica que aceita `secrets`.

    Enquanto ela existia, a composicao entregava um resolvedor INTEIRO ao
    adapter, e ele resolvia a referencia que quisesse -- sem identidade,
    concessao, capacidade, validade nem revogacao.
    """
    fonte = (ADAPTERS / "registry.py").read_text(encoding="utf-8")
    culpados = []
    for n, linha in enumerate(fonte.splitlines(), 1):
        codigo = linha.split("#", 1)[0]
        if 'o["secrets"]' in codigo or 'o.get("secrets"' in codigo:
            culpados.append(f"registry.py:{n}")
    assert not culpados, ("fabrica ainda aceita um SecretProvider:\n  "
                          + "\n  ".join(culpados))


def test_the_secret_provider_is_not_registered_as_a_buildable_adapter():
    """Pedir um resolvedor pelo nome era o atalho para entrega-lo a alguem."""
    from regente.adapters import registry
    from regente.ports import Capability

    with pytest.raises(KeyError):
        registry.create(Capability.SECRETS, "scoped", {})


def test_composition_hands_no_adapter_a_secret_provider():
    """A composicao injeta dependencia; ela nao concede autoridade."""
    fonte = Path("regente/app/container.py").read_text(encoding="utf-8")
    culpados = []
    for n, linha in enumerate(fonte.splitlines(), 1):
        codigo = linha.split("#", 1)[0]
        if '"secrets":' in codigo:
            culpados.append(f"container.py:{n}: {codigo.strip()[:60]}")
    assert not culpados, ("a composicao ainda entrega resolvedor a adapter:\n  "
                          + "\n  ".join(culpados))


# ---------------------------------------------------------------------------
# 2. Nenhum adapter resolve endereco por conta propria
# ---------------------------------------------------------------------------

def test_no_adapter_resolves_a_secret_reference_itself():
    """`resolve(referencia)` fora da fonte e uma segunda porta.

    A fonte pode: e ela. O `CredentialService` pode: e o caminho governado.
    Qualquer outro arquivo que resolva endereco tem uma rota propria ate
    material, e nao passa por barreira nenhuma.
    """
    culpados = []
    for path in sorted(list(ADAPTERS.rglob("*.py")) + list(ENGINE.rglob("*.py"))):
        rel = path.as_posix()
        if rel in FONTE or rel in GOVERNADO:
            continue
        for no in ast.walk(_arvore(path)):
            if (isinstance(no, ast.Call) and isinstance(no.func, ast.Attribute)
                    and no.func.attr == "resolve"
                    and isinstance(no.func.value, ast.Name)
                    and no.func.value.id in ("secrets", "secret", "fonte")):
                culpados.append(f"{rel}:{no.lineno}")
    assert not culpados, ("adapter resolve segredo sozinho:\n  "
                          + "\n  ".join(culpados))


def test_no_adapter_reads_credential_material_from_the_environment():
    """`os.environ` continua permitido -- para o que nao e segredo.

    O que nao pode e um adapter procurar CREDENCIAL no ambiente por conta
    propria: seria o caminho legado sem nem passar pela composicao.
    """
    marcas = ("TOKEN", "SECRET", "PASSWORD", "KEY", "CREDENTIAL", "API")
    culpados = []
    for path in sorted(ADAPTERS.rglob("*.py")):
        if path.as_posix() in FONTE:
            continue
        for no in ast.walk(_arvore(path)):
            # Uma LEITURA de os.environ: `os.environ[X]` ou `os.environ.get(X)`.
            # Uma mensagem de erro que fala em ambiente nao e leitura nenhuma --
            # e a de `headless.py` promete justamente o contrario.
            alvo = ""
            if (isinstance(no, ast.Subscript)
                    and "environ" in ast.unparse(no.value)):
                alvo = ast.unparse(no.slice)
            elif (isinstance(no, ast.Call)
                  and isinstance(no.func, ast.Attribute)
                  and no.func.attr == "get"
                  and "environ" in ast.unparse(no.func.value)):
                alvo = ast.unparse(no.args[0]) if no.args else ""
            if alvo and any(m in alvo.upper() for m in marcas):
                culpados.append(f"{path.as_posix()}:{no.lineno}: {alvo[:50]}")
    assert not culpados, ("adapter procura credencial no ambiente:\n  "
                          + "\n  ".join(culpados))


def test_only_the_source_opens_a_secret_file():
    """Um adapter que abra arquivo de credencial e outra porta."""
    culpados = []
    for path in sorted(ADAPTERS.rglob("*.py")):
        if path.as_posix() in FONTE:
            continue
        for n, bruto in _somente_codigo(path):
            codigo = bruto.lower()
            if ("read_text" in codigo or "open(" in codigo) and any(
                    m in codigo for m in ("credential", "secret", "token",
                                          "senha", ".netrc")):
                culpados.append(f"{path.as_posix()}:{n}")
    assert not culpados, ("adapter le arquivo de credencial:\n  "
                          + "\n  ".join(culpados))


# ---------------------------------------------------------------------------
# 3. Construir nao e autorizar
# ---------------------------------------------------------------------------

def test_building_an_agent_resolves_no_material(tmp_path):
    """A construcao produz um objeto SEM segredo dentro.

    Antes, `_auth` resolvia durante o build: o objeto carregava o material em
    memoria pelo resto do processo, com ninguem tendo autorizado nada.
    """
    import sys

    from regente.adapters import registry
    from regente.ports import Capability

    class PortaQueConta:
        def __init__(self):
            self.pedidos = []

        def material(self, use):
            self.pedidos.append(use)
            return "material"

        def allows(self, use):
            return True

    porta = PortaQueConta()
    agente = registry.create(Capability.RUNNER, "headless-agent", {
        "command": [sys.executable, "-c", "pass"], "auth": "resolved_secret",
        "agent_env": ["TOKEN_DO_AGENTE"], "credentials": porta})

    assert porta.pedidos == [], "construir o adapter resolveu material"
    assert agente.sandbox.env == {}
    assert agente.sandbox.credential_env == ("TOKEN_DO_AGENTE",)

    # E so ao montar o ambiente do filho o material e pedido -- uma vez.
    agente._child_env()
    assert porta.pedidos, "o material nunca foi pedido pelo caminho governado"


def test_the_doctor_never_resolves_credential_material(tmp_path, monkeypatch):
    """`doctor` e o comando que todo mundo roda primeiro.

    Se ele resolvesse segredo, seria o caminho mais curto para extrair material
    -- e ninguem estranharia.
    """
    from regente.app.container import _SemCredencial
    from regente.ports.support import CredentialDenied

    porta = _SemCredencial()
    assert porta.allows("qualquer") is False
    with pytest.raises(CredentialDenied):
        porta.material("qualquer")


# ---------------------------------------------------------------------------
# 4. O adapter nao e autoridade
# ---------------------------------------------------------------------------

def test_no_adapter_decides_whether_it_may_use_a_credential():
    """Um adapter que consultasse policy, concessao ou principal seria a
    segunda autoridade que este marco eliminou."""
    # `abilities` FICA DE FORA: e o nome do que um AGENTE sabe fazer
    # (`AgentCapabilities.abilities`), e nao o que um principal PODE fazer. As
    # duas palavras colidem e as duas ideias nao -- um guard que confunde as
    # duas acusa metade dos adapters por existirem.
    proibidos = ("PolicyEngine", "AccessService(", "AccessGrant(",
                 "CredentialService(", "may_decide", "Principal(")
    culpados = []
    for path in sorted(ADAPTERS.rglob("*.py")):
        # `identity/` produz Principal por definicao -- e o que ele faz.
        if "identity" in path.as_posix():
            continue
        for n, codigo in _somente_codigo(path):
            for nome in proibidos:
                if nome in codigo:
                    culpados.append(f"{path.as_posix()}:{n}: {nome}")
    assert not culpados, ("adapter virou autoridade:\n  "
                          + "\n  ".join(culpados))


def test_no_adapter_creates_or_elevates_a_credential():
    proibidos = ("open_credential", "revoke_credential", "AccessGrant(",
                 "Credential(")
    culpados = []
    for path in sorted(ADAPTERS.rglob("*.py")):
        fonte = path.read_text(encoding="utf-8")
        for n, linha in enumerate(fonte.splitlines(), 1):
            codigo = linha.split("#", 1)[0]
            for nome in proibidos:
                if nome in codigo:
                    culpados.append(f"{path.as_posix()}:{n}: {nome}")
    assert not culpados, ("adapter cria credencial:\n  " + "\n  ".join(culpados))


# ---------------------------------------------------------------------------
# 5. O motor continua agnostico
# ---------------------------------------------------------------------------

def test_the_broker_port_is_the_only_credential_surface_an_adapter_sees():
    """Um adapter recebe uma PORTA, e ela responde uma pergunta so.

    Se ele recebesse o servico, teria acesso a `register`, a `revoke` e a
    `listing` -- e um adapter que pode registrar credencial pode conceder
    autoridade a si mesmo.
    """
    from regente.engine.credentials import BoundBroker
    from regente.ports.support import CredentialBroker

    assert issubclass(BoundBroker, CredentialBroker)
    publico = {n for n in dir(CredentialBroker) if not n.startswith("_")}
    assert "material" in publico and "allows" in publico
    for proibido in ("register", "revoke", "listing", "store", "policy"):
        assert proibido not in publico, (
            f"a porta expoe '{proibido}' ao adapter")


def test_a_new_adapter_naturally_needs_the_governed_path():
    """Nao ha API conveniente que permita escapar.

    A unica forma de um adapter alcancar material e receber uma porta e chamar
    `material(use)`. Nao existe funcao publica no motor que devolva segredo sem
    passar por `resolve` -- e este teste procura uma.
    """
    culpados = []
    for no in ast.walk(_arvore(ENGINE / "credentials.py")):
        if not isinstance(no, ast.FunctionDef) or no.name.startswith("_"):
            continue
        devolve_material = any(
            isinstance(x, ast.Return) and "material" in ast.unparse(x)
            for x in ast.walk(no))
        if devolve_material and no.name not in ("resolve", "material",
                                                "use_secret"):
            culpados.append(f"credentials.py:{no.lineno}: {no.name}")
    assert not culpados, ("funcao publica devolve material fora do caminho:\n  "
                          + "\n  ".join(culpados))


def test_the_engine_grants_itself_no_authority(tmp_path, monkeypatch):
    """O motor e um principal como qualquer outro: identidade, e o que uma
    concessao gravada disser.

    Uma excecao para o processo automatico seria exatamente a segunda
    autoridade que este marco eliminou -- e a mais confortavel de justificar,
    porque "o motor precisa funcionar".
    """
    from regente.app import config as config_mod
    from regente.app import container

    (tmp_path / "board").mkdir(exist_ok=True)
    (tmp_path / "policies.yaml").write_text(
        "rules:\n  - name: tudo\n    effect: ALLOW\n"
        "    match: {action: '*'}\n", encoding="utf-8")
    (tmp_path / "regente.yaml").write_text(
        "organization: org\nclient: cli\nworkspace: ws\nautonomy: L1\n"
        "shadow: true\nroot: .regente\npolicies: policies.yaml\n"
        "providers:\n"
        "  tasks:\n    name: filesystem\n    directory: board\n"
        "  workspace_provider:\n    name: directory\n"
        "  runner:\n    name: script\n"
        "projects:\n- name: P\n  default_environment: staging\n",
        encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    motor = container.build(config_mod.load(tmp_path / "regente.yaml"))
    try:
        quem = motor.engine_principal()
        # Tem identidade: e um principal, com provedor e sujeito proprios.
        assert quem.authenticated is True
        assert quem.provider == "engine"
        assert quem.ref.key.startswith("engine:")
        # E nao tem autoridade nenhuma ate alguem conceder.
        assert dict(quem.abilities) == {}, (
            "o motor concedeu autoridade a si mesmo")
    finally:
        motor.close()


def test_the_broker_the_composition_builds_carries_no_implicit_authority(
        tmp_path, monkeypatch):
    """A porta que a composicao entrega ao adapter nasce sem autoridade.

    E o ponto exato onde seria mais facil abrir uma excecao: a composicao ja
    tem o store, ja tem a policy, e conceder ali "para o motor funcionar"
    pareceria pragmatico. A prova e que ela nao faz isso.
    """
    from regente.app import config as config_mod
    from regente.app import container
    from regente.core.credential import Use
    from regente.ports.support import CredentialDenied

    (tmp_path / "board").mkdir(exist_ok=True)
    (tmp_path / "policies.yaml").write_text(
        "rules:\n  - name: tudo\n    effect: ALLOW\n"
        "    match: {action: '*'}\n", encoding="utf-8")
    (tmp_path / "regente.yaml").write_text(
        "organization: org\nclient: cli\nworkspace: ws\nautonomy: L1\n"
        "shadow: true\nroot: .regente\npolicies: policies.yaml\n"
        "providers:\n"
        "  tasks:\n    name: filesystem\n    directory: board\n"
        "  workspace_provider:\n    name: directory\n"
        "  runner:\n    name: script\n"
        "projects:\n- name: P\n  default_environment: staging\n",
        encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    motor = container.build(config_mod.load(tmp_path / "regente.yaml"))
    try:
        porta = motor.credentials().broker(
            motor.engine_principal(), motor.workspace.id, "tasks")
        assert porta.allows(Use.TASK_READ) is False
        with pytest.raises(CredentialDenied) as erro:
            porta.material(Use.TASK_READ)
        # Sem concessao, o motor nem chega a existencia da credencial.
        assert erro.value.refusal == "NOT_FOUND"
    finally:
        motor.close()


def test_the_composition_reads_the_engines_authority_and_never_grants_it(
        tmp_path, monkeypatch):
    """A montagem que a composicao usa para os adapters, exercitada.

    Ela vivia numa closure que nenhum teste alcancava -- e o sweep de mutacao
    conseguiu conceder `owner` ao motor ali dentro sem nada perceber. Agora e
    uma funcao so, usada pelos dois caminhos, e esta e a prova dela.
    """
    from regente.app import config as config_mod
    from regente.app.container import _engine_broker
    from regente.core.credential import Use
    from regente.engine.store_sqlite import SqliteStore

    (tmp_path / "board").mkdir(exist_ok=True)
    (tmp_path / "policies.yaml").write_text(
        "rules:\n  - name: tudo\n    effect: ALLOW\n"
        "    match: {action: '*'}\n", encoding="utf-8")
    (tmp_path / "regente.yaml").write_text(
        "organization: org\nclient: cli\nworkspace: ws\nautonomy: L1\n"
        "shadow: true\nroot: .regente\npolicies: policies.yaml\n"
        "providers:\n"
        "  tasks:\n    name: filesystem\n    directory: board\n"
        "  workspace_provider:\n    name: directory\n"
        "  runner:\n    name: script\n"
        "projects:\n- name: P\n  default_environment: staging\n",
        encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    cfg = config_mod.load(tmp_path / "regente.yaml")

    from regente.core.model import Workspace
    from regente.core.policy import AutonomyLevel

    store = SqliteStore(cfg.banco)
    store.migrate()
    ws = Workspace(id="wks_x", client_id="cli_x", name="ws",
                   max_autonomy=AutonomyLevel.L1)
    store.save_workspace(ws)
    try:
        porta = _engine_broker(store, cfg, ws, "tasks", cfg.projects)
        assert porta.principal.provider == "engine"
        assert dict(porta.principal.abilities) == {}, (
            "a composicao concedeu autoridade ao motor")
        assert porta.allows(Use.TASK_READ) is False
    finally:
        store.close()
