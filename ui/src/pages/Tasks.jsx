// A lista de tasks, com busca e filtro.
//
// O filtro por situacao mora na ROTA (`#/tasks/READY`) e a busca no estado
// local. A diferenca e proposital: um filtro de situacao e um lugar para onde
// se manda alguem -- os numeros da visao geral apontam para ele -- e uma busca
// digitada nao e.

import { useState } from "react";
import { useRegente, useLeitura } from "../estado.jsx";
import { Leitura, Link, Painel, Secao, Vazio } from "../ui.jsx";
import { rotulo } from "../present.js";
import TabelaTasks, { estadosDe } from "../components/TabelaTasks.jsx";

export default function Tasks({ args }) {
  const { api } = useRegente();
  const filtro = args[0] || "";
  const [busca, setBusca] = useState("");
  const leitura = useLeitura(api("/tasks"));

  return (
    <Leitura estado={leitura} oQue="as tasks">
      {({ tasks }) => {
        if (!tasks.length) {
          return (
            <Secao
              pagina
              eyebrow="Operação"
              titulo="Tasks"
              sub="Todo o trabalho que o Regente conhece neste workspace."
            >
              <Painel>
                <Vazio
                  icone="tasks"
                  titulo="Nenhuma task ainda"
                  texto="O Regente lê o trabalho de um board. Conecte um para ele ter o que fazer."
                  acao={
                    <Link href="#/config/providers" variante="btn btn-primary">
                      Conectar um board
                    </Link>
                  }
                />
              </Painel>
            </Secao>
          );
        }

        const termo = busca.trim().toLowerCase();
        const linhas = tasks
          .filter((t) => !filtro || t.state.name === filtro)
          .filter(
            (t) =>
              !termo ||
              `${t.key} ${t.title} ${t.project}`.toLowerCase().includes(termo),
          );

        return (
          <Secao
            titulo="Tasks"
            hint={`Mostrando ${linhas.length} de ${tasks.length}`}
          >
            <div className="toolbar">
              <input
                className="input grow"
                type="search"
                placeholder="Buscar por chave, título ou projeto"
                aria-label="Buscar tasks"
                value={busca}
                onChange={(e) => setBusca(e.target.value)}
              />
              <select
                className="input"
                style={{ maxWidth: "250px" }}
                aria-label="Filtrar por situação"
                value={filtro}
                onChange={(e) =>
                  (location.hash = e.target.value
                    ? `#/tasks/${e.target.value}`
                    : "#/tasks")
                }
              >
                <option value="">Todas as situações</option>
                {estadosDe(tasks).map((s) => (
                  <option key={s} value={s}>
                    {rotulo(s)}
                  </option>
                ))}
              </select>
            </div>

            <Painel flush>
              <TabelaTasks
                linhas={linhas}
                vazio={
                  <Vazio
                    icone="tasks"
                    titulo="Nada com este filtro"
                    texto="Nenhuma task combina com a busca e a situação escolhidas."
                  />
                }
              />
            </Painel>
          </Secao>
        );
      }}
    </Leitura>
  );
}
