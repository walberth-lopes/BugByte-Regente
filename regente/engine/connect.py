# -*- coding: utf-8 -*-
"""Conectar um servico: um pedido, e todas as barreiras de sempre.

Este servico existe para apagar cinco passos manuais, e nao para apagar
nenhuma barreira. Ele nao grava nada por conta propria: a configuracao vai pelo
`SettingsService` e a credencial pelo `CredentialService`, os mesmos que a CLI
usa. Se um dos dois recusar, conectar recusa -- e a recusa vem de quem tem
autoridade para dar, e nao daqui.

    identidade -> concessao -> capacidade -> policy -> servico -> auditoria

O que este modulo acrescenta e a ORDEM. Antes, ligar o GitHub era: escolher um
adapter numa tela, digitar a organizacao, ir a outra tela, registrar uma
credencial digitando `helper:gh`, voltar, e so entao buscar. Cada um desses
passos era uma decisao que o Regente podia ter tomado sozinho.

O que ele NAO acrescenta e caminho ate segredo. O conector propoe uma
REFERENCIA (`helper:gh`); o material continua saindo so pelo broker, no momento
do uso, como sempre saiu.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from ..core.credential import uses_from
from .credentials import Refusal


@dataclass(frozen=True, slots=True)
class ConnectOutcome:
    """O que aconteceu. Nunca traz material."""
    accepted: bool
    reason: str = ""
    refusal: Refusal | None = None
    #: Que servico, e em que papel da configuracao ele entrou.
    connector: str = ""
    role: str = ""
    #: Preenchido quando ha uma credencial no caminho impedindo. E o id, para a
    #: tela poder oferecer substituir sem a pessoa ir procurar.
    credential_id: str = ""
    #: Conectou, e algo ficou faltando. Um CODIGO, e nao uma frase: quem
    #: escreve portugues e a tela, que ja traduz todo o resto do vocabulario do
    #: motor. Uma frase daqui apareceria sem acento no meio de uma interface.
    aviso: str = ""
    #: O detalhe tecnico do aviso, para quem for investigar.
    aviso_detalhe: str = ""

    def as_dict(self) -> dict:
        return {"accepted": self.accepted, "detail": self.reason,
                "connector": self.connector, "role": self.role,
                "credential_id": self.credential_id, "warning": self.aviso,
                "warning_detail": self.aviso_detalhe,
                "refusal": self.refusal.value if self.refusal else ""}


def _no(refusal: Refusal, reason: str, **kw) -> ConnectOutcome:
    return ConnectOutcome(accepted=False, refusal=refusal, reason=reason, **kw)


@dataclass(slots=True)
class ConnectService:
    """Ligar um servico ao workspace, do jeito que uma pessoa esperaria.

    `conectores` vem da COMPOSICAO -- um dicionario de portas ja construidas.
    Este modulo nao sabe o que e um GitHub, nao importa adapter, e nao executa
    nada: ele pergunta ao conector o que falta, e grava o que ele propoe pelas
    portas de sempre.
    """
    settings: Any
    credentials: Any
    #: Para autorizar o MOTOR a usar o que acabou de ser conectado. Opcional:
    #: sem ele, conectar continua funcionando e avisa que o tick nao vai usar.
    access: Any = None
    #: `{nome: Conector}`, pronto.
    conectores: dict[str, Any] = field(default_factory=dict)
    #: O que o `regente.yaml` declara em `providers`. Entregue por quem le
    #: arquivo -- o motor nao le configuracao.
    providers_do_arquivo: Callable[[], dict] = dict

    # ------------------------------------------------------------------
    # leitura
    # ------------------------------------------------------------------

    def listar(self, actor, workspace_id: str) -> list[dict]:
        """Os servicos conectaveis, e o que falta em cada um.

        Leitura livre: ver o que EXISTE para conectar nao conta nada sobre este
        workspace. O que ja esta conectado vem da configuracao efetiva, que a
        tela ja podia ler.
        """
        if not actor.may_read(workspace_id):
            return []

        configurados = self._providers(workspace_id)
        saida = []
        for nome, c in sorted(self.conectores.items()):
            atual = configurados.get(c.papel) or {}
            passo = c.estado()
            saida.append({
                "connector": nome,
                "title": c.titulo,
                "description": c.descricao,
                "role": c.papel,
                # "Conectado" aqui significa: a configuracao aponta para ESTE
                # adapter. Nao significa que a credencial serve -- quem responde
                # isso e a tela de conexoes, que prova de verdade.
                "connected": str(atual.get("name") or "") == c.name,
                "current": atual.get("org") or atual.get("site") or "",
                "step": passo.as_dict(),
            })
        return saida

    def contas(self, actor, workspace_id: str, nome: str) -> list[dict]:
        c = self._conector(nome)
        if c is None or not actor.may_read(workspace_id):
            return []
        return [x.as_dict() for x in c.contas()]

    # ------------------------------------------------------------------
    # o navegador
    # ------------------------------------------------------------------

    def autorizar(self, actor, workspace_id: str, nome: str) -> dict:
        """Dispara a autorizacao do servico. NAO grava nada.

        Exige a mesma autoridade de conectar, e nao apenas leitura: abrir uma
        janela de autorizacao no computador de quem esta rodando o motor e um
        efeito no mundo, mesmo sem tocar no banco.
        """
        c = self._conector(nome)
        if c is None:
            return {"code": "desconhecido",
                    "title": f"nao conheco um servico chamado '{nome}'"}
        recusa = self._pode_escrever(actor, workspace_id)
        if recusa is not None:
            return {"code": "recusado", "title": recusa.reason}
        return c.autorizar().as_dict()

    # ------------------------------------------------------------------
    # a escrita
    # ------------------------------------------------------------------

    def conectar(self, actor, workspace_id: str, nome: str, conta: str = "",
                 substituir: bool = False) -> ConnectOutcome:
        """Grava a configuracao e a credencial. Nesta ordem, e pelas portas.

        A CREDENCIAL VEM PRIMEIRO de proposito. Uma configuracao apontando para
        um servico que nao tem como ser alcancado deixa o workspace num estado
        que parece pronto e nao esta -- e o proximo tick e que descobre.
        """
        c = self._conector(nome)
        if c is None:
            return _no(Refusal.NOT_FOUND,
                       f"nao conheco um servico chamado '{nome}'")

        try:
            proposta = c.proposta(conta)
        except Exception as e:                            # noqa: BLE001
            return _no(Refusal.INVALID, str(e), connector=nome)

        if proposta.precisa_credencial:
            saida = self._credencial(actor, workspace_id, c, proposta,
                                     substituir)
            if saida is not None:
                return saida

        providers = dict(self._providers(workspace_id))
        providers[proposta.papel] = dict(proposta.provider)
        gravada = self.settings.put(actor, workspace_id, "providers", providers)
        if not gravada.accepted:
            # `SettingsService` recusa com o MESMO vocabulario, em texto. Uma
            # recusa que ele conheca e este enum nao seria um defeito de
            # traducao mascarado de negativa -- por isso o `try`, e nao um
            # `.get` com um default simpatico.
            try:
                recusa = Refusal(gravada.refusal)
            except ValueError:
                recusa = Refusal.INVALID
            return _no(recusa, gravada.reason, connector=nome,
                       role=proposta.papel)

        return ConnectOutcome(
            accepted=True, connector=nome, role=proposta.papel,
            reason=f"{c.titulo} conectado; escolha o que este workspace usa",
            **self._motor_pode_usar(actor, workspace_id))

    # ------------------------------------------------------------------
    # dentro
    # ------------------------------------------------------------------

    def _motor_pode_usar(self, actor, workspace_id: str) -> dict:
        """Garante que o MOTOR alcance a conexao nos ciclos automaticos.

        Devolve `{}` quando esta tudo certo, ou o codigo do que ficou faltando.

        POR QUE AQUI: quem conecta esta dizendo "o Regente pode usar isto". Sem
        a concessao ao principal de servico, o tick recusa credencial e a fila
        para -- longe daqui, sem ninguem por perto, e sem nada na tela ligando
        uma coisa a outra.

        POR QUE NAO E SILENCIOSO: passa pelo `AccessService` com quem conectou
        como concedente, entra na trilha, e sai por `regente access revogar`
        como qualquer outra. Ampliar autoridade continua sendo uma decisao
        registrada -- o que deixa de existir e o comando escondido.
        """
        if self.access is None:
            return {}
        from ..core.access import Ability, PrincipalRef

        motor = PrincipalRef(provider="engine", subject=workspace_id)
        ja = self.access.abilities_for(motor).get(workspace_id, frozenset())
        if Ability.CREDENTIAL_USE in ja:
            return {}

        saida = self.access.grant(
            actor, workspace_id, motor, "service",
            note="autorizado ao conectar um servico")
        if saida.accepted:
            return {}
        return {"aviso": "motor_sem_acesso",
                "aviso_detalhe": (
                    f"{saida.reason} -- conceda com: regente access conceder "
                    f"engine:{workspace_id} --papel service")}

    def _conector(self, nome: str):
        return self.conectores.get(str(nome).strip())

    def _providers(self, workspace_id: str) -> dict:
        """A configuracao efetiva de providers: o arquivo, com a tela por cima.

        Ler so a sobreposicao apagaria o que o `regente.yaml` declara -- e
        conectar um servico deixaria o workspace sem os outros.
        """
        do_arquivo = dict(self.providers_do_arquivo() or {})
        sobreposto = (self.settings.overlay(workspace_id).get("providers")
                      if self.settings is not None else None)
        if isinstance(sobreposto, dict):
            do_arquivo.update(sobreposto)
        return do_arquivo

    def _pode_escrever(self, actor, workspace_id: str):
        """Quem pode configurar pode disparar a autorizacao. Nada e gravado.

        Identidade e capacidade, e nao policy: a policy avalia ACOES sobre o
        workspace, e abrir uma janela de navegador nao e uma delas. Chamar
        `workspace.settings.write` aqui pediria autoridade para uma escrita que
        nao vai acontecer -- e a escrita de verdade, logo depois, e avaliada
        pelo `SettingsService`, como sempre foi.
        """
        from ..core.access import Ability

        if not actor.authenticated:
            return _no(Refusal.UNAUTHENTICATED,
                       "esta requisicao nao foi autenticada")
        if not actor.can(workspace_id, Ability.SETTINGS_WRITE):
            return _no(Refusal.NOT_FOUND, "recurso nao encontrado neste escopo")
        return None

    def _credencial(self, actor, workspace_id: str, conector, proposta,
                    substituir: bool) -> ConnectOutcome | None:
        """Registra a credencial. `None` quando seguiu adiante.

        O caso interessante e o CONFLITO: ja existe uma credencial viva para
        este papel. Trata-la como sucesso seria o defeito silencioso deste
        fluxo -- uma credencial antiga sem a permissao de LISTAR deixaria o
        botao dizer "conectado" e a busca recusar depois, longe daqui.
        """
        saida = self.credentials.register(
            actor, workspace_id, proposta.nome_credencial, proposta.papel,
            proposta.secret_ref, list(proposta.capacidades),
            note=f"registrada ao conectar {conector.titulo}")
        if saida.accepted:
            return None
        if saida.refusal is not Refusal.CONFLICT:
            return _no(saida.refusal or Refusal.INVALID, saida.reason,
                       connector=conector.nome, role=proposta.papel)

        # Ha uma viva. Ela serve?
        existente = self._viva(actor, workspace_id, proposta)
        if existente is None:
            return _no(Refusal.CONFLICT, saida.reason,
                       connector=conector.nome, role=proposta.papel)

        precisa = uses_from(proposta.capacidades)
        if precisa <= set(existente.capabilities) and not substituir:
            # Ja da conta. Reconectar para trocar de organizacao nao deveria
            # obrigar ninguem a mexer numa credencial que esta boa.
            return None

        if not substituir:
            faltando = sorted(u.value for u in precisa
                              if u not in existente.capabilities)
            return _no(
                Refusal.CONFLICT,
                f"ja existe uma credencial para '{proposta.papel}' sem "
                f"{', '.join(faltando)}. Substituir revoga a atual e registra "
                f"uma nova com as permissoes que faltam.",
                connector=conector.nome, role=proposta.papel,
                credential_id=existente.id)

        revogada = self.credentials.revoke(
            actor, workspace_id, existente.id,
            reason=f"substituida ao reconectar {conector.titulo}")
        if not revogada.accepted:
            return _no(revogada.refusal or Refusal.CONFLICT, revogada.reason,
                       connector=conector.nome, role=proposta.papel)

        de_novo = self.credentials.register(
            actor, workspace_id, proposta.nome_credencial, proposta.papel,
            proposta.secret_ref, list(proposta.capacidades),
            note=f"substituida ao reconectar {conector.titulo}")
        if not de_novo.accepted:
            return _no(de_novo.refusal or Refusal.INVALID, de_novo.reason,
                       connector=conector.nome, role=proposta.papel)
        return None

    def _viva(self, actor, workspace_id: str, proposta):
        listagem = self.credentials.listing(actor, workspace_id)
        if not isinstance(listagem, list):
            return None
        from ..core.credential import Status

        agora = self.credentials.clock()
        for c in listagem:
            if c.provider == proposta.papel and c.status(agora) is Status.ACTIVE:
                return c
        return None
