import { useState } from "react";
import { ConsentScreen } from "./ConsentScreen.jsx";
import { HubScreen } from "./HubScreen.jsx";
import { LoginScreen } from "./LoginScreen.jsx";
import { PreflightScreen } from "./PreflightScreen.jsx";
import { ProfileScreen } from "./ProfileScreen.jsx";
import { SubjectScreen } from "./SubjectScreen.jsx";

/**
 * Screens are selected by an explicit `stage` rather than by inferring one from
 * which pieces of state happen to be set. Inference is what turns "the operator
 * went back one step" into an unreachable screen.
 */
const STAGE = {
  login: "login",
  hub: "hub",
  subject: "subject",
  profile: "profile",
  consent: "consent",
  preflight: "preflight",
};

export function TerminalApp({ adapter }) {
  const [credentials, setCredentials] = useState({ organization: "", password: "" });
  const [snapshot, setSnapshot] = useState(null);
  const [stage, setStage] = useState(STAGE.login);
  const [subject, setSubject] = useState(null);
  const [profile, setProfile] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  async function handleSubmit(event) {
    event.preventDefault();
    setLoading(true);
    setError("");
    try {
      await adapter.login(credentials);
      setSnapshot(await adapter.snapshot());
      setStage(STAGE.hub);
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

  function confirmSubject(chosen) {
    setSubject(chosen);
    setStage(STAGE.profile);
  }

  async function quickCreate() {
    confirmSubject(await adapter.createSubject());
  }

  function finishProfile(filled) {
    setProfile(filled);
    setStage(STAGE.consent);
  }

  if (stage === STAGE.subject) {
    return (
      <SubjectScreen
        lookup={(enteredId) => adapter.lookupSubject(enteredId)}
        protocolSeconds={snapshot?.protocolSeconds}
        onConfirm={confirmSubject}
        onQuickCreate={quickCreate}
      />
    );
  }

  if (stage === STAGE.profile) {
    return (
      <ProfileScreen
        profile={profile}
        onContinue={finishProfile}
        // Skipping keeps whatever was already there — including nothing at all,
        // which is the AC-02 path.
        onSkip={() => finishProfile(profile)}
      />
    );
  }

  if (stage === STAGE.consent) {
    return (
      <ConsentScreen
        subjectLabel={subject?.maskedId}
        onAgree={() => setStage(STAGE.preflight)}
        onDecline={() => {
          setSubject(null);
          setProfile(null);
          setStage(STAGE.hub);
        }}
      />
    );
  }

  if (stage === STAGE.preflight) {
    return (
      <PreflightScreen
        runChecks={() => adapter.runPreflight()}
        // P-06 (wear guidance) is RAY-222 and is held pending RAY-260, so the
        // ready state parks at the hub rather than pretending to go further.
        onReady={() => setStage(STAGE.hub)}
      />
    );
  }

  if (stage === STAGE.hub && snapshot) {
    return (
      <HubScreen
        snapshot={snapshot}
        onRecheck={handleRecheck}
        onStartNewAssessment={() => setStage(STAGE.subject)}
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
