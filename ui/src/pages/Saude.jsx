// A saúde: "o Regente está bem?", respondida em três níveis.
//
// A tela não tem uma segunda opinião sobre estar tudo bem: uma segunda
// implementação de saúde na borda seria a que não viu o problema. O que ela faz
// é separar o que está funcionando do que pede leitura, e — para o que pede —
// responder as três perguntas: o que aconteceu, por que importa, o que fazer.

import { useRegente, useLeitura } from "../estado.jsx";
import {
  Alerta,
  Badge,
  Dito,
  Leitura,
  Link,
  Painel,
  Secao,
  Tecnico,
  Vazio,
} from "../ui.jsx";
import { pergunta, when } from "../present.js";

/**
 * O que fazer com cada sinal ruim.
 *
 * Um sinal sem próximo passo é um diagnóstico sem receita. Onde o Regente
 * resolve sozinho, o texto diz isso — e essa também é uma resposta útil.
 */
const OQUEFAZER = {
  expired_leases: [
    "O Regente devolve essas reservas sozinho no próximo ciclo. Se o número não cair, um worker pode ter morrido sem se anunciar.",
    null,
  ],
  stalled_work: [
    "Abra a task parada e veja o motivo registrado nela.",
    ["Ver o que precisa de você", "#/atencao"],
  ],
  dead_end_tasks: [
    "Essas tasks chegaram a um estado de onde o Regente não tem para onde seguir. Uma pessoa precisa decidir.",
    ["Ver o que precisa de você", "#/atencao"],
  ],
  escalations: [
    "Há decisão esperando. Enquanto ela não for tomada, a task não anda.",
    ["Decidir agora", "#/atencao"],
  ],
  budget: [
    "O orçamento do dia limita quantas tasks o Regente despacha. Ele volta a despachar quando o dia virar, ou quando o limite for aumentado na configuração.",
    ["Abrir configuração", "#/config/avancado"],
  ],
  provider_failures: [
    "Um serviço externo recusou ou não respondeu. Teste a conexão para descobrir se é a credencial ou o serviço.",
    ["Testar conexões", "#/config/providers"],
  ],
  interrupted_runs: [
    "Execuções interrompidas costumam vir de um processo encerrado no meio. O Regente tenta de novo; se repetir, veja a execução.",
    ["Ver execuções", "#/execucoes"],
  ],
  deliveries_in_flight: [
    "Há entrega sem desfecho — normalmente um CI que ainda não terminou.",
    ["Ver entregas", "#/entregas"],
  ],
  database_growth: [
    "Este sinal só compara com dias anteriores. Numa instalação nova ele fica assim até existir um segundo dia de história.",
    null,
  ],
  orphan_areas: [
    "Sobrou pasta de trabalho sem execução dona. Ela não atrapalha o processamento, e pode ser removida com segurança pelo terminal.",
    null,
  ],
  running: [
    "Nada está executando agora. Se você esperava o contrário, verifique se o processamento está ligado.",
    ["Ver o painel", "#/"],
  ],
};

const TOM = { OK: "ok", ATTENTION: "warn", UNKNOWN: "warn", STUCK: "danger" };

export default function Saude() {
  const { api } = useRegente();
  const leitura = useLeitura([api("/health"), api("/connections")]);

  return (
    <Leitura estado={leitura} oQue="a saúde">
      {(h, con) => {
        const sinais = h.signals || [];
        const ruins = sinais.filter((s) => s.level !== "OK");
        const bons = sinais.filter((s) => s.level === "OK");

        return (
          <>
            <div className="secao-cabeca">
              <h1>Saúde</h1>
              <Badge termo={h.level} />
              <span className="spacer" />
              <span className="hint">Medida em {when(h.at)}</span>
            </div>
            <p className="muted">
              O Regente responde estas perguntas sobre si mesmo a cada leitura. A
              tela mostra as respostas dele — ela não tem uma segunda opinião.
            </p>

            <Conexoes conexoes={con.connections} />

            {ruins.length ? (
              <Secao
                titulo="O que pede leitura"
                hint={`${ruins.length} de ${sinais.length} verificações`}
              >
                <div className="stack">
                  {ruins.map((s) => {
                    const [oQueFazer, acao] = OQUEFAZER[s.question] || [null, null];
                    return (
                      <Painel key={s.question}>
                        <div className="row">
                          <h3>{pergunta(s.question)}</h3>
                          <Badge termo={s.level} />
                        </div>
                        <div className="q" style={{ marginTop: "var(--s-3)" }}>
                          <span className="rot">O que o Regente encontrou</span>
                          <Dito valor={s.detail} ausente="Sem detalhe registrado." />
                        </div>
                        {(s.evidence || []).length > 0 && (
                          <div className="q">
                            <span className="rot">Onde</span>
                            <ul className="perms">
                              {s.evidence.slice(0, 6).map((e, i) => (
                                <li key={i}>
                                  <span className="dim" aria-hidden="true">
                                    ·
                                  </span>
                                  {e}
                                </li>
                              ))}
                            </ul>
                          </div>
                        )}
                        {oQueFazer && (
                          <div className="q">
                            <span className="rot">O que fazer</span>
                            <span>{oQueFazer}</span>
                            {acao && (
                              <div style={{ marginTop: "var(--s-3)" }}>
                                <Link href={acao[1]} variante="btn btn-sm">
                                  {acao[0]}
                                </Link>
                              </div>
                            )}
                          </div>
                        )}
                        <Tecnico
                          pares={[
                            ["Verificação", s.question],
                            ["Nível", s.level],
                          ]}
                        />
                      </Painel>
                    );
                  })}
                </div>
              </Secao>
            ) : (
              <Painel>
                <Vazio
                  icone="ok"
                  titulo="Nenhum sinal de problema"
                  texto={`Todas as ${sinais.length} verificações responderam dentro do esperado.`}
                />
              </Painel>
            )}

            {bons.length > 0 && (
              <Secao titulo="Funcionando" hint={`${bons.length} verificações`}>
                <div className="grid-cards">
                  {bons.map((s) => (
                    <div className="mini" key={s.question}>
                      <span className="badge" data-tone="ok">
                        Normal
                      </span>
                      <div className="t">{pergunta(s.question)}</div>
                      <div className="d">
                        <Dito valor={s.detail} ausente="Sem detalhe" />
                      </div>
                    </div>
                  ))}
                </div>
              </Secao>
            )}

            <Tecnico
              titulo="Medidas do motor"
              pares={Object.entries(h.measurements || {}).sort()}
            />
          </>
        );
      }}
    </Leitura>
  );
}

/**
 * As conexões, aqui também.
 *
 * "O Regente está saudável?" inclui "ele alcança o que precisa alcançar?", e
 * essa resposta mora em `/connections`. Repetir o cartão aqui evita mandar
 * alguém que veio investigar um problema para outra página descobrir metade
 * dele.
 */
function Conexoes({ conexoes }) {
  if (!conexoes?.length) return null;
  return (
    <Secao titulo="Conexões" hint="O que o Regente alcança daqui">
      <div className="grid-cards">
        {conexoes.map((c) => (
          <div className="mini" key={c.role}>
            <Badge termo={c.state} />
            <div className="t">{c.adapter || "Não escolhido"}</div>
            <div className="d">
              {c.needs_credential
                ? `${c.live_credentials} credencial(is) viva(s)`
                : "Não precisa de credencial"}
            </div>
          </div>
        ))}
      </div>
    </Secao>
  );
}
