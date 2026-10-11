import { useState } from "react";
import favicon from "@assets/favicon.svg";
import { PillButton, SectionHeading } from "../components/ui.jsx";

// The login screen in front of the whole dashboard; on first run (no account yet) it creates the one
// admin account instead.
export default function Login({ mode, error, onLogin, onSetup }) {
  const setup = mode === "setup";
  const [username, setUsername] = useState(setup ? "admin" : "");
  const [password, setPassword] = useState("");
  const [repeat, setRepeat] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");

  const submit = async (e) => {
    e.preventDefault();
    if (setup && password !== repeat) return setMessage("The two passwords differ.");
    setBusy(true);
    setMessage("");
    try {
      const out = await (setup ? onSetup : onLogin)(username, password);
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
              eyebrow={setup ? "First run" : "Organiser only"}
              title={setup ? "Create the admin account" : "Log in"}
              tagline={setup ? "one account runs this dashboard" : "to watch and run the network"}
            />
            <form className="card assess-form auth__form" onSubmit={submit} data-testid={setup ? "setup-form" : "login-form"}>
              <label className="field field--wide">
                <span className="field__label">Username</span>
                <input name="username" autoComplete="username" required value={username}
                  onChange={(e) => setUsername(e.target.value)} />
              </label>
              <label className="field field--wide">
                <span className="field__label">Password {setup && <em>at least 10 characters</em>}</span>
                <input name="password" type="password" autoComplete={setup ? "new-password" : "current-password"} required
                  value={password} onChange={(e) => setPassword(e.target.value)} />
              </label>
              {setup && (
                <label className="field field--wide">
                  <span className="field__label">Password again</span>
                  <input name="repeat" type="password" autoComplete="new-password" required value={repeat}
                    onChange={(e) => setRepeat(e.target.value)} />
                </label>
              )}
              <div className="field--wide assess-actions">
                <PillButton type="submit" disabled={busy}>{setup ? "Create account" : "Log in"}</PillButton>
              </div>
              {(message || error) && <p className="field--wide assess-message" role="alert">{message || error}</p>}
            </form>
          </div>
        </section>
      </main>
    </>
  );
}
