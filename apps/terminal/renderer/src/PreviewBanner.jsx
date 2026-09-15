import { Banner } from "@gait/design-system";

/**
 * 真 sidecar 路径上的常驻提示（RAY-493）。
 *
 * 「预览版」始终在：这一版的采集链还没过外场验收，屏上的每个数都不该被当成交付结论。
 * 数据来源不是真机（`snapshot.source` 存在且不是 `"ble"`）时再加一句「演示数据，非实测」
 * —— stub / synthetic / replay 产生的界面与真机采集长得一模一样，不写出来就分不开。
 *
 * `source` 缺失（旧 sidecar 还没有这个字段）时不猜：只显示「预览版」。
 */
export function PreviewBanner({ source }) {
  const notMeasured = typeof source === "string" && source !== "ble";
  return (
    <Banner tone="warning" title="预览版" role="note" aria-label="预览版提示" className="preview-banner">
      {notMeasured ? `演示数据，非实测（数据来源：${source}）` : "本版本仅供预览，结论不作为正式检测依据。"}
    </Banner>
  );
}
