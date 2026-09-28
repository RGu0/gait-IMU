/**
 * 已安装的 Preview 应用，演示模式端到端验收（RAY-493 A5；scope windows-ci-acceptance）。
 *
 *   A5_APP=<已安装的可执行文件> A5_OUT=<结果目录> node packaging/acceptance/preview_acceptance.mjs
 *
 * 自己拉起应用（`--remote-debugging-port` + 一次性 `--user-data-dir`），经 CDP 驱动真实渲染进程，
 * 同时读主进程 stdout 的 `sidecar state:` 行、按进程树找 sidecar。选择器与场景移植自
 * `.project-context/evidence/ray-493/parent-acceptance/2026-09-16-A5-macos-installed-7774809/`
 * 的 macOS 驱动；本脚本 macOS / Windows 通用，Windows 上另外判「无控制台窗口」（经
 * win_snapshot.ps1 枚举可见顶层窗口）。
 *
 * 场景：冷启动到工作台（零外网请求）→ 走满 60 秒出报告（waived、两条注记）→ 中途停止 →
 * 检测记录（完成 / 已停止、重开报告、工作台计数）→ 采集中强杀 sidecar（检测已中断、恢复、
 * 记录「未正常结束」）→ 落盘 meta → 退出无残留。全程视口固定 1280×720 并在四个页面判横向溢出。
 *
 * 退出码：任一项 FAIL 即 1。结果写 `<A5_OUT>/results.json`，截图在 `<A5_OUT>/screenshots/`。
 */
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { execFileSync, spawn } from "node:child_process";
import { fileURLToPath } from "node:url";
import { isSidecar, leftoverSidecars } from "./process_match.mjs";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const WIN = process.platform === "win32";
if (!process.env.A5_APP || !process.env.A5_OUT) throw new Error("A5_APP / A5_OUT required");
const APP = path.resolve(process.env.A5_APP);
const OUT = path.resolve(process.env.A5_OUT);
const PORT = process.env.CDP_PORT ?? "9333";
const USERDATA = process.env.A5_USERDATA ?? fs.mkdtempSync(path.join(os.tmpdir(), "gait-a5-"));
// 安装目录：用来认「这个安装包里的 sidecar」，而不是机器上别处的同名进程。
const INSTALL_ROOT = (WIN ? path.dirname(APP) : path.resolve(APP, "../../..")).toLowerCase();
const VIEWPORT = { width: 1280, height: 720 };
const SHOTS = path.join(OUT, "screenshots");
fs.mkdirSync(SHOTS, { recursive: true });

const logFile = fs.createWriteStream(path.join(OUT, "driver.log"), { flags: "a" });
const T0 = Date.now();
const ts = () => new Date().toISOString();
function log(line) {
  const text = `${ts()} +${((Date.now() - T0) / 1000).toFixed(1)}s ${line}`;
  logFile.write(`${text}\n`);
  console.log(`[a5] ${text}`);
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// ── 进程与窗口 ────────────────────────────────────────────────────────────
function winSnapshot() {
  const raw = execFileSync(
    "powershell.exe",
    ["-NoProfile", "-ExecutionPolicy", "Bypass", "-File", path.join(HERE, "win_snapshot.ps1")],
    { encoding: "utf8", maxBuffer: 64 * 1024 * 1024, windowsHide: true },
  );
  const snap = JSON.parse(raw);
  return { windows: snap.windows ?? [], processes: snap.processes ?? [] };
}
// macOS：可执行路径（comm）与完整命令行分两次取 —— 两列都可能含空格，只有放在最后一列才不会被截断。
// 判定只用 exe；command 仅作证据（RAY-547）。
function psColumn(col) {
  const out = new Map();
  for (const l of execFileSync("ps", ["-axo", `pid=,ppid=,${col}=`], { encoding: "utf8", maxBuffer: 64 * 1024 * 1024 }).split("\n")) {
    const m = /^\s*(\d+)\s+(\d+)\s+(.*)$/.exec(l);
    if (m) out.set(+m[1], { ppid: +m[2], value: m[3].trimEnd() });
  }
  return out;
}
function processes() {
  if (WIN) return winSnapshot().processes;
  const exes = psColumn("comm");
  const commands = psColumn("command");
  return [...exes].map(([pid, { ppid, value: exe }]) =>
    ({ pid, ppid, name: path.basename(exe), exe, command: commands.get(pid)?.value ?? null }));
}
function descendants(root, procs) {
  const byParent = new Map();
  for (const p of procs) { if (!byParent.has(p.ppid)) byParent.set(p.ppid, []); byParent.get(p.ppid).push(p); }
  const out = [];
  const stack = [root];
  while (stack.length) {
    const pid = stack.pop();
    for (const c of byParent.get(pid) ?? []) { out.push(c); stack.push(c.pid); }
  }
  return out;
}
// 应用拉起的 sidecar：进程树里最外层的那一个（PyInstaller 若有子进程也一并算作它）。
function sidecarProcesses(procs = processes()) {
  const tree = descendants(app.pid, procs).filter(isSidecar);
  const pids = new Set(tree.map((p) => p.pid));
  return tree.filter((p) => !pids.has(p.ppid));
}
function killHard(pid) {
  if (WIN) execFileSync("taskkill", ["/F", "/PID", String(pid)], { windowsHide: true });
  else execFileSync("kill", ["-9", String(pid)]);
}
const CONSOLE_CLASSES = /^(ConsoleWindowClass|CASCADIA_HOSTING_WINDOW_CLASS)$/;

// ── 启动已安装的应用 ──────────────────────────────────────────────────────
// Windows：先记下启动前已有的控制台窗口，之后只看新出现的。
const baselineConsoles = WIN ? new Set(winSnapshot().windows.filter((w) => CONSOLE_CLASSES.test(w.cls)).map((w) => w.hwnd)) : new Set();
const appLog = fs.createWriteStream(path.join(OUT, "app-stdout-stderr.log"), { flags: "a" });
const mainLines = [];
const env = Object.fromEntries(Object.entries(process.env).filter(([k]) => !k.startsWith("GAIT_")));
const app = spawn(APP, [`--remote-debugging-port=${PORT}`, `--user-data-dir=${USERDATA}`], { stdio: ["ignore", "pipe", "pipe"], env });
let appExited = null;
app.on("exit", (code, signal) => { appExited = { code, signal, t: Date.now() }; log(`app exited code=${code} signal=${signal}`); });
for (const stream of [app.stdout, app.stderr]) {
  let buf = "";
  stream.on("data", (chunk) => {
    appLog.write(chunk);
    buf += chunk.toString();
    let i;
    while ((i = buf.indexOf("\n")) >= 0) { mainLines.push({ t: Date.now(), line: buf.slice(0, i) }); buf = buf.slice(i + 1); }
  });
}
log(`spawned ${APP} pid=${app.pid} userData=${USERDATA}`);
async function waitMainLine(pred, timeout, since = 0) {
  const end = Date.now() + timeout;
  while (Date.now() < end) {
    const hit = mainLines.find((m) => m.t >= since && pred(m.line));
    if (hit) return hit;
    await sleep(100);
  }
  return null;
}

// ── CDP ──────────────────────────────────────────────────────────────────
let page = null;
for (let i = 0; i < 300 && !page; i++) {
  try { page = (await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json()).find((t) => t.type === "page"); } catch {}
  if (!page) await sleep(200);
}
if (!page) throw new Error("no page target on the remote debugging port");
log(`attaching to ${page.title} ${page.webSocketDebuggerUrl}`);
const ws = new WebSocket(page.webSocketDebuggerUrl);
await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });
let msgId = 0;
const pending = new Map();
const netRequests = [];
ws.onmessage = (ev) => {
  const m = JSON.parse(ev.data);
  if (m.id && pending.has(m.id)) {
    const { resolve, reject } = pending.get(m.id);
    pending.delete(m.id);
    if (m.error) reject(new Error(JSON.stringify(m.error)));
    else resolve(m.result);
  }
  if (m.method === "Network.requestWillBeSent") netRequests.push({ ms: Date.now() - T0, url: m.params.request.url });
};
function send(method, params = {}) {
  const id = ++msgId;
  return new Promise((resolve, reject) => {
    pending.set(id, { resolve, reject });
    try { ws.send(JSON.stringify({ id, method, params })); } catch (e) { pending.delete(id); reject(e); return; }
    setTimeout(() => { if (pending.has(id)) { pending.delete(id); reject(new Error(`${method} timeout`)); } }, 60_000);
  });
}
await send("Network.enable");
await send("Page.enable");
await send("Runtime.enable");
// 视口钉在 1280×720（A5 的布局判据）。不靠改窗口大小：CI runner 的屏幕可能只有 1024×768，
// 而窗口 minWidth 是 1280，真实窗口尺寸在不同机器上不可复现。
await send("Emulation.setDeviceMetricsOverride", { ...VIEWPORT, deviceScaleFactor: 1, mobile: false });
const isExternal = (u) => !/^(file:|devtools:|data:|blob:|chrome-extension:|about:)/.test(u ?? "");

async function js(expression) {
  const r = await send("Runtime.evaluate", { expression, returnByValue: true, awaitPromise: true });
  if (r.exceptionDetails) throw new Error(`${r.exceptionDetails.text} ${r.exceptionDetails.exception?.description ?? ""}`);
  return r.result?.value;
}
async function safeJs(code, fallback = null) { try { return await js(code); } catch { return fallback; } }
const bodyText = () => safeJs("document.body ? document.body.innerText : ''", "");
async function waitFor(predicateCode, timeout = 30_000, interval = 200) {
  const end = Date.now() + timeout;
  while (Date.now() < end) {
    if (await safeJs(`(() => { try { return Boolean(${predicateCode}); } catch (e) { return false; } })()`, false)) return true;
    await sleep(interval);
  }
  return false;
}
const waitForText = (text, timeout = 30_000) => waitFor(`document.body && document.body.innerText.includes(${JSON.stringify(text)})`, timeout);
async function clickButton(text, within = "body", timeout = 15_000) {
  const code = `(() => {
    const root = document.querySelector(${JSON.stringify(within)});
    if (!root) return false;
    const buttons = [...root.querySelectorAll("button")].filter((b) => !b.disabled && b.textContent.includes(${JSON.stringify(text)}));
    const target = buttons.find((b) => b.textContent.trim() === ${JSON.stringify(text)}) || buttons[0];
    if (!target) return false;
    target.click();
    return true;
  })()`;
  const end = Date.now() + timeout;
  while (Date.now() < end) {
    if (await safeJs(code, false)) { log(`click "${text}" (within ${within})`); return; }
    await sleep(200);
  }
  throw new Error(`button "${text}" not clickable within ${timeout}ms`);
}
async function check(labelText, timeout = 10_000) {
  const code = `(() => {
    const label = [...document.querySelectorAll("label")].find((l) => l.textContent.includes(${JSON.stringify(labelText)}));
    const input = label?.querySelector('input[type="checkbox"]');
    if (!input) return false;
    if (!input.checked) input.click();
    return input.checked;
  })()`;
  const end = Date.now() + timeout;
  while (Date.now() < end) {
    if (await safeJs(code, false)) return;
    await sleep(200);
  }
  throw new Error(`checkbox "${labelText}" not found`);
}
let shotSeq = 0;
async function shot(name) {
  shotSeq += 1;
  const file = `${String(shotSeq).padStart(2, "0")}-${name}.png`;
  try {
    const { data } = await send("Page.captureScreenshot", { format: "png" });
    fs.writeFileSync(path.join(SHOTS, file), Buffer.from(data, "base64"));
  } catch (e) { log(`screenshot ${file} FAILED: ${e.message}`); }
  return file;
}
const results = [];
function record(id, status, observed, shots = []) {
  results.push({ id, status, observed, shots, at: ts() });
  log(`RESULT ${id}: ${status} — ${JSON.stringify(observed).slice(0, 400)}`);
  fs.writeFileSync(path.join(OUT, "results.json"), JSON.stringify({ app: APP, platform: process.platform, results }, null, 2));
}
const pass = (ok) => (ok ? "PASS" : "FAIL");
async function failDump(id, error) {
  const file = await shot(`${id}-FAILURE`);
  fs.writeFileSync(path.join(OUT, `${id}-FAILURE-body.txt`), String(await bodyText()));
  log(`FAILURE in ${id}: ${error?.stack ?? error}`);
  return file;
}
function readMetas() {
  const root = path.join(USERDATA, "sessions");
  if (!fs.existsSync(root)) return [];
  return fs.readdirSync(root).sort().flatMap((dir) => {
    const metaPath = path.join(root, dir, "meta.json");
    if (!fs.existsSync(metaPath)) return [];
    try { return [{ dir, meta: JSON.parse(fs.readFileSync(metaPath, "utf8")) }]; }
    catch (e) { return [{ dir, error: e.message }]; }
  });
}
const sessionDirs = () => readMetas().map((m) => m.dir);

// ── 可复用的判定 ──────────────────────────────────────────────────────────
async function layout(id, label) {
  const m = await safeJs(`(() => {
    const w = document.documentElement.clientWidth;
    const over = [...document.querySelectorAll("body *")].filter((e) => {
      const r = e.getBoundingClientRect();
      return r.width > 0 && r.height > 0 && getComputedStyle(e).visibility !== "hidden" && r.right > w + 1;
    }).slice(0, 10).map((e) => e.tagName.toLowerCase() + "." + String(e.className).split(" ")[0] + " right=" + Math.round(e.getBoundingClientRect().right));
    return { innerWidth, innerHeight, clientWidth: w, scrollWidth: document.documentElement.scrollWidth, bodyScrollWidth: document.body.scrollWidth, over };
  })()`, null);
  const s = await shot(`layout-${label}`);
  record(id, pass(m && m.innerWidth === VIEWPORT.width && m.innerHeight === VIEWPORT.height
    && m.scrollWidth <= m.clientWidth && m.bodyScrollWidth <= m.clientWidth && m.over.length === 0), { page: label, ...m }, [s]);
}

// Windows：应用进程树里不许有可见的控制台窗口，启动后也不许冒出新的控制台窗口。
// 正对照：主窗口本身必须能被看见 —— 看不见说明这个桌面会话里窗口枚举不可信，判 FAIL 而非放过。
let controlHwnds = new Set();
async function consoleCheck(id, moment) {
  if (!WIN) return;
  const snap = winSnapshot();
  const tree = descendants(app.pid, snap.processes);
  const treePids = new Set([app.pid, ...tree.map((p) => p.pid)]);
  const sidecarSide = new Set(tree.filter((p) => isSidecar(p) || /^conhost\.exe$/i.test(p.name ?? "")).map((p) => p.pid));
  for (const sc of tree.filter(isSidecar)) for (const d of descendants(sc.pid, snap.processes)) sidecarSide.add(d.pid);
  const mine = snap.windows.filter((w) => treePids.has(w.pid));
  const mainWindow = mine.find((w) => w.pid === app.pid && !CONSOLE_CLASSES.test(w.cls));
  const offending = mine.filter((w) => CONSOLE_CLASSES.test(w.cls) || sidecarSide.has(w.pid));
  const newConsoles = snap.windows.filter((w) => CONSOLE_CLASSES.test(w.cls) && !baselineConsoles.has(w.hwnd) && !controlHwnds.has(w.hwnd));
  record(id, pass(Boolean(mainWindow) && offending.length === 0 && newConsoles.length === 0), {
    moment,
    mainWindow: mainWindow ?? "NOT VISIBLE — window enumeration not trustworthy here",
    appTreeWindows: mine,
    offending,
    newConsoles,
    sidecars: tree.filter(isSidecar),
  });
}
async function consolePositiveControl() {
  if (!WIN) return;
  // detached 在 Windows 上即 CREATE_NEW_CONSOLE：一个肯定可见的控制台窗口。
  const probe = spawn("cmd.exe", ["/c", "ping -n 30 127.0.0.1 >nul"], { detached: true, stdio: "ignore", windowsHide: false });
  await sleep(2_500);
  const snap = winSnapshot();
  const probeTree = new Set([probe.pid, ...descendants(probe.pid, snap.processes).map((p) => p.pid)]);
  const seen = snap.windows.filter((w) => CONSOLE_CLASSES.test(w.cls) && (probeTree.has(w.pid) || !baselineConsoles.has(w.hwnd)));
  controlHwnds = new Set(seen.map((w) => w.hwnd));
  try { execFileSync("taskkill", ["/F", "/T", "/PID", String(probe.pid)], { windowsHide: true }); } catch {}
  record("W.0 positive control: a deliberately visible console IS detected", pass(seen.length > 0), { probePid: probe.pid, seen });
}

// ── 流程 ──────────────────────────────────────────────────────────────────
async function waitHub(timeout = 90_000) {
  const ok = await waitFor(`[...document.querySelectorAll("button")].some((b) => b.textContent.trim() === "开始新的检测") && document.body.innerText.includes("工作台")`, timeout);
  if (!ok) throw new Error("hub did not appear");
}
async function toHub() {
  if (await safeJs(`[...document.querySelectorAll("button")].some((b) => b.textContent.trim() === "开始新的检测")`, false)) return;
  try { await clickButton("工作台", ".app-bar", 5_000); } catch {}
  await waitHub(30_000);
}
const readRun = () => safeJs(`(() => ({
  clock: document.querySelector(".run-bar__clock")?.textContent?.replace(/\\s+/g, "") ?? null,
  steps: [...document.querySelectorAll(".step-tile__value")].map((e) => e.textContent),
}))()`, null);
const hubCounts = () => safeJs(`(() => ({
  cards: [...document.querySelectorAll(".hub-card")].map((c) => c.innerText.replace(/\\s+/g, " ")).join(" | "),
  recent: document.querySelector(".recent-records")?.innerText.replace(/\\s+/g, " ") ?? "",
}))()`, { cards: "", recent: "" });

async function walkToRun(prefix) {
  const obs = {};
  await clickButton("开始新的检测");
  if (!(await waitForText("受试者识别"))) throw new Error("subject screen missing");
  await clickButton("无编号，快速建档");
  if (!(await waitForText("选填档案"))) throw new Error("profile screen missing");
  await clickButton("跳过");
  if (!(await waitForText("数据授权"))) throw new Error("consent screen missing");
  await check("采集与本地保存行走过程中的足踝运动数据");
  await check("将本次检测数据上传至所属机构的账户");
  await clickButton("同意并继续");
  if (!(await waitForText("安全确认与设备自检"))) throw new Error("preflight screen missing");
  await check("往返通道已清空");
  await check("跌倒高风险受试者已有工作人员在侧陪护");
  await check("受试者当前状态适合进行 3 分钟步行");
  await waitFor(`document.querySelector(".preflight-section") && !document.querySelector(".preflight-running") && document.querySelector(".preflight-section").innerText.includes("出厂标定")`, 60_000, 50);
  obs.preflightText = await safeJs(`document.querySelector(".preflight-section")?.innerText ?? ""`, "");
  obs.preflightWaivedIcon = await safeJs(`Boolean(document.querySelector('.preflight-section [aria-label="已豁免"]'))`, false);
  obs.preflightShot = await shot(`${prefix}-preflight`);
  if (!(await waitForText("佩戴引导", 30_000))) throw new Error("preflight did not advance to the wear guide");
  await check("已按图示佩戴完成");
  await clickButton("佩戴完成，开始标定");
  if (!(await waitForText("确认左右"))) throw new Error("wear confirm screen missing");
  await check("已逐一核对");
  const dirsBefore = sessionDirs();
  await clickButton("确认无误，开始检测");
  if (!(await waitFor(`document.querySelector(".run-page") || document.body.innerText.includes("检测未能开始")`, 60_000))) throw new Error("run screen did not appear");
  if (await safeJs(`document.body.innerText.includes("检测未能开始")`, false)) throw new Error(`检测未能开始: ${await bodyText()}`);
  obs.runStartedAt = Date.now();
  for (let i = 0; i < 50 && !obs.sessionId; i++) {
    obs.sessionId = sessionDirs().find((d) => !dirsBefore.includes(d)) ?? null;
    if (!obs.sessionId) await sleep(100);
  }
  log(`${prefix} run started, session ${obs.sessionId}`);
  return obs;
}

const sessions = {};

async function scenarioBoot() {
  const hub = await waitFor(`[...document.querySelectorAll("button")].some((b) => b.textContent.trim() === "开始新的检测") && document.body.innerText.includes("工作台")`, 120_000, 100);
  const msToHub = Date.now() - T0;
  await waitForText("演示数据", 10_000);
  const text = await bodyText();
  const ready = mainLines.find((m) => m.line.includes("sidecar state: ready"));
  const s = await shot("boot-hub");
  record("B.1 cold boot reaches the hub with preview + demo banner", pass(hub && text.includes("预览版") && text.includes("演示数据，非实测")),
    { msToHub, sidecarReadyMs: ready ? ready.t - T0 : null, banner: await safeJs(`document.querySelector(".preview-banner")?.innerText ?? ""`, "") }, [s]);
  const resources = await safeJs(`performance.getEntriesByType("resource").map((e) => e.name)`, []);
  const external = [...resources.filter(isExternal), ...netRequests.map((r) => r.url).filter(isExternal)];
  record("B.2 zero external requests during boot", pass(external.length === 0), { resourceCount: resources.length, external });
  await layout("L.1 1280×720 hub: no horizontal overflow", "hub");
}

async function scenarioFull() {
  let obs;
  try { obs = await walkToRun("F"); }
  catch (error) { record("F.1 full walk: wizard to run", "FAIL", String(error.message), [await failDump("F", error)]); return; }
  sessions.full = obs.sessionId;
  record("F.1 quick-create → consent → preflight (factory calibration waived) → wear → confirm → run",
    pass(obs.preflightWaivedIcon && /已豁免/.test(obs.preflightText) && obs.sessionId),
    { sessionId: obs.sessionId, calibLine: obs.preflightText.split("\n").find((l) => l.includes("出厂标定")) ?? "" }, [obs.preflightShot]);
  while (Date.now() < obs.runStartedAt + 5_000) await sleep(100);
  const s5 = await readRun();
  await consoleCheck("W.2 no console window while capturing", "mid-run t≈5 s");
  while (Date.now() < obs.runStartedAt + 30_000) await sleep(100);
  const s30 = await readRun();
  const sh30 = await shot("F-run-t30");
  record("F.2 countdown and step counts refresh from session.tick", pass(Number(s30?.steps?.[0]) > Number(s5?.steps?.[0]) && s5?.clock !== s30?.clock), { s5, s30 }, [sh30]);
  await layout("L.2 1280×720 run page: no horizontal overflow", "run");
  const reached = await waitFor(`document.querySelector(".rp-page") || document.body.innerText.includes("报告未能生成") || document.body.innerText.includes("本次检测未生成报告")`, 150_000, 500);
  const secondsToReport = ((Date.now() - obs.runStartedAt) / 1000).toFixed(1);
  const report = await safeJs(`(() => ({
    hasPage: Boolean(document.querySelector(".rp-page")),
    annotation: document.querySelector(".rp-annotation")?.innerText ?? "",
    metrics: [...document.querySelectorAll(".rp-metric")].map((m) => m.innerText.replace(/\\s+/g, " ")).slice(0, 12),
  }))()`, null);
  const rs = await shot("F-report");
  if (!reached || !report?.hasPage) {
    record("F.3 report appears with metrics", "FAIL", { reached, secondsToReport, finalText: (await bodyText()).slice(0, 1500) }, [rs]);
    return;
  }
  record("F.3 report appears with metrics", pass(report.metrics.length > 0), { secondsToReport, metrics: report.metrics }, [rs]);
  const full = await bodyText();
  record("F.4 report carries both annotations (preview waiver, demo data), no 「。；」",
    pass(report.annotation.includes("预览版：出厂标定参数未匹配") && report.annotation.includes("演示数据") && !full.includes("。；")),
    { annotation: report.annotation });
  await layout("L.3 1280×720 report: no horizontal overflow", "report");
  await toHub();
}

async function scenarioStopped() {
  try {
    await toHub();
    const obs = await walkToRun("S");
    sessions.stopped = obs.sessionId;
    while (Date.now() < obs.runStartedAt + 5_000) await sleep(100);
    await clickButton("停止检测", ".run-stop");
    if (!(await waitFor(`document.querySelector('[role="alertdialog"]')`, 5_000))) throw new Error("stop dialog did not open");
    await clickButton("停止检测", '[role="alertdialog"]');
    let hubOk = true;
    try { await waitHub(30_000); } catch { hubOk = false; }
    await sleep(800);
    const leftover = sidecarProcesses().length;
    record("S.1 stop at ≈5 s returns to the hub; one sidecar, no leftover capture", pass(hubOk && leftover === 1), { sessionId: obs.sessionId, sidecars: leftover }, [await shot("S-hub-after-stop")]);
  } catch (error) {
    record("S.1 stopped walk", "FAIL", String(error.message), [await failDump("S", error)]);
    try { await toHub(); } catch {}
  }
}

async function readRecords(prefix) {
  await clickButton("检测记录", ".app-bar");
  await waitFor(`!document.querySelector(".rp-page") && (document.querySelector('[aria-label="检测记录"] table') || document.body.innerText.includes("当前筛选条件下没有记录"))`, 15_000);
  await sleep(300);
  const rows = await safeJs(`[...document.querySelectorAll('[aria-label="检测记录"] table tbody tr')].map((r) => r.innerText.replace(/\\s+/g, " "))`, []);
  return { rows, shot: await shot(`${prefix}-records`) };
}
const rowFor = (rows, id) => rows.find((r) => id && r.includes(id)) ?? null;

async function scenarioRecords() {
  try {
    const { rows, shot: s } = await readRecords("R");
    const full = rowFor(rows, sessions.full);
    const stopped = rowFor(rows, sessions.stopped);
    record("R.1 full walk row shows 「完成」", pass(full && /(^| )完成( |$)/.test(full) && !full.includes("已停止")), { row: full, rows }, [s]);
    const m = stopped ? /已停止（(\d+)\/(\d+) 秒）/.exec(stopped) : null;
    record("R.2 stopped row shows 「已停止（N/60 秒）」 with N≈5", pass(m && Math.abs(Number(m[1]) - 5) <= 2 && m[2] === "60"), { row: stopped });
    await layout("L.4 1280×720 records: no horizontal overflow", "records");
    // A2：检测记录里可以重开该条的报告。
    const clicked = await safeJs(`(() => {
      const row = [...document.querySelectorAll('[aria-label="检测记录"] table tbody tr')].find((r) => r.innerText.includes(${JSON.stringify(sessions.full ?? "")}));
      const button = row && [...row.querySelectorAll("button")].find((b) => b.textContent.includes("查看"));
      if (!button) return false;
      button.click();
      return true;
    })()`, false);
    const reopened = clicked && (await waitFor(`document.querySelector(".rp-page") && document.querySelectorAll(".rp-metric").length > 0`, 30_000));
    const ann = await safeJs(`document.querySelector(".rp-annotation")?.innerText ?? ""`, "");
    record("R.3 reopen the full walk's report from the records list", pass(reopened && ann.includes("演示数据")), { clicked, annotation: ann }, [await shot("R-reopened-report")]);
    await clickButton("工作台", ".app-bar");
    await waitHub(15_000);
    await sleep(1_000);
    const c = await hubCounts();
    const upload = /待上传 (\d+) 条/.exec(c.cards)?.[1] ?? null;
    const recentN = /最近检测记录\s*(\d+)\s*条/.exec(c.recent)?.[1] ?? null;
    record("R.4 hub refreshes: 待上传 2 条 / 最近检测记录 2 条", pass(upload === "2" && recentN === "2"), { upload, recentN, ...c }, [await shot("R-hub-counts")]);
  } catch (error) {
    record("R", "FAIL", String(error.message), [await failDump("R", error)]);
    try { await toHub(); } catch {}
  }
}

async function scenarioKilled() {
  try {
    await toHub();
    const obs = await walkToRun("K");
    sessions.killed = obs.sessionId;
    while (Date.now() < obs.runStartedAt + 8_000) await sleep(100);
    const target = sidecarProcesses()[0];
    if (!target) throw new Error(`no sidecar process under app pid ${app.pid}`);
    const killedAt = Date.now();
    killHard(target.pid);
    log(`hard-killed sidecar ${target.pid} at run t+${((killedAt - obs.runStartedAt) / 1000).toFixed(1)}s`);
    let interruptedAtMs = null;
    while (Date.now() < killedAt + 5_000 && interruptedAtMs === null) {
      if (await safeJs(`document.body.innerText.includes("检测已中断")`, false)) interruptedAtMs = Date.now() - killedAt;
      else await sleep(50);
    }
    const ready = await waitMainLine((l) => l.includes("sidecar state: ready"), 60_000, killedAt);
    record("K.1 sidecar hard-killed mid-walk: 「检测已中断」 within 3 s, sidecar restarts",
      pass(interruptedAtMs !== null && interruptedAtMs <= 3_000 && ready),
      { killed: target, interruptedAtMs, readyAfterMs: ready ? ready.t - killedAt : null,
        states: mainLines.filter((m) => m.t >= killedAt && m.line.includes("sidecar state")).map((m) => `+${m.t - killedAt}ms ${m.line}`) },
      [await shot("K-interrupted")]);
    await sleep(15_000);
    const held = await safeJs(`({ interrupted: document.body.innerText.includes("检测已中断"), run: Boolean(document.querySelector(".run-page")),
      reportFailed: document.body.innerText.includes("报告未能生成") || document.body.innerText.includes("会话尚未开始") })`, null);
    record("K.2 interrupted screen holds (no countdown, no report error) 15 s later", pass(held?.interrupted && !held.run && !held.reportFailed), held);
    await consoleCheck("W.3 no console window from the restarted sidecar", "after sidecar restart");
    let hubBack = false;
    try { await clickButton("返回工作台", ".invalid-body", 5_000); await waitHub(30_000); hubBack = true; } catch {}
    const killedAlive = processes().some((p) => p.pid === target.pid);
    record("K.3 「返回工作台」 recovers to the hub; killed process gone", pass(hubBack && !killedAlive), { killedAlive, sidecars: sidecarProcesses() }, [await shot("K-hub")]);
    const { rows, shot: s } = await readRecords("K");
    const row = rowFor(rows, sessions.killed);
    record("K.4 killed session row shows 「未正常结束」; earlier rows unchanged",
      pass(row?.includes("未正常结束") && rowFor(rows, sessions.full)?.includes("完成") && /已停止（\d+\/60 秒）/.test(rowFor(rows, sessions.stopped) ?? "")),
      { row, rows }, [s]);
    await toHub();
  } catch (error) {
    record("K", "FAIL", String(error.message), [await failDump("K", error)]);
    try { await toHub(); } catch {}
  }
}

function scenarioDisk() {
  const metas = readMetas();
  fs.writeFileSync(path.join(OUT, "sessions-meta.json"), JSON.stringify(metas, null, 2));
  const pick = (id) => metas.find((m) => m.dir === id)?.meta ?? null;
  const f = pick(sessions.full);
  const s = pick(sessions.stopped);
  const k = pick(sessions.killed);
  const pc = (m) => m?.protocol_config ?? {};
  record("D.1 full walk on disk: finished, elapsed 59.5–64 s", pass(f && pc(f).state === "finished" && pc(f).elapsed_seconds >= 59.5 && pc(f).elapsed_seconds <= 64), pc(f));
  record("D.2 stopped walk on disk: finished, elapsed ≈5 s", pass(s && pc(s).state === "finished" && Math.abs(pc(s).elapsed_seconds - 5) <= 2), pc(s));
  record("D.3 killed walk on disk never reached close (state walking)", pass(k && pc(k).state === "walking"), pc(k));
  record("D.4 every session records provenance synthetic / hardware=false / factory_calibration_waived",
    pass(metas.length === 3 && metas.every((m) => m.meta?.extra?.provenance?.source === "synthetic" && m.meta?.extra?.provenance?.hardware === false && m.meta?.extra?.preview?.factory_calibration_waived === true)),
    metas.map((m) => ({ dir: m.dir, provenance: m.meta?.extra?.provenance, preview: m.meta?.extra?.preview })));
}

// ── 运行 ──────────────────────────────────────────────────────────────────
try {
  await scenarioBoot();
  await consolePositiveControl();
  await consoleCheck("W.1 no console window after boot", "hub, sidecar ready");
  await scenarioFull();
  await scenarioStopped();
  await scenarioRecords();
  await scenarioKilled();
  scenarioDisk();
} catch (error) {
  record("driver", "FAIL", String(error?.stack ?? error), [await failDump("driver", error)]);
} finally {
  const quitAt = Date.now();
  await safeJs("window.close()", null);
  for (let i = 0; i < 150 && !appExited; i++) await sleep(100);
  const quitVia = appExited ? "window.close()" : "forced";
  if (!appExited) {
    log("window.close() did not quit the app in 15 s; killing it");
    try { killHard(app.pid); } catch {}
    for (let i = 0; i < 100 && !appExited; i++) await sleep(100);
  }
  await sleep(4_000);
  const leftovers = leftoverSidecars(processes(), INSTALL_ROOT);
  record("Q.1 clean quit via window close, no orphan sidecar", pass(quitVia === "window.close()" && leftovers.length === 0),
    { quitVia, msToExit: appExited ? appExited.t - quitAt : null, leftovers });
  const summary = results.reduce((a, r) => ({ ...a, [r.status]: (a[r.status] ?? 0) + 1 }), {});
  log(`SUMMARY ${JSON.stringify(summary)}`);
  fs.writeFileSync(path.join(OUT, "results.json"), JSON.stringify({ app: APP, platform: process.platform, userData: USERDATA, sessions, summary, results }, null, 2));
  try { ws.close(); } catch {}
  process.exit(summary.FAIL ? 1 : 0);
}
