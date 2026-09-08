// De onde este ajuste veio, e o que fazer a respeito.
//
// O Regente tem duas fontes de configuração: o arquivo `regente.yaml`, que é a
// base, e o que foi definido por aqui, que fica por cima. Sem dizer isso,
// alguém edita o arquivo, nada muda, e a conclusão razoável é que o Regente
// está quebrado.
//
// O caso perigoso — o arquivo diz uma coisa e a tela diz outra — é dito com
// todas as letras, e vem com o botão que o desfaz.

import { useState } from "react";
import { del } from "../api.js";
import { useRegente } from "../estado.jsx";
import { Alerta } from "../ui.jsx";
import { digaOErro } from "../present.js";

const TITULO = {
  arquivo: "Vem do arquivo de configuração",
  tela: "Definido aqui na interface",
  ausente: "Ainda não configurado",
};

const FRASE = {
  arquivo:
    "Está no regente.yaml deste workspace. Editar o arquivo muda o que o Regente usa.",
  ausente: "Nem o arquivo nem esta interface disseram nada sobre isto ainda.",
};

export default function Procedencia({ campo, chave }) {
  const { api, podeAqui, recarregar } = useRegente();
  const [erro, setErro] = useState(null);
  const [indo, setIndo] = useState(false);

  const frase =
    campo.source === "tela"
      ? campo.conflicts
        ? "Foi definido aqui e substitui o que está no regente.yaml. Editar o arquivo não muda nada enquanto esta definição existir — remova-a para o arquivo voltar a valer."
        : "Foi definido aqui na interface. O regente.yaml não diz nada sobre isto."
      : FRASE[campo.source] || campo.explain || "";

  async function voltarAoArquivo() {
    setIndo(true);
    const r = await del(api(`/settings/${encodeURIComponent(chave)}`));
    setIndo(false);
    if (!r.ok) {
      setErro(digaOErro(r));
      return;
    }
    recarregar();
  }

  return (
    <>
      <Alerta
        tone={campo.conflicts ? "warn" : "info"}
        icone={campo.conflicts ? "!" : "i"}
        titulo={TITULO[campo.source] || campo.source}
        detalhe={frase}
        acao={
          campo.overridden && podeAqui("workspace.settings.write") ? (
            <button
              className="btn btn-sm"
              disabled={indo}
              onClick={voltarAoArquivo}
            >
              Voltar a usar o arquivo
            </button>
          ) : null
        }
      />
      {erro && (
        <div className="outcome bad" role="status">
          {erro}
        </div>
      )}
    </>
  );
}
