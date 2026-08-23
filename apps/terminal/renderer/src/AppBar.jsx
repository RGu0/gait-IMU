const navItems = ["工作台", "检测记录", "设备与支持"];

export function AppBar() {
  return (
    <header className="app-bar">
      <div className="app-bar__brand">步态健康筛查与分析平台</div>
      <nav aria-label="主导航">
        <ul className="app-bar__nav">
          {navItems.map((item, index) => (
            <li key={item}>
              <button
                type="button"
                className={index === 0 ? "app-bar__nav-item app-bar__nav-item--active" : "app-bar__nav-item"}
                aria-current={index === 0 ? "page" : undefined}
              >
                {item}
              </button>
            </li>
          ))}
        </ul>
      </nav>
    </header>
  );
}
