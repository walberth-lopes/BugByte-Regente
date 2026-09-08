// Atividade: tudo o que o Regente registrou, do mais recente ao mais antigo.
//
// Cada linha e uma frase. A chave de identidade, o tipo interno e o
// identificador da execucao continuam ali, atras de "Ver detalhes" -- que e
// exatamente onde servem, quando alguem esta investigando o que houve.

import { useState } from "react";
import { useRegente, useLeitura } from "../estado.jsx";
import { Leitura, LinhaDoTempo, Painel, Secao, Vazio } from "../ui.jsx";

export default function Eventos() {
  const { api } = useRegente();
  const [busca, setBusca] = useState("");
  const leitura = useLeitura(api("/events?limit=200"));

  return (
    <Leitura estado={leitura} oQue="a atividade">
      {({ events }) => {
        const termo = busca.trim().toLowerCase();
        const linhas = termo
          ? events.filter((e) =>
              `${e.kind} ${e.actor} ${e.summary}`.toLowerCase().includes(termo),
            )
          : events;

        return (
          <Secao
            pagina
            eyebrow="Diagnóstico"
            titulo="Atividade"
            sub="Tudo o que o Regente registrou, contado em português. O registro original fica em “Ver detalhes”."
            hint={
              termo
                ? `${linhas.length} de ${events.length}`
                : `${events.length} registros mais recentes`
            }
          >
            <div className="toolbar">
              <input
                className="input grow"
                type="search"
                placeholder="Buscar na atividade"
                aria-label="Buscar na atividade"
                value={busca}
                onChange={(e) => setBusca(e.target.value)}
              />
            </div>
            <Painel>
              {linhas.length ? (
                <LinhaDoTempo eventos={linhas} />
              ) : (
                <Vazio
                  icone="atividade"
                  titulo={
                    events.length
                      ? "Nada com esta busca"
                      : "Nenhuma atividade ainda"
                  }
                  texto={
                    events.length
                      ? "Nenhum registro combina com o que você digitou."
                      : "Assim que o Regente rodar um ciclo, tudo o que ele fizer fica registrado aqui."
                  }
                />
              )}
            </Painel>
          </Secao>
        );
      }}
    </Leitura>
  );
}
