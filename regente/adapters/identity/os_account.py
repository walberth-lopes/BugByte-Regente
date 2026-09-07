# -*- coding: utf-8 -*-
"""A conta do sistema operacional. Identidade real, com procedencia.

Nao ha provedor corporativo neste ambiente -- nem OIDC, nem SSO, nem dominio.
Ha **uma** identidade real e verificavel: a conta sob a qual o processo roda. O
sistema operacional ja a autenticou (senha, PIN, cartao -- nao importa qual), e
este adapter so traduz esse fato para o vocabulario do motor.

O que ele traz de diferente do que existia:

**Identificador estavel, nao um nome.** `getpass.getuser()` consulta `LOGNAME`,
`USER`, `LNAME` e `USERNAME` **antes** do sistema -- todas editaveis por quem
roda o processo. O sujeito gravado na auditoria era, na pratica, texto escolhido
por quem decide. Aqui o sujeito e o identificador que o sistema emite (o SID no
Windows, o uid em POSIX), e ele nao se edita por variavel de ambiente.

**Emissor nomeado.** A maquina ou o dominio que respondeu pela conta. Uma
concessao feita numa maquina nao vale noutra, e o registro precisa dizer qual.

O que ele NAO e, e precisa ser dito: nao serve ao navegador. Uma requisicao HTTP
nao carrega a conta do sistema, e faze-la carregar exigiria autenticacao
integrada -- uma integracao que este ambiente nao tem. Ele autentica o terminal,
onde o processo E a conta.
"""

from __future__ import annotations

import os
import platform
import socket
from dataclasses import dataclass

from ...core.principal import Principal
from ...ports import Capability
from ...ports.identity import Identity, IdentityProvider


def _windows_account() -> tuple[str, str, str] | None:
    """`(sid, nome, emissor)` da conta atual, ou `None` fora do Windows.

    Via `ctypes`, e nao chamando um executavel: um subprocesso poderia ser
    substituido no `PATH` por quem roda o processo, e a identidade passaria a
    ser o que esse programa dissesse. A chamada direta a biblioteca do sistema
    nao tem esse caminho.
    """
    if platform.system() != "Windows":
        return None
    import ctypes
    from ctypes import wintypes

    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)

    # As assinaturas sao DECLARADAS, e nao e detalhe de estilo. Sem `restype`,
    # o ctypes assume `c_int` e TRUNCA um ponteiro de 64 bits -- o handle do
    # processo chegava pela metade e a chamada falhava silenciosamente, sem
    # erro do sistema. A primeira versao disto devolvia `None` sempre, e uma
    # identidade que sempre falha e indistinguivel de um ambiente sem conta.
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    advapi.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                        ctypes.POINTER(wintypes.HANDLE)]
    advapi.OpenProcessToken.restype = wintypes.BOOL
    advapi.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                           ctypes.c_void_p, wintypes.DWORD,
                                           ctypes.POINTER(wintypes.DWORD)]
    advapi.GetTokenInformation.restype = wintypes.BOOL
    advapi.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p,
                                              ctypes.POINTER(ctypes.c_wchar_p)]
    advapi.ConvertSidToStringSidW.restype = wintypes.BOOL

    TOKEN_QUERY = 0x0008
    TokenUser = 1

    token = wintypes.HANDLE()
    if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), TOKEN_QUERY,
                                   ctypes.byref(token)):
        return None
    try:
        size = wintypes.DWORD()
        advapi.GetTokenInformation(token, TokenUser, None, 0,
                                   ctypes.byref(size))
        if not size.value:
            return None
        buffer = ctypes.create_string_buffer(size.value)
        if not advapi.GetTokenInformation(token, TokenUser, buffer, size,
                                          ctypes.byref(size)):
            return None

        # TOKEN_USER comeca com SID_AND_ATTRIBUTES, cujo primeiro campo e o
        # ponteiro para o SID.
        sid = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
        texto = ctypes.c_wchar_p()
        if not advapi.ConvertSidToStringSidW(ctypes.c_void_p(sid),
                                             ctypes.byref(texto)):
            return None
        try:
            identificador = str(texto.value or "")
        finally:
            kernel.LocalFree(texto)
    finally:
        kernel.CloseHandle(token)

    if not identificador:
        return None
    nome = os.environ.get("USERNAME", "")
    emissor = os.environ.get("USERDOMAIN") or socket.gethostname()
    return identificador, nome, emissor


def _posix_account() -> tuple[str, str, str] | None:
    """`(uid, nome, emissor)`. O uid vem do sistema, nao do ambiente."""
    if not hasattr(os, "getuid"):
        return None
    uid = os.getuid()                              # type: ignore[attr-defined]
    nome = ""
    try:
        import pwd

        nome = pwd.getpwuid(uid).pw_name           # type: ignore[attr-defined]
    except Exception:                              # noqa: BLE001
        nome = ""
    return str(uid), nome, socket.gethostname()


def current_account() -> tuple[str, str, str] | None:
    """A conta atual, ou `None` quando o sistema nao consegue responder.

    `None` e uma resposta. Inventar um sujeito quando o sistema nao respondeu
    produziria uma identidade que nao corresponde a ninguem -- e uma concessao
    amarrada a ela valeria para qualquer processo que caisse no mesmo default.
    """
    return _windows_account() or _posix_account()


@dataclass(slots=True)
class OsAccountIdentity(IdentityProvider):
    """Quem esta rodando o processo, segundo o sistema operacional."""

    capability = Capability.IDENTITY
    name: str = "os-account"
    #: NAO e de desenvolvimento. A conta do sistema e uma identidade real, com
    #: identificador estavel e emissor nomeado -- so nao e uma identidade
    #: corporativa, e a diferenca esta em `issuer`, nao num rotulo de mentira.
    development_only: bool = False
    #: Workspaces que esta sessao LE. Ver e concessao de composicao; agir nao.
    reads: frozenset[str] | None = None

    def verify(self) -> None:
        if current_account() is None:
            raise ValueError(
                "o sistema operacional nao respondeu qual conta roda este "
                "processo; sem isso nao ha identidade a provar")

    def describe(self) -> str:
        conta = current_account()
        if conta is None:
            return "conta do sistema: INDISPONIVEL"
        identificador, nome, emissor = conta
        return (f"conta do sistema '{nome or identificador}' emitida por "
                f"'{emissor}' (id estavel {identificador})")

    # ------------------------------------------------------------------
    def authenticate(self, credential: str | None) -> Identity | None:
        """A credencial e ignorada, e isso e uma afirmacao, nao um descuido.

        Quem prova aqui e o sistema operacional: o processo ESTA rodando sob
        aquela conta. Aceitar um segredo daria a impressao de um segundo fator
        que nao existe.

        Por isso este provedor so e oferecido ao terminal. Numa superficie de
        rede ele autenticaria o servidor, nao quem chamou -- que e o modo mais
        silencioso possivel de dar a identidade errada a alguem.
        """
        conta = current_account()
        if conta is None:
            return None
        identificador, nome, emissor = conta
        return Identity(subject=identificador, display=nome or identificador,
                        method=self.name, provider=self.name, issuer=emissor)

    def principal(self, identity: Identity) -> Principal:
        """A identidade, e NADA de autoridade.

        `abilities` fica vazio de proposito. Quem concede e uma concessao
        persistida, lida depois por `AccessService.authorize`. Enquanto este
        metodo devolvia autoridade, quem editava a configuracao concedia a si
        mesmo poder de escrita e nada guardava esse fato.
        """
        return Principal(subject=identity.subject, display=identity.display,
                         method=identity.method, provider=identity.provider,
                         issuer=identity.issuer,
                         authenticated_at=identity.authenticated_at,
                         workspaces=self.reads)
