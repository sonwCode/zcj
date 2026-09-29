import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { 
  Play, 
  CheckCircle, 
  XCircle, 
  AlertTriangle,
  Globe,
  Mail,
  Smartphone,
  Shield,
  Route as RouteIcon,
  Settings2
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { apiFetch } from "@/lib/utils";

type Platform = {
  name: string;
  display_name: string;
  description?: string;
  verification?: string;
};

type ProviderSetting = {
  provider_key: string;
  display_name?: string;
  enabled?: boolean;
  configured?: boolean;
  status_message?: string;
};

type ProxyStatus = {
  manual_pool: { total: number; active: number; available: boolean };
  mihomo: { configured: boolean; available: boolean; nodes: number };
};

type Options = {
  platforms: Platform[];
  mailbox_settings: ProviderSetting[];
  captcha_settings: ProviderSetting[];
  sms_settings: ProviderSetting[];
  proxy_status: ProxyStatus;
};

export default function RegisterCenter() {
  const navigate = useNavigate();
  const [options, setOptions] = useState<Options | null>(null);
  const [selectedPlatform, setSelectedPlatform] = useState<string>("");
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    loadOptions();
  }, []);

  const loadOptions = async () => {
    try {
      const data = await apiFetch("/api/registration/options") as Options;
      setOptions(data);
      if (data.platforms.length > 0) {
        setSelectedPlatform(data.platforms[0].name);
      }
    } catch (error) {
      console.error("加载配置失败:", error);
    } finally {
      setLoading(false);
    }
  };

  const getStatusIcon = (configured: boolean, available?: boolean) => {
    if (configured && available !== false) {
      return <CheckCircle className="h-4 w-4 text-[var(--tone-success)]" />;
    }
    if (configured) {
      return <AlertTriangle className="h-4 w-4 text-[var(--tone-warning)]" />;
    }
    return <XCircle className="h-4 w-4 text-[var(--tone-danger)]" />;
  };

  const handleStart = () => {
    if (selectedPlatform === "chatgpt" || selectedPlatform === "chatgpt_free") {
      navigate("/register");
    } else {
      navigate("/register-other");
    }
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="text-[var(--text-muted)]">加载中...</div>
      </div>
    );
  }

  const selectedPlatformData = options?.platforms.find(p => p.name === selectedPlatform);
  const mailboxProvider = options?.mailbox_settings.find(s => s.enabled);
  const smsProvider = options?.sms_settings.find(s => s.enabled);
  const captchaProvider = options?.captcha_settings.find(s => s.enabled);

  return (
    <div className="space-y-6">
      {/* 页面标题 */}
      <div>
        <h1 className="text-xl font-semibold text-[var(--text-primary)]">注册中心</h1>
        <p className="mt-1 text-sm text-[var(--text-muted)]">
          选择平台和配置参数，一键启动注册任务
        </p>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        {/* 左侧：平台选择 */}
        <Card className="p-6">
          <div className="space-y-4">
            <div>
              <label className="block text-sm font-medium text-[var(--text-primary)] mb-2">
                选择平台
              </label>
              <select
                value={selectedPlatform}
                onChange={(e) => setSelectedPlatform(e.target.value)}
                className="w-full px-3 py-2 bg-[var(--bg-input)] border border-[var(--border)] rounded-md text-[var(--text-primary)] focus:outline-none focus:ring-2 focus:ring-[var(--accent)]"
              >
                {options?.platforms.map((p) => (
                  <option key={p.name} value={p.name}>
                    {p.display_name}
                  </option>
                ))}
              </select>
            </div>

            {selectedPlatformData && (
              <div className="p-4 bg-[var(--bg-card)] border border-[var(--border)] rounded-md">
                <div className="text-sm font-medium text-[var(--text-primary)] mb-2">
                  {selectedPlatformData.display_name}
                </div>
                {selectedPlatformData.description && (
                  <div className="text-sm text-[var(--text-muted)]">
                    {selectedPlatformData.description}
                  </div>
                )}
                {selectedPlatformData.verification && (
                  <div className="mt-2 flex items-center gap-2 text-sm text-[var(--text-muted)]">
                    <Shield className="h-4 w-4" />
                    验证方式: {selectedPlatformData.verification}
                  </div>
                )}
              </div>
            )}

            <div>
              <label className="block text-sm font-medium text-[var(--text-primary)] mb-2">
                批量数量
              </label>
              <input
                type="number"
                defaultValue={1}
                min={1}
                max={100}
                className="w-full px-3 py-2 bg-[var(--bg-input)] border border-[var(--border)] rounded-md text-[var(--text-primary)] focus:outline-none focus:ring-2 focus:ring-[var(--accent)]"
              />
            </div>

            <div>
              <label className="block text-sm font-medium text-[var(--text-primary)] mb-2">
                执行器
              </label>
              <select className="w-full px-3 py-2 bg-[var(--bg-input)] border border-[var(--border)] rounded-md text-[var(--text-primary)] focus:outline-none focus:ring-2 focus:ring-[var(--accent)]">
                <option value="protocol">纯协议（速度优先）</option>
                <option value="browser">浏览器自动化</option>
              </select>
            </div>
          </div>
        </Card>

        {/* 右侧：启动检查 */}
        <Card className="p-6">
          <div className="space-y-4">
            <div className="flex items-center gap-2 text-sm font-medium text-[var(--text-primary)] mb-4">
              <Settings2 className="h-4 w-4" />
              启动检查
            </div>

            {/* 平台 */}
            <div className="flex items-center justify-between py-3 border-b border-[var(--border)]">
              <div className="flex items-center gap-3">
                <Globe className="h-4 w-4 text-[var(--text-muted)]" />
                <div>
                  <div className="text-sm font-medium text-[var(--text-primary)]">平台</div>
                  <div className="text-sm text-[var(--text-muted)]">
                    {selectedPlatformData?.display_name || "-"}
                  </div>
                </div>
              </div>
              {getStatusIcon(!!selectedPlatform)}
            </div>

            {/* 邮箱服务 */}
            <div className="flex items-center justify-between py-3 border-b border-[var(--border)]">
              <div className="flex items-center gap-3">
                <Mail className="h-4 w-4 text-[var(--text-muted)]" />
                <div>
                  <div className="text-sm font-medium text-[var(--text-primary)]">邮箱服务</div>
                  <div className="text-sm text-[var(--text-muted)]">
                    {mailboxProvider?.display_name || "未配置"}
                  </div>
                </div>
              </div>
              {getStatusIcon(!!mailboxProvider?.configured, true)}
            </div>

            {/* 接码服务 */}
            <div className="flex items-center justify-between py-3 border-b border-[var(--border)]">
              <div className="flex items-center gap-3">
                <Smartphone className="h-4 w-4 text-[var(--text-muted)]" />
                <div>
                  <div className="text-sm font-medium text-[var(--text-primary)]">接码服务</div>
                  <div className="text-sm text-[var(--text-muted)]">
                    {smsProvider?.display_name || "未配置"}
                  </div>
                </div>
              </div>
              {getStatusIcon(!!smsProvider?.configured, true)}
            </div>

            {/* 验证码服务 */}
            <div className="flex items-center justify-between py-3 border-b border-[var(--border)]">
              <div className="flex items-center gap-3">
                <Shield className="h-4 w-4 text-[var(--text-muted)]" />
                <div>
                  <div className="text-sm font-medium text-[var(--text-primary)]">验证码</div>
                  <div className="text-sm text-[var(--text-muted)]">
                    {captchaProvider?.display_name || "未配置"}
                  </div>
                </div>
              </div>
              {getStatusIcon(!!captchaProvider?.configured, true)}
            </div>

            {/* 代理策略 */}
            <div className="flex items-center justify-between py-3 border-b border-[var(--border)]">
              <div className="flex items-center gap-3">
                <RouteIcon className="h-4 w-4 text-[var(--text-muted)]" />
                <div>
                  <div className="text-sm font-medium text-[var(--text-primary)]">代理策略</div>
                  <div className="text-sm text-[var(--text-muted)]">
                    {options?.proxy_status.mihomo.configured
                      ? `自动: Mihomo → 手动池 → 直连`
                      : options?.proxy_status.manual_pool.available
                      ? "手动代理池"
                      : "直连"}
                  </div>
                </div>
              </div>
              {getStatusIcon(
                options?.proxy_status.mihomo.configured || options?.proxy_status.manual_pool.available || false,
                true
              )}
            </div>

            {/* 启动按钮 */}
            <div className="pt-4">
              <Button
                onClick={handleStart}
                className="w-full"
                disabled={!selectedPlatform}
              >
                <Play className="mr-2 h-4 w-4" />
                启动注册任务
              </Button>
            </div>
          </div>
        </Card>
      </div>
    </div>
  );
}
