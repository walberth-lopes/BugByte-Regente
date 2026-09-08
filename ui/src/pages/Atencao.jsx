// Precisa de voce.
//
// Quatro coisas diferentes, mantidas separadas de proposito: uma decisao sua
// nao e um bloqueio, e nenhuma das duas e uma espera. Juntar as tres numa lista
// de "problemas" faria alguem procurar o que fazer numa linha em que nao ha
// nada a fazer.

import { useRegente, useLeitura } from "../estado.jsx";
import { Leitura, Link, Painel, Secao, Vazio, LinhaDoTempo } from "../ui.jsx";
import CartaoDecisao from "../components/Decisao.jsx";
import TabelaTasks from "../components/TabelaTasks.jsx";

export default function Atencao() {
  const { api } = useRegente();
  const leitura = useLeitura([
    api("/escalations"),
    api("/tasks"),
    api("/events?limit=60"),
  ]);

  return (
    <Leitura estado={leitura} oQue="o que precisa de você">
      {(esc, t, ev) => {
        const escaladas = esc.escalations;
        const tasks = t.tasks;
        // Quem decidiu e quando, lido da trilha do motor -- e nao do que o
        // navegador acabou de enviar. O que a tela mostra e o que ficou gravado.
        const decididas = ev.events.filter(
          (e) => e.kind === "decisao_humana_autenticada",
        );
        const bloqueadas = tasks.filter((x) => x.blocked);
        const paradas = tasks.filter((x) => x.state.owner === "nobody");
        const esperando = tasks.filter((x) => x.state.owner === "external");

        const nada =
          !escaladas.length &&
          !bloqueadas.length &&
          !paradas.length &&
          !esperando.length;

        return (
          <>
            <Secao
              pagina
              eyebrow="Operação"
              titulo="Precisa de você"
              sub="Quatro coisas diferentes, mantidas separadas: uma decisão sua não é um bloqueio, e nenhuma das duas é uma espera."
            >
            </Secao>

            {nada && (
              <Painel>
                <Vazio
                  icone="ok"
                  titulo="Nada esperando por você"
                  texto="Nenhuma decisão na fila, nada bloqueado e nada parado sem rota."
                  acao={<Link href="#/">Ver a visão geral</Link>}
                />
              </Painel>
            )}

            {escaladas.length > 0 && (
              <Secao
                titulo="Decisões na fila"
                hint={`${escaladas.length} esperando`}
              >
                <div className="stack">
                  {escaladas.map((e) => (
                    <CartaoDecisao key={e.id} escalada={e} />
                  ))}
                </div>
              </Secao>
            )}

            {bloqueadas.length > 0 && (
              <Secao
                titulo="Bloqueadas"
                hint="Algo impede, e não é uma decisão sua"
              >
                <Painel flush>
                  <TabelaTasks linhas={bloqueadas} />
                </Painel>
              </Secao>
            )}

            {paradas.length > 0 && (
              <Secao
                titulo="Sem rota"
                hint="O Regente não tem etapa que avance daqui"
              >
                <Painel flush>
                  <TabelaTasks linhas={paradas} />
                </Painel>
              </Secao>
            )}

            {esperando.length > 0 && (
              <Secao
                titulo="Aguardando um sistema de fora"
                hint="Nada a fazer além de esperar"
              >
                <Painel flush>
                  <TabelaTasks linhas={esperando} />
                </Painel>
              </Secao>
            )}

            {decididas.length > 0 && (
              <Secao
                titulo="Decididas recentemente"
                hint="Como ficou gravado na trilha"
              >
                <Painel>
                  <LinhaDoTempo eventos={decididas} />
                </Painel>
              </Secao>
            )}
          </>
        );
      }}
    </Leitura>
  );
}
