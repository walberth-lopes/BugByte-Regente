// A lista de tasks, na MESMA ordem que o motor usa.
//
// Prioridade, e a chave como desempate. Uma tela que ordena diferente do motor
// faz a pessoa ver a segunda da lista rodar primeiro e concluir que o Regente
// nao respeita a propria fila.

import { Tabela, Titulo, Badge } from "../ui.jsx";
import { Frase, DONO, rotulo } from "../present.js";

export const emOrdem = (linhas) =>
  [...linhas].sort(
    (a, b) => a.priority - b.priority || String(a.key).localeCompare(String(b.key)),
  );

const COLUNAS = [
  {
    rot: "Task",
    classe: "key",
    corpo: (t) => (
      <Titulo
        chave={t.key}
        sub={t.title}
        href={`#/task/${encodeURIComponent(t.id)}`}
      />
    ),
  },
  { rot: "Situação", corpo: (t) => <Badge termo={t.state.name} /> },
  {
    rot: "Próximo passo",
    corpo: (t) => (
      <>
        {t.state.next_action ? (
          Frase(t.state.next_action)
        ) : (
          <span className="dim">Sem próxima etapa</span>
        )}
        <div className="dim" style={{ fontSize: "var(--fs-xs)" }}>
          {DONO[t.state.owner] || t.state.owner}
        </div>
      </>
    ),
  },
  { rot: "Prioridade", num: true, corpo: (t) => t.priority },
  { rot: "Parada há", classe: "dim", corpo: (t) => t.state.age },
];

export default function TabelaTasks({ linhas, vazio }) {
  return (
    <Tabela
      colunas={COLUNAS}
      linhas={emOrdem(linhas)}
      chave={(t) => t.id}
      vazio={vazio}
    />
  );
}

export const estadosDe = (tasks) =>
  [...new Set(tasks.map((t) => t.state.name))].sort((a, b) =>
    rotulo(a).localeCompare(rotulo(b), "pt-BR"),
  );
