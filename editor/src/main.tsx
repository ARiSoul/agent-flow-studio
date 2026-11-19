import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "@xyflow/react/dist/style.css";
import "./styles.css";
import App from "./App";
import { DialogProvider } from "./Dialog";
import { I18nProvider } from "./i18n";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <I18nProvider>
      <DialogProvider>
        <App />
      </DialogProvider>
    </I18nProvider>
  </StrictMode>,
);
