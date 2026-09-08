// O workspace: o que ele é, e o que esta versão do Regente ainda não faz por
// aqui.
//
// Criar um workspace é uma operação de ARQUIVO — `regente init` escreve o
// `regente.yaml` e o `policies.yaml` numa pasta. Esta tela não inventa um botão
// "criar workspace" que não existiria de verdade: dizer o comando é honesto, e
// fingir que a tela cria pastas não é.

import { useRegente, useLeitura } from "../estado.jsx";
import { Alerta, Dado, Leitura, Painel, Secao, Tecnico } from "../ui.jsx";
import { when } from "../present.js";

export default function Workspace() {
  const { api, identidade } = useRegente();
  const leitura = useLeitura([api("/overview"), api("/operation")]);

  return (
    <Leitura estado={leitura} oQue="este workspace">
      {(o, op) => (
        <>
          <p className="muted">
            Um workspace é uma pasta com a configuração do Regente e o histórico
            do que ele fez ali. Tudo o que você vê nesta janela pertence a este
            workspace, e a nenhum outro.
          </p>

          <Painel>
            <div className="dados">
              <Dado rotuloTexto="Cliente" valor={o.client} />
              <Dado rotuloTexto="Workspace" valor={o.workspace_name} />
              <Dado rotuloTexto="Organização" valor={o.organization} />
              <Dado
                rotuloTexto="Tasks conhecidas"
                valor={o.total_tasks}
                porque={`Última leitura do board ${o.last_tick_age || "nunca"}`}
              />
            </div>

            <Tecnico
              pares={[
                ["Identificador", o.workspace_id],
                ["Última atividade", o.last_activity ? when(o.last_activity) : ""],
                ["Último ciclo", o.last_tick ? when(o.last_tick) : ""],
                ["Intervalo entre ciclos", `${op.interval_seconds}s`],
                ["Identidade desta janela", identidade?.display || ""],
                ["Como esta janela autentica", identidade?.mechanism || ""],
              ]}
            />
          </Painel>

          <Secao titulo="Criar outro workspace">
            <Alerta
              tone="info"
              icone="i"
              titulo="Isto ainda é feito no terminal"
              detalhe="Criar um workspace escreve arquivos numa pasta nova, e esta versão do Regente não dá esse poder a uma página web. Numa pasta vazia, execute `regente init` e depois `regente ui` — a nova janela abre já apontando para ele."
            />
          </Secao>

          {identidade?.development_only && (
            <Secao titulo="Sobre esta instalação">
              <Alerta
                tone="warn"
                titulo="O mecanismo de identidade é de desenvolvimento"
                detalhe="Esta janela autentica por um token local, adequado para uso na sua própria máquina. Antes de expor o Regente numa rede, ligue um provedor de identidade real — o próprio servidor recusa escutar fora do loopback sem isso."
              />
            </Secao>
          )}
        </>
      )}
    </Leitura>
  );
}
