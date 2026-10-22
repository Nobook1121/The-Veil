import { useCallback, useEffect, useRef, useState } from "react";
import { useAgent } from "./AgentProvider";
import { AgentChat } from "./AgentChat";

interface Rect {
    x: number;
    y: number;
    w: number;
    h: number;
}

interface DragState {
    mode: "move" | "resize";
    startX: number;
    startY: number;
    rect: Rect;
}

const STORAGE_KEY = "trpg.agent.window";
const MIN_WIDTH = 360;
const MIN_HEIGHT = 480;

function clamp(value: number, minimum: number, maximum: number): number {
    return Math.max(minimum, Math.min(maximum, value));
}

function defaultRect(): Rect {
    const viewportWidth = window.innerWidth || 1280;
    const viewportHeight = window.innerHeight || 800;
    const width = Math.min(420, viewportWidth - 32);
    const height = Math.min(620, viewportHeight - 120);
    return {
        x: viewportWidth - width - 24,
        y: viewportHeight - height - 24,
        w: width,
        h: height,
    };
}

function loadRect(): Rect {
    const fallback = defaultRect();
    try {
        const raw = window.localStorage.getItem(STORAGE_KEY);
        if (!raw) return fallback;
        const data = JSON.parse(raw) as Partial<Rect>;
        if (![data.x, data.y, data.w, data.h].every((value) => typeof value === "number" && Number.isFinite(value))) {
            return fallback;
        }
        return {
            x: clamp(Number(data.x), 0, Math.max(0, window.innerWidth - MIN_WIDTH)),
            y: clamp(Number(data.y), 0, Math.max(0, window.innerHeight - MIN_HEIGHT)),
            w: clamp(Number(data.w), MIN_WIDTH, window.innerWidth),
            h: clamp(Number(data.h), MIN_HEIGHT, window.innerHeight),
        };
    } catch {
        return fallback;
    }
}

function currentRoomLabel(): string {
    const room = (window as unknown as { currentRoom?: { name?: string; scenario_title?: string } | null }).currentRoom;
    return room?.scenario_title || room?.name || "通用";
}

export function AgentWindow() {
    const { windowOpen, canUse, closeWindow, openPage, newConversation, activeConversation } = useAgent();
    const [rect, setRect] = useState<Rect>(() => loadRect());
    const [drag, setDrag] = useState<DragState | null>(null);
    const rectRef = useRef(rect);
    rectRef.current = rect;

    const persist = useCallback((value: Rect) => {
        try {
            window.localStorage.setItem(STORAGE_KEY, JSON.stringify(value));
        } catch {
            // 偏好无法持久化不影响本次会话。
        }
    }, []);

    useEffect(() => {
        if (!windowOpen) return;
        const onKeyDown = (event: globalThis.KeyboardEvent): void => {
            if (event.key === "Escape") closeWindow();
        };
        window.addEventListener("keydown", onKeyDown);
        return () => window.removeEventListener("keydown", onKeyDown);
    }, [closeWindow, windowOpen]);

    useEffect(() => {
        if (!drag) return;
        const onMove = (event: PointerEvent): void => {
            const deltaX = event.clientX - drag.startX;
            const deltaY = event.clientY - drag.startY;
            const viewportWidth = window.innerWidth;
            const viewportHeight = window.innerHeight;
            if (drag.mode === "move") {
                setRect({
                    ...drag.rect,
                    x: clamp(drag.rect.x + deltaX, 0, Math.max(0, viewportWidth - drag.rect.w)),
                    y: clamp(drag.rect.y + deltaY, 0, Math.max(0, viewportHeight - drag.rect.h)),
                });
            } else {
                setRect({
                    ...drag.rect,
                    w: clamp(drag.rect.w + deltaX, MIN_WIDTH, Math.max(MIN_WIDTH, viewportWidth - drag.rect.x)),
                    h: clamp(drag.rect.h + deltaY, MIN_HEIGHT, Math.max(MIN_HEIGHT, viewportHeight - drag.rect.y)),
                });
            }
        };
        const onUp = (): void => {
            setDrag(null);
            persist(rectRef.current);
        };
        window.addEventListener("pointermove", onMove);
        window.addEventListener("pointerup", onUp);
        return () => {
            window.removeEventListener("pointermove", onMove);
            window.removeEventListener("pointerup", onUp);
        };
    }, [drag, persist]);

    if (!windowOpen || !canUse) return null;

    return (
        <section
            className="agent-window"
            style={{ left: rect.x, top: rect.y, width: rect.w, height: rect.h }}
            aria-label="网站 Agent 窗口"
        >
            <header
                className="agent-window-header"
                onPointerDown={(event) => {
                    if ((event.target as HTMLElement).closest("button")) return;
                    event.preventDefault();
                    setDrag({ mode: "move", startX: event.clientX, startY: event.clientY, rect });
                }}
            >
                <div className="agent-window-title">
                    <i className="fa fa-magic" aria-hidden="true" />
                    <span>网站 Agent</span>
                    <span className="agent-window-context">{currentRoomLabel()}</span>
                </div>
                <div className="agent-window-actions">
                    <button
                        type="button"
                        title="新建会话"
                        aria-label="新建会话"
                        onClick={() => void newConversation()}
                        disabled={Boolean(activeConversation && activeConversation.id === "")}
                    >
                        <i className="fa fa-plus" aria-hidden="true" />
                    </button>
                    <button type="button" title="在 Agent 页打开" aria-label="在 Agent 页打开" onClick={openPage}>
                        <i className="fa fa-expand" aria-hidden="true" />
                    </button>
                    <button type="button" title="关闭窗口" aria-label="关闭窗口" onClick={closeWindow}>
                        <i className="fa fa-times" aria-hidden="true" />
                    </button>
                </div>
            </header>
            <AgentChat variant="window" />
            <button
                type="button"
                className="agent-window-resize"
                title="调整窗口大小"
                aria-label="调整窗口大小"
                onPointerDown={(event) => {
                    event.preventDefault();
                    event.stopPropagation();
                    setDrag({ mode: "resize", startX: event.clientX, startY: event.clientY, rect });
                }}
            />
        </section>
    );
}