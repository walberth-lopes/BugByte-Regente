// As execuções. Uma página de investigação: o que rodou, quanto durou, quanto
// custou, e como terminou. Sem desfecho registrado a coluna diz isso — e não
// fica em branco, que se leria como "correu bem".

import { useRegente, useLeitura } from "../estado.jsx";
import {
  Badge,
  Dito,
  Leitura,
  Link,
  Painel,
  Secao,
  Tabela,
  Vazio,
} from "../ui.jsx";
import { Frase, when } from "../present.js";

const COLUNAS = [
  {
    rot: "Task",
    classe: "key",
    corpo: (r) => (
      <a href={`#/execucao/${encodeURIComponent(r.id)}`}>{r.task_key || r.id}</a>
    ),
  },
  { rot: "Situação", corpo: (r) => <Badge termo={r.state} /> },
  {
    rot: "Agente",
    corpo: (r) => <Dito valor={r.agent} ausente="Não informado" />,
  },
  { rot: "Começou", classe: "dim", corpo: (r) => when(r.started_at) },
  { rot: "Durou", classe: "dim", corpo: (r) => r.duration || "—" },
  {
    rot: "Resultado",
    corpo: (r) => <Dito valor={Frase(r.reason)} ausente="Sem observação" />,
  },
];

export default function Execucoes() {
  const { api } = useRegente();
  const leitura = useLeitura(api("/runs?limit=80"));

  return (
    <Leitura estado={leitura} oQue="as execuções">
      {({ runs }) => (
        <Secao
          pagina
          eyebrow="Operação"
          titulo="Execuções"
          sub="Cada vez que o Regente entregou uma task ao agente: quanto durou, quanto custou, e como terminou."
          hint={`${runs.length} mais recente(s)`}
        >
          <Painel flush>
            <Tabela
              colunas={COLUNAS}
              linhas={runs}
              chave={(r) => r.id}
              vazio={
                <Vazio
                  icone="execucoes"
                  titulo="Nenhuma execução ainda"
                  texto="Quando o Regente despachar uma task para o agente, a execução aparece aqui com duração, custo e resultado."
                  acao={<Link href="#/tasks">Ver tasks</Link>}
                />
              }
            />
          </Painel>
        </Secao>
      )}
    </Leitura>
  );
}
