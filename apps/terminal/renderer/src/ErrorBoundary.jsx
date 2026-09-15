import { Component } from "react";
import { Button } from "@gait/design-system";

/**
 * 最后一道兜底（RAY-493）。
 *
 * 任何一屏渲染时抛错，React 默认卸掉整棵树 —— 终端上就是一块白屏，没有任何按钮。
 * 这里至少给出发生了什么，以及一条能走的路：重新载入回到工作台。
 */
export class ErrorBoundary extends Component {
  constructor(props) {
    super(props);
    this.state = { error: null };
  }

  static getDerivedStateFromError(error) {
    return { error };
  }

  componentDidCatch(error, info) {
    // 诊断信息只进控制台，不进界面。
    console.error("界面渲染失败", error, info?.componentStack);
  }

  render() {
    const { error } = this.state;
    if (!error) return this.props.children;
    const reset = this.props.onReset ?? (() => globalThis.location?.reload());
    return (
      <div className="sidecar-down" role="alert">
        <div className="sidecar-down__panel">
          <p className="sidecar-down__message">界面出现了意外错误。</p>
          <p className="sidecar-down__action">{String(error?.message ?? error)}</p>
          <Button size="lg" onClick={reset}>返回工作台</Button>
        </div>
      </div>
    );
  }
}
