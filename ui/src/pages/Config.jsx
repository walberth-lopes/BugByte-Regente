// Configuração, organizada pela ordem em que as coisas fazem sentido.
//
// Conexões antes de credenciais porque não se autoriza acesso a um serviço que
// ainda não foi escolhido. Integrações depois das duas porque descobrir o que
// existe num serviço exige credencial: oferecer a busca antes disso seria
// oferecer um botão que só sabe recusar. Status antes de regras porque uma regra sobre
// "status" só significa alguma coisa depois que o Regente sabe o que os status
// do seu board querem dizer. Avançado por último, e explicitamente marcado como
// a mesma coisa em outro formato.

import { useRegente, useLeitura } from "../estado.jsx";
import { Painel, Secao } from "../ui.jsx";

import Conexoes from "../config/Conexoes.jsx";
import Credenciais from "../config/Credenciais.jsx";
import Integracoes from "../config/Integracoes.jsx";
import StatusMap from "../config/StatusMap.jsx";
import Regras from "../config/Regras.jsx";
import Acesso from "../config/Acesso.jsx";
import Avancado from "../config/Avancado.jsx";
import Workspace from "../config/Workspace.jsx";

const ABAS = [
  ["providers", "Conexões", Conexoes],
  ["credenciais", "Credenciais", Credenciais],
  ["integracoes", "Integrações", Integracoes],
  ["status", "Status do board", StatusMap],
  ["prioridades", "Regras e prioridade", Regras],
  ["acesso", "Acesso", Acesso],
  ["workspace", "Workspace", Workspace],
  ["avancado", "Avançado", Avancado],
];

export default function Config({ args }) {
  const escolhida = ABAS.find(([id]) => id === args[0]) || ABAS[0];
  const [, , Conteudo] = escolhida;

  return (
    <>
      <div className="secao-cabeca">
        <h1>Configuração</h1>
      </div>

      <nav className="tabs" aria-label="Seções da configuração">
        {ABAS.map(([id, nome]) => (
          <a
            key={id}
            href={`#/config/${id}`}
            aria-current={id === escolhida[0] ? "page" : undefined}
          >
            {nome}
          </a>
        ))}
      </nav>

      <Conteudo />
    </>
  );
}
