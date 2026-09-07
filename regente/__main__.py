# -*- coding: utf-8 -*-
"""`python -m regente`.

Existe porque o entry point instalado (`regente`) so aparece no PATH depois de
`pip install` E da ativacao do ambiente -- e a primeira coisa que alguem tenta
quando o comando "nao e reconhecido" e chamar o modulo pelo interpretador.
Sem este arquivo, `python -m regente` respondia que o pacote nao e executavel,
que e uma segunda parede na mesma esquina.
"""

from .cli import main

raise SystemExit(main())
