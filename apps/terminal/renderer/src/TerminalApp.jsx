import { useEffect, useState } from "react";
import { Button, StatusPill } from "@gait/design-system";
import { ConsentScreen } from "./ConsentScreen.jsx";
import { DeviceSupportScreen } from "./DeviceSupportScreen.jsx";
import { RecordsScreen } from "./RecordsScreen.jsx";
import { ReportPreviewScreen } from "./ReportPreviewScreen.jsx";
import { HubScreen } from "./HubScreen.jsx";
import { PreflightScreen } from "./PreflightScreen.jsx";
import { ProfileScreen } from "./ProfileScreen.jsx";
import { ResultScreen } from "./ResultScreen.jsx";
import { SubjectScreen } from "./SubjectScreen.jsx";
import { TestRunScreen } from "./TestRunScreen.jsx";
import { WearGuideScreen } from "./WearGuideScreen.jsx";
import { WearConfirmScreen } from "./WearConfirmScreen.jsx";
import { CapabilityGap } from "./CapabilityGap.jsx";
import { SessionVerdictSummary } from "./SessionVerdictSummary.jsx";
import { SidecarDownScreen } from "./SidecarDownScreen.jsx";
import { AppBar } from "./AppBar.jsx";

/**
 * Screens are selected by an explicit `stage` rather than by inferring one from
 * which pieces of state happen to be set. Inference is what turns "the operator
 * went back one step" into an unreachable screen.
 */
const STAGE = {
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

/**
 * `lifecycle` 是可选的：它由主进程提供（`window.gaitSidecar.onSidecarState`）。
 * 走 mock 时没有进程可看护，因此不传 —— 而不是造一个永远 ready 的假生命周期。
 */
export function TerminalApp({ adapter, lifecycle }) {
  const [snapshot, setSnapshot] = useState(null);
  // 最小 MVP 无登录（P-00 暂不考虑）：冷启动直接进工作台。
  const [stage, setStage] = useState(STAGE.hub);
  const [subject, setSubject] = useState(null);
  const [profile, setProfile] = useState(null);
  const [live, setLive] = useState(null);
  const [result, setResult] = useState(null);
  const [records, setRecords] = useState([]);
  const [report, setReport] = useState(null);
  const [deviceInfo, setDeviceInfo] = useState(null);
  // 报告预览的缺口（RAY-224）。与 report 分开存：一个「打不开」和一个「还没接通」
  // 在界面上要说不同的话。
  const [reportGap, setReportGap] = useState(null);
  // 佩戴确认（P-07 最小接线）：`wearing` 是 PRD §13 的佩戴底线，`swapped` 是「一键对调」。
  const [wearing, setWearing] = useState("unknown");
  const [swapped, setSwapped] = useState(false);
  // sidecar 的进程状态。null 表示「没有进程可看护」（mock 路径），
  // 与「进程状态未知」不是一回事，所以不给它一个默认的 ready。
  const [sidecar, setSidecar] = useState(null);

  async function navigate(label) {
    const next = NAV_STAGE[label];
    if (!next) return;
    if (next === STAGE.records) setRecords(await adapter.listRecords());
    if (next === STAGE.deviceSupport) setDeviceInfo(await adapter.deviceSupport());
    setStage(next);
  }

  useEffect(() => {
    if (!lifecycle?.subscribe) return undefined;
    return lifecycle.subscribe(setSidecar);
  }, [lifecycle]);

  // 冷启动直接进工作台（无登录）：快照在挂载时拉取一次，之后由「重新检查」刷新。
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const snap = await adapter.snapshot();
        if (!cancelled) setSnapshot(snap);
      } catch {
        // 快照失败不挡路：工作台留空，操作员可点「重新检查」再拉。
        if (!cancelled) setSnapshot(null);
      }
    })();
    return () => { cancelled = true; };
  }, [adapter]);

  // The live sidebar values arrive while the walk is happening. They are held
  // here rather than inside TestRunScreen so that screen stays a view of a
  // session it does not own — the session outlives any one render of it.
  useEffect(() => {
    if (stage !== STAGE.running || !adapter.subscribeSession) return undefined;
    return adapter.subscribeSession((update) =>
      setLive((current) => (current ? { ...current, ...update } : current)),
    );
  }, [stage, adapter]);

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

  function startWalk() {
    setLive(adapter.startSession());
    setStage(STAGE.running);
  }

  /**
   * 打开一份报告：缺省用当前会话，records 路径传 record（含 id=sessionId）。
   * `swapped` 是佩戴确认里的一键对调，决定 sidecar 读哪一侧的录制。
   */
  async function openReport(record) {
    const opened = await adapter.reportFor(record);
    if (opened?.unimplemented) setReportGap(opened.unimplemented);
    else setReport(opened);
    setStage(STAGE.reportPreview);
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
      <WearConfirmScreen
        onDone={({ wearing: w, swapped: s }) => {
          setWearing(w);
          setSwapped(s);
          startWalk();
        }}
        onBack={() => setStage(STAGE.wear)}
      />
    );
  }

  if (stage === STAGE.running && live) {
    return (
      <TestRunScreen
        live={live}
        onFinish={async () => {
          // 先收尾落盘，再判定、再生成报告 —— 顺序反了报告读到的是未排空的录制。
          await adapter.stopSession();
          const sessionResult = await adapter.sessionResult({ wearing });
          setResult(sessionResult);
          if (sessionResult.report?.status === "ready") {
            await openReport({ swapped });
          } else {
            setStage(STAGE.result);
          }
        }}
        onAbort={() => {
          setLive(null);
          setStage(STAGE.hub);
        }}
      />
    );
  }

  // 会话级无效：不生成报告（PRD §13），给判定 + 重测。这不是缺口，是真实的结论，
  // 所以不借 CapabilityGap —— 借了就把「没建成」和「没通过」画成了同一件事。
  if (stage === STAGE.result && result?.report?.status === "invalid") {
    return (
      <div className="invalid-page">
        <AppBar />
        <main className="invalid-body" role="alert">
          <StatusPill tone="warning" icon="warning">未通过质量检查</StatusPill>
          <h1>本次检测未生成报告</h1>
          <SessionVerdictSummary result={result} />
          {result.error ? <p className="invalid-reason">{result.error.message}</p> : null}
          {result.error ? <p className="invalid-advice">{result.error.action}</p> : null}
          <div className="invalid-actions">
            <Button size="lg" onClick={() => { setResult(null); startWalk(); }}>重新检测</Button>
            <Button variant="secondary" onClick={() => { setResult(null); setStage(STAGE.hub); }}>
              返回工作台
            </Button>
          </div>
        </main>
      </div>
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
          setWearing("unknown");
          setSwapped(false);
          setStage(STAGE.subject);
        }}
        onOpenReport={() => openReport({ swapped })}
        onRetry={() => {
          setResult(null);
          startWalk();
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
        onOpenRecord={(record) => openReport(record)}
      />
    );
  }

  // 放在所有分支最前面：sidecar 不在时，别的屏上每一个按钮点下去都只会再失败
  // 一次，让它们看起来还能用才是真正的伤害。
  if (sidecar && (sidecar.state === "unavailable" || sidecar.state === "restarting")) {
    return (
      <SidecarDownScreen
        notice={sidecar.notice ?? { message: "采集服务正在启动。", action: "请稍候。", recoverable: true }}
        onRetry={() => setStage(STAGE.hub)}
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

  // 冷启动的极短间隙：快照还没回来。给一个诚实的占位，而不是空屏或假登录页。
  return <div className="app-boot" role="status">正在连接采集服务…</div>;
}
