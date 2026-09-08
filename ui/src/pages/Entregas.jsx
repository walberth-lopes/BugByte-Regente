// As entregas: uma fila de resultados, e não um registro de log.
//
// Cada cartão responde "o que saiu daqui, e onde parou". O veredito do CI vem
// do motor; a tela não tem uma segunda definição de verde.

import { useRegente, useLeitura } from "../estado.jsx";
import { Leitura, Link, Painel, Secao, Vazio } from "../ui.jsx";
import Entrega from "../components/Entrega.jsx";

export default function Entregas() {
  const { api } = useRegente();
  const leitura = useLeitura(api("/deliveries"));

  return (
    <Leitura estado={leitura} oQue="as entregas">
      {({ deliveries }) => {
        if (!deliveries.length) {
          return (
            <Secao
              pagina
              eyebrow="Resultado"
              titulo="Entregas"
              sub="O que saiu daqui, e onde parou."
            >
              <Painel>
                <Vazio
                  icone="entregas"
                  titulo="Nenhuma entrega ainda"
                  texto="Quando o Regente publicar uma mudança, ela aparece aqui com o commit, o pull request e o resultado do CI — do começo ao fim, numa linha só."
                  acao={<Link href="#/tasks">Ver tasks</Link>}
                />
              </Painel>
            </Secao>
          );
        }
        const verdes = deliveries.filter((d) => d.ci_green).length;
        return (
          <Secao
            titulo="Entregas"
            hint={`${deliveries.length} no total · ${verdes} com CI verde`}
          >
            <div className="stack">
              {deliveries.map((d) => (
                <Entrega key={d.id} entrega={d} />
              ))}
            </div>
          </Secao>
        );
      }}
    </Leitura>
  );
}
