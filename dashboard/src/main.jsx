import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "@fontsource/bebas-neue/400.css";
import "@fontsource/caveat/400.css";
import "@fontsource/dm-sans/400.css";
import "@fontsource/dm-sans/500.css";
import "@fontsource/jost/400.css";
import "@fontsource/jost/500.css";
import "@fontsource/ibm-plex-mono/400.css";
import "@design/tokens.css";
import "./styles.css";
import App from "./App.jsx";

createRoot(document.getElementById("root")).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
