# -*- coding: utf-8 -*-
"""Registro de adapters: nome na configuracao -> fabrica.

Este e o **unico** modulo do motor que importa adapters, e ele fica fora do Core
e do Engine de proposito. Adicionar um provedor novo e adicionar uma entrada
aqui; se algum dia for preciso mexer em `core/` ou `engine/` para isso, a
abstracao falhou -- e o teste de fronteira acusa.

O import e tardio (dentro da fabrica) para que um adapter com dependencia pesada
nao seja exigido de quem nao o usa: o motor precisa subir num ambiente sem SDK
de nuvem nenhum.
"""

from __future__ import annotations

from typing import Any, Callable

from ..ports import Capability, Port

#: Adapters que NAO alcancam nada fora desta maquina, e portanto nao precisam
#: de credencial nenhuma.
#:
#: Declarado aqui porque saber isso e conhecimento de fornecedor, e fornecedor
#: mora nesta camada. A tela pergunta; ela nao adivinha pelo nome do papel --
#: cobrar credencial de um provider de arquivos e o mesmo erro de dizer
#: "conectado" para quem so tem configuracao, invertido.
LOCAL_ADAPTERS: frozenset[str] = frozenset({
    "filesystem", "directory", "clone", "worktree", "git-local", "script",
    "deterministic-agent", "console",
})


def needs_credential(adapter_name: str) -> bool:
    """Este adapter precisa de credencial para funcionar?

    Desconhecido responde SIM. Um adapter novo que alcance a rede e seja tratado
    como local apareceria como pronto sem credencial -- e a pessoa so descobriria
    no primeiro tick.
    """
    return bool(adapter_name) and adapter_name not in LOCAL_ADAPTERS


Fabrica = Callable[[dict[str, Any]], Port]
_REGISTRO: dict[tuple[Capability, str], Fabrica] = {}


def register(cap: Capability, name: str, fabrica: Fabrica) -> None:
    _REGISTRO[(cap, name)] = fabrica


def create(cap: Capability, name: str, options: dict[str, Any] | None = None) -> Port:
    key = (cap, name)
    if key not in _REGISTRO:
        available = sorted(n for (c, n) in _REGISTRO if c == cap)
        raise KeyError(
            f"nao existe adapter '{name}' para {cap.value}. "
            f"Disponiveis: {', '.join(available) or 'nenhum'}")
    return _REGISTRO[key](options or {})


def has(cap: Capability, name: str) -> bool:
    """Existe adapter deste nome para esta capacidade?

    Pergunta, e nao tentativa: a composicao precisa saber se um provedor
    DESCOBRE recursos sem construir nada nem tratar `KeyError` como resposta
    normal. Um provedor que nao descobre e um caso comum e legitimo -- um
    diretorio de arquivos nao tem o que listar --, e nao um erro.
    """
    return (cap, name) in _REGISTRO


def available(cap: Capability | None = None) -> dict[str, list[str]]:
    output: dict[str, list[str]] = {}
    for (c, n) in sorted(_REGISTRO, key=lambda k: (k[0].value, k[1])):
        if cap is None or c == cap:
            output.setdefault(c.value, []).append(n)
    return output


# ---- fabricas embutidas -------------------------------------------------

def _tasks_filesystem(o: dict[str, Any]) -> Port:
    from .tasks.filesystem import FilesystemTasks
    return FilesystemTasks(o["directory"])


def _workspace_directory(o: dict[str, Any]) -> Port:
    from .workspace.local import IsolatedDirectory
    return IsolatedDirectory(o["root"])


def _workspace_worktree(o: dict[str, Any]) -> Port:
    from .workspace.local import GitWorktree
    return GitWorktree(clones=o.get("clones", {}), root=o["root"])


def _tasks_jira(o: dict[str, Any]) -> Port:
    """Jira Cloud, somente leitura.

    Dois transportes pelo mesmo adapter: `http` fala com o site de verdade,
    `instantaneo` reproduz respostas reais gravadas. O adapter e identico nos
    dois casos -- e por isso o teste de contrato exercita o mesmo codigo que
    roda contra a rede.
    """
    from ..core.credential import Use
    from ..ports.tasks import status_map_from
    from .tasks.jira import JiraTasks
    from .tasks.transport import HttpTransport, SnapshotTransport

    observer = o.get("observer")
    modo = o.get("transport", "http")
    if modo == "snapshot":
        from pathlib import Path
        transport = SnapshotTransport(
            directory=Path(o["snapshots"]), observer=observer)
    elif modo == "http":
        site = o["site"].rstrip("/")
        # A CREDENCIAL vem do caminho governado, e nada mais.
        #
        # Antes, a composicao injetava um `SecretProvider` e este arquivo
        # montava um `lambda` que resolvia uma referencia escrita no YAML. Nao
        # havia identidade, concessao, capacidade, validade nem revogacao -- e
        # funcionava perfeitamente, que e o que tornava o defeito duravel.
        #
        # O usuario NAO passa por aqui: um email e um identificador, nao um
        # segredo, e trata-lo como segredo esconderia o que e realmente secreto.
        broker = o["credentials"]
        usuario = str(o.get("user") or "")
        transport = HttpTransport(
            base_url=site,
            credencial=lambda: (usuario, broker.material(Use.TASK_READ)),
            timeout=int(o.get("timeout", 30)),
            max_attempts=int(o.get("max_tentativas", 3)),
            observer=observer)
    else:
        raise KeyError(f"transport desconhecido para jira: {modo!r}. "
                       f"Use http ou snapshot")

    return JiraTasks(
        transport=transport,
        jql=o.get("jql") or "statusCategory != Done ORDER BY updated DESC",
        resources_by=o.get("resources_by", "parent"),
        max_pages=int(o.get("max_pages", 10)),
        per_page=int(o.get("per_page", 100)),
        status_overrides=status_map_from(o.get("status_map")),
        site=o.get("site", ""))


def _credencial(o: dict[str, Any]) -> dict[str, Any]:
    """A porta governada e o nome sob o qual o material entra no filho.

    Uma so montagem para os tres adapters que disparam a CLI de hospedagem.
    Tres montagens divergiriam, e a que divergisse seria a que esqueceu de
    passar a porta -- e um adapter sem porta procura credencial sozinho, que e
    o defeito que o marco 6.1 fechou.

    `credential_env` so entra quando a configuracao diz: ausente significa "use
    o default do fornecedor", e nao "nenhuma variavel".
    """
    saida: dict[str, Any] = {"credentials": o.get("credentials"),
                             "config_dir": str(o.get("config_dir", ""))}
    nomes = o.get("credential_env")
    if nomes is not None:
        saida["credential_env"] = tuple(str(n) for n in nomes)
    return saida


def _repos_git_local(o: dict[str, Any]) -> Port:
    from .repos.git_local import GitLocal
    return GitLocal(root=o["root"], observer=o.get("observer"),
                    timeout=int(o.get("timeout", 60)))


def _repos_github(o: dict[str, Any]) -> Port:
    from .repos.github import GitHubRepos
    return GitHubRepos(org=o["org"], cli_path=o.get("cli", "gh"),
                       observer=o.get("observer"),
                       timeout=int(o.get("timeout", 60)),
                       list_limit=int(o.get("limit", 200)),
                       **_credencial(o))


def _agent_headless(o: dict[str, Any]) -> Port:
    """Any headless agent that speaks JSON on stdin/stdout, sandboxed.

    This is the factory for an agent reached over an API or through a corporate
    gateway: the process is whatever the client wrote, and the engine resolves
    the credential it needs and hands it over in a composed environment.
    """
    from .runner.headless import HeadlessAgent, SandboxProfile

    mode, env, variaveis, broker = _auth({**o, "auth": o.get("auth", "none")})
    return HeadlessAgent(
        command=list(o["command"]),
        auth_mode=mode,
        sandbox=SandboxProfile(
            allowed_tools=tuple(o.get("tools", ())),
            denied_tools=tuple(o.get("denied_tools", ())),
            allow_command_execution=bool(o.get("allow_command_execution", False)),
            allow_network=bool(o.get("allow_network", False)),
            env=env,
            credential_env=variaveis, broker=broker,
            max_cost_usd=float(o.get("max_cost_usd", 2.0))))


def _auth(o: dict[str, Any]) -> tuple[Any, dict[str, str], tuple[str, ...], Any]:
    """Resolve whatever THIS workspace's agent authenticates with.

    Five shapes, and the engine picks none of them: the workspace's
    configuration does. A client with a corporate coding-agent subscription
    writes `auth: session` and the engine resolves nothing. A client with a key
    writes `auth: resolved_secret` and names the reference. A client behind an
    internal gateway writes `auth: gateway` and names its own variables.

    The credential values never appear here -- only references, resolved through
    the workspace's own SecretProvider, which is what keeps one client's
    credential out of another client's agent.
    """
    from ..ports.agent import AuthMode

    mode = AuthMode(str(o.get("auth", "session")).upper())
    broker = o.get("credentials")
    env: dict[str, str] = {}

    # `agent_env` names the variables the agent expects. O VALOR nao e resolvido
    # aqui: cada nome vira uma promessa que o adapter cumpre NO MOMENTO DE
    # RODAR, pelo caminho governado.
    #
    # Antes, isto resolvia o material durante a CONSTRUCAO do adapter -- antes
    # de existir identidade, antes de a policy ser consultada, antes de qualquer
    # decisao sobre se aquele uso era autorizado. Um objeto construido carregava
    # o segredo em memoria pelo resto do processo, e ninguem havia autorizado
    # nada.
    variaveis = tuple(str(v) for v in (o.get("agent_env") or ()))

    if mode in (AuthMode.RESOLVED_SECRET, AuthMode.GATEWAY) and not variaveis:
        raise KeyError(
            f"auth mode '{mode.value}' needs an `agent_env` list of "
            f"variable -> secret reference; none was configured")
    return mode, env, variaveis, broker


def _agent_claude_code(o: dict[str, Any]) -> Port:
    """One headless coding-agent CLI. The vendor's name stops at this factory."""
    from .runner.vendors.claude_code import ClaudeCodeAgent, restricted_profile

    mode, env, variaveis, broker = _auth(o)
    return ClaudeCodeAgent(
        cli_path=o["cli"],
        model=str(o.get("model", "sonnet")),
        auth_mode=mode,
        sandbox=restricted_profile(
            max_cost_usd=float(o.get("max_cost_usd", 2.0)), env=env,
            credential_env=variaveis, broker=broker),
        extra_args=tuple(o.get("extra_args", ())))


def _agent_codex_cli(o: dict[str, Any]) -> Port:
    """A second headless coding-agent CLI, to prove the swap is a config line.

    Identical shape to the factory above and to nothing above `adapters/`. If
    adding this had required a change in `core/`, `ports/` or `engine/`, the
    abstraction would have failed and the boundary test would say so.
    """
    from .runner.vendors.codex_cli import CodexCliAgent, restricted_profile

    mode, env, variaveis, broker = _auth(o)
    return CodexCliAgent(
        cli_path=o["cli"],
        model=str(o.get("model", "")),
        auth_mode=mode,
        sandbox=restricted_profile(
            max_cost_usd=float(o.get("max_cost_usd", 2.0)), env=env,
            credential_env=variaveis, broker=broker),
        extra_args=tuple(o.get("extra_args", ())))


def _agent_deterministic(o: dict[str, Any]) -> Port:
    from .runner.external import DeterministicAgent
    return DeterministicAgent(
        script=dict(o.get("script", {})),
        fallback=dict(o.get("fallback", {})) or {
            "status": "NO_PROGRESS",
            "summary": "no edit declared for this task"})


def _repos_github_write(o: dict[str, Any]) -> Port:
    from .repos.github_write import GitHubWrite
    return GitHubWrite(org=o["org"], cli_path=o.get("cli", "gh"),
                       observer=o.get("observer"), **_credencial(o))


def _cicd_github(o: dict[str, Any]) -> Port:
    from .cicd.github_checks import GitHubChecks
    return GitHubChecks(org=o["org"], cli_path=o.get("cli", "gh"),
                        observer=o.get("observer"), **_credencial(o))


def _scoped_secrets(o: dict[str, Any]) -> Port:
    """DESREGISTRADO no marco 16. Mantido como funcao, sem porta.

    Enquanto esta fabrica estava registrada, qualquer composicao podia pedir um
    `SecretProvider` inteiro pelo nome e entrega-lo a um adapter -- que e
    exatamente o caminho legado que este marco eliminou. Quem monta a fonte de
    segredo agora e o `CredentialService`, e ele e o unico.
    """
    from .secrets import ScopedSecrets
    return ScopedSecrets(allowed_from=frozenset(o.get("allowed", ())),
                    workspace=o.get("workspace", "?"))


def _workspace_clone(o: dict[str, Any]) -> Port:
    from .workspace.local import GitClone
    return GitClone(root=o["root"], sources=o.get("sources", {}),
                    remotes=o.get("remotes", {}),
                    credentials=o.get("credentials"),
                    credential_user=str(o.get("credential_user")
                                        or "x-access-token"))


def _notify_console(o: dict[str, Any]) -> Port:
    from .notify.console import Console
    return Console(journal=o.get("journal"))


def _runner_script(o: dict[str, Any]) -> Port:
    from .runner.scripted import ScriptedAgent
    return ScriptedAgent(
        script=o.get("script", {}),
        default_value=o.get("fallback", {
            "status": "NO_PROGRESS", "summary": "no outcome declared"}),
        leave_trace=bool(o.get("leave_trace", True)))


def _discovery_github(o: dict[str, Any]) -> Port:
    """Descoberta no GitHub, sobre o provider de repositorio ja composto.

    Recebe o provider pronto em vez de construir outro: dois objetos falando
    com o mesmo GitHub teriam duas configuracoes, e a que divergisse seria
    justamente a que ninguem revisou.
    """
    from .discovery import GitHubDiscovery
    return GitHubDiscovery(repos=o["repos"], org=o["org"])


def _discovery_jira(o: dict[str, Any]) -> Port:
    """Descoberta num site Jira, com transporte e credencial PROPRIOS.

    O transporte nao e o mesmo que le issues, e a diferenca esta numa palavra:
    a capacidade pedida ao broker. Ler issues pede `task.read`; listar projetos
    pede `task.discover`. Compartilhar o transporte faria as duas capacidades
    virarem uma, e conectar o Jira passaria a conceder leitura do board inteiro.
    """
    from ..core.credential import Use
    from .discovery import JiraDiscovery
    from .tasks.transport import HttpTransport

    site = str(o["site"]).rstrip("/")
    broker = o["credentials"]
    usuario = str(o.get("user") or "")
    return JiraDiscovery(
        transport=HttpTransport(
            base_url=site,
            credencial=lambda: (usuario, broker.material(Use.TASK_DISCOVER)),
            timeout=int(o.get("timeout", 30)),
            max_attempts=int(o.get("max_tentativas", 3)),
            observer=o.get("observer")),
        site=site,
        max_results=int(o.get("descoberta_limite", 100)))


register(Capability.DISCOVERY, "github", _discovery_github)
register(Capability.DISCOVERY, "jira", _discovery_jira)
register(Capability.TASKS, "filesystem", _tasks_filesystem)
register(Capability.TASKS, "jira", _tasks_jira)
register(Capability.REPOSITORY, "git-local", _repos_git_local)
register(Capability.REPOSITORY, "github", _repos_github)
register(Capability.REPOSITORY, "github-write", _repos_github_write)
register(Capability.CICD, "github-checks", _cicd_github)
register(Capability.WORKSPACE, "directory", _workspace_directory)
register(Capability.WORKSPACE, "worktree", _workspace_worktree)
register(Capability.WORKSPACE, "clone", _workspace_clone)
register(Capability.NOTIFICATION, "console", _notify_console)
register(Capability.RUNNER, "script", _runner_script)
register(Capability.RUNNER, "headless-agent", _agent_headless)
register(Capability.RUNNER, "claude-code", _agent_claude_code)
register(Capability.RUNNER, "codex-cli", _agent_codex_cli)
register(Capability.RUNNER, "deterministic-agent", _agent_deterministic)


# =========================================================================
# CATALOGO
#
# O que cada adapter E, e o que ele precisa saber para funcionar.
#
# Isto existe para que uma TELA possa oferecer um formulario em vez de uma
# caixa de JSON. A alternativa seria o frontend carregar a lista de campos de
# cada fornecedor -- e nesse dia haveria duas definicoes do formato do Jira, a
# do motor e a da tela, e a que divergisse aceitaria o que o motor recusa.
#
# Mora AQUI, e nao em `core/` ou `engine/`, pelo mesmo motivo que
# `LOCAL_ADAPTERS`: saber que o Jira precisa de um endereco de site e de um
# email e conhecimento de fornecedor, e fornecedor mora nesta camada.
#
# O catalogo NAO substitui a validacao. Ele descreve o que perguntar; quem
# recusa continua sendo o motor, com as mensagens dele.
#
# E ele e a UNICA parte do pacote que escreve portugues acentuado. O resto usa
# ASCII porque suas mensagens vao parar num console do Windows, que nem sempre
# esta em UTF-8. Estes textos nao sao impressos em lugar nenhum: sao
# serializados em JSON e lidos por uma tela. Texto de produto sem acento numa
# interface em portugues le-se como descuido.
# =========================================================================

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Campo:
    """Uma coisa que a pessoa precisa informar para este adapter funcionar.

    `ajuda` nao e enfeite: quem instala o Regente pela primeira vez nao sabe o
    que e um "site do Jira" nem onde encontrar o proprio. Um formulario com
    rotulos e sem explicacao apenas move o problema do YAML para a tela.
    """
    chave: str
    rotulo: str
    ajuda: str = ""
    tipo: str = "texto"          # texto | numero | booleano | caminho | lista
    obrigatorio: bool = False
    exemplo: str = ""
    padrao: object = None
    #: Quando verdadeiro, este campo e para quem ja sabe o que esta fazendo, e a
    #: tela o guarda atras de "opcoes avancadas".
    avancado: bool = False


@dataclass(frozen=True, slots=True)
class Oferta:
    """Um adapter do ponto de vista de quem ESCOLHE, e nao de quem o escreve."""
    nome: str
    rotulo: str
    descricao: str
    campos: tuple = ()
    #: O uso de credencial que este adapter exige, ou vazio se nao exige nenhum.
    #: E o valor de `Use`, e nao um nome novo: uma segunda nomenclatura aqui
    #: seria a tabela de traducao onde as duas divergem em silencio.
    uso: str = ""
    #: Um adapter local nao alcanca nada fora desta maquina.
    local: bool = False

    def as_dict(self) -> dict:
        return {
            "name": self.nome, "label": self.rotulo,
            "description": self.descricao,
            "needs_credential": not self.local,
            "credential_use": self.uso,
            "fields": [
                {"key": c.chave, "label": c.rotulo, "help": c.ajuda,
                 "kind": c.tipo, "required": c.obrigatorio,
                 "example": c.exemplo, "default": c.padrao,
                 "advanced": c.avancado}
                for c in self.campos],
        }


#: O que cada PAPEL faz no Regente, dito para quem nunca leu a arquitetura.
PAPEIS: tuple[tuple[str, str, str, bool], ...] = (
    ("tasks", "Board de tasks",
     "De onde o Regente lê o trabalho a fazer.", True),
    ("repository", "Repositório",
     "Para o Regente enxergar o código, as branches e o histórico.", False),
    ("repository_write", "Publicação de mudanças",
     "Para enviar commits e abrir pull requests com o trabalho pronto.", False),
    ("cicd", "Integração contínua",
     "Para acompanhar os checks que rodam em cada commit.", False),
    ("runner", "Agente",
     "O modelo que lê a task e escreve a mudança.", True),
    ("workspace_provider", "Área de trabalho",
     "Onde cada execução fica isolada, nesta máquina.", False),
)

#: Qual `Capability` do registro atende cada papel da configuracao.
CAPACIDADE_DO_PAPEL: dict[str, Capability] = {
    "tasks": Capability.TASKS,
    "repository": Capability.REPOSITORY,
    "repository_write": Capability.REPOSITORY,
    "cicd": Capability.CICD,
    "runner": Capability.RUNNER,
    "workspace_provider": Capability.WORKSPACE,
}

_TIMEOUT = Campo("timeout", "Tempo limite por chamada (s)", tipo="numero",
                 padrao=30, avancado=True)

CATALOGO: dict[str, tuple[Oferta, ...]] = {
    "tasks": (
        Oferta(
            "jira", "Jira",
            "Lê as tasks de um projeto no Jira Cloud. Somente leitura: o "
            "Regente nunca altera o seu board.",
            uso="task.read",
            campos=(
                Campo("site", "Endereço do seu Jira",
                      "O endereço que você usa no navegador, sem barra no fim.",
                      obrigatorio=True,
                      exemplo="https://suaempresa.atlassian.net"),
                Campo("user", "Seu email no Jira",
                      "O email da conta a que a credencial pertence. Um email é "
                      "um identificador, e não um segredo — o segredo fica na "
                      "credencial.",
                      obrigatorio=True, exemplo="voce@suaempresa.com"),
                Campo("jql", "Quais tasks buscar (JQL)",
                      "A busca que o Regente executa no Jira. O padrão traz "
                      "tudo que não está concluído.",
                      exemplo="project = SG AND statusCategory != Done",
                      padrao="statusCategory != Done ORDER BY updated DESC"),
                Campo("per_page", "Tasks por página", tipo="numero",
                      padrao=100, avancado=True),
                Campo("max_pages", "Máximo de páginas por ciclo", tipo="numero",
                      padrao=10, avancado=True),
                _TIMEOUT,
            )),
        Oferta(
            "filesystem", "Arquivos nesta máquina",
            "Lê tasks de arquivos YAML numa pasta. Serve para experimentar o "
            "Regente sem conectar nada, e não precisa de credencial.",
            local=True,
            campos=(
                Campo("directory", "Pasta das tasks",
                      "Cada arquivo .yaml nesta pasta vira uma task.",
                      obrigatorio=True, tipo="caminho", exemplo="tasks"),
            )),
    ),
    "repository": (
        Oferta(
            "github", "GitHub",
            "Lê repositórios, branches e arquivos de uma organização no GitHub.",
            uso="repo.read",
            campos=(
                Campo("org", "Organização ou usuário",
                      "A parte antes da barra no endereço do repositório.",
                      obrigatorio=True, exemplo="suaempresa"),
                Campo("cli", "Caminho do gh",
                      "Onde está a ferramenta de linha de comando do GitHub.",
                      padrao="gh", avancado=True),
                Campo("limit", "Máximo de repositórios listados", tipo="numero",
                      padrao=200, avancado=True),
            )),
        Oferta(
            "git-local", "Repositórios nesta máquina",
            "Lê clones que já existem numa pasta local. Não alcança a rede.",
            local=True,
            campos=(
                Campo("root", "Pasta com os repositórios", obrigatorio=True,
                      tipo="caminho", exemplo="C:/repos"),
            )),
    ),
    "repository_write": (
        Oferta(
            "github-write", "GitHub",
            "Envia commits e abre pull requests. Esta é a única conexão que "
            "escreve fora desta máquina.",
            uso="repo.pr",
            campos=(
                Campo("org", "Organização ou usuário", obrigatorio=True,
                      exemplo="suaempresa"),
                Campo("cli", "Caminho do gh", padrao="gh", avancado=True),
            )),
    ),
    "cicd": (
        Oferta(
            "github-checks", "GitHub Actions",
            "Lê o resultado dos checks de cada commit. O Regente nunca decide "
            "que um CI passou: ele lê o veredito de quem executou.",
            uso="ci.read",
            campos=(
                Campo("org", "Organização ou usuário", obrigatorio=True,
                      exemplo="suaempresa"),
                Campo("cli", "Caminho do gh", padrao="gh", avancado=True),
            )),
    ),
    "runner": (
        Oferta(
            "claude-code", "Claude Code",
            "Usa o Claude Code instalado nesta máquina para executar as tasks.",
            uso="agent.run",
            campos=(
                Campo("cli", "Caminho do executável", obrigatorio=True,
                      tipo="caminho", exemplo="claude"),
                Campo("model", "Modelo", padrao="sonnet"),
                Campo("max_cost_usd", "Custo máximo por execução (US$)",
                      tipo="numero", padrao=2.0),
                Campo("auth", "Como autenticar",
                      "“session” usa a sessão já aberta na máquina; “credential” "
                      "usa uma credencial registrada aqui.",
                      padrao="session", avancado=True),
            )),
        Oferta(
            "codex-cli", "Codex CLI",
            "Usa o Codex CLI instalado nesta máquina para executar as tasks.",
            uso="agent.run",
            campos=(
                Campo("cli", "Caminho do executável", obrigatorio=True,
                      tipo="caminho", exemplo="codex"),
                Campo("model", "Modelo"),
                Campo("max_cost_usd", "Custo máximo por execução (US$)",
                      tipo="numero", padrao=2.0),
                Campo("auth", "Como autenticar", padrao="session",
                      avancado=True),
            )),
        Oferta(
            "script", "Um programa seu",
            "Chama um programa que você escreveu. Serve para experimentar o "
            "fluxo do Regente sem um modelo, e não precisa de credencial.",
            local=True,
            campos=(
                Campo("command", "Comando", tipo="lista", obrigatorio=True,
                      exemplo="python meu_agente.py"),
            )),
    ),
    "workspace_provider": (
        Oferta(
            "directory", "Uma pasta por execução",
            "Cada execução recebe uma pasta isolada. Não clona repositório "
            "nenhum — serve para experimentar.",
            local=True,
            campos=(
                Campo("root", "Pasta raiz", obrigatorio=True, tipo="caminho",
                      exemplo="areas"),
            )),
        Oferta(
            "clone", "Um clone por execução",
            "Clona o repositório da task numa pasta isolada. É o que permite "
            "commit, push e pull request.",
            local=True,
            campos=(
                Campo("root", "Pasta raiz dos clones", obrigatorio=True,
                      tipo="caminho", exemplo="areas"),
            )),
        Oferta(
            "worktree", "Um worktree por execução",
            "Usa `git worktree` sobre clones que já existem. Mais rápido que "
            "clonar, e exige que os clones já estejam na máquina.",
            local=True,
            campos=(
                Campo("root", "Pasta raiz dos worktrees", obrigatorio=True,
                      tipo="caminho", exemplo="areas"),
            )),
    ),
}


def catalogo() -> dict:
    """O catalogo inteiro, pronto para a API.

    So aparecem ofertas cujo adapter esta REGISTRADO: uma tela que oferecesse
    um provedor que o motor nao sabe criar transformaria uma escolha razoavel
    numa falha no primeiro ciclo.
    """
    papeis = []
    for papel, rotulo, descricao, essencial in PAPEIS:
        cap = CAPACIDADE_DO_PAPEL[papel]
        ofertas = [o.as_dict() for o in CATALOGO.get(papel, ())
                   if (cap, o.nome) in _REGISTRO]
        papeis.append({"role": papel, "label": rotulo,
                       "description": descricao, "essential": essencial,
                       "options": ofertas})
    return {"roles": papeis}
