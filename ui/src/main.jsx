import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import "./styles.css";
import { Provedor } from "./estado.jsx";
import App from "./App.jsx";

createRoot(document.getElementById("raiz")).render(
  <StrictMode>
    <Provedor>
      <App />
    </Provedor>
  </StrictMode>,
);
