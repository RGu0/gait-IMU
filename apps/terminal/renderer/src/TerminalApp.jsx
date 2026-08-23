import { useEffect, useState } from "react";
import { ConsentScreen } from "./ConsentScreen.jsx";
import { HubScreen } from "./HubScreen.jsx";
import { LoginScreen } from "./LoginScreen.jsx";
import { PreflightScreen } from "./PreflightScreen.jsx";
import { ProfileScreen } from "./ProfileScreen.jsx";
import { ResultScreen } from "./ResultScreen.jsx";
import { SubjectScreen } from "./SubjectScreen.jsx";
import { TestRunScreen } from "./TestRunScreen.jsx";

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
  running: "running",
  result: "result",
};

export function TerminalApp({ adapter }) {
  const [credentials, setCredentials] = useState({ organization: "", password: "" });
  const [snapshot, setSnapshot] = useState(null);
  const [stage, setStage] = useState(STAGE.login);
  const [subject, setSubject] = useState(null);
  const [profile, setProfile] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [live, setLive] = useState(null);
  const [result, setResult] = useState(null);

  // The live sidebar values arrive while the walk is happening. They are held
  // here rather than inside TestRunScreen so that screen stays a view of a
  // session it does not own — the session outlives any one render of it.
  useEffect(() => {
    if (stage !== STAGE.running || !adapter.subscribeSession) return undefined;
    return adapter.subscribeSession((update) =>
      setLive((current) => (current ? { ...current, ...update } : current)),
    );
  }, [stage, adapter]);

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
        // P-06/P-07 (wear guidance and calibration) are RAY-222, held pending
        // the RAY-260 decision. Until then a ready terminal goes straight to
        // the walk rather than through two screens that do not exist yet.
        onReady={() => {
          setLive(adapter.startSession());
          setStage(STAGE.running);
        }}
      />
    );
  }

  if (stage === STAGE.running && live) {
    return (
      <TestRunScreen
        live={live}
        onFinish={async () => {
          setResult(await adapter.sessionResult());
          setStage(STAGE.result);
        }}
        onAbort={() => {
          setLive(null);
          setStage(STAGE.hub);
        }}
      />
    );
  }

  if (stage === STAGE.result && result) {
    return (
      <ResultScreen
        result={result}
        onNextSubject={() => {
          setResult(null);
          setSubject(null);
          setProfile(null);
          setStage(STAGE.subject);
        }}
        onOpenReport={() => {}}
        onRetry={() => {
          setResult(null);
          setLive(adapter.startSession());
          setStage(STAGE.running);
        }}
        onBackToHub={() => {
          setResult(null);
          setStage(STAGE.hub);
        }}
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
