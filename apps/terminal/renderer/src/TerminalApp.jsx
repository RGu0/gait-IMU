import { useState } from "react";
import { HubScreen } from "./HubScreen.jsx";
import { LoginScreen } from "./LoginScreen.jsx";

export function TerminalApp({ adapter }) {
  const [credentials, setCredentials] = useState({ organization: "", password: "" });
  const [snapshot, setSnapshot] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  async function handleSubmit(event) {
    event.preventDefault();
    setLoading(true);
    setError("");
    try {
      await adapter.login(credentials);
      setSnapshot(await adapter.snapshot());
    } catch (loginError) {
      setError(loginError instanceof Error ? loginError.message : "登录失败，请稍后重试。");
    } finally {
      setLoading(false);
    }
  }

  function handleCredentialChange(name, value) {
    setCredentials((current) => ({ ...current, [name]: value }));
  }

  async function handleRecheck() {
    await adapter.recheckDevices();
    setSnapshot(await adapter.snapshot());
  }

  if (snapshot) {
    return (
      <HubScreen
        snapshot={snapshot}
        onRecheck={handleRecheck}
        onStartNewAssessment={() => {}}
      />
    );
  }

  return (
    <LoginScreen
      credentials={credentials}
      error={error}
      loading={loading}
      onChange={handleCredentialChange}
      onSubmit={handleSubmit}
    />
  );
}
