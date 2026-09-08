# -*- coding: utf-8 -*-
"""A tela como PRODUTO -- e a fronteira entre o vocabulario dela e o do motor.

Este arquivo guarda uma classe de defeito, e vale a pena dizer qual, porque ela
custou caro duas vezes durante este marco.

A tela precisa falar portugues: ninguem instala o Regente para ler
`WAITING_HUMAN`, `status_map` ou `repo.pr`. Traduzir exige uma TABELA, e uma
tabela e uma SEGUNDA COPIA do vocabulario do motor. No dia em que o motor ganha
um estado, um balde ou uma capacidade, a copia fica velha -- e o sintoma nao e
uma falha: e um identificador em ingles aparecendo no meio de uma frase, ou uma
opcao que a tela oferece e o motor recusa.

Os dois defeitos reais deste marco foram exatamente isso:

* a tela gravava `status_map` na direcao contraria (`{nome: balde}` em vez de
  `{balde: [nomes]}`), e so descobrimos configurando pelo navegador;
* os sinais de saude apareciam como `Database_growth`, porque ninguem tinha
  traduzido as chaves do motor.

As guardas abaixo leem a FONTE da tela e comparam com os enums do motor. Elas
nao provam que a tela e bonita. Provam que ela nao esta falando sozinha.
"""

import json
import re

import pytest

from uifonte import FONTE, arquivos, texto


# ===========================================================================
# ferramentas
# ===========================================================================

def _fonte(nome: str) -> str:
    caminho = FONTE / nome
    assert caminho.is_file(), f"a tela nao tem mais {nome}"
    return caminho.read_text(encoding="utf-8")


def _so_o_que_aparece(codigo: str) -> str:
    """A fonte sem comentario. E o que a pessoa poderia ler na tela.

    Existe porque um guard que le o arquivo inteiro acusa a explicacao junto com
    o defeito: o comentario que diz "nunca escreva 'sumiu'" contem a palavra
    'sumiu'. Um guard que da alarme falso e desligado, e um guard desligado nao
    guarda nada.
    """
    sem_bloco = re.sub(r"/\*.*?\*/", " ", codigo, flags=re.S)
    return "\n".join(l for l in sem_bloco.splitlines()
                     if not l.lstrip().startswith(("//", "*")))


def _chaves_do_mapa(codigo: str, nome: str) -> set[str]:
    """As chaves de um objeto literal `const NOME = {...}` na fonte.

    Um analisador de JavaScript inteiro seria exagero: estes mapas sao literais
    escritos a mao, com uma chave por linha. O que importa e falhar alto quando
    o bloco nao for encontrado, em vez de devolver um conjunto vazio -- um
    conjunto vazio faria toda comparacao abaixo passar sem olhar nada.
    """
    m = re.search(rf"const {nome}\s*=\s*\{{(.*?)\n\}};", codigo, re.S)
    assert m, f"nao achei o mapa {nome} na fonte da tela"
    corpo = m.group(1)
    return set(re.findall(r'^\s{2}"?([A-Za-z_][\w.]*)"?\s*:', corpo, re.M))


def _valores_de_lista(codigo: str, nome: str) -> set[str]:
    """Os primeiros elementos de `const NOME = [["a", ...], ["b", ...]]`."""
    m = re.search(rf"const {nome}\s*=\s*\[(.*?)\n\];", codigo, re.S)
    assert m, f"nao achei a lista {nome} na fonte da tela"
    return set(re.findall(r'\[\s*"([^"]+)"', m.group(1)))


# ===========================================================================
# 1. O VOCABULARIO DO MOTOR CHEGA INTEIRO NA TELA
# ===========================================================================

def test_every_task_state_has_a_human_label():
    """Um estado sem rotulo aparece como `QA_STAGING` na cara de quem olha."""
    from regente.core.states import TaskState

    rotulados = _chaves_do_mapa(_fonte("present.js"), "LABEL")
    faltando = {s.name for s in TaskState} - rotulados
    assert not faltando, (
        "estados sem rotulo em portugues na tela: " + ", ".join(sorted(faltando)))


def test_every_engine_phase_has_a_human_label():
    """A fase e a primeira palavra do painel: 'Regente ...'."""
    from regente.core.operation import Phase

    rotuladas = _chaves_do_mapa(_fonte("present.js"), "FASE")
    faltando = {f.name for f in Phase} - rotuladas
    assert not faltando, "fases sem rotulo: " + ", ".join(sorted(faltando))


def test_every_run_state_has_a_human_label():
    from regente.core.model import RunState

    rotulados = _chaves_do_mapa(_fonte("present.js"), "LABEL")
    faltando = {s.name for s in RunState} - rotulados
    assert not faltando, (
        "estados de execucao sem rotulo: " + ", ".join(sorted(faltando)))


def test_every_health_signal_has_a_question_in_portuguese():
    """O defeito que isto guarda apareceu de verdade: `Database_growth`.

    O motor nomeia cada verificacao por uma chave em ingles. Sem traducao, a
    tela mostrava a chave com a primeira letra em maiuscula -- que nao e
    portugues nem ingles, e nao ajuda ninguem.
    """
    import inspect

    from regente.engine import health as modulo

    fonte = inspect.getsource(modulo)
    # As perguntas que o motor realmente emite, lidas de onde ele as escreve.
    emitidas = set(re.findall(r'Signal\(\s*"([a-z_]+)"', fonte))
    emitidas |= set(re.findall(r'question="([a-z_]+)"', fonte))
    assert emitidas, "nao achei nenhuma verificacao em engine/health.py"

    traduzidas = _chaves_do_mapa(_fonte("present.js"), "SINAL")
    faltando = emitidas - traduzidas
    assert not faltando, (
        "verificacoes de saude que apareceriam como identificador na tela: "
        + ", ".join(sorted(faltando)))


def test_every_discovery_failure_tells_the_person_what_to_do():
    """Cada falha manda a pessoa a um lugar diferente.

    Reduzi-las todas a "erro ao buscar" apagaria justamente a diferenca -- e
    quem visse a frase generica iria procurar o problema na rede quando o que
    faltava era registrar uma credencial. Por isso a tela precisa de uma frase
    POR falha, e nao de um mapa parcial com um fallback simpatico.
    """
    from regente.core.resource import Falha

    fonte = _fonte("present.js")
    descritas = _chaves_do_mapa(fonte, "FALHA_DA_BUSCA")
    faltando = {f.value for f in Falha} - descritas
    assert not faltando, (
        "falhas de descoberta que apareceriam como identificador na tela: "
        + ", ".join(sorted(faltando)))


def test_every_resource_situation_and_role_has_a_human_name():
    """`NAO_ENCONTRADO` na tela e um identificador vazando para o produto."""
    from regente.core.resource import Kind, Situacao

    fonte = _fonte("present.js")
    faltando = {s.value for s in Situacao} - _chaves_do_mapa(fonte, "SITUACAO")
    assert not faltando, ("situacoes sem nome humano: "
                          + ", ".join(sorted(faltando)))

    faltando = ({k.value for k in Kind}
                - _chaves_do_mapa(fonte, "PAPEL_DO_RECURSO"))
    assert not faltando, ("papeis de recurso sem nome humano: "
                          + ", ".join(sorted(faltando)))


def test_the_screen_never_says_a_resource_vanished():
    """Nao se afirma uma causa que ninguem apurou.

    Um recurso que nao veio na ultima busca pode ter sumido, pode ter perdido
    acesso, e pode ser que a busca nem tenha chegado a rodar. A frase precisa
    falar do que se OBSERVOU -- "sumiu" manda alguem remover uma selecao boa.
    """
    texto = _so_o_que_aparece(_fonte("present.js")
                             + _fonte("config/Integracoes.jsx"))
    for proibida in ("sumiu", "foi apagado", "deixou de existir",
                     "não existe mais", "nao existe mais"):
        assert proibida not in texto.lower(), (
            f"a tela afirma uma causa nao apurada: {proibida!r}")


def test_a_failed_search_never_looks_like_an_empty_account():
    """A tela precisa dizer que nada foi removido quando a busca falha.

    E o defeito central deste marco visto do lado de quem olha: uma lista vazia
    depois de um provedor fora do ar le-se como "minha conta esvaziou", e a
    reacao razoavel e remover a selecao que ainda estava certa.
    """
    fonte = _so_o_que_aparece(_fonte("config/Integracoes.jsx"))
    assert "nada foi removido" in fonte.lower(), (
        "a tela nao tranquiliza quem viu a busca falhar")
    assert "continua valendo" in fonte.lower()


def test_every_ability_has_a_human_description():
    """`workspace.credential.use` nao diz nada a quem administra um time."""
    from regente.core.access import Ability

    descritas = _chaves_do_mapa(_fonte("present.js"), "ACAO")
    faltando = {a.value for a in Ability} - descritas
    assert not faltando, (
        "capacidades sem descricao humana: " + ", ".join(sorted(faltando)))


def test_every_credential_use_has_a_human_description():
    """A tela oferece permissoes em caixas marcaveis, e nao `repo.pr` digitado."""
    from regente.core.credential import Use

    descritos = _chaves_do_mapa(_fonte("present.js"), "PERMISSAO")
    faltando = {u.value for u in Use} - descritos
    assert not faltando, "usos sem descricao humana: " + ", ".join(sorted(faltando))


# ===========================================================================
# 2. A TELA NAO OFERECE O QUE O MOTOR NAO ACEITA
# ===========================================================================

def test_the_rule_builder_offers_only_fields_the_engine_knows():
    """Um campo inventado vira uma regra que o motor recusa ao salvar.

    A recusa apareceria como um erro vermelho depois de a pessoa preencher o
    formulario inteiro -- que e a pior hora possivel para descobrir que a opcao
    nunca existiu.
    """
    from regente.core.selection import FIELDS

    oferecidos = _valores_de_lista(_fonte("config/Regras.jsx"), "CAMPOS")
    assert oferecidos <= set(FIELDS), (
        "a tela oferece campos que o motor nao conhece: "
        + ", ".join(sorted(oferecidos - set(FIELDS))))
    assert set(FIELDS) <= oferecidos, (
        "campos que o motor aceita e a tela esconde: "
        + ", ".join(sorted(set(FIELDS) - oferecidos)))


def test_the_rule_builder_offers_only_comparisons_the_engine_knows():
    from regente.core.selection import Match

    oferecidas = _valores_de_lista(_fonte("config/Regras.jsx"), "COMPARACOES")
    esperadas = {m.value for m in Match}
    assert oferecidas == esperadas, (
        f"comparacoes fora de sincronia: tela={sorted(oferecidas)} "
        f"motor={sorted(esperadas)}")


def test_the_rule_builder_produces_only_effects_the_engine_knows():
    from regente.core.selection import Effect

    codigo = _fonte("config/Regras.jsx")
    produzidos = set(re.findall(r'effect:\s*"([a-z]+)"', codigo))
    esperados = {e.value for e in Effect}
    assert produzidos <= esperados, (
        "a tela monta efeitos que o motor nao conhece: "
        + ", ".join(sorted(produzidos - esperados)))
    assert produzidos == esperados, (
        "efeitos que o motor aceita e a tela nao oferece: "
        + ", ".join(sorted(esperados - produzidos)))


def test_the_status_map_screen_offers_only_buckets_the_engine_knows():
    """O balde e a unica parte que o motor VALIDA -- e a que ele recusa alto."""
    from regente.ports.tasks import STATUS_BUCKETS

    oferecidos = _valores_de_lista(_fonte("config/StatusMap.jsx"), "BALDES")
    assert oferecidos <= set(STATUS_BUCKETS), (
        "a tela oferece baldes que o motor nao entende: "
        + ", ".join(sorted(oferecidos - set(STATUS_BUCKETS))))


def test_the_status_map_screen_writes_in_the_direction_the_engine_reads():
    """O defeito real: a tela gravava `{nome: balde}`, e o motor le o inverso.

    Perguntar na direcao da pessoa ("e a coluna 'A fazer'?") e gravar na direcao
    do motor (`{balde: [nomes]}`) sao duas coisas certas. Confundi-las produz um
    `400` no meio da configuracao, com uma mensagem sobre baldes que a pessoa
    nao escreveu.

    A guarda e estrutural: existe uma funcao que INVERTE, e o que vai para a
    rota passa por ela. Sem a inversao, a tela estaria gravando o formato
    errado de novo.
    """
    codigo = _fonte("config/StatusMap.jsx")
    assert "const paraMotor" in codigo, (
        "sumiu a conversao para o formato do motor; a tela voltaria a gravar "
        "`{nome: balde}`")
    assert "const paraTela" in codigo, "sumiu a conversao para a direcao humana"

    envio = re.search(r'post\(api\("/settings/status_map"\),\s*\{(.*?)\}\)',
                      codigo, re.S)
    assert envio, "nao achei a escrita de status_map"
    assert "paraMotor(" in envio.group(1), (
        "a escrita de status_map nao passa pela conversao: o formato enviado "
        "seria o da tela, e o motor recusa")


def test_the_engine_really_rejects_the_direction_the_screen_must_not_send():
    """A contraprova, contra o motor de verdade.

    Sem ela, as guardas acima seriam sobre a forma do codigo. Esta prova que a
    direcao importa: o formato humano e recusado, e o do motor e aceito.
    """
    from regente.ports.tasks import status_map_from

    with pytest.raises(ValueError) as recusa:
        status_map_from({"A fazer": "available"})
    assert "nao e um estado que o motor entenda" in str(recusa.value)

    aceito = status_map_from({"available": ["A fazer"], "ignorado": ["Arquivado"]})
    # O nome do balde nao e o nome do estado interno: `available` significa
    # "ainda nao comecou". Prender os dois aqui amarraria a guarda a uma
    # coincidencia de vocabulario.
    from regente.ports.tasks import STATUS_BUCKETS
    assert aceito["A FAZER"] is STATUS_BUCKETS["available"]
    assert aceito["ARQUIVADO"] is STATUS_BUCKETS["ignorado"]


# ===========================================================================
# 3. O CATALOGO DE PROVEDORES
# ===========================================================================

def test_the_catalogue_never_offers_an_adapter_the_engine_cannot_build():
    """Oferecer um provedor que o motor nao sabe criar adia a falha.

    A pessoa escolheria, salvaria, e o erro apareceria no primeiro ciclo --
    longe da tela onde a escolha foi feita. `catalogo()` filtra pelo registro
    justamente para isso.
    """
    from regente.adapters.registry import (
        CAPACIDADE_DO_PAPEL, _REGISTRO, catalogo)

    for papel in catalogo()["roles"]:
        cap = CAPACIDADE_DO_PAPEL[papel["role"]]
        for oferta in papel["options"]:
            assert (cap, oferta["name"]) in _REGISTRO, (
                f"o catalogo oferece {oferta['name']} para {papel['role']}, "
                f"e o registro nao sabe cria-lo")


def test_the_catalogue_never_drops_an_offer_in_silence():
    """Uma oferta escrita e nao servida some sem ninguem notar.

    E o defeito silencioso deste desenho: renomear um adapter no registro faz
    `catalogo()` descartar a oferta correspondente, e a opcao simplesmente
    deixa de existir na tela -- sem erro, sem log, sem nada.
    """
    from regente.adapters.registry import CATALOGO, catalogo

    servidas = {
        (p["role"], o["name"]) for p in catalogo()["roles"] for o in p["options"]
    }
    escritas = {
        (papel, oferta.nome)
        for papel, ofertas in CATALOGO.items()
        for oferta in ofertas
    }
    assert escritas == servidas, (
        "ofertas escritas no catalogo e descartadas ao servir: "
        + ", ".join(f"{p}/{n}" for p, n in sorted(escritas - servidas)))


def test_every_catalogue_field_says_what_it_is_for():
    """Um formulario com rotulos e sem explicacao so muda o problema de lugar.

    Quem instala o Regente hoje nao sabe o que e "o site do Jira" nem onde
    achar o proprio. O campo obrigatorio e o que trava a configuracao: ele
    precisa de ajuda ou de exemplo, sempre.
    """
    from regente.adapters.registry import catalogo

    mudos = [
        f"{p['role']}/{o['name']}/{c['key']}"
        for p in catalogo()["roles"]
        for o in p["options"]
        for c in o["fields"]
        if c["required"] and not (c["help"] or c["example"])
    ]
    assert not mudos, "campos obrigatorios sem explicacao: " + ", ".join(mudos)


def test_the_catalogue_never_asks_for_a_secret():
    """Um campo de segredo no formulario de provedor seria o bypass inteiro.

    A configuracao do provedor vai para o banco em texto claro -- e por isso ela
    diz COM QUEM falar, e nunca COM O QUE. O material continua saindo so pelo
    caminho governado.
    """
    from regente.adapters.registry import catalogo

    suspeitos = [
        f"{p['role']}/{o['name']}/{c['key']}"
        for p in catalogo()["roles"]
        for o in p["options"]
        for c in o["fields"]
        if re.search(r"token|secret|senha|password|api_key|apikey", c["key"], re.I)
    ]
    assert not suspeitos, (
        "o catalogo pede segredo no formulario de configuracao: "
        + ", ".join(suspeitos))


def test_the_catalogue_route_refuses_when_the_composition_gave_none():
    """Sem catalogo, a rota RECUSA -- ela nao finge que nao existe.

    Uma composicao que nao entrega catalogo e uma instalacao em que a tela nao
    monta formulario de provedor. Dizer isso e melhor que devolver uma lista
    vazia, que se leria como "nao ha provedor nenhum".
    """
    from regente.app.api import Api

    api = Api(read=None, catalog=None)
    r = api.resolve("GET", "/api/catalog", {}, _ninguem())
    assert r.status == 501
    assert "catalogo" in r.payload["detail"]


def _ninguem():
    from regente.core.principal import Principal

    return Principal(subject="anonimo", display="nao autenticado")


# ===========================================================================
# 4. MICROCOPIA
# ===========================================================================

#: Palavras que sao MESMO siglas ou identificadores, e continuam em caixa alta.
SIGLAS = {
    "CI", "PR", "JQL", "JSON", "YAML", "API", "URL", "HTTP", "US", "SG", "R",
    "GET", "POST", "DELETE", "OK", "ID", "UI", "CD", "AND", "OR", "TODO",
    "FAXINA", "URGENTE", "CPU", "SVG", "DEV", "MIT",
}


def test_no_user_facing_string_shouts():
    """A tela inteira em caixa alta era o defeito visual mais evidente.

    A busca e por FRASE gritada -- duas ou mais palavras em caixa alta seguidas
    dentro de um texto de interface. Uma sigla sozinha continua valendo, e
    identificadores tecnicos tambem: `POLICY_DENIED` num detalhe tecnico esta
    no lugar certo.
    """
    gritos = []
    for arquivo in arquivos():
        for numero, linha in enumerate(
                arquivo.read_text(encoding="utf-8").splitlines(), 1):
            if "//" in linha.split('"')[0]:
                continue
            for trecho in re.findall(r'"([^"]{6,})"', linha):
                palavras = re.findall(r"\b[A-ZÁÉÍÓÚÂÊÔÃÕÇ]{2,}\b", trecho)
                reais = [p for p in palavras if p not in SIGLAS
                         and not re.fullmatch(r"[A-Z_]+", p)]
                if len(reais) >= 2:
                    gritos.append(f"{arquivo.name}:{numero} {trecho[:60]}")
    assert not gritos, "frases em caixa alta na interface:\n  " + "\n  ".join(gritos)


SEM_ACENTO = {
    "nao ": "não", "voce": "você", "configuracao": "configuração",
    "organizacao": "organização", "autorizacao": "autorização",
    "seguranca": "segurança", "codigo": "código", "unico": "único",
    "credencia": None, "permissao": "permissão", "conexao": "conexão",
    "usuario": "usuário", "proximo": "próximo", "servico": "serviço",
    "enderec": "endereç", "e-mail nao": "e-mail não",
}


def _sem_acento(frases) -> list[str]:
    achados = []
    for onde, frase in frases:
        baixo = str(frase).lower()
        for errada, certa in SEM_ACENTO.items():
            if certa and errada in baixo:
                achados.append(f"{onde}: '{errada}' deveria ser '{certa}' "
                               f"em {frase!r}")
    return achados


def test_the_python_product_copy_is_also_portuguese():
    """O texto que vem do PACOTE tambem aparece na tela, e tambem precisa.

    A guarda le o que e RENDERIZADO, e nao o codigo-fonte. A primeira versao
    lia os arquivos `.py` inteiros e acusou uma docstring e a mensagem de um
    `KeyError` -- nenhum dos dois vai para tela nenhuma. Uma guarda que acusa o
    inocente ensina a ignorar a guarda, e foi a terceira vez neste marco.

    O defeito real que ela pega: "Onde o Regente le o codigo e abre pull
    requests" apareceu na interface, em portugues capenga, vindo de um conector.
    """
    from regente.adapters.registry import catalogo, conectores

    frases = []
    for nome, c in conectores().items():
        frases.append((f"conector {nome}.titulo", c.titulo))
        frases.append((f"conector {nome}.descricao", c.descricao))
        passo = c.estado()
        frases.append((f"conector {nome}.estado.titulo", passo.titulo))
        frases.append((f"conector {nome}.estado.detalhe", passo.detalhe))

    for papel, dados in (catalogo().get("roles") and
                         {r["role"]: r for r in catalogo()["roles"]} or {}
                         ).items():
        frases.append((f"catalogo {papel}.label", dados.get("label", "")))
        frases.append((f"catalogo {papel}.help", dados.get("help", "")))
        for oferta in dados.get("options", []):
            frases.append((f"catalogo {papel}/{oferta.get('name')}",
                           oferta.get("label", "")))
            frases.append((f"catalogo {papel}/{oferta.get('name')} ajuda",
                           oferta.get("help", "")))
            for campo in oferta.get("fields", []):
                frases.append((f"catalogo campo {campo.get('key')}",
                               campo.get("label", "")))
                frases.append((f"catalogo campo {campo.get('key')} ajuda",
                               campo.get("help", "")))

    achados = _sem_acento(frases)
    assert not achados, ("portugues sem acento vindo do pacote:\n  "
                         + "\n  ".join(achados))


def test_the_interface_writes_portuguese_with_accents():
    """Portugues sem acento numa tela le-se como descuido.

    A guarda procura as palavras que MAIS aparecem numa interface em portugues
    e que perdem o sentido sem acento. Ela nao tenta corrigir portugues; ela
    pega a regressao obvia.
    """
    erradas = {
        "nao ": "não", "voce": "você", "credencia": None,  # placeholder
        "configuracao": "configuração", "execucao": "execução",
        "permissao": "permissão", "conexao": "conexão", "saude": "saúde",
        "usuario": "usuário", "proximo": "próximo", "ultima": "última",
    }
    achados = []
    for arquivo in arquivos():
        for numero, linha in enumerate(
                arquivo.read_text(encoding="utf-8").splitlines(), 1):
            # So FRASE que va para a tela. Comentarios seguem a convencao ASCII
            # da casa; caminho de import, chave de rota e valor comparado com o
            # motor nao sao texto de produto -- e uma guarda que acusa o
            # inocente ensina a ignorar a guarda.
            if linha.lstrip().startswith(("//", "*", "/*", "#")):
                continue
            if re.search(r'\bimport\b|\bfrom\s+"|===|!==|startsWith\(', linha):
                continue
            # Os literais EM PARES, e nao qualquer trecho entre duas aspas: a
            # primeira versao disto casava o espaco ENTRE dois literais e
            # acusava `{ task: "tasks", execucao: "execucoes" }` de portugues
            # errado. Uma guarda que acusa o inocente ensina a ignorar a guarda.
            for trecho in re.findall(r'"((?:[^"\\]|\\.)*)"', linha):
                if len(trecho) < 12 or trecho.count(" ") < 2:
                    continue
                if trecho.startswith(("#/", "/api", "http")) or "_" in trecho:
                    continue
                for errada, certa in erradas.items():
                    if certa and errada in trecho.lower():
                        achados.append(f"{arquivo.name}:{numero} '{errada}' "
                                       f"deveria ser '{certa}'")
    assert not achados, "portugues sem acento na tela:\n  " + "\n  ".join(achados)


def test_every_table_column_carries_a_label_for_the_phone_layout():
    """Abaixo de 640px a tabela vira lista, e o rotulo vem do `data-label`.

    Sem ele, cada celula aparece sem dizer o que e -- e a alternativa, rolagem
    horizontal, esconde colunas inteiras sem avisar. Uma coluna que ninguem ve
    e uma informacao que nao existe.
    """
    tabela = _fonte("ui.jsx")
    assert 'data-label={c.oculto ? "" : c.rot}' in tabela, (
        "a tabela deixou de rotular as celulas para o layout de telefone")
    assert "table.data td::before" in (FONTE / "styles.css").read_text(
        encoding="utf-8"), "o CSS deixou de usar o `data-label` em telefone"


def test_state_is_never_communicated_by_colour_alone():
    """Quem nao distingue verde de vermelho continua lendo a tela.

    Todo indicador de estado carrega a PALAVRA. A guarda olha o componente e o
    CSS: o `Badge` renderiza texto, e o ponto colorido e desenhado por
    `::before` -- decoracao ao lado do texto, e nunca no lugar dele.
    """
    ui = _fonte("ui.jsx")
    badge = re.search(r"export const Badge = .*?\);", ui, re.S)
    assert badge, "sumiu o componente de estado"
    assert "rotulo(termo)" in badge.group(0), (
        "o indicador de estado deixou de escrever a palavra")

    css = (FONTE / "styles.css").read_text(encoding="utf-8")
    assert ".badge::before" in css, (
        "o ponto colorido deixou de ser decoracao ao lado do texto")


# ===========================================================================
# 5. A TELA NAO E AUTORIDADE
# ===========================================================================

def test_every_write_goes_through_a_named_api_route():
    """Nenhuma escrita da tela inventa rota, e nenhuma toca o banco.

    A lista e fechada de proposito: uma rota nova aqui e uma decisao de
    arquitetura, e nao um detalhe de tela.
    """
    permitidas = {
        "/operation/intent", "/settings/", "/credentials", "/access",
        "/approvals/", "/credentials/",
    }
    achadas = set(re.findall(r'(?:post|del)\(\s*(?:api\()?["`]([^"`$]*)', texto()))
    estranhas = {
        a for a in achadas
        if a and not any(a.startswith(p) for p in permitidas)
    }
    assert not estranhas, (
        "a tela escreve em rotas nao previstas: " + ", ".join(sorted(estranhas)))


def test_the_ui_reads_the_state_back_after_every_write():
    """`200` nao e o novo estado: e a aceitacao de um pedido.

    Toda escrita chama `recarregar()`. Sem isso a tela passaria a mostrar o que
    ela ACHA que gravou, que e uma segunda fonte de verdade -- e a que diverge
    e sempre a da tela.
    """
    faltando = []
    for arquivo in arquivos():
        codigo = arquivo.read_text(encoding="utf-8")
        if not re.search(r"\b(post|del)\(api\(", codigo):
            continue
        if "recarregar()" not in codigo and "aoPronto" not in codigo:
            faltando.append(arquivo.name)
    assert not faltando, (
        "escrevem e nao releem o estado: " + ", ".join(sorted(faltando)))


# ===========================================================================
# 6. ONDE OS FORMULARIOS ACONTECEM
# ===========================================================================

def test_every_form_opens_in_a_drawer_and_not_below_the_page():
    """Um formulario que brota abaixo do cartao e tres defeitos de uma vez.

    A pagina muda de altura debaixo do cursor; o formulario nasce fora da tela
    em qualquer lista que ja role; e ele esconde justamente o cartao que estava
    sendo configurado. A gaveta resolve os tres, e esta guarda impede que o
    proximo formulario volte a ser escrito no fluxo da pagina.
    """
    formularios = {
        "config/Conexoes.jsx": "FormConexao",
        "config/Credenciais.jsx": "FormCredencial",
        "config/Acesso.jsx": "FormAcesso",
        "config/Regras.jsx": "FormRegra",
        "config/StatusMap.jsx": "Editor",
    }
    for arquivo, funcao in formularios.items():
        codigo = _fonte(arquivo)
        assert "<Gaveta" in codigo, (
            f"{arquivo}: o formulario de {funcao} nao abre numa gaveta")
        # Os invólucros que o formulário TINHA quando era desenhado no fluxo da
        # página, e não `form-row`/`form-actions`, que sao layout e continuam
        # valendo dentro da gaveta. A primeira versao desta guarda procurava
        # `className="form-` e acusava a grade de campos.
        for antigo in ("form-conexao", "form-credencial", "form-acesso",
                       "form-regra"):
            assert antigo not in codigo, (
                f"{arquivo}: sobrou `{antigo}` -- o formulario voltou a ser "
                f"desenhado no fluxo da pagina")


def test_overlays_are_rendered_outside_the_page_subtree():
    """A gaveta e o modal saem para o `body`, e nao ficam onde foram escritos.

    Isto nao e preferencia: QUALQUER ancestral com `transform`, `filter` ou uma
    animacao que deixe matriz identidade vira o bloco de contencao de todo
    `position: fixed` abaixo dele -- e a sobreposicao some para fora da tela sem
    erro no console e sem nada que aponte a causa.

    Aconteceu de verdade neste marco: a animacao de entrada da pagina
    (`ui-pop`, com `animation-fill-mode: both`) deixava
    `transform: matrix(1,0,0,1,0,0)` aplicado para sempre, e no telefone a
    gaveta nascia 800px abaixo da dobra.
    """
    for arquivo in ("components/Gaveta.jsx", "components/Formulario.jsx"):
        codigo = _fonte(arquivo)
        assert "createPortal" in codigo, (
            f"{arquivo}: a sobreposicao voltou a depender de onde foi escrita")
        assert "document.body" in codigo, (
            f"{arquivo}: o portal nao aponta para o `body`")


def test_no_animation_leaves_a_transform_applied_forever():
    """`animation-fill-mode: both` sobre um `transform` e uma armadilha.

    Visualmente nao muda nada -- a matriz e a identidade. O que ela faz e
    transformar o elemento em bloco de contencao de `position: fixed`, o que so
    aparece quando alguem poe uma sobreposicao dentro dele, meses depois.
    """
    css = (FONTE / "styles.css").read_text(encoding="utf-8")
    com_transform = {
        m.group(1)
        for m in re.finditer(r"@keyframes ([\w-]+) \{(?:[^{}]|\{[^}]*\})*?transform:",
                             css, re.S)
    }
    culpados = [
        linha.strip()
        for linha in css.splitlines()
        if re.search(r"animation:.*(both|forwards)", linha)
        and any(nome in linha for nome in com_transform)
    ]
    assert not culpados, (
        "animacoes que deixam um transform aplicado depois de terminar:\n  "
        + "\n  ".join(culpados))


def test_the_drawer_can_be_closed_with_the_keyboard():
    """Uma sobreposicao sem saida pelo teclado prende quem nao usa o mouse."""
    codigo = _fonte("components/Gaveta.jsx")
    assert 'e.key === "Escape"' in codigo, "a gaveta nao fecha com Escape"
    assert 'e.key !== "Tab"' in codigo, "a gaveta nao prende o Tab"
    assert 'aria-modal="true"' in codigo, "a gaveta nao se declara modal"
    assert "anterior.current?.focus" in codigo, (
        "a gaveta nao devolve o foco para quem a abriu")
