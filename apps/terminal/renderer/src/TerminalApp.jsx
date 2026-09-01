import { useEffect, useState } from "react";
import { CalibrationScreen } from "./CalibrationScreen.jsx";
import { ConsentScreen } from "./ConsentScreen.jsx";
import { DeviceSupportScreen } from "./DeviceSupportScreen.jsx";
import { RecordsScreen } from "./RecordsScreen.jsx";
import { ReportPreviewScreen } from "./ReportPreviewScreen.jsx";
import { HubScreen } from "./HubScreen.jsx";
import { LoginScreen } from "./LoginScreen.jsx";
import { PreflightScreen } from "./PreflightScreen.jsx";
import { ProfileScreen } from "./ProfileScreen.jsx";
import { ResultScreen } from "./ResultScreen.jsx";
import { SubjectScreen } from "./SubjectScreen.jsx";
import { TestRunScreen } from "./TestRunScreen.jsx";
import { WearGuideScreen } from "./WearGuideScreen.jsx";
import { CapabilityGap } from "./CapabilityGap.jsx";
import { SessionVerdictSummary } from "./SessionVerdictSummary.jsx";

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
  wear: "wear",
  calibration: "calibration",
  running: "running",
  result: "result",
  records: "records",
  reportPreview: "reportPreview",
  deviceSupport: "deviceSupport",
};

/** Nav labels are the AppBar's contract; the mapping lives in one place. */
const NAV_STAGE = {
  工作台: STAGE.hub,
  检测记录: STAGE.records,
  设备与支持: STAGE.deviceSupport,
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
  const [records, setRecords] = useState([]);
  const [report, setReport] = useState(null);
  const [deviceInfo, setDeviceInfo] = useState(null);
  // 报告预览的缺口（RAY-224）。与 report 分开存：一个「打不开」和一个「还没接通」
  // 在界面上要说不同的话。
  const [reportGap, setReportGap] = useState(null);

  async function navigate(label) {
    const next = NAV_STAGE[label];
    if (!next) return;
    if (next === STAGE.records) setRecords(await adapter.listRecords());
    if (next === STAGE.deviceSupport) setDeviceInfo(await adapter.deviceSupport());
    setStage(next);
  }

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
        onReady={() => setStage(STAGE.wear)}
      />
    );
  }

  if (stage === STAGE.wear) {
    return <WearGuideScreen onContinue={() => setStage(STAGE.calibration)} />;
  }

  if (stage === STAGE.calibration) {
    return (
      <CalibrationScreen
        runCalibration={() => adapter.runCalibration()}
        onDone={() => {
          setLive(adapter.startSession());
          setStage(STAGE.running);
        }}
        onAbandon={() => setStage(STAGE.hub)}
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

  // 真实后端下 P-09 没有指标可显示 —— 指标出自基础报告，而基础报告（RAY-224）
  // 还不存在。能诚实显示的只有会话判定本身（它是真的），外加一个说明为什么到此
  // 为止的缺口。用 mock 走的路径不带 report 字段，因此不进这个分支。
  if (stage === STAGE.result && result?.report?.status === "unimplemented") {
    return (
      <CapabilityGap
        gap={{
          capability: result.report.capability,
          issue: result.report.issue,
          summary: "本地基础报告尚未实现，因此这次检测没有生成报告。",
        }}
        step={6}
        details={<SessionVerdictSummary result={result} />}
        onBack={() => {
          setResult(null);
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

  if (stage === STAGE.records) {
    return (
      <RecordsScreen
        records={records}
        onNavigate={navigate}
        onOpenRecord={async (record) => {
          const opened = await adapter.reportFor(record);
          if (opened?.unimplemented) {
            setReportGap(opened.unimplemented);
          } else {
            setReport(opened);
          }
          setStage(STAGE.reportPreview);
        }}
      />
    );
  }

  if (stage === STAGE.reportPreview && reportGap) {
    return (
      <CapabilityGap
        gap={reportGap}
        step={7}
        onBack={() => {
          setReportGap(null);
          setStage(STAGE.records);
        }}
      />
    );
  }

  if (stage === STAGE.reportPreview && report) {
    return (
      <ReportPreviewScreen
        report={report}
        onNavigate={navigate}
        onBack={() => setStage(STAGE.records)}
      />
    );
  }

  if (stage === STAGE.deviceSupport && deviceInfo) {
    return (
      <DeviceSupportScreen
        devices={deviceInfo.devices}
        support={deviceInfo.support}
        onNavigate={navigate}
        onRecheck={handleRecheck}
        onRepair={() => {}}
      />
    );
  }

  if (stage === STAGE.hub && snapshot) {
    return (
      <HubScreen
        snapshot={snapshot}
        onNavigate={navigate}
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
