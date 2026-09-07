# -*- coding: utf-8 -*-
"""O caminho governado ate um segredo. O unico.

Antes deste modulo existia `adapter -> secret`: a composicao montava o adapter e
o valor era resolvido ali mesmo, antes de haver identidade, antes de a policy
ser consultada, antes de existir qualquer decisao sobre *se aquele uso e
autorizado*. Funcionava perfeitamente, e nao passava por lugar nenhum.

Agora a ordem e esta, e nenhuma linha e pulavel:

    identidade    quem esta pedindo?         sem `method`, ninguem provou nada
    acesso        ela manda NESTE workspace? uma concessao viva, do marco 14
    credencial    existe, e e deste escopo?  lida ja escopada, nunca por id solto
    estado        viva? nao expirou?         revogada vence expirada
    capacidade    ESTA credencial pode isso? nao e o que o provider sabe fazer
    policy        a organizacao permite?     autoridade independente das acima
    ---------------------------------------------------------------
    so entao      resolve o material

**O material nunca sobe.** `Resolved` carrega o segredo e uma unica maneira de
alcanca-lo; ele nao vai para o `AccessOutcome`, nao vai para o evento, nao vai
para a resposta HTTP e nao vai para a tela. O que sobe e a decisao.

**`UNKNOWN` nunca vira `ALLOW`.** Uma fonte de segredo indisponivel e uma
recusa, e nao um seguir-em-frente: nao saber se a credencial existe e a unica
situacao em que usa-la seria mais perigoso do que nao usar.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Callable

from ..core import ids
from ..core.access import Ability, PrincipalRef
from ..core.credential import Credential, SecretRef, Status, Use, uses_from
from ..core.model import Event, now
from ..core.policy import (Action, AutonomyLevel, Effect, PolicyContext,
                           PolicyEngine)
from ..core.principal import Principal
from ..ports import AdapterError
from ..ports.store import Store
from ..ports.support import CredentialBroker, CredentialDenied, SecretProvider

#: A trilha de credenciais. Verbos separados de proposito: "concedida" e
#: "usada" respondem perguntas diferentes, e junta-las num `credential.event`
#: obrigaria quem investiga a ler `data` para saber o que aconteceu.
REGISTERED = "credencial_registrada"
REVOKED = "credencial_revogada"
RESOLVED = "credencial_resolvida"
USED = "credencial_usada"
DENIED = "credencial_negada"


class Refusal(str, Enum):
    """Por que um uso nao aconteceu. Vocabulario fechado.

    `EXPIRED` e `REVOKED` sao separados porque mandam fazer coisas diferentes:
    uma pede renovacao, a outra pede conversa com quem revogou. E
    `SOURCE_UNAVAILABLE` e separado dos dois porque nao diz nada sobre a
    credencial -- diz que nao foi possivel perguntar.
    """
    UNAUTHENTICATED = "UNAUTHENTICATED"
    FORBIDDEN = "FORBIDDEN"
    NOT_FOUND = "NOT_FOUND"
    INVALID = "INVALID"
    POLICY_DENIED = "POLICY_DENIED"
    CONFLICT = "CONFLICT"
    EXPIRED = "EXPIRED"
    REVOKED = "REVOKED"
    NO_CAPABILITY = "NO_CAPABILITY"
    SOURCE_UNAVAILABLE = "SOURCE_UNAVAILABLE"


@dataclass(frozen=True, slots=True)
class CredentialOutcome:
    """O que aconteceu com uma operacao ADMINISTRATIVA. Nunca traz segredo."""
    accepted: bool
    reason: str
    refusal: Refusal | None = None
    credential: Credential | None = None
    actor: str = ""
    target: str = ""


@dataclass(slots=True)
class Resolved:
    """Material secreto, com validade de uso curta e deliberada.

    Nao e `frozen` e nao e imprimivel por acidente: `__repr__` e `__str__` sao
    reescritos para que um `print` de depuracao, uma excecao ou um log de
    excecao nao carreguem o valor. O segredo sai por `use()`, uma vez, e some.

    `use()` esvaziar o campo nao apaga o valor da memoria do processo -- Python
    nao permite isso, e fingir o contrario seria pior que admitir. O que ele
    garante e que o mesmo objeto nao vire uma fonte reutilizavel passeando pelo
    codigo: quem precisar de novo passa pelo caminho governado de novo, e a
    passagem fica auditada de novo.
    """
    credential_id: str
    provider: str
    use: Use
    _material: str = field(repr=False, default="")
    _spent: bool = field(repr=False, default=False)

    def use_secret(self) -> str:
        if self._spent:
            raise AdapterError(
                "este material ja foi entregue; peca de novo pelo caminho "
                "governado, para que o uso volte a ser auditado")
        value = self._material
        self._material = ""
        self._spent = True
        return value

    def __repr__(self) -> str:
        return (f"Resolved(credential={self.credential_id!r}, "
                f"provider={self.provider!r}, use={self.use.value!r}, "
                f"material=<oculto>)")

    __str__ = __repr__


class Reach(str, Enum):
    """O que o provedor REMOTO respondeu. Quatro fatos, nunca fundidos.

    O marco exige que estes nao virem um o outro, e a razao e operacional:

    * `AUTHENTICATED` -- o provedor aceitou a credencial. Nao diz que ela pode
      fazer o que se quer dela.
    * `REJECTED` -- o provedor a recusou. E fato sobre a credencial.
    * `UNAVAILABLE` -- nao deu para perguntar. Nao e fato sobre a credencial
      nenhum, e trata-lo como recusa manda alguem trocar um token que estava bom.
    * `UNKNOWN` -- perguntamos e nao entendemos a resposta. Nunca vira sucesso.
    """
    AUTHENTICATED = "AUTHENTICATED"
    REJECTED = "REJECTED"
    UNAVAILABLE = "UNAVAILABLE"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class Connection:
    """O resultado de um teste de conexao. Quatro perguntas, quatro respostas.

    `authorized` e o veredito do REGENTE -- concessao, capacidade, policy,
    validade. `reach` e o veredito do PROVEDOR. Uma credencial pode estar
    autorizada aqui e recusada la, e o contrario tambem: as duas coisas
    precisam caber na mesma tela sem se disfarcarem uma de outra.

    `capability_supported` e a terceira: o provider oferece esta operacao? Uma
    credencial autorizada para `repo.push` num provider que so le nao produz
    push -- produz uma surpresa, a menos que alguem diga isso antes.
    """
    authorized: bool
    reach: Reach
    capability_supported: bool
    detail: str
    credential_id: str = ""
    provider: str = ""
    use: str = ""
    refusal: Refusal | None = None

    @property
    def usable(self) -> bool:
        """Os tres, juntos. Nenhum dos tres sozinho basta."""
        return (self.authorized and self.capability_supported
                and self.reach is Reach.AUTHENTICATED)


def _no(refusal: Refusal, reason: str, **kw) -> CredentialOutcome:
    return CredentialOutcome(accepted=False, refusal=refusal, reason=reason, **kw)


@dataclass(slots=True)
class BoundBroker(CredentialBroker):
    """A porta que um adapter recebe: ja presa a quem age e a onde.

    O adapter chama `material(use)` no momento da operacao. Cada chamada refaz
    a autorizacao inteira -- e por isso revogar fecha a porta sem reiniciar o
    motor, e por isso nao existe material guardado esperando reuso.

    Nao ha como o adapter trocar o principal, o workspace ou o provider: os tres
    entram na construcao, feita pela composicao. Um adapter que pudesse escolhe-
    los escolheria o mais conveniente.
    """

    service: "CredentialService"
    principal: Principal
    workspace_id: str
    provider: str
    name: str = "governed"

    def verify(self) -> None:
        return None

    def material(self, use: Use) -> str:
        """O segredo, ou uma recusa com motivo. Nunca as duas coisas."""
        saida = self.service.resolve(self.principal, self.workspace_id,
                                     self.provider, use)
        if isinstance(saida, CredentialOutcome):
            raise CredentialDenied(
                saida.refusal.value if saida.refusal else "DENIED",
                saida.reason)
        return saida.use_secret()

    def allows(self, use: Use) -> bool:
        """Pergunta que NAO consome nem autoriza.

        Le o registro; nao resolve material e nao audita uso. Um adapter usa
        isto para anunciar o que sabe fazer com o que tem -- e `material()`
        refaz tudo de qualquer forma.
        """
        at = self.service.clock()
        return any(c.allows(use, at) for c in self.service.store.credentials(
            self.workspace_id, provider=self.provider, include_revoked=True))


@dataclass(slots=True)
class CredentialService:
    """Registra, revoga, lista e -- so no fim de tudo -- resolve."""

    store: Store
    policy: PolicyEngine
    #: Quem sabe transformar uma referencia em material. Trocavel: o motor nao
    #: sabe o que e um chaveiro, um arquivo ou um gerenciador de segredos.
    secrets: SecretProvider | None = None
    clock: Callable[[], datetime] = now
    organization: str = "*"
    client: str = "*"
    workspace_name: str = "*"
    environment: str = "staging"

    # ------------------------------------------------------------------
    # Administracao
    # ------------------------------------------------------------------
    def register(self, actor: Principal, workspace_id: str, name: str,
                 provider: str, secret_ref: str, capabilities,
                 kind: str = "token", expires_at: datetime | None = None,
                 note: str = "") -> CredentialOutcome:
        guard = self._may(actor, workspace_id, Ability.CREDENTIAL_GRANT)
        if guard is not None:
            return guard

        try:
            ref = SecretRef.parse(secret_ref)
        except ValueError as e:
            return _no(Refusal.INVALID, str(e), actor=actor.label)

        uses = uses_from(capabilities)
        if not uses:
            # Uma credencial sem capacidade nao serve para nada e parece servir
            # para tudo. Recusar aqui e melhor que guardar uma linha inutil que
            # alguem depois "corrige" ampliando.
            return _no(Refusal.INVALID,
                       "uma credencial precisa declarar ao menos uma "
                       "capacidade; sem isso ela nao autoriza nada",
                       actor=actor.label)

        workspace = self.store.workspace(workspace_id)
        if workspace is None:
            return _no(Refusal.NOT_FOUND, "recurso nao encontrado neste escopo")

        credential = Credential(
            id=ids.new_id(ids.CREDENTIAL), client_id=workspace.client_id,
            workspace_id=workspace_id, name=name.strip(), provider=provider,
            kind=kind, secret_ref=ref, capabilities=uses,
            granted_by=actor.ref.key, granted_at=self.clock(),
            expires_at=expires_at, note=note)

        if not self.store.open_credential(credential):
            return _no(Refusal.CONFLICT,
                       f"ja existe credencial viva para '{provider}/{name}' "
                       f"neste workspace; revogue antes de registrar outra",
                       actor=actor.label, target=name)

        self._audit(REGISTERED, actor, credential, use=None, result="ACCEPTED")
        return CredentialOutcome(
            accepted=True, credential=credential, actor=actor.label,
            target=f"{provider}/{name}",
            reason=f"credencial '{name}' registrada para {provider}")

    def revoke(self, actor: Principal, workspace_id: str, credential_id: str,
               reason: str = "") -> CredentialOutcome:
        guard = self._may(actor, workspace_id, Ability.CREDENTIAL_REVOKE)
        if guard is not None:
            return guard

        credential = self.store.credential(workspace_id, credential_id)
        if credential is None:
            return _no(Refusal.NOT_FOUND, "recurso nao encontrado neste escopo",
                       actor=actor.label)

        if not self.store.revoke_credential(workspace_id, credential_id,
                                            revoked_by=actor.ref.key,
                                            when=self.clock(), reason=reason):
            return _no(Refusal.CONFLICT, "esta credencial ja estava revogada",
                       actor=actor.label, target=credential.name)

        self._audit(REVOKED, actor, credential, use=None, result="ACCEPTED")
        return CredentialOutcome(
            accepted=True, actor=actor.label, target=credential.name,
            reason=f"credencial '{credential.name}' revogada")

    def listing(self, actor: Principal, workspace_id: str,
                include_revoked: bool = True):
        guard = self._may(actor, workspace_id, Ability.CREDENTIAL_LIST)
        if guard is not None:
            return guard
        return self.store.credentials(workspace_id,
                                      include_revoked=include_revoked)

    # ------------------------------------------------------------------
    # O caminho ate o material
    # ------------------------------------------------------------------
    def resolve(self, actor: Principal, workspace_id: str, provider: str,
                use: Use) -> Resolved | CredentialOutcome:
        """A unica porta. Autoriza tudo, e so entao pergunta o valor.

        Devolve `Resolved` (com o material dentro, e nenhuma forma de imprimi-lo
        por acidente) ou uma recusa. Nao ha caminho em que uma recusa carregue
        material, nem em que um sucesso deixe de ser auditado.
        """
        at = self.clock()

        if not actor.authenticated:
            return _no(Refusal.UNAUTHENTICATED,
                       "esta requisicao nao foi autenticada")

        # Usar credencial e uma capacidade NOMEADA.
        #
        # A primeira versao aceitava "qualquer capacidade neste workspace" como
        # prova de pertencimento. Isso fazia de quem responde a fila humana um
        # usuario de credencial por tabela -- e obrigava o motor a tomar
        # emprestada uma capacidade que nao tem nada a ver com o que ele faz.
        if not actor.can(workspace_id, Ability.CREDENTIAL_USE):
            return _no(Refusal.NOT_FOUND, "recurso nao encontrado neste escopo",
                       actor=actor.label)

        found = [c for c in self.store.credentials(
            workspace_id, provider=provider, include_revoked=True)]
        if not found:
            return self._deny(actor, workspace_id, provider, use,
                              Refusal.NOT_FOUND,
                              "nenhuma credencial registrada para este provider")

        # A mais recente que sirva. `include_revoked=True` acima e proposital:
        # uma credencial revogada precisa produzir `REVOKED`, e nao
        # `NOT_FOUND` -- os dois mandam a pessoa fazer coisas diferentes.
        usaveis = [c for c in found if c.allows(use, at)]
        if not usaveis:
            return self._deny(actor, workspace_id, provider, use,
                              *self._why_not(found, use, at))

        credential = usaveis[-1]
        decision = self.policy.decide(PolicyContext(
            action=Action(kind=use.value, resource=f"{provider}:{credential.name}",
                          environment=self.environment),
            organization=self.organization, client=self.client,
            workspace=self.workspace_name, agent=actor.label,
            autonomy=AutonomyLevel.L4))
        if decision.effect is Effect.DENY:
            return self._deny(actor, workspace_id, provider, use,
                              Refusal.POLICY_DENIED,
                              f"policy DENY: {decision.reason}", credential)

        if self.secrets is None:
            return self._deny(actor, workspace_id, provider, use,
                              Refusal.SOURCE_UNAVAILABLE,
                              "este workspace nao tem fonte de segredo "
                              "configurada", credential)
        try:
            material = self.secrets.resolve(credential.secret_ref.text)
        except AdapterError as e:
            # A fonte nao respondeu. Nao saber NAO e permissao: e uma recusa
            # com nome proprio, distinta de expirada e de revogada.
            return self._deny(actor, workspace_id, provider, use,
                              Refusal.SOURCE_UNAVAILABLE,
                              f"a fonte de segredo nao respondeu: {e}",
                              credential)

        self._audit(RESOLVED, actor, credential, use=use, result="ACCEPTED")
        return Resolved(credential_id=credential.id, provider=provider,
                        use=use, _material=material)

    def test_connection(self, actor: Principal, workspace_id: str,
                        provider: str, use: Use, probe) -> Connection:
        """Autentica no provedor e faz UMA leitura minima. Nunca muta.

        O teste passa pelo mesmo caminho governado de qualquer uso: se a
        credencial esta revogada, expirada ou sem capacidade, ele nem chega a
        falar com o provedor -- e essa e a resposta certa, porque testar uma
        credencial que o Regente nao autorizaria informaria sobre a credencial
        e nada sobre o sistema.

        O `probe` vem da composicao e e quem sabe falar com o provedor. Ele
        recebe o material e devolve um veredito; o motor nao sabe o que e uma
        chamada HTTP nem o que e um repositorio.
        """
        resolved = self.resolve(actor, workspace_id, provider, use)
        if isinstance(resolved, CredentialOutcome):
            return Connection(
                authorized=False, reach=Reach.UNKNOWN,
                capability_supported=False, detail=resolved.reason,
                provider=provider, use=use.value, refusal=resolved.refusal,
                credential_id=(resolved.credential.id
                               if resolved.credential else ""))

        suportado = bool(getattr(probe, "supports", lambda _u: True)(use))
        try:
            reach, detail = probe.check(resolved.use_secret())
        except AdapterError as e:
            # Nao deu para perguntar. Isto NAO e recusa da credencial.
            reach, detail = Reach.UNAVAILABLE, str(e)[:200]

        self.record_use(actor, resolved.credential_id, workspace_id, use,
                        ok=(reach is Reach.AUTHENTICATED),
                        detail=f"teste de conexao: {reach.value}")
        return Connection(
            authorized=True, reach=reach, capability_supported=suportado,
            detail=detail[:200], credential_id=resolved.credential_id,
            provider=provider, use=use.value)

    def broker(self, actor: Principal, workspace_id: str,
               provider: str) -> BoundBroker:
        """A porta deste adapter, presa a quem age.

        Criada pela composicao e entregue ao adapter ja vinculada. E a unica
        forma de um adapter alcancar material -- nao ha metodo aqui que devolva
        segredo sem passar por `resolve`.
        """
        return BoundBroker(service=self, principal=actor,
                           workspace_id=workspace_id, provider=provider)

    def record_use(self, actor: Principal, credential_id: str,
                   workspace_id: str, use: Use, ok: bool,
                   detail: str = "") -> None:
        """O que aconteceu DEPOIS de o material sair.

        Separado de `RESOLVED` porque resolver e usar sao fatos diferentes: um
        segredo entregue e uma operacao que falhou contam uma historia que "uso
        autorizado" sozinho nao conta.
        """
        credential = self.store.credential(workspace_id, credential_id)
        if credential is None:
            return
        self._audit(USED, actor, credential, use=use,
                    result="OK" if ok else "FAILED", detail=detail)

    # ------------------------------------------------------------------
    @staticmethod
    def _why_not(found: list[Credential], use: Use,
                 at: datetime) -> tuple[Refusal, str]:
        """A recusa mais informativa entre as candidatas.

        Se existe uma credencial que so falta capacidade, isso e mais util que
        dizer "expirada" sobre outra. A ordem persegue o que a pessoa pode
        consertar: capacidade, depois validade, depois revogacao.
        """
        ativas = [c for c in found if c.status(at) is Status.ACTIVE]
        if ativas:
            return (Refusal.NO_CAPABILITY,
                    f"a credencial existe e nao autoriza '{use.value}'; "
                    f"autoriza {sorted(u.value for u in ativas[-1].capabilities)}")
        expiradas = [c for c in found if c.status(at) is Status.EXPIRED]
        if expiradas:
            return (Refusal.EXPIRED,
                    f"a credencial venceu em {expiradas[-1].expires_at}")
        return (Refusal.REVOKED,
                f"a credencial foi revogada por {found[-1].revoked_by}")

    def _deny(self, actor: Principal, workspace_id: str, provider: str,
              use: Use, refusal: Refusal, reason: str,
              credential: Credential | None = None) -> CredentialOutcome:
        """Recusa AUDITADA. Uma negativa silenciosa nao ajuda ninguem depois."""
        self.store.record_event(Event(
            id=ids.new_id(ids.EVENT), workspace_id=workspace_id, kind=DENIED,
            actor=actor.label,
            summary=f"{actor.label}: {provider}/{use.value} negado -- {reason}"[:300],
            data={
                "actor": actor.ref.key if actor.authenticated else "anonimo",
                "workspace_id": workspace_id,
                "provider": provider,
                "use": use.value,
                "credential_id": credential.id if credential else "",
                "refusal": refusal.value,
                "result": "DENIED",
            }))
        return _no(refusal, reason, actor=actor.label, credential=credential)

    def _may(self, actor: Principal, workspace_id: str,
             ability: Ability) -> CredentialOutcome | None:
        """As barreiras comuns da administracao. `None` = pode seguir."""
        if not actor.authenticated:
            return _no(Refusal.UNAUTHENTICATED,
                       "esta requisicao nao foi autenticada")
        if not actor.can(workspace_id, ability):
            return _no(Refusal.NOT_FOUND, "recurso nao encontrado neste escopo",
                       actor=actor.label)
        if self.store.workspace(workspace_id) is None:
            return _no(Refusal.NOT_FOUND, "recurso nao encontrado neste escopo",
                       actor=actor.label)

        decision = self.policy.decide(PolicyContext(
            action=Action(kind=ability.value, resource=workspace_id,
                          environment=self.environment),
            organization=self.organization, client=self.client,
            workspace=self.workspace_name, agent=actor.label,
            autonomy=AutonomyLevel.L4))
        if decision.effect is Effect.DENY:
            return _no(Refusal.POLICY_DENIED, f"policy DENY: {decision.reason}",
                       actor=actor.label)
        return None

    def _audit(self, kind: str, actor: Principal, credential: Credential,
               use: Use | None, result: str, detail: str = "") -> None:
        """A trilha. Endereco do segredo, nunca o segredo.

        `secret_ref` entra porque e um ENDERECO -- `env:GH_TOKEN` diz onde
        procurar e nao vale nada para quem nao esta nesta maquina. O material
        nunca entra, e nao ha caminho neste metodo que o alcance: ele nao esta
        no `Credential`.
        """
        self.store.record_event(Event(
            id=ids.new_id(ids.EVENT), workspace_id=credential.workspace_id,
            kind=kind, actor=actor.label,
            summary=(f"{actor.label}: {kind} {credential.provider}/"
                     f"{credential.name}"
                     + (f" para {use.value}" if use else ""))[:300],
            data={
                "credential_id": credential.id,
                "actor": actor.ref.key,
                "actor_method": actor.method,
                "workspace_id": credential.workspace_id,
                "client_id": credential.client_id,
                "provider": credential.provider,
                "name": credential.name,
                "secret_ref": credential.secret_ref.text,
                "capabilities": sorted(u.value for u in credential.capabilities),
                "use": use.value if use else "",
                "expires_at": credential.expires_at,
                "result": result,
                "detail": detail[:200],
            }))
