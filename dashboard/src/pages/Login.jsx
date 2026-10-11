import { useState } from "react";
import favicon from "@assets/favicon.svg";
import { PillButton, SectionHeading } from "../components/ui.jsx";

// The login screen in front of the whole dashboard. With no admin account yet (MEDCHAIN_ADMIN_PASSWORD
// was never set) it says how the organiser creates one instead.
export default function Login({ mode, error, onLogin }) {
  const noAccount = mode === "no-account";
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");

  const submit = async (e) => {
    e.preventDefault();
    setBusy(true);
    setMessage("");
    try {
      const out = await onLogin(username, password);
      if (!out.ok) setMessage(out.detail || "That did not work.");
    } catch {
      setMessage("Could not reach the dashboard server.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <header className="topbar">
        <div className="page topbar__inner">
          <span className="brand">
            <img src={favicon} alt="" width="28" height="28" />
            <span>MedChain</span>
          </span>
        </div>
      </header>
      <main>
        <section className="section">
          <div className="page auth">
            <SectionHeading
              eyebrow="Organiser only"
              title={noAccount ? "No admin account yet" : "Log in"}
              tagline={noAccount ? "the organiser sets its password" : "to watch and run the network"}
            />
            {noAccount ? (
              <div className="card assess-form auth__form" data-testid="no-account">
                <p className="field--wide" role="alert">
                  The organiser has to set <code>MEDCHAIN_ADMIN_PASSWORD</code> (at least 10 characters; the user
                  is <code>admin</code>), in <code>hub.env</code> or before <code>make up</code>, and restart the
                  dashboard. Then reload this page and log in.
                </p>
                {error && <p className="field--wide assess-message" role="alert">{error}</p>}
              </div>
            ) : (
              <form className="card assess-form auth__form" onSubmit={submit} data-testid="login-form">
                <label className="field field--wide">
                  <span className="field__label">Username</span>
                  <input name="username" autoComplete="username" required value={username}
                    onChange={(e) => setUsername(e.target.value)} />
                </label>
                <label className="field field--wide">
                  <span className="field__label">Password</span>
                  <input name="password" type="password" autoComplete="current-password" required
                    value={password} onChange={(e) => setPassword(e.target.value)} />
                </label>
                <div className="field--wide assess-actions">
                  <PillButton type="submit" disabled={busy}>Log in</PillButton>
                </div>
                {(message || error) && <p className="field--wide assess-message" role="alert">{message || error}</p>}
              </form>
            )}
          </div>
        </section>
      </main>
    </>
  );
}
