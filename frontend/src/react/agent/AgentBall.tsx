import { useEffect, useState } from "react";
import { useAgent } from "./AgentProvider";

export function AgentBall() {
    const { canUse, ballVisible, unread, windowOpen, toggleWindow, openPage, setBallVisible } = useAgent();
    const [menuOpen, setMenuOpen] = useState(false);

    useEffect(() => {
        if (!menuOpen) return;
        const close = (): void => setMenuOpen(false);
        document.addEventListener("click", close);
        document.addEventListener("keydown", close);
        return () => {
            document.removeEventListener("click", close);
            document.removeEventListener("keydown", close);
        };
    }, [menuOpen]);

    if (!canUse || !ballVisible) return null;

    return (
        <div className="agent-ball-layer">
            {menuOpen ? (
                <div className="agent-ball-menu" role="menu">
                    <button type="button" role="menuitem" onClick={openPage}>
                        <i className="fa fa-expand" aria-hidden="true" /> 打开 Agent 页
                    </button>
                    <button
                        type="button"
                        role="menuitem"
                        onClick={() => {
                            setMenuOpen(false);
                            toggleWindow();
                        }}
                    >
                        <i className="fa fa-comments-o" aria-hidden="true" /> {windowOpen ? "收起窗口" : "打开窗口"}
                    </button>
                    <button
                        type="button"
                        role="menuitem"
                        onClick={() => {
                            setMenuOpen(false);
                            setBallVisible(false);
                        }}
                    >
                        <i className="fa fa-eye-slash" aria-hidden="true" /> 隐藏悬浮球
                    </button>
                </div>
            ) : null}
            <button
                type="button"
                className={`agent-ball${windowOpen ? " agent-ball-open" : ""}`}
                aria-label={windowOpen ? "关闭 Agent 窗口" : "打开 Agent 窗口"}
                title={windowOpen ? "关闭 Agent 窗口" : "打开 Agent 窗口"}
                onClick={toggleWindow}
                onContextMenu={(event) => {
                    event.preventDefault();
                    setMenuOpen((open) => !open);
                }}
            >
                <i className={`fa ${windowOpen ? "fa-times" : "fa-magic"}`} aria-hidden="true" />
                {unread && !windowOpen ? <span className="agent-ball-dot" aria-label="有未读回复" /> : null}
            </button>
        </div>
    );
}