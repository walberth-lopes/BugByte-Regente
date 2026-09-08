# -*- coding: utf-8 -*-
"""Quais tasks o motor pode pegar, e em que ordem. Puro e deterministico.

Duas perguntas, e colapsa-las e o defeito que este modulo existe para evitar:

    elegibilidade   esta task PODE ser pega?        booleana, um filtro
    prioridade      entre as que podem, qual antes?  um numero, uma ordem

Uma task pode ser elegivel e ter prioridade baixa. Se as duas fossem a mesma
coisa, "despriorizar" viraria "esconder" -- e e assim que trabalho some de um
board sem ninguem perceber.

**O motor nao ganha vocabulario de fornecedor.** Uma regra fala de campos que a
port ja expoe -- titulo, rotulo, projeto, status, chave, prioridade de origem --
e nunca de "Jira", "epic" ou "sprint". Traduzir o board para esses campos e
trabalho do adapter, na fronteira.

**A ordem final continua sendo a do scheduler:** `(prioridade, chave)`, menor
primeiro, chave como desempate estavel. As regras daqui mudam o NUMERO; nunca o
criterio de desempate. Um empate sempre resolve pela chave, sempre igual, em
toda execucao -- sem isso o mesmo tick escolhe tasks diferentes a cada vez e o
motor fica indo e voltando sem terminar nada.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

#: Campos sobre os quais uma regra pode falar. Lista FECHADA de proposito: um
#: campo novo e uma decisao de desenho, e nao um nome que alguem digitou num
#: YAML e que falha em silencio quando esta errado.
FIELDS: tuple[str, ...] = (
    "title", "key", "project", "status", "labels", "priority", "assignee",
    "type", "components",
)


class Match(str, Enum):
    """Como comparar. Poucos operadores, todos obvios ao ler."""
    CONTAINS = "contains"
    EQUALS = "equals"
    STARTS_WITH = "starts_with"
    IN = "in"
    LT = "lt"
    GT = "gt"


class Effect(str, Enum):
    """O que a regra faz quando casa."""
    #: So tasks que casem com ALGUMA regra `REQUIRE` sao elegiveis. Sem nenhuma
    #: regra deste tipo, toda task e elegivel -- ausencia de filtro nao filtra.
    REQUIRE = "require"
    #: Casou, esta fora. Vence qualquer `REQUIRE`: o mais restritivo ganha, a
    #: mesma regra do motor de policy, pelo mesmo motivo.
    EXCLUDE = "exclude"
    #: Muda a prioridade. Negativo roda antes, porque menor roda antes.
    PRIORITY = "priority"


@dataclass(frozen=True, slots=True)
class Rule:
    """Uma regra do workspace. Sem fornecedor dentro."""
    field_name: str
    match: Match
    value: Any
    effect: Effect = Effect.PRIORITY
    #: Quanto somar a prioridade quando casa. So para `PRIORITY`.
    #:
    #: NEGATIVO roda antes. E contraintuitivo dito assim, e por isso a
    #: configuracao aceita `prioridade: alta` e converte -- o numero cru fica
    #: aqui, onde ele tem uma regra so.
    delta: int = 0
    name: str = ""
    #: Case-insensitive por default: ninguem quer descobrir que a regra nao
    #: pegou porque o board escreveu `Faxina` e a regra dizia `FAXINA`.
    case_sensitive: bool = False

    def __post_init__(self) -> None:
        if self.field_name not in FIELDS:
            raise ValueError(
                f"regra fala do campo '{self.field_name}', que nao existe. "
                f"Campos disponiveis: {', '.join(FIELDS)}")
        if self.effect is Effect.PRIORITY and self.delta == 0:
            raise ValueError(
                f"regra de prioridade '{self.name or self.field_name}' nao muda "
                f"nada: `delta` e zero. Uma regra sem efeito e ruido que parece "
                f"protecao")

    def matches(self, task: "Selectable") -> bool:
        bruto = task.field(self.field_name)
        if bruto is None:
            return False
        return _compare(bruto, self.match, self.value, self.case_sensitive)


def _norm(v: Any, sensitive: bool) -> Any:
    if isinstance(v, str) and not sensitive:
        return v.lower()
    if isinstance(v, (list, tuple, set, frozenset)):
        return [_norm(x, sensitive) for x in v]
    return v


def _compare(bruto: Any, how: Match, esperado: Any, sensitive: bool) -> bool:
    campo = _norm(bruto, sensitive)
    alvo = _norm(esperado, sensitive)

    if how is Match.CONTAINS:
        if isinstance(campo, list):
            # Um campo de lista (rotulos, componentes) "contem" quando algum
            # item contem. Comparar a lista inteira como texto acertaria por
            # acidente e erraria por acidente.
            return any(isinstance(x, str) and str(alvo) in x for x in campo)
        return str(alvo) in str(campo)
    if how is Match.EQUALS:
        if isinstance(campo, list):
            return alvo in campo
        return campo == alvo
    if how is Match.STARTS_WITH:
        return str(campo).startswith(str(alvo))
    if how is Match.IN:
        valores = alvo if isinstance(alvo, list) else [alvo]
        if isinstance(campo, list):
            return any(x in valores for x in campo)
        return campo in valores
    if how is Match.LT:
        return _numero(campo) is not None and _numero(campo) < _numero(alvo)
    if how is Match.GT:
        return _numero(campo) is not None and _numero(campo) > _numero(alvo)
    return False


def _numero(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


class Selectable:
    """O que uma regra consegue perguntar a uma task.

    Uma classe, e nao um dict, porque o conjunto de campos e fechado e um erro
    de digitacao precisa doer na construcao da REGRA -- nao virar `None` em
    silencio no meio de um tick de madrugada.
    """

    __slots__ = ("_valores",)

    def __init__(self, **valores: Any) -> None:
        self._valores = {k: v for k, v in valores.items() if k in FIELDS}

    def field(self, name: str) -> Any:
        return self._valores.get(name)

    def as_dict(self) -> dict[str, Any]:
        return dict(self._valores)


@dataclass(frozen=True, slots=True)
class Verdict:
    """O resultado, com o PORQUE junto.

    `reasons` existe para a tela poder mostrar por que uma task esta onde esta.
    Uma ordem que ninguem consegue explicar e uma ordem em que ninguem confia --
    e a primeira pergunta de quem ve o board reordenado e "por que essa
    primeiro?".
    """
    eligible: bool
    priority: int
    reasons: tuple[str, ...] = ()
    #: Regra que excluiu, quando houve. Nomeada, para a tela dizer qual.
    excluded_by: str = ""


#: Limites da prioridade final. Existem para que um `delta` absurdo digitado num
#: YAML nao invente uma ordem que ninguem pediu -- nao para "normalizar" nada.
#:
#: O piso e NEGATIVO de proposito. Com piso em zero, uma task que a origem marcou
#: como critica (10) e uma comum (100) somariam o mesmo `-100` e as duas
#: encostariam no chao empatadas -- a regra do workspace teria apagado a
#: informacao do board em vez de somar a ela. O scheduler ordena inteiros; sinal
#: nao lhe faz diferenca.
MIN_PRIORITY = -1000
MAX_PRIORITY = 1000


@dataclass(frozen=True, slots=True)
class Selection:
    """As regras de um workspace, aplicadas na ordem em que foram escritas."""

    rules: tuple[Rule, ...] = ()

    @property
    def requires(self) -> tuple[Rule, ...]:
        return tuple(r for r in self.rules if r.effect is Effect.REQUIRE)

    def evaluate(self, task: Selectable, base_priority: int = 100) -> Verdict:
        """Elegibilidade e prioridade, com o motivo de cada uma.

        A ordem de avaliacao e fixa e importa:

        1. `EXCLUDE` primeiro -- o mais restritivo vence, e nada depois disso
           pode reabrir. Avaliar prioridade de uma task excluida so produziria
           um numero que ninguem vai usar.
        2. `REQUIRE` depois -- sem nenhuma regra dessas, tudo e elegivel.
           Ausencia de filtro nao filtra.
        3. `PRIORITY` por ultimo, somando TODOS os deltas que casarem. Somar, e
           nao usar o primeiro: duas razoes para adiantar uma task adiantam
           mais do que uma.
        """
        motivos: list[str] = []

        for regra in self.rules:
            if regra.effect is Effect.EXCLUDE and regra.matches(task):
                nome = regra.name or f"{regra.field_name} {regra.match.value}"
                return Verdict(False, base_priority,
                               (f"excluida por '{nome}'",), excluded_by=nome)

        exigencias = self.requires
        if exigencias and not any(r.matches(task) for r in exigencias):
            nomes = ", ".join(r.name or r.field_name for r in exigencias)
            return Verdict(False, base_priority,
                           (f"nao casa com nenhuma exigencia ({nomes})",),
                           excluded_by="requisito")

        prioridade = base_priority
        for regra in self.rules:
            if regra.effect is not Effect.PRIORITY or not regra.matches(task):
                continue
            prioridade += regra.delta
            nome = regra.name or f"{regra.field_name} {regra.match.value}"
            sinal = "+" if regra.delta > 0 else ""
            motivos.append(f"{nome}: {sinal}{regra.delta}")

        travada = max(MIN_PRIORITY, min(MAX_PRIORITY, prioridade))
        if travada != prioridade:
            motivos.append(f"prioridade limitada a {travada}")
        return Verdict(True, travada, tuple(motivos))

    def order(self, tasks: list[tuple[str, Selectable, int]]) -> list[tuple[str, Verdict]]:
        """As elegiveis, na ordem final. `tasks` e (chave, campos, prioridade base).

        Empate resolve pela CHAVE, sempre, em qualquer execucao. E a mesma regra
        do scheduler, repetida aqui porque esta funcao existe para a tela
        mostrar a ordem ANTES de o motor rodar -- e duas ordens diferentes para
        a mesma pergunta seriam piores que nenhuma.
        """
        avaliadas = [(chave, self.evaluate(campos, base))
                     for chave, campos, base in tasks]
        elegiveis = [(k, v) for k, v in avaliadas if v.eligible]
        return sorted(elegiveis, key=lambda item: (item[1].priority, item[0]))


def rules_from(config: Any) -> Selection:
    """Le regras de configuracao. Um nome errado LEVANTA, nunca vira silencio.

    O contrario -- ignorar o que nao entende -- transformaria erro de digitacao
    em regra que nunca casa, e ninguem descobre isso olhando a tela: ela mostra
    uma ordem plausivel, so que a errada.
    """
    regras: list[Rule] = []
    for i, bruto in enumerate(config or (), start=1):
        if not isinstance(bruto, dict):
            raise ValueError(f"regra {i} nao e um mapeamento")
        nome = str(bruto.get("name") or bruto.get("nome") or f"regra {i}")
        campo = str(bruto.get("field") or bruto.get("campo") or "")
        try:
            como = Match(str(bruto.get("match") or bruto.get("compara")
                             or "contains").lower())
        except ValueError:
            raise ValueError(
                f"'{nome}': comparacao '{bruto.get('match')}' nao existe. "
                f"Disponiveis: {', '.join(m.value for m in Match)}") from None
        try:
            efeito = Effect(str(bruto.get("effect") or bruto.get("efeito")
                                or "priority").lower())
        except ValueError:
            raise ValueError(
                f"'{nome}': efeito '{bruto.get('effect')}' nao existe. "
                f"Disponiveis: {', '.join(e.value for e in Effect)}") from None
        regras.append(Rule(
            field_name=campo, match=como,
            value=bruto.get("value", bruto.get("valor")),
            effect=efeito, delta=int(bruto.get("delta") or 0), name=nome,
            case_sensitive=bool(bruto.get("case_sensitive"))))
    return Selection(tuple(regras))
