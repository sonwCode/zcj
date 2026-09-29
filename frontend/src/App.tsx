import {
  BrowserRouter,
  NavLink,
  Route,
  Routes,
  useLocation,
} from "react-router-dom";
import { lazy, Suspense, useEffect, useState } from "react";
import { API, cn } from "@/lib/utils";
import { I18nProvider, useI18n } from "@/lib/i18n-context";
import type { TranslationKey } from "@/lib/i18n";
import UpdateBanner from "@/components/UpdateBanner";
import ActiveTaskDock from "@/components/tasks/ActiveTaskDock";
import { SchedulerHealth } from "@/components/SchedulerHealth";
import {
  LayoutDashboard,
  Moon,
  Settings as SettingsIcon,
  Sun,
  Monitor,
  Languages,
  Users,
  ClipboardList,
  UserPlus,
  CreditCard,
  Network,
  Cable,
  Boxes,
  ShieldAlert,
  Inbox,
  Route as RouteIcon,
  PanelLeftClose,
  PanelLeft,
} from "lucide-react";

const Dashboard = lazy(() => import("@/pages/Dashboard"));
const Accounts = lazy(() => import("@/pages/Accounts"));
const SmsPoolBlacklist = lazy(() => import("@/pages/SmsPoolBlacklist"));
const Register = lazy(() => import("@/pages/RegisterWorkbench"));
const RegisterCenter = lazy(() => import("@/pages/RegisterCenter"));
const OtherRegisterWorkbench = lazy(() => import("@/pages/OtherRegisterWorkbench"));
const Tasks = lazy(() => import("@/pages/Tasks"));
const Integrations = lazy(() => import("@/pages/Integrations"));
const SmsSettings = lazy(() => import("@/pages/SmsSettings"));
const Proxies = lazy(() => import("@/pages/Proxies"));
const SettingsPage = lazy(() => import("@/pages/SettingsPage"));
const TaskHistory = lazy(() => import("@/pages/TaskHistory"));
const CtfGptPlus = lazy(() => import("@/pages/CtfGptPlus"));
const GoPayGptPlus = lazy(() => import("@/pages/GoPayGptPlus"));
const PlusManager = lazy(() => import("@/pages/PlusManager"));
const MicrosoftMailboxes = lazy(() => import("@/pages/MicrosoftMailboxes"));
const ProxyPoolSettings = lazy(() => import("@/pages/ProxyPoolSettings"));

function RouteFallback() {
  return (
    <div className="flex min-h-[320px] items-center justify-center text-sm text-[var(--text-muted)]">
      页面加载中…
    </div>
  );
}

/* ------------------------------------------------------------------ */
/*  Sidebar                                                            */
/* ------------------------------------------------------------------ */

type NavItem = {
  path: string;
  labelKey?: TranslationKey;
  label?: string;
  icon: any;
  exact?: boolean;
  group?: string;
};

const SETTINGS_NAV_ITEMS: { labelKey: TranslationKey; hash: string }[] = [
  { labelKey: "nav.settings.general", hash: "general" },
  { labelKey: "nav.settings.register", hash: "register" },
  { labelKey: "nav.settings.mailbox", hash: "mailbox" },
  { labelKey: "nav.settings.captcha", hash: "captcha" },
  { labelKey: "nav.settings.sms", hash: "sms" },
  { labelKey: "nav.settings.proxies", hash: "proxies" },
  { labelKey: "nav.settings.chatgpt", hash: "chatgpt" },
  { labelKey: "nav.settings.bitbrowser", hash: "bitbrowser" },
  { labelKey: "nav.settings.advanced", hash: "advanced" },
  { labelKey: "nav.settings.about", hash: "about" },
];

const NAV_ITEMS: NavItem[] = [
  { path: "/", labelKey: "nav.dashboard", icon: LayoutDashboard, exact: true, group: "总览" },
  { path: "/accounts/chatgpt", label: "账号池", icon: Users, group: "账号" },
  { path: "/register", label: "注册中心", icon: UserPlus, group: "账号" },
  { path: "/register-other", label: "多平台注册", icon: Boxes, group: "账号" },
  { path: "/tasks", label: "任务中心", icon: ClipboardList, group: "账号" },
  { path: "/history", label: "任务历史", icon: ClipboardList, group: "账号" },
  { path: "/plus-manager", label: "Plus 管理", icon: CreditCard, group: "PLUS" },
  { path: "/ctf-gpt-plus", label: "CTF Plus", icon: CreditCard, group: "PLUS" },
  { path: "/gopay-gpt-plus", label: "GoPay Plus", icon: CreditCard, group: "PLUS" },
  { path: "/proxies", label: "代理池", icon: Network, group: "工具" },
  { path: "/accounts/sms-pool", label: "号码黑名单", icon: ShieldAlert, group: "工具" },
  { path: "/microsoft-mailboxes", label: "微软邮箱", icon: Inbox, group: "工具" },
  { path: "/integrations", label: "功能适配", icon: Cable, group: "工具" },
  { path: "/proxy-nodes", label: "代理节点", icon: RouteIcon, group: "工具" },
  { path: "/settings", labelKey: "nav.settings", icon: SettingsIcon, group: "系统" },
];

function Sidebar({
  theme,
  toggleTheme,
  collapsed,
  compactViewport,
  setCollapsed,
}: {
  theme: string;
  toggleTheme: () => void;
  collapsed: boolean;
  compactViewport: boolean;
  setCollapsed: (v: boolean) => void;
}) {
  const { t, toggleLanguage } = useI18n();
  const location = useLocation();
  const isSettings = location.pathname === "/settings";
  const currentTab = new URLSearchParams(location.search).get("tab") || "general";

  const navLinkClass = (active: boolean) =>
    cn(
      "group flex items-center gap-3 rounded-lg px-3 py-2 text-[13px] font-medium transition-colors",
      active
        ? "bg-[var(--accent-soft)] text-[var(--text-primary)]"
        : "text-[var(--text-secondary)] hover:bg-[var(--bg-hover)] hover:text-[var(--text-primary)]",
      collapsed && "justify-center px-0",
    );

  const iconClass = (active: boolean) =>
    cn(
      "h-[18px] w-[18px] shrink-0",
      active
        ? "text-[var(--accent)]"
        : "text-[var(--text-muted)] group-hover:text-[var(--text-secondary)]",
    );

  return (
    <aside
      className={cn(
        "app-sidebar flex h-screen flex-col transition-[width] duration-200",
        collapsed ? "w-16" : "w-[220px]",
      )}
    >
      {/* Header */}
      <div
        className={cn(
          "flex h-12 shrink-0 items-center border-b border-[var(--border)] px-3",
          collapsed ? "justify-center" : "justify-between",
        )}
      >
        {!collapsed && (
          <div className="flex min-w-0 flex-1 items-center gap-3">
            <div className="flex h-7 w-7 shrink-0 items-center justify-center rounded-lg bg-[var(--accent)] text-[11px] font-bold text-white">
              Z
            </div>
            <div className="min-w-0">
              <span className="block truncate text-sm font-bold tracking-[-0.02em] text-[var(--text-primary)]">
                注册机zcj
              </span>
              <span className="block truncate text-[10px] font-medium uppercase tracking-[0.18em] text-[var(--text-muted)]">
                Multi-Platform Console
              </span>
            </div>
          </div>
        )}
        {collapsed && (
          <div className="flex h-7 w-7 items-center justify-center rounded-lg bg-[var(--accent)] text-[11px] font-bold text-white">
            Z
          </div>
        )}
      </div>

      {/* Nav */}
      <nav className="flex-1 overflow-y-auto px-2 py-3">
        {NAV_ITEMS.map(({ path, labelKey, label: itemLabel, icon: Icon, exact, group }, index) => {
          const active = exact
            ? location.pathname === path
            : location.pathname.startsWith(path);
          const label = itemLabel || (labelKey ? t(labelKey) : path);
          const showGroup = !collapsed && group && group !== NAV_ITEMS[index - 1]?.group;
          return (
            <div key={path}>
              {showGroup && (
                <div className="sidebar-section-label">
                  {group}
                </div>
              )}
              <NavLink
                to={path}
                end={exact}
                className={navLinkClass(active)}
                title={collapsed ? label : undefined}
              >
                <Icon className={iconClass(active)} />
                {!collapsed && <span>{label}</span>}
              </NavLink>
            </div>
          );
        })}
        {!collapsed && isSettings && (
          <div className="ml-[21px] mt-0.5 space-y-px border-l border-[var(--border)] pl-3">
            {SETTINGS_NAV_ITEMS.map((item) => {
              const active = currentTab === item.hash;
              return (
                <NavLink
                  key={item.hash}
                  to={`/settings?tab=${item.hash}`}
                  className={cn(
                    "relative block rounded-md px-2.5 py-1.5 text-[13px] transition-colors",
                    active
                      ? "text-[var(--accent)] font-medium bg-[var(--accent-soft)]"
                      : "text-[var(--text-muted)] hover:text-[var(--text-secondary)] hover:bg-[var(--bg-hover)]",
                  )}
                >
                  {t(item.labelKey)}
                </NavLink>
              );
            })}
          </div>
        )}
      </nav>

      {/* Footer */}
      <div className="shrink-0 border-t border-[var(--border)] px-2 py-1.5">
        <SchedulerHealth compact={collapsed} className={cn(collapsed ? "mx-auto mb-2" : "mb-2 w-full")} />
        <div className="flex items-center gap-1">
        <button
          onClick={toggleTheme}
          className={cn(
            "flex items-center justify-center rounded-md p-2 text-[var(--text-muted)] transition-colors hover:bg-[var(--bg-hover)] hover:text-[var(--text-secondary)]",
          )}
          title={
            theme === "light"
              ? t("sidebar.theme.toDark")
              : theme === "dark"
                ? t("sidebar.theme.toLight")
                : t("sidebar.theme.followSystem")
          }
        >
          {theme === "light" ? (
            <Moon className="h-4 w-4" />
          ) : theme === "system" ? (
            <Monitor className="h-4 w-4" />
          ) : (
            <Sun className="h-4 w-4" />
          )}
        </button>
        {!collapsed && (
          <span className="flex-1 text-[12px] text-[var(--text-muted)]">
            {theme === "light"
              ? t("sidebar.theme.light")
              : theme === "dark"
                ? t("sidebar.theme.dark")
                : t("sidebar.theme.system")}
          </span>
        )}
        <button
          onClick={toggleLanguage}
          className="flex items-center justify-center rounded-md p-2 text-[var(--text-muted)] transition-colors hover:bg-[var(--bg-hover)] hover:text-[var(--text-secondary)]"
          title={t("sidebar.languageToggle")}
        >
          <Languages className="h-4 w-4" />
        </button>
        {!compactViewport && (
          <button
            onClick={() => setCollapsed(!collapsed)}
            className="flex items-center justify-center rounded-md p-2 text-[var(--text-muted)] transition-colors hover:bg-[var(--bg-hover)] hover:text-[var(--text-secondary)]"
            title={collapsed ? t("sidebar.expand") : t("sidebar.collapse")}
          >
            {collapsed ? (
              <PanelLeft className="h-4 w-4" />
            ) : (
              <PanelLeftClose className="h-4 w-4" />
            )}
          </button>
        )}
        </div>
      </div>
    </aside>
  );
}

/* ------------------------------------------------------------------ */
/*  Shell                                                              */
/* ------------------------------------------------------------------ */

function Shell({
  theme,
  setTheme,
  toggleTheme,
}: {
  theme: string;
  setTheme: (t: string) => void;
  toggleTheme: () => void;
}) {
  const [collapsed, setCollapsed] = useState(
    () => localStorage.getItem("sidebar-collapsed") === "true",
  );
  const [compactViewport, setCompactViewport] = useState(
    () => window.matchMedia("(max-width: 720px)").matches,
  );

  useEffect(() => {
    localStorage.setItem("sidebar-collapsed", String(collapsed));
  }, [collapsed]);

  useEffect(() => {
    const media = window.matchMedia("(max-width: 720px)");
    const sync = () => setCompactViewport(media.matches);
    sync();
    media.addEventListener("change", sync);
    return () => media.removeEventListener("change", sync);
  }, []);

  return (
    <div className="flex h-screen overflow-hidden bg-[var(--bg-base)]">
      <Sidebar
        theme={theme}
        toggleTheme={toggleTheme}
        collapsed={collapsed || compactViewport}
        compactViewport={compactViewport}
        setCollapsed={setCollapsed}
      />
      <main className="flex-1 overflow-y-auto">
        <div className="mx-auto w-full max-w-[1600px] px-4 py-5 sm:px-6 lg:px-8">
          <UpdateBanner />
          <Suspense fallback={<RouteFallback />}>
            <Routes>
              <Route path="/" element={<Dashboard />} />
              <Route path="/accounts" element={<Accounts />} />
              <Route path="/accounts/sms-pool" element={<SmsPoolBlacklist />} />
              <Route path="/accounts/:platform" element={<Accounts />} />
              <Route path="/register" element={<Register />} />
              <Route path="/register-center" element={<RegisterCenter />} />
              <Route path="/register-other" element={<OtherRegisterWorkbench />} />
              <Route path="/tasks" element={<Tasks />} />
              <Route path="/ctf-gpt-plus" element={<CtfGptPlus />} />
              <Route path="/gopay-gpt-plus" element={<GoPayGptPlus />} />
              <Route path="/plus-manager" element={<PlusManager />} />
              <Route path="/history" element={<TaskHistory />} />
              <Route path="/proxies" element={<Proxies />} />
              <Route path="/microsoft-mailboxes" element={<MicrosoftMailboxes />} />
              <Route path="/integrations" element={<Integrations />} />
              <Route path="/sms-settings" element={<SmsSettings />} />
              <Route path="/proxy-nodes" element={<ProxyPoolSettings />} />
              <Route
                path="/settings"
                element={<SettingsPage theme={theme} setTheme={setTheme} />}
              />
            </Routes>
          </Suspense>
        </div>
      </main>
      <ActiveTaskDock />
    </div>
  );
}

/* ------------------------------------------------------------------ */
/*  Login                                                              */
/* ------------------------------------------------------------------ */

function LoginScreen({ onLogin }: { onLogin: (token: string) => void }) {
  const { t } = useI18n();
  const [pw, setPw] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setLoading(true);
    setError("");
    try {
      const res = await fetch(API + "/auth/login", {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ password: pw }),
      });
      const data = await res.json();
      if (data.ok) {
        onLogin("");
      } else {
        setError(data.error || t("login.passwordError"));
      }
    } catch {
      setError(t("login.requestFailed"));
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="sub2-login-screen">
      <form
        onSubmit={submit}
        className="sub2-login-card space-y-5 p-7"
      >
        <div className="flex items-center gap-3">
          <div className="sub2-brand-mark h-10 w-10 text-sm font-black">
            Z
          </div>
          <div>
            <h1 className="text-lg font-bold tracking-[-0.02em] text-[var(--text-primary)]">
              注册机zcj
            </h1>
            <p className="text-[11px] uppercase tracking-[0.18em] text-[var(--text-muted)]">Multi-Platform Console</p>
          </div>
        </div>
        <p className="text-sm text-[var(--text-muted)]">{t("login.prompt")}</p>
        <input
          type="password"
          value={pw}
          onChange={(e) => setPw(e.target.value)}
          placeholder={t("login.passwordPlaceholder")}
          autoFocus
          className="control-surface w-full"
        />
        {error && <p className="text-xs text-red-500">{error}</p>}
        <button
          type="submit"
          disabled={loading || !pw}
          className="w-full rounded-xl bg-gradient-to-r from-[var(--accent)] to-[var(--accent-strong)] px-4 py-2.5 text-sm font-semibold text-white shadow-[0_12px_28px_rgba(var(--accent-rgb),0.24)] transition-all hover:shadow-[0_16px_36px_rgba(var(--accent-rgb),0.30)] disabled:opacity-50"
        >
          {loading ? t("login.checking") : t("login.submit")}
        </button>
      </form>
    </div>
  );
}

/* ------------------------------------------------------------------ */
/*  App root                                                           */
/* ------------------------------------------------------------------ */

function AppContent() {
  const { t } = useI18n();
  const [theme, setTheme] = useState(
    () => localStorage.getItem("theme") || "dark",
  );
  const [authState, setAuthState] = useState<
    "loading" | "open" | "locked" | "authed"
  >("loading");

  useEffect(() => {
    const applyTheme = () => {
      let effective = theme;
      if (theme === "system") {
        effective = window.matchMedia("(prefers-color-scheme: light)").matches
          ? "light"
          : "dark";
      }
      document.documentElement.classList.toggle("light", effective === "light");
    };
    applyTheme();
    localStorage.setItem("theme", theme);
    const mq = window.matchMedia("(prefers-color-scheme: light)");
    const handler = () => {
      if (theme === "system") applyTheme();
    };
    mq.addEventListener("change", handler);
    return () => mq.removeEventListener("change", handler);
  }, [theme]);

  useEffect(() => {
    fetch(API + "/auth/check")
      .then((r) => r.json())
      .then((data) => {
        if (data.authenticated) setAuthState("authed");
        else setAuthState("locked");
      })
      .catch(() => setAuthState("open"));
  }, []);

  const toggleTheme = () =>
    setTheme((c) =>
      c === "dark" ? "light" : c === "light" ? "system" : "dark",
    );

  if (authState === "loading") {
    return (
      <div className="flex h-screen items-center justify-center bg-[var(--bg-base)] text-[var(--text-muted)] text-sm">
        {t("app.loading")}
      </div>
    );
  }
  if (authState === "locked") {
    return <LoginScreen onLogin={() => setAuthState("authed")} />;
  }

  return (
    <BrowserRouter>
      <Shell theme={theme} setTheme={setTheme} toggleTheme={toggleTheme} />
    </BrowserRouter>
  );
}

export default function App() {
  return (
    <I18nProvider>
      <AppContent />
    </I18nProvider>
  );
}

