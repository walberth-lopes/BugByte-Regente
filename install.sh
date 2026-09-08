#!/usr/bin/env sh
# Instalador do Regente para Linux e macOS.
#
#   curl -LsSf https://raw.githubusercontent.com/walberth-lopes/BugByte-Regente/main/install.sh | sh
#
# O que ele faz, e por que faz assim.
#
# O Regente e um programa Python, e a forma tradicional de instalar um programa
# Python -- criar um ambiente virtual, ativar, instalar dentro -- e um ritual
# que so faz sentido para quem desenvolve em Python. Quem quer USAR uma
# ferramenta espera o que o `gcloud` faz: baixa, instala, e o comando existe em
# qualquer terminal.
#
# `uv tool install` faz exatamente isso: cria um ambiente ISOLADO para a
# ferramenta (as dependencias dela nao se misturam com nada seu) e poe um
# atalho num diretorio do PATH. E se nao houver Python nenhum na maquina, o uv
# baixa um -- entao a unica dependencia real deste script e ele mesmo.
#
# POSIX sh de proposito: `sh` existe em toda parte, `bash` nao (Alpine, alguns
# contêineres). Nada aqui usa extensao de bash.

set -eu

REPO="${REGENTE_REPO:-https://github.com/walberth-lopes/BugByte-Regente}"
REF="${REGENTE_REF:-main}"
# `REGENTE_FROM` permite instalar do PyPI (`regente`), de um caminho local, ou
# de outro repositorio -- sem editar este arquivo.
FROM="${REGENTE_FROM:-git+${REPO}@${REF}}"

diga() { printf '%s\n' "$*"; }
erro() { printf '\nErro: %s\n' "$*" >&2; exit 1; }

diga ""
diga "  Regente — instalacao"
diga "  --------------------"
diga ""

# ---------------------------------------------------------------- 1. o uv
if command -v uv >/dev/null 2>&1; then
    diga "  [1/3] uv ja instalado ($(uv --version 2>/dev/null || echo '?'))"
else
    diga "  [1/3] Instalando o uv (o gerenciador que isola a ferramenta)..."
    if command -v curl >/dev/null 2>&1; then
        curl -LsSf https://astral.sh/uv/install.sh | sh >/dev/null 2>&1 ||
            erro "nao consegui instalar o uv. Instale-o manualmente:
       curl -LsSf https://astral.sh/uv/install.sh | sh"
    elif command -v wget >/dev/null 2>&1; then
        wget -qO- https://astral.sh/uv/install.sh | sh >/dev/null 2>&1 ||
            erro "nao consegui instalar o uv. Instale-o manualmente:
       wget -qO- https://astral.sh/uv/install.sh | sh"
    else
        erro "preciso de curl ou wget para baixar o uv."
    fi
    # O instalador do uv poe o binario aqui, e o PATH desta sessao ainda nao
    # sabe disso.
    for d in "$HOME/.local/bin" "$HOME/.cargo/bin"; do
        [ -x "$d/uv" ] && PATH="$d:$PATH" && export PATH
    done
    command -v uv >/dev/null 2>&1 || erro "o uv foi instalado e nao esta no PATH desta sessao.
       Abra um terminal novo e rode este script de novo."
fi

# ------------------------------------------------------------ 2. o Regente
diga "  [2/3] Instalando o Regente a partir de ${FROM}..."
# `--force` para reinstalar por cima de uma versao anterior sem pedir nada:
# quem roda o instalador de novo esta pedindo a versao nova.
uv tool install --force "$FROM" ||
    erro "a instalacao falhou. A saida do uv acima diz o motivo."

# --------------------------------------------------------------- 3. o PATH
# Esta e a etapa que quase todo instalador esquece, e e a que produz o
# `'regente' is not recognized` na cara de quem seguiu tudo certo.
diga "  [3/3] Colocando o Regente no PATH..."
uv tool update-shell >/dev/null 2>&1 || true

BIN="$(uv tool dir --bin 2>/dev/null || echo "$HOME/.local/bin")"
PATH="$BIN:$PATH"
export PATH

diga ""
if command -v regente >/dev/null 2>&1; then
    diga "  Pronto. $(regente --version)"
else
    diga "  Pronto — o Regente foi instalado em ${BIN}."
fi
diga ""
diga "  IMPORTANTE: um terminal que ja estava aberto nao conhece o PATH novo."
diga "  Abra um terminal NOVO e confira:"
diga ""
diga "      regente --version"
diga ""
diga "  Depois, numa pasta vazia, um comando so:"
diga ""
diga "      regente init"
diga ""
diga "  Ele pergunta os nomes, concede o acesso e abre a Mission Control."
diga ""
diga "  Para atualizar depois:   uv tool upgrade regente"
diga "  Para desinstalar:        uv tool uninstall regente"
diga ""
