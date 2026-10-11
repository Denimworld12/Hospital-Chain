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
import Login from "./pages/Login.jsx";
import { useAuth } from "./auth.js";

// Nothing of the dashboard (not even the snapshot stream) runs until the admin has logged in.
function Gate() {
  const auth = useAuth();
  if (auth.status === "loading") return null;
  if (auth.status !== "in") return <Login mode={auth.status} error={auth.error} onLogin={auth.login} onSetup={auth.setup} />;
  return <App username={auth.username} onLogout={auth.logout} />;
}

createRoot(document.getElementById("root")).render(
  <StrictMode>
    <Gate />
  </StrictMode>,
);
