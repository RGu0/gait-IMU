import { Button, Field, StatusPill } from "@gait/design-system";

export function LoginScreen({ credentials, error, loading, onChange, onSubmit }) {
  return (
    <main className="login-page">
      <section className="login-card" aria-labelledby="login-title">
        <header className="login-header">
          <StatusPill tone="success" icon="check">
            设备已就绪
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
