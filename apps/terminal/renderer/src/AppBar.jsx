const navItems = ["工作台", "检测记录", "设备与支持"];

/**
 * `onNavigate` is optional on purpose: during capture (P-08) there is no nav at
 * all, and the screens that do show the bar before routing exists render it
 * inert rather than pretending. A nav item that looks pressable and does
 * nothing is worse than one that is plainly not offered.
 */
export function AppBar({ current = "工作台", onNavigate }) {
  return (
    <header className="app-bar">
      <div className="app-bar__brand">步态健康筛查与分析平台</div>
      <nav aria-label="主导航">
        <ul className="app-bar__nav">
          {navItems.map((item) => {
            const active = item === current;
            return (
              <li key={item}>
                <button
                  type="button"
                  className={active ? "app-bar__nav-item app-bar__nav-item--active" : "app-bar__nav-item"}
                  aria-current={active ? "page" : undefined}
                  disabled={!onNavigate}
                  onClick={onNavigate ? () => onNavigate(item) : undefined}
                >
                  {item}
                </button>
              </li>
            );
          })}
        </ul>
      </nav>
    </header>
  );
}
