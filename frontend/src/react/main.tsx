import { createRoot } from "react-dom/client";
import { flushSync } from "react-dom";
import "./app.css";
import { HomeChat } from "./home/HomeChat";
import { Sidebar } from "./shell/Sidebar";
import { AgentProvider } from "./agent/AgentProvider";
import { AgentBall } from "./agent/AgentBall";
import { AgentWindow } from "./agent/AgentWindow";
import { AgentPage } from "./agent/AgentPage";

function ReactRuntimeBridge() {
    return <span hidden data-framework="react" aria-hidden="true" />;
}

const mountNode = document.getElementById("react-runtime-root");
if (mountNode) {
    createRoot(mountNode).render(<ReactRuntimeBridge />);
}

const sidebarMountNode = document.getElementById("react-sidebar-root");
if (sidebarMountNode) {
    const root = createRoot(sidebarMountNode);
    flushSync(() => {
        root.render(<Sidebar />);
    });
}

const homeMountNode = document.getElementById("react-home-root");
if (homeMountNode) {
    const root = createRoot(homeMountNode);
    flushSync(() => {
        root.render(<HomeChat />);
    });
}

// 悬浮球、悬浮窗口与全屏 Agent 页共享同一个 Provider，避免重复的会话状态与请求。
const agentPortalNode = document.getElementById("react-agent-portalroot");
if (agentPortalNode) {
    const root = createRoot(agentPortalNode);
    flushSync(() => {
        root.render(
            <AgentProvider>
                <AgentBall />
                <AgentWindow />
                <AgentPage />
            </AgentProvider>,
        );
    });
}
