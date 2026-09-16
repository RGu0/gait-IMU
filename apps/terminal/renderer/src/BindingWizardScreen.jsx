import { useEffect, useRef, useState } from "react";
import { Banner, Button, StepBar } from "@gait/design-system";

/**
 * 左右模块配对向导（RAY-479）。
 *
 * 用户拍板（2026-09-16）：**左脚是蓝色外壳的模块，右脚是橙色外壳的模块**；配对先左后右，
 * 每一步只开那一种颜色的模块，由 sidecar 读出它自报的 MAC 存为这只脚的绑定。
 *
 * 这里说的「蓝色 / 橙色」是**外壳颜色**，不是屏幕上的左右识别色（设计令牌里左蓝右青）——
 * 操作员手里拿的是外壳，所以文案只说外壳。
 *
 * 屏上没有「跳过」，也没有「对调」：一只脚没绑成就停在这一步，错误原样给出 sidecar 的
 * 现象与动作（例如「发现多个未绑定模块：请只打开蓝色（左脚）模块」），然后「重试」。
 */

export const BINDING_STEPS = ["绑定左脚", "绑定右脚", "完成"];

const FEET = [
  { foot: "L", side: "left", name: "左脚", color: "蓝色" },
  { foot: "R", side: "right", name: "右脚", color: "橙色" },
];

const LEADS = {
  L: "请先把所有模块关机，然后只打开蓝色模块，放在电脑旁。",
  R: "只打开橙色模块，放在电脑旁。蓝色模块开着也没关系 —— 它已经绑定为左脚。",
};

function failureOf(error) {
  return {
    message: error?.notice?.message ?? error?.message ?? "识别失败。",
    action: error?.notice?.action ?? error?.action ?? "",
    code: error?.code,
  };
}

export function BindingWizardScreen({ bindFoot, onDone, onCancel }) {
  const [step, setStep] = useState(0);
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState(null);
  const [bound, setBound] = useState({});
  const mounted = useRef(true);
  useEffect(() => () => {
    mounted.current = false;
  }, []);

  const current = FEET[step];
  const finished = step >= FEET.length;

  async function identify() {
    if (busy || !current) return;
    setBusy(true);
    setFailure(null);
    try {
      const result = await bindFoot(current.foot);
      if (!mounted.current) return;
      setBound((previous) => ({ ...previous, [current.foot]: result?.masked ?? "" }));
      setStep((previous) => previous + 1);
    } catch (error) {
      if (mounted.current) setFailure(failureOf(error));
    } finally {
      if (mounted.current) setBusy(false);
    }
  }

  const title = finished
    ? "左右模块已绑定"
    : `绑定${current.name}：只打开${current.color}模块`;

  return (
    <div className="wizard-page binding-wizard">
      <header className="wizard-header">
        <StepBar steps={BINDING_STEPS} current={step} />
      </header>
      <main className="wizard-body wizard-body--narrow">
        <div className="wizard-heading">
          <h1>{title}</h1>
          <p className="wizard-lead">
            {finished ? "之后每次连接都按绑定区分左右：蓝色模块戴左脚、橙色模块戴右脚。" : LEADS[current.foot]}
          </p>
        </div>

        {FEET.filter(({ foot }) => bound[foot] !== undefined).map(({ foot, name }) => (
          <Banner key={foot} tone="success" aria-label={`${name}绑定结果`}>
            {name} 已绑定 {bound[foot]}
          </Banner>
        ))}

        {failure ? (
          <Banner tone="danger" title={failure.message} aria-label="识别失败">
            {failure.action}
            {failure.code ? `（${failure.code}）` : ""}
          </Banner>
        ) : null}

        {busy ? (
          <p className="binding-wizard__busy" role="status">
            正在识别{current?.color}模块，约需 10–30 秒…
          </p>
        ) : null}
      </main>
      <footer className="wizard-actions">
        {finished ? (
          <Button size="lg" onClick={onDone}>完成</Button>
        ) : (
          <>
            <Button variant="secondary" onClick={onCancel} disabled={busy}>取消</Button>
            <Button size="lg" onClick={identify} loading={busy} loadingText="正在识别…">
              {failure ? "重试" : "开始识别"}
            </Button>
          </>
        )}
      </footer>
    </div>
  );
}
