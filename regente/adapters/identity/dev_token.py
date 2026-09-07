# -*- coding: utf-8 -*-
"""Um token de desenvolvimento. NAO e autenticacao para valer.

Existe para provar a travessia -- requisicao -> principal autenticado -- sem
inventar um sistema de identidade que este projeto ainda nao precisa. E foi
escrito com mais cuidado com o que ele NAO deve virar do que com o que ele faz.

O que o torna aceitavel:

1. **Nao passa despercebido.** `development_only = True`, e `doctor` e `health`
   dizem isso em voz alta. Um mecanismo de desenvolvimento indistinguivel de um
   real e pior que nenhum: cria a sensacao de que ha autenticacao.
2. **Nao roda fora do loopback.** `bind_is_local=False` recusa autenticar
   qualquer coisa. Nao e conselho no README; e uma recusa no codigo.
3. **O cliente nao declara nada.** Ele apresenta um segredo que ESTE processo
   gerou. O nome de quem esta usando vem da configuracao, nunca da requisicao.
4. **E substituivel.** Esta atras de `IdentityProvider`; trocar por OIDC nao
   toca em API, motor nem tela.

O que ele nao tem, e precisa ser dito: nao ha usuarios, nao ha expiracao, nao ha
revogacao, nao ha segundo fator, e o token vale para quem o tiver. Ele prova que
quem chama e quem rodou `regente ui` nesta maquina. Nada mais.
"""

from __future__ import annotations

import hmac
import secrets
from dataclasses import dataclass, field

from ...core.principal import Principal
from ...ports import Capability
from ...ports.identity import Identity, IdentityProvider

@dataclass(slots=True)
class DevTokenIdentity(IdentityProvider):
    """Um segredo por processo, apresentado em cada requisicao.

    O token existe **so em memoria** e chega a tela injetado na pagina que este
    mesmo processo serve. Ele nunca toca o disco.

    A primeira versao o gravava num arquivo -- que nada lia. Um segredo escrito
    e nunca consultado e liability pura: sobrevive a um `kill` (o `finally` que
    o apagaria nao roda), fica para tras, e nao servia para nada. Um site aberto
    noutra aba pode disparar um POST para o loopback, mas nao consegue LER esta
    pagina, entao nao alcanca o token -- e essa e a defesa que importa, sem
    arquivo nenhum.
    """

    capability = Capability.IDENTITY
    name: str = "dev-token"
    development_only: bool = True

    #: Como a pessoa que rodou o processo se chama. Vem da CONFIGURACAO.
    #: Se viesse da requisicao, qualquer um seria qualquer um.
    operator: str = "operador local"
    #: Workspaces que este operador le e onde decide. Concessao da composicao.
    reads: frozenset[str] | None = None
    decides: frozenset[str] = field(default_factory=frozenset)
    #: False quando o servidor escuta fora do loopback. Recusa tudo.
    bind_is_local: bool = True

    _token: str = ""

    def __post_init__(self) -> None:
        self._token = secrets.token_urlsafe(32)

    # ------------------------------------------------------------------
    @property
    def token(self) -> str:
        return self._token

    def verify(self) -> None:
        if not self._token:
            raise ValueError("nenhum token foi gerado")

    def describe(self) -> str:
        """O que um diagnostico precisa dizer sobre isto."""
        if not self.bind_is_local:
            return ("dev-token FORA DO LOOPBACK -- recusando autenticar; "
                    "este mecanismo nao serve para exposicao em rede")
        return (f"dev-token (SOMENTE DESENVOLVIMENTO) para '{self.operator}'; "
                f"decide em {len(self.decides)} workspace(s)")

    # ------------------------------------------------------------------
    def authenticate(self, credential: str | None) -> Identity | None:
        """Compara em tempo constante, e recusa fora do loopback.

        A recusa por bind vem PRIMEIRO. Verificar o token e so depois decidir
        que o endereco era publico ainda teria autenticado alguem -- e a ordem
        de duas linhas e a diferenca entre uma recusa e um vazamento.
        """
        if not self.bind_is_local:
            return None
        if not credential or not self._token:
            return None
        if not hmac.compare_digest(credential, self._token):
            return None
        return Identity(subject=self.operator, display=self.operator,
                        method=self.name)

    def principal(self, identity: Identity) -> Principal:
        return Principal(subject=identity.subject, display=identity.display,
                         method=identity.method, workspaces=self.reads,
                         decides=self.decides)

    def close(self) -> None:
        """Nada a limpar: o segredo nunca saiu da memoria.

        Existe para que a composicao possa fechar todo provedor do mesmo jeito,
        e para que um provedor futuro com sessao de verdade tenha onde encerrar.
        """
        self._token = ""
