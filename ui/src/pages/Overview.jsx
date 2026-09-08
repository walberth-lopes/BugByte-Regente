// O painel.
//
// A ordem da página é a tese do produto: ESTADO primeiro (o que o Regente está
// fazendo agora), AÇÃO em seguida (os controles, no topo, porque ligar e
// desligar é a operação principal), depois O QUE PRECISA DE VOCÊ, e só então os
// NÚMEROS. O detalhe técnico fica atrás de "Ver detalhes técnicos", em todo
// lugar.
//
// Nada aqui conclui nada. Fase, saúde, bloqueio e contagem chegam prontos da
// API; esta página escolhe onde pôr na tela o que já veio decidido.

import { useEffect, useState } from "react";
import { post } from "../api.js";
import { useRegente, useLeitura, chaveDaSessao } from "../estado.jsx";
import {
  Alerta,
  Badge,
  Botao,
  Cabecalho,
  Faixa,
  Leitura,
  Link,
  LinhaDoTempo,
  Painel,
  Secao,
  Tecnico,
  Vazio,
} from "../ui.jsx";
import {
  EXPLICA_FASE,
  PAPEL,
  digaOErro,
  fase as faseDe,
  pergunta,
  rotulo,
  when,
} from "../present.js";
import { Icon, COR } from "../Icon.jsx";
import { ComoConceder } from "../components/Acesso.jsx";

export default function Overview({ setContagens }) {
  const { api } = useRegente();
  const leitura = useLeitura([
    api("/overview"),
    api("/operation"),
    api("/connections"),
    api("/events?limit=10"),
  ]);

  return (
    <Leitura estado={leitura} oQue="o painel" alto>
      {(o, op, con, ev) => (
        <Conteudo
          o={o}
          op={op}
          conexoes={con.connections}
          eventos={ev.events}
          setContagens={setContagens}
        />
      )}
    </Leitura>
  );
}

function Conteudo({ o, op, conexoes, eventos, setContagens }) {
  useEffect(() => {
    setContagens({
      atencao: o.needs_human + o.blocked,
      tasks: o.total_tasks,
      entregas: o.deliveries_in_flight,
    });
  }, [
    o.needs_human,
    o.blocked,
    o.total_tasks,
    o.deliveries_in_flight,
    setContagens,
  ]);

  const passos = prontidao(conexoes, o);
  const faltam = passos.filter((p) => p.estado !== "done");

  return (
    <>
      <Cabecalho
        eyebrow="Operação"
        titulo="Painel"
        sub={`Tudo o que está acontecendo em ${o.client} / ${o.workspace_name}.`}
      />

      <Motor op={op} o={o} />

      <Numeros o={o} />

      {faltam.length > 0 && (
        <Onboarding passos={passos} faltam={faltam.length} />
      )}

      <div className="cols">
        <div className="stack">
          <Atencao o={o} conexoes={conexoes} />
        </div>
        <div className="stack">
          <SaudeResumo h={o.health} />
          <Painel
            cabeca="Atividade recente"
            acao={
              <a className="hint" href="#/atividade">
                Ver tudo
              </a>
            }
            flush
          >
            <div className="panel-corpo">
              {eventos.length ? (
                <LinhaDoTempo eventos={eventos} alto />
              ) : (
                <Vazio
                  icone="atividade"
                  titulo="Nada aconteceu ainda"
                  texto="Quando o Regente rodar um ciclo, o que ele fizer aparece aqui."
                />
              )}
            </div>
          </Painel>
        </div>
      </div>
    </>
  );
}

/**
 * O estado do motor, e a ação que faz sentido agora.
 *
 * Os controles gravam INTENÇÃO — a tela não cria processos. O único caso em que
 * isso importa para quem opera é `DEGRADED`, e ali a frase diz exatamente o que
 * fazer, em vez de transformar a página inteira num aviso.
 */
function Motor({ op, o }) {
  const { api, podeAqui, recarregar, identidade } = useRegente();
  const [erro, setErro] = useState(null);
  const [enviando, setEnviando] = useState(false);

  const f = op.phase || "STOPPED";
  const [nome, tone] = faseDe(f);
  const proc = op.process;
  const pode = op.controllable !== false && podeAqui("workspace.engine.control");

  async function pedir(intent) {
    setEnviando(true);
    setErro(null);
    // `post` NÃO lança — devolve `{ok, status, payload}`. Uma versão anterior
    // deste bloco usava try/catch, e por isso toda recusa acabava em silêncio:
    // a pessoa clicava em Iniciar e a tela não dizia nada.
    const r = await post(api("/operation/intent"), { intent });
    setEnviando(false);
    if (!r.ok) {
      setErro(digaOErro(r));
      return;
    }
    recarregar();
  }

  const B = ({ children, intent, variante, icone }) => (
    <Botao
      variante={variante}
      icone={icone}
      disabled={enviando}
      onClick={() => pedir(intent)}
    >
      {children}
    </Botao>
  );

  let acoes = null;
  if (pode) {
    if (f === "RUNNING") {
      acoes = (
        <>
          <B intent="PAUSED" variante="btn-lg" icone="pausar">
            Pausar
          </B>
          <B intent="STOPPED" variante="btn-danger btn-lg" icone="parar">
            Parar
          </B>
        </>
      );
    } else if (f === "PAUSED") {
      acoes = (
        <>
          <B intent="RUNNING" variante="btn-primary btn-lg" icone="retomar">
            Retomar
          </B>
          <B intent="STOPPED" variante="btn-danger btn-lg" icone="parar">
            Parar
          </B>
        </>
      );
    } else if (f === "STOPPED") {
      acoes = (
        <B intent="RUNNING" variante="btn-primary btn-lg" icone="iniciar">
          Iniciar o Regente
        </B>
      );
    } else if (f === "DEGRADED") {
      acoes = (
        <B intent="STOPPED" variante="btn-lg" icone="parar">
          Registrar como parado
        </B>
      );
    } else {
      acoes = (
        <B intent="STOPPED" variante="btn-danger btn-lg" icone="parar">
          Parar
        </B>
      );
    }
  }

  const ICONE = {
    RUNNING: "rodando",
    PAUSED: "pausar",
    STOPPED: "parar",
    STARTING: "relogio",
    STOPPING: "relogio",
    DEGRADED: "perigo",
  };

  return (
    <section
      className="heroi"
      style={{ "--tone": COR[tone] }}
      data-vivo={String(f === "RUNNING")}
    >
      <div>
        <div className="titulo">
          <span className="bolha">
            <Icon nome={ICONE[f] || "relogio"} tamanho={24} />
          </span>
          <h1>Regente {nome}</h1>
        </div>

        <p className="lede">{EXPLICA_FASE[f] || op.explain}</p>

        {f === "DEGRADED" && (
          <div style={{ marginTop: "var(--s-4)" }}>
            <Alerta
              tone="danger"
              titulo="Nenhum processo está atendendo este workspace"
              detalhe={
                <>
                  Você pediu para rodar, e ninguém respondeu. Abra um terminal na
                  pasta deste workspace e execute <code>regente run</code>. Se o
                  Regente deve mesmo ficar desligado, clique em “Registrar como
                  parado”.
                </>
              }
            />
          </div>
        )}

        {!pode && op.controllable !== false && (
          <>
            <p className="lede" style={{ fontSize: "var(--fs-sm)" }}>
              Você não tem permissão para operar o Regente neste workspace, então
              os controles não aparecem.
            </p>
            <Tecnico titulo="Como conceder essa permissão">
              <ComoConceder chave={chaveDaSessao(identidade)} />
            </Tecnico>
          </>
        )}

        {erro && (
          <div style={{ marginTop: "var(--s-4)" }}>
            <Alerta
              tone="danger"
              titulo="Não foi possível concluir"
              detalhe={erro}
            />
          </div>
        )}

        <div className="meta">
          <span>
            Último ciclo <strong>{o.last_tick_age || "desconhecido"}</strong>
          </span>
          {f === "RUNNING" && op.interval_seconds && (
            <span>
              Verifica a cada <strong>{op.interval_seconds}s</strong>
            </span>
          )}
          {proc && (
            <span>
              <strong>{proc.ticks}</strong> ciclo(s) neste processo
            </span>
          )}
          <span>
            <strong>{o.total_tasks}</strong> task(s) monitoradas
          </span>
        </div>

        <Tecnico
          pares={[
            ["Fase (derivada)", op.phase],
            ["Intenção gravada", op.intent],
            ["Explicação do motor", op.explain],
            ["Alterada por", op.changed_by],
            ["Alterada em", op.changed_at ? when(op.changed_at) : ""],
            [
              "Processo",
              proc ? `pid ${proc.pid} em ${proc.host}` : "nenhum sinal de vida",
            ],
            ["Último sinal", proc ? when(proc.at) : ""],
            ["Detalhe do processo", proc ? proc.detail : ""],
            ["Comando que sobe o processo", "regente run"],
          ]}
        />
      </div>
      <div className="acoes">{acoes}</div>
    </section>
  );
}

function Numeros({ o }) {
  const porEstado = o.tasks_by_state || [];
  const conta = (n) => porEstado.find((c) => c.name === n)?.count || 0;

  return (
    <Faixa
      itens={[
        {
          icone: "tasks",
          rotulo: "Tasks monitoradas",
          n: o.total_tasks,
          why: `${porEstado.length} situação(ões) diferentes`,
          href: "#/tasks",
          tone: "info",
        },
        {
          icone: "pronta",
          rotulo: "Prontas para executar",
          n: conta("READY"),
          why: "Esperando um lugar na fila",
          href: "#/tasks/READY",
          tone: "ok",
        },
        {
          icone: "rodando",
          rotulo: "Em processamento",
          n: o.active_runs,
          why: `${o.workers} worker(s) disponível(is)`,
          href: "#/execucoes",
          tone: "info",
        },
        {
          icone: "bloqueada",
          rotulo: "Bloqueadas",
          n: o.blocked,
          why: "Algo de fora impede",
          href: "#/atencao",
          tone: o.blocked ? "danger" : "idle",
        },
        {
          icone: "humano",
          rotulo: "Precisam de você",
          n: o.needs_human,
          why: "Decisões na fila",
          href: "#/atencao",
          tone: o.needs_human ? "warn" : "idle",
        },
        {
          icone: "entregas",
          rotulo: "Entregas em voo",
          n: o.deliveries_in_flight,
          why: "Ainda sem desfecho",
          href: "#/entregas",
          tone: "info",
        },
      ]}
    />
  );
}

/** O checklist de instalação. Cada item leva ao lugar exato de resolvê-lo. */
function prontidao(conexoes, o) {
  const acha = (papel) => conexoes.find((c) => c.role === papel) || {};
  const tasks = acha("tasks");
  const runner = acha("runner");
  const querCred = conexoes.filter((c) => c.needs_credential);
  const comCred = querCred.filter((c) => c.live_credentials);

  return [
    {
      label: "Workspace criado",
      estado: "done",
      nota: `${o.client} / ${o.workspace_name}`,
      href: "#/config/workspace",
    },
    {
      label: "Board de tasks conectado",
      estado: tasks.state === "PRONTO" ? "done" : !tasks.adapter ? "todo" : "warn",
      nota: tasks.adapter
        ? `${tasks.adapter} — ${rotulo(tasks.state)}`
        : "Nenhum board declarado neste workspace",
      href: "#/config/providers",
    },
    {
      label: "Credenciais registradas",
      estado: !querCred.length
        ? "done"
        : comCred.length === querCred.length
          ? "done"
          : "todo",
      nota: !querCred.length
        ? "Nenhuma conexão deste workspace precisa de credencial"
        : `${comCred.length} de ${querCred.length} conexão(ões) com credencial viva`,
      href: "#/config/credenciais",
    },
    {
      label: "Agente configurado",
      estado:
        runner.state === "PRONTO" ? "done" : runner.adapter ? "blocked" : "todo",
      nota:
        runner.state === "PRONTO"
          ? `${runner.adapter} pronto`
          : "Sem agente, o Regente organiza a fila e não executa nada",
      href: "#/config/providers",
    },
    {
      label: "Regras de prioridade",
      estado: "todo",
      nota: "Opcional — sem regras, vale a prioridade que veio do board",
      href: "#/config/prioridades",
    },
  ];
}

const MARCA = { done: "✓", blocked: "✕", warn: "!", todo: "" };

function Onboarding({ passos, faltam }) {
  return (
    <Painel
      cabeca="Configure seu Regente"
      eyebrow={`${faltam} pendente(s)`}
      acao={
        <Link href="#/config" variante="btn btn-primary btn-sm" icone="config">
          Continuar
        </Link>
      }
    >
      <div className="checklist">
        {passos.map((p) => (
          <a key={p.label} className="check" data-state={p.estado} href={p.href}>
            <span className="mark" aria-hidden="true">
              {MARCA[p.estado]}
            </span>
            <span>
              <span className="label">{p.label}</span>
              <br />
              <span className="note">{p.nota}</span>
            </span>
            <Icon nome="seta" tamanho={16} className="dim" />
          </a>
        ))}
      </div>
    </Painel>
  );
}

/**
 * O que precisa de você.
 *
 * Cada alerta responde três perguntas: o que aconteceu, por que importa, e o
 * que dá para fazer. Um alerta sem a terceira é só uma notificação de mau
 * humor.
 */
function Atencao({ o, conexoes }) {
  const itens = [];

  for (const c of conexoes.filter(
    (x) => x.needs_credential && x.state !== "PRONTO",
  )) {
    const [nome] = PAPEL[c.role] || [c.role];
    itens.push(
      <Alerta
        key={`con-${c.role}`}
        icone="credencial"
        tone={c.state === "SEM_CREDENCIAL" ? "warn" : "danger"}
        titulo={`${nome}: ${rotulo(c.state).toLowerCase()}`}
        detalhe={
          c.state === "SEM_CREDENCIAL"
            ? `O Regente não alcança ${c.adapter} sem uma credencial autorizada, e nada que dependa dessa conexão vai andar.`
            : "A credencial desta conexão não vale mais, e o Regente parou de alcançá-la."
        }
        acao={
          <Link href="#/config/credenciais" variante="btn btn-sm">
            Resolver
          </Link>
        }
      />,
    );
  }

  if (o.needs_human) {
    itens.push(
      <Alerta
        key="humano"
        icone="humano"
        tone="warn"
        titulo={`${o.needs_human} task(s) esperando uma decisão sua`}
        detalhe="Elas chegaram a um ponto que o Regente não atravessa sozinho, e ficam paradas até alguém escolher."
        acao={
          <Link href="#/atencao" variante="btn btn-primary btn-sm">
            Ver e decidir
          </Link>
        }
      />,
    );
  }

  if (o.blocked) {
    itens.push(
      <Alerta
        key="bloqueado"
        icone="bloqueada"
        tone="danger"
        titulo={`${o.blocked} task(s) bloqueada(s)`}
        detalhe="Algo externo impede o avanço. O Regente registrou o quê em cada uma."
        acao={
          <Link href="#/atencao" variante="btn btn-sm">
            Ver bloqueios
          </Link>
        }
      />,
    );
  }

  const semRota = (o.stalled || []).filter((t) => t.state.owner === "nobody");
  if (semRota.length) {
    itens.push(
      <Alerta
        key="sem-rota"
        icone="duvida"
        tone="warn"
        titulo={`${semRota.length} task(s) sem próxima etapa`}
        detalhe="Elas pararam num ponto de onde o Regente não tem para onde seguir."
        acao={
          <Link href="#/atencao" variante="btn btn-sm">
            Ver tasks paradas
          </Link>
        }
      />,
    );
  }

  if (o.leases_expired) {
    itens.push(
      <Alerta
        key="leases"
        icone="relogio"
        tone="warn"
        titulo={`${o.leases_expired} reserva(s) de trabalho vencida(s)`}
        detalhe="Um worker parou sem devolver o que havia reservado. O Regente recupera sozinho no próximo ciclo."
        acao={
          <Link href="#/saude" variante="btn btn-sm">
            Ver saúde
          </Link>
        }
      />,
    );
  }

  if (!itens.length) {
    return (
      <Painel cabeca="O que precisa de você">
        <Vazio
          icone="ok"
          titulo="Nada pendente"
          texto="Nenhuma decisão, bloqueio ou credencial está esperando por você agora."
        />
      </Painel>
    );
  }

  return (
    <Secao titulo="O que precisa de você" hint={`${itens.length} item(ns)`}>
      <div className="stack">{itens}</div>
    </Secao>
  );
}

function SaudeResumo({ h }) {
  if (!h) return null;
  const ruins = (h.signals || []).filter((s) => s.level !== "OK");
  return (
    <Painel
      cabeca="Saúde"
      acao={
        <a className="hint" href="#/saude">
          Ver tudo
        </a>
      }
    >
      <div className="row">
        <Badge termo={h.level} />
        <span className="muted">
          {h.healthy
            ? "Nenhuma verificação acusou problema."
            : `${ruins.length} verificação(ões) pedem leitura.`}
        </span>
      </div>
      {ruins.length > 0 && (
        <ul className="perms" style={{ marginTop: "var(--s-3)" }}>
          {ruins.slice(0, 4).map((s) => (
            <li key={s.question}>
              <Icon
                nome={s.level === "STUCK" ? "perigo" : "duvida"}
                tamanho={14}
                style={{
                  color: s.level === "STUCK" ? "var(--c-red)" : "var(--c-amber)",
                  flex: "none",
                }}
              />
              {pergunta(s.question)}
            </li>
          ))}
        </ul>
      )}
    </Painel>
  );
}
