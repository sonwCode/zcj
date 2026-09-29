import { useState } from "react";
import Proxies from "./Proxies";
import ProxyPoolSettings from "./ProxyPoolSettings";

export default function ProxyManagement() {
  const [activeTab, setActiveTab] = useState<"subscriptions" | "nodes">("subscriptions");

  return (
    <div className="space-y-6">
      {/* Tab 导航 */}
      <div className="tab-nav">
        <button
          className={`tab-item ${activeTab === "subscriptions" ? "active" : ""}`}
          onClick={() => setActiveTab("subscriptions")}
        >
          代理 URL 管理
        </button>
        <button
          className={`tab-item ${activeTab === "nodes" ? "active" : ""}`}
          onClick={() => setActiveTab("nodes")}
        >
          全部节点管理
        </button>
      </div>

      {/* Tab 内容 */}
      <div>
        {activeTab === "subscriptions" && <Proxies />}
        {activeTab === "nodes" && <ProxyPoolSettings />}
      </div>
    </div>
  );
}
