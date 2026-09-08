# Instalador do Regente para Windows.
#
#   irm https://raw.githubusercontent.com/walberth-lopes/BugByte-Regente/main/install.ps1 | iex
#
# O gemeo do `install.sh`, e o raciocinio e o mesmo: criar um ambiente virtual e
# ativa-lo e um ritual de quem desenvolve em Python. Quem quer USAR a ferramenta
# espera o que o `gcloud` faz -- baixa, instala, e o comando existe em qualquer
# terminal novo.
#
# `uv tool install` cria um ambiente isolado para a ferramenta e poe um atalho
# num diretorio do PATH. Sem Python na maquina, o uv baixa um: a unica
# dependencia real deste script e ele mesmo.

$ErrorActionPreference = 'Stop'

$repo = if ($env:REGENTE_REPO) { $env:REGENTE_REPO } else { 'https://github.com/walberth-lopes/BugByte-Regente' }
$ref  = $env:REGENTE_REF
# O padrao e o PyPI: e o caminho mais curto e o que nao depende do GitHub estar
# no ar. `REGENTE_REF` instala um branch (util para testar uma versao antes de
# publicar), e `REGENTE_FROM` aponta para uma pasta local ou outro repositorio.
$from = if ($env:REGENTE_FROM) { $env:REGENTE_FROM }
        elseif ($ref) { "git+$repo@$ref" }
        else { 'regente' }

function Diga($t) { Write-Host $t }
function Morra($t) { Write-Host ""; Write-Host "Erro: $t" -ForegroundColor Red; exit 1 }

Diga ""
Diga "  Regente - instalacao"
Diga "  --------------------"
Diga ""

# ---------------------------------------------------------------- 1. o uv
if (Get-Command uv -ErrorAction SilentlyContinue) {
    Diga "  [1/3] uv ja instalado ($(uv --version))"
} else {
    Diga "  [1/3] Instalando o uv (o gerenciador que isola a ferramenta)..."
    try {
        Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
    } catch {
        Morra "nao consegui instalar o uv. Instale-o manualmente:
       irm https://astral.sh/uv/install.ps1 | iex"
    }
    # O instalador do uv poe o binario aqui, e o PATH desta sessao ainda nao
    # sabe disso.
    foreach ($d in @("$env:USERPROFILE\.local\bin", "$env:USERPROFILE\.cargo\bin")) {
        if (Test-Path (Join-Path $d 'uv.exe')) { $env:PATH = "$d;$env:PATH" }
    }
    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
        Morra "o uv foi instalado e nao esta no PATH desta sessao.
       Abra um terminal novo e rode este script de novo."
    }
}

# ------------------------------------------------------------ 2. o Regente
#
# O REGENTE PRECISA DE PYTHON 3.13, E O uv BAIXA UM se a maquina nao tiver.
#
# `UV_PYTHON_DOWNLOADS=automatic` e explicito de proposito: quem tem
# `python-downloads = never` na configuracao do uv veria a instalacao falhar com
# uma mensagem sobre versao de interpretador, sem nenhuma pista de que o proprio
# uv resolveria aquilo. Instalar uma ferramenta nao deveria exigir que a pessoa
# saiba o que e um interpretador.
$env:UV_PYTHON_DOWNLOADS = 'automatic'

Diga "  [2/3] Instalando o Regente a partir de $from..."
Diga "        (se faltar Python 3.13 na maquina, o uv baixa um - ~25 MB)"
# `--force` reinstala por cima de uma versao anterior sem perguntar: quem roda o
# instalador de novo esta pedindo a versao nova.
#
# Um detalhe que ja mordeu de verdade neste projeto: no Windows, um `regente.exe`
# EM EXECUCAO nao pode ser sobrescrito -- a instalacao falha com "Access is
# denied". Por isso o aviso vem antes, e nao depois do erro.
$rodando = Get-Process -Name 'regente' -ErrorAction SilentlyContinue
if ($rodando) {
    Morra "ha um Regente em execucao (pid $($rodando.Id -join ', ')).
       O Windows nao deixa substituir um executavel aberto.
       Feche o `regente ui` ou o `regente run` e rode este script de novo."
}
uv tool install --force $from
if ($LASTEXITCODE -ne 0) { Morra "a instalacao falhou. A saida do uv acima diz o motivo." }

# --------------------------------------------------------------- 3. o PATH
# A etapa que quase todo instalador esquece, e a que produz o
# "'regente' is not recognized" na cara de quem seguiu tudo certo.
Diga "  [3/3] Colocando o Regente no PATH..."
uv tool update-shell 2>&1 | Out-Null

$bin = try { uv tool dir --bin 2>$null } catch { "$env:USERPROFILE\.local\bin" }
if ($bin) { $env:PATH = "$bin;$env:PATH" }

Diga ""
if (Get-Command regente -ErrorAction SilentlyContinue) {
    Diga "  Pronto. $(regente --version)"
} else {
    Diga "  Pronto - o Regente foi instalado em $bin."
}
Diga ""
Diga "  IMPORTANTE: um terminal que ja estava aberto nao conhece o PATH novo."
Diga "  Abra um terminal NOVO (PowerShell ou Prompt) e confira:"
Diga ""
Diga "      regente --version"
Diga ""
Diga "  Depois, numa pasta vazia, um comando so:"
Diga ""
Diga "      regente init"
Diga ""
Diga "  Ele pergunta os nomes, concede o acesso e abre a Mission Control."
Diga ""
Diga "  Para atualizar depois:   uv tool upgrade regente"
Diga "  Para desinstalar:        uv tool uninstall regente"
Diga ""
