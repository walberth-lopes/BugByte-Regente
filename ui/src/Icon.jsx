// Um ícone.
//
// Os desenhos vêm do conjunto Solar, extraídos em tempo de desenvolvimento
// para `icones.gerado.js` — o produto não busca nada pela rede para desenhar
// um ícone, porque o Regente roda em loopback e às vezes sem internet.
//
// `aria-hidden` por padrão: um ícone ao lado de um rótulo escrito é
// decoração, e anunciá-lo faria o leitor de tela dizer a mesma coisa duas
// vezes. Quando o ícone é a única informação, passe `titulo`.

import { ICONES, VIEWBOX } from "./icones.gerado.js";

export function Icon({ nome, tamanho = 18, titulo, className, style }) {
  const corpo = ICONES[nome];
  if (!corpo) {
    // Um nome que ninguém gerou não derruba a página: some, e o rótulo ao
    // lado continua dizendo tudo. O aviso no console é para quem desenvolve.
    if (import.meta.env.DEV) console.warn(`ícone desconhecido: ${nome}`);
    return null;
  }
  return (
    <svg
      className={className}
      style={style}
      width={tamanho}
      height={tamanho}
      viewBox={VIEWBOX}
      role={titulo ? "img" : undefined}
      aria-hidden={titulo ? undefined : true}
      aria-label={titulo}
      focusable="false"
      dangerouslySetInnerHTML={{ __html: corpo }}
    />
  );
}

/** O ícone dentro de um círculo tingido — o padrão dos cartões de métrica. */
export function Bolha({ nome, tone = "var(--c-accent)", tamanho = 24, className = "bolha" }) {
  return (
    <span className={className} style={{ "--tone": tone }}>
      <Icon nome={nome} tamanho={tamanho} />
    </span>
  );
}

/** A cor de cada tom, num lugar só. */
export const COR = {
  ok: "var(--c-green)",
  warn: "var(--c-amber)",
  danger: "var(--c-red)",
  info: "var(--c-accent)",
  idle: "var(--c-neutral)",
};
