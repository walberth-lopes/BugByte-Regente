import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// O build vai DIRETO para dentro do pacote Python, e vai versionado.
//
// A alternativa -- construir na instalacao -- faria `uv pip install regente`
// exigir Node na maquina de quem so quer abrir a tela. O servidor Python serve
// esta pasta como arquivo estatico, e e ele que troca `{{SESSION_TOKEN}}` no
// index.html a cada requisicao.
export default defineConfig({
  plugins: [react()],
  build: {
    outDir: "../regente/app/ui",
    emptyOutDir: true,
    // Sem sourcemap: ele dobraria o tamanho do que vai no wheel para servir a
    // quem tem o repositorio -- e quem tem o repositorio roda `npm run dev`.
    sourcemap: false,
  },
  server: {
    port: 5173,
    // Em desenvolvimento a tela roda no Vite e a API continua no Python: um
    // processo por responsabilidade, sem CORS e sem uma segunda origem.
    proxy: { "/api": "http://127.0.0.1:8787" },
  },
});
