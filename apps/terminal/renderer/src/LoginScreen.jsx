import { Button, Field, StatusPill } from "@gait/design-system";

/**
 * P-00 机构登录。只在要登录的终端上出现（RAY-323 R2，由 TerminalApp 的门决定）。
 *
 * 设备状态胶囊按快照画：原稿写死「设备已就绪」，而 P-00 是冷启动第一屏，模块这时
 * 常常还在连 —— 一句恒真的「已就绪」就是在第一屏上说错话。
 */
export function LoginScreen({ credentials, error, loading, deviceReady = false, onChange, onSubmit }) {
  return (
    <main className="login-page">
      <section className="login-card" aria-labelledby="login-title">
        <header className="login-header">
          <StatusPill tone={deviceReady ? "success" : "warning"} icon={deviceReady ? "check" : "warning"}>
            {deviceReady ? "设备已就绪" : "设备需要检查"}
          </StatusPill>
          <h1 id="login-title">步态健康筛查与分析平台</h1>
          <p>机构账户由服务方开通。终端接入凭据已在安装时写入，无需在此填写。</p>
        </header>
        <form className="login-form" onSubmit={onSubmit}>
          <Field
            label="机构账号"
            value={credentials.organization}
            onChange={(event) => onChange("organization", event.target.value)}
            autoComplete="username"
          />
          <Field
            label="登录密码"
            type="password"
            value={credentials.password}
            onChange={(event) => onChange("password", event.target.value)}
            autoComplete="current-password"
            error={error}
          />
          <Button type="submit" fullWidth loading={loading} loadingText="正在登录">
            登录
          </Button>
        </form>
        <p className="login-help">无法登录？请联系服务方确认机构账号。</p>
      </section>
    </main>
  );
}
