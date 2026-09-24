import { useEffect, useRef, useState } from "react";
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
import { BindingWizardScreen } from "./BindingWizardScreen.jsx";
import { PreviewBanner } from "./PreviewBanner.jsx";
import { ReportErrorScreen } from "./ReportErrorScreen.jsx";

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
  reportError: "reportError",
  binding: "binding",
};

/** Nav labels are the AppBar's contract; the mapping lives in one place. */
const NAV_STAGE = {
  工作台: STAGE.hub,
  检测记录: STAGE.records,
  设备与支持: STAGE.deviceSupport,
};

/**
 * TestRunScreen 读的每一个字段。sidecar 的 `startSession` 只给计时与步数，
 * 缺的那几样在这里补齐 —— 补在屏外，因为屏是会话的视图，不负责猜会话的形状。
 */
const LIVE_DEFAULTS = Object.freeze({
  instruction: "",
  steps: { left: 0, right: 0 },
  // 没拿到链路读数时按 sidecar 自己的约定记「差」，不替它报「好」。
  link: { left: "bad", right: "bad" },
  footfalls: { left: [], right: [] },
  notices: [],
  aborted: null,
});

export const SNAPSHOT_RETRY_MS = 1000;

// 真设备源在后台连接时（`deviceSummary.state === "connecting"`）工作台重拉快照的间隔（RAY-503）。
export const DEVICE_POLL_MS = 2000;

/**
 * 采集中 sidecar 进程没了（RAY-493 preview-rc2-fixes，装机 A5 §6-1）。
 *
 * 会话活在**那个**进程里。主进程重启出来的是一个新进程，它没有这场会话：之前界面
 * 回到采集页接着倒计时（步数冻结），走满后向新进程发 stopSession，拿回「会话尚未开始」
 * —— 让受试者白走五十秒，再给一句与事实相反的话。
 *
 * 这不是 sidecar 的错误应答（没有码、没有域，同 SidecarDownScreen 的理由），是渲染端
 * 自己亲眼看见的事：生命周期在这场检测进行中离开过 ready。所以文案在这里。
 * 磁盘上那场会话停在 `walking`，检测记录里显示「未正常结束」（RAY-496）。
 */
export const WALK_INTERRUPTED = Object.freeze({
  title: "检测已中断",
  message: "采集服务中断，本次检测已中断。",
  action: "数据已尽可能保存；请回到工作台重新检测。",
});

/**
 * `lifecycle` 是可选的：它由主进程提供（`window.gaitSidecar.onSidecarState`）。
 * 走 mock 时没有进程可看护，因此不传 —— 而不是造一个永远 ready 的假生命周期。
 *
 * `preview` 为真时每一屏顶部都有「预览版」条（真 sidecar 路径）；mock 路径由 main.jsx
 * 自己的演示数据条负责，这里不重复。
 */
export function TerminalApp({
  adapter,
  lifecycle,
  preview = false,
  snapshotRetryMs = SNAPSHOT_RETRY_MS,
  devicePollMs = DEVICE_POLL_MS,
}) {
  // 预览条要知道数据来源（`snapshot.source`）；快照归工作台，条只借它的 source 一读。
  const [source, setSource] = useState(undefined);
  return (
    <>
      {preview ? <PreviewBanner source={source} /> : null}
      <TerminalStages
        adapter={adapter}
        lifecycle={lifecycle}
        snapshotRetryMs={snapshotRetryMs}
        devicePollMs={devicePollMs}
        preview={preview}
        onSnapshot={(snap) => setSource(snap?.source)}
      />
    </>
  );
}

function TerminalStages({ adapter, lifecycle, preview, snapshotRetryMs, devicePollMs, onSnapshot }) {
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
  // 佩戴确认（P-07 最小接线）：`wearing` 是 PRD §13 的佩戴底线。RAY-479 起没有「一键对调」：
  // 左右只由配对绑定决定（蓝色模块左脚、橙色模块右脚）。
  const [wearing, setWearing] = useState("unknown");
  // 配对向导结束后回到哪一屏（RAY-479）：设备页、工作台或自检。
  const [bindingReturn, setBindingReturn] = useState(STAGE.hub);
  // sidecar 的进程状态。null 表示「没有进程可看护」（mock 路径），
  // 与「进程状态未知」不是一回事，所以不给它一个默认的 ready。
  const [sidecar, setSidecar] = useState(null);
  // 报告/收尾失败（RAY-493）。之前 reportFor 的拒绝没人接，界面就停在采集页上。
  const [reportError, setReportError] = useState(null);
  // lifecycle 每回到 ready 一次就重拉一次快照：sidecar 重启后旧快照已不可信。
  const [snapshotEpoch, setSnapshotEpoch] = useState(0);
  const lastSidecarState = useRef(null);
  // 进行中的这场检测（startSession 成功到出结果/报告或操作员停止）。它记着开始时的
  // sidecar 代数：代数变了，说明这期间进程没过，会话已随旧进程一起没了。
  const walkRef = useRef(null);
  const sidecarGenerationRef = useRef(0);
  // 收尾开始后 sidecar 仍会继续推 remainingSeconds: 0 的 tick，直到 stopSession 落地。
  // 那些 tick 不再属于这一场，也不能让收尾被触发第二次。
  const finishingRef = useRef(false);
  // 「重新检查设备」（RAY-493 B2）：进行中不叠加点击；失败要说出来，不能静默。
  const [rechecking, setRechecking] = useState(false);
  const recheckingRef = useRef(false);
  const [recheckError, setRecheckError] = useState(null);
  // 每次回到工作台都重拉快照（RAY-493 B1）：走完、停止、从报告/错误屏/顶栏回来时，
  // sidecar 已经多了会话，旧快照里的待上传数与最近记录都过期了。
  const wasHubRef = useRef(true);

  async function navigate(label) {
    const next = NAV_STAGE[label];
    if (!next) return;
    // 上一页的「重新检查失败」不该跟到别的页上（工作台与设备页共用这份状态）。
    setRecheckError(null);
    // 已在工作台时再点「工作台」不会触发进入工作台的重拉，这里补一次。
    if (next === STAGE.hub && stage === STAGE.hub) setSnapshotEpoch((epoch) => epoch + 1);
    try {
      if (next === STAGE.records) setRecords(await adapter.listRecords());
      if (next === STAGE.deviceSupport) setDeviceInfo(await adapter.deviceSupport());
    } catch (error) {
      if (next === STAGE.records) setRecords([]);
      if (next === STAGE.deviceSupport) {
        setReportError({ error, retryStage: null });
        setStage(STAGE.reportError);
        return;
      }
    }
    setStage(next);
  }

  useEffect(() => {
    if (!lifecycle?.subscribe) return undefined;
    return lifecycle.subscribe((next) => {
      setSidecar(next);
      const previous = lastSidecarState.current;
      lastSidecarState.current = next?.state ?? null;
      // 不要求先看到过 ready：主进程在 did-finish-load 时补发的那次状态，渲染端的订阅
      // 常常还没挂上（dev Electron 实测），于是第一次重启时 previous 仍是 null。
      // 任何非 ready 状态都算进程换代；多记几代无害。
      if (next?.state && next.state !== "ready") {
        sidecarGenerationRef.current += 1;
        // 当场结束这场检测：倒计时停、不再收 tick、不向重启后的新进程收尾。
        if (walkRef.current) interruptWalk();
      }
      if (next?.state === "ready" && previous !== null && previous !== "ready") {
        setSnapshotEpoch((epoch) => epoch + 1);
      }
    });
  }, [lifecycle]);

  // 冷启动直接进工作台（无登录）：快照在挂载时拉取，失败就隔一会儿再拉，直到拿到为止。
  // 原先失败一次就放弃，而占位屏上没有任何按钮 —— sidecar 起得比窗口慢一点，
  // 界面就永远停在「正在连接采集服务…」。
  useEffect(() => {
    let cancelled = false;
    let timer = null;
    const load = async () => {
      try {
        const snap = await adapter.snapshot();
        if (!cancelled) setSnapshot(snap);
      } catch {
        if (!cancelled) timer = setTimeout(load, snapshotRetryMs);
      }
    };
    load();
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [adapter, snapshotRetryMs, snapshotEpoch]);

  // 进入工作台时换一个 epoch，由上面的拉取 effect 重拉。重拉期间继续显示上一份快照，
  // 不回到「正在连接采集服务…」占位。
  const isHub = stage === STAGE.hub;
  useEffect(() => {
    const entering = isHub && !wasHubRef.current;
    wasHubRef.current = isHub;
    if (entering) setSnapshotEpoch((epoch) => epoch + 1);
  }, [isHub]);

  // 真设备源正在后台连接：留在工作台就隔一会儿重拉快照，连上（或失败）即停（RAY-503）。
  // 在此之前快照只在进入工作台时拉一次，模块在后台连上后界面一直停在「电量读不到」。
  // 失败不轮询 —— 那一步归「重新检查设备」，否则工作台开着就会无休止地后台扫描。
  const connecting = isHub && snapshot?.deviceSummary?.state === "connecting";
  useEffect(() => {
    if (!connecting) return undefined;
    let cancelled = false;
    const timer = setTimeout(async () => {
      try {
        const snap = await adapter.snapshot();
        if (!cancelled) setSnapshot(snap);
      } catch {
        // 拉不到就让快照保持 connecting，下面这个 effect 不会重跑；换 epoch 让主拉取接手重试。
        if (!cancelled) setSnapshotEpoch((epoch) => epoch + 1);
      }
    }, devicePollMs);
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [connecting, snapshot, adapter, devicePollMs]);

  const onSnapshotRef = useRef(onSnapshot);
  onSnapshotRef.current = onSnapshot;
  useEffect(() => {
    if (snapshot) onSnapshotRef.current?.(snapshot);
  }, [snapshot]);

  // The live sidebar values arrive while the walk is happening. They are held
  // here rather than inside TestRunScreen so that screen stays a view of a
  // session it does not own — the session outlives any one render of it.
  useEffect(() => {
    if (stage !== STAGE.running || !adapter.subscribeSession) return undefined;
    return adapter.subscribeSession((update) => {
      if (finishingRef.current) return;
      setLive((current) => (current ? { ...current, ...update } : current));
    });
  }, [stage, adapter]);

  // 工作台与设备页共用同一个「重新检查」：先让 sidecar 重查，再按所在页面重拉该页的数据。
  // 设备页原先也调这个、却只重拉工作台快照 —— 点了页面毫无变化（RAY-530）。
  async function handleRecheck() {
    if (recheckingRef.current) return;
    recheckingRef.current = true;
    setRechecking(true);
    try {
      await adapter.recheckDevices();
      if (stage === STAGE.deviceSupport) setDeviceInfo(await adapter.deviceSupport());
      else setSnapshot(await adapter.snapshot());
      setRecheckError(null);
    } catch (error) {
      // 保留上一份快照，但要告诉操作员这次没查成、原因是什么、接下来能做什么。
      setRecheckError(recheckFailureMessage(error, preview));
    } finally {
      recheckingRef.current = false;
      setRechecking(false);
    }
  }

  function openBindingWizard(returnTo) {
    setBindingReturn(returnTo);
    setStage(STAGE.binding);
  }

  /** 向导结束（完成或取消）：回到来处，并让那一屏重读 —— 绑定变了，旧读数不再成立。 */
  async function closeBindingWizard() {
    if (bindingReturn === STAGE.deviceSupport) {
      await navigate("设备与支持");
      return;
    }
    // 回工作台时由「进入工作台即重拉快照」负责刷新；自检屏重新挂载即重跑。
    setStage(bindingReturn);
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

  function toHub() {
    setResult(null);
    setReport(null);
    setReportGap(null);
    setReportError(null);
    setLive(null);
    setStage(STAGE.hub);
  }

  function failed(error, retryStage, title) {
    setReportError({ error, retryStage, title });
    setStage(STAGE.reportError);
  }

  /** 这场检测是否已随 sidecar 进程一起没了。 */
  function walkLost(walk) {
    return walk !== walkRef.current || walk.generation !== sidecarGenerationRef.current;
  }

  /**
   * 结束当前检测并显示中断屏。「重新检测」回到自检、保留受检者（同报告失败的重试）。
   * finishingRef 置真：已在路上的收尾在下一个 await 之后看见它就停手。
   */
  function interruptWalk() {
    walkRef.current = null;
    finishingRef.current = true;
    setLive(null);
    setResult(null);
    failed(
      { message: WALK_INTERRUPTED.message, action: WALK_INTERRUPTED.action },
      STAGE.preflight,
      WALK_INTERRUPTED.title,
    );
  }

  /**
   * 真 sidecar 的 startSession 是异步的。以前这里把 Promise 直接塞进 live，
   * TestRunScreen 在 `live.steps.left` 上当场崩。
   */
  async function startWalk() {
    const generation = sidecarGenerationRef.current;
    let started;
    try {
      started = await adapter.startSession(subject);
    } catch (error) {
      failed(error, STAGE.preflight, "检测未能开始");
      return;
    }
    if (generation !== sidecarGenerationRef.current) {
      // 开始的应答回来前进程已经换过：这场会话不在当前进程里。
      interruptWalk();
      return;
    }
    walkRef.current = { generation };
    finishingRef.current = false;
    const totalSeconds = started?.totalSeconds ?? started?.remainingSeconds ?? snapshot?.protocolSeconds ?? 0;
    setLive({ ...LIVE_DEFAULTS, ...started, totalSeconds });
    setStage(STAGE.running);
  }

  /** 打开一份报告：缺省用当前会话，records 路径传 record（含 id=sessionId）。 */
  async function openReport(record, retryStage = null) {
    let opened;
    try {
      opened = await adapter.reportFor(record);
    } catch (error) {
      failed(error, retryStage);
      return;
    }
    if (opened?.unimplemented) setReportGap(opened.unimplemented);
    else setReport(opened);
    setStage(STAGE.reportPreview);
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

  if (stage === STAGE.reportError && reportError) {
    const { retryStage } = reportError;
    return (
      <ReportErrorScreen
        error={reportError.error}
        title={reportError.title}
        onBackToHub={toHub}
        onRetry={
          retryStage
            ? () => {
                setReportError(null);
                setResult(null);
                setLive(null);
                setStage(subject ? retryStage : STAGE.subject);
              }
            : null
        }
      />
    );
  }

  if (stage === STAGE.binding) {
    return (
      <BindingWizardScreen
        bindFoot={(foot) => adapter.bindFoot(foot)}
        onDone={closeBindingWizard}
        onCancel={closeBindingWizard}
      />
    );
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
        onRepairBinding={() => openBindingWizard(STAGE.preflight)}
      />
    );
  }

  if (stage === STAGE.wear) {
    return <WearGuideScreen onContinue={() => setStage(STAGE.calibration)} />;
  }

  if (stage === STAGE.calibration) {
    return (
      <WearConfirmScreen
        onDone={({ wearing: w }) => {
          setWearing(w);
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
          if (finishingRef.current) return;
          finishingRef.current = true;
          const walk = walkRef.current;
          // 收尾途中进程没了：请求会以 SidecarDown 失败，或者（重启够快时）被新进程以
          // 「会话尚未开始」拒绝。两种都不是这场检测的真相，交给中断屏。
          const lost = (error) => (walk && walkLost(walk)) || error?.name === "SidecarDown";
          let sessionResult;
          try {
            await adapter.stopSession();
            if (walk && walkLost(walk)) return;
            sessionResult = await adapter.sessionResult({ wearing });
          } catch (error) {
            if (lost(error)) {
              if (walkRef.current === walk) interruptWalk();
              return;
            }
            failed(error, STAGE.preflight);
            return;
          }
          if (walk && walkLost(walk)) return;
          // 结果已从旧进程拿到：会话收尾完成，此后的报告读取不再属于「检测中」。
          walkRef.current = null;
          setResult(sessionResult);
          if (sessionResult?.report?.status === "ready") {
            await openReport({ subjectLabel: subject?.maskedId }, STAGE.preflight);
          } else {
            setStage(STAGE.result);
          }
        }}
        onAbort={async () => {
          // 操作员停止或 sidecar 已中止：都要让 sidecar 收尾，否则采集一直开着。
          // 已中止的会话再 stop 会被拒 —— 那个拒绝不影响回工作台。
          if (finishingRef.current) return;
          finishingRef.current = true;
          // 操作员已决定结束：此后进程再出事也只是回工作台，不再报中断。
          walkRef.current = null;
          try {
            await adapter.stopSession?.();
          } catch {
            // ignore
          }
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
            <Button variant="secondary" onClick={toHub}>
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
          setStage(STAGE.subject);
        }}
        onOpenReport={() => openReport({ subjectLabel: subject?.maskedId }, STAGE.preflight)}
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
        devices={deviceInfo.devices ?? { modules: deviceInfo.modules ?? [] }}
        support={deviceInfo.support ?? {}}
        onNavigate={navigate}
        onRecheck={handleRecheck}
        rechecking={rechecking}
        recheckError={recheckError}
        onDismissRecheckError={() => setRecheckError(null)}
        onRepair={() => openBindingWizard(STAGE.deviceSupport)}
      />
    );
  }

  if (stage === STAGE.hub && snapshot) {
    return (
      <HubScreen
        snapshot={snapshot}
        onNavigate={navigate}
        onRecheck={handleRecheck}
        rechecking={rechecking}
        recheckError={recheckError}
        onDismissRecheckError={() => setRecheckError(null)}
        onStartNewAssessment={() => setStage(STAGE.subject)}
        onBind={() => openBindingWizard(STAGE.hub)}
      />
    );
  }

  // 冷启动的极短间隙：快照还没回来。给一个诚实的占位，而不是空屏或假登录页。
  return <div className="app-boot" role="status">正在连接采集服务…</div>;
}

/** 句末标点由这里统一补，原因里自带的「。」先去掉，免得出现「。。」。 */
function trimSentenceEnd(text) {
  return String(text ?? "").trim().replace(/[。；;.！!]+$/u, "");
}

/**
 * 重查失败的说明：原因（sidecar 或主进程给的原话）+ sidecar 的建议动作（如有）+ 通用建议。
 * 「预览」菜单只在预览版（Electron 外壳）里存在，别处不提。
 */
export function recheckFailureMessage(error, preview = false) {
  const reason = trimSentenceEnd(error?.message) || "未知原因";
  const action = trimSentenceEnd(error?.action);
  const advice = preview
    ? "请确认两个模块已开机并在附近；也可在菜单「预览」中切换到演示模式。"
    : "请确认两个模块已开机并在附近。";
  return `重新检查设备失败：${reason}。${action ? `${action}。` : ""}${advice}`;
}
