import {
    useCallback,
    useEffect,
    useLayoutEffect,
    useRef,
    useState,
    type MouseEvent as ReactMouseEvent,
    type PointerEvent as ReactPointerEvent,
} from "react";
import { useAgent } from "./AgentProvider";

const BALL_SIZE = 56;
const EDGE_MARGIN = 8;
const DEFAULT_OFFSET = 24;
const DRAG_THRESHOLD = 5;
const CLICK_MAX_DURATION = 500;
const MENU_GAP = 8;
const MENU_MARGIN = 8;
const POSITION_KEY_PREFIX = "trpg.agent.ball.position";

interface BallPosition {
    x: number;
    y: number;
}

interface DragState {
    pointerId: number;
    startX: number;
    startY: number;
    originX: number;
    originY: number;
    moved: boolean;
    startedAt: number;
}

interface MenuPlacement {
    left: number;
    top: number;
}

function viewportSize(): { width: number; height: number } {
    return {
        width: window.innerWidth || 1280,
        height: window.innerHeight || 800,
    };
}

function clamp(value: number, minimum: number, maximum: number): number {
    return Math.max(minimum, Math.min(maximum, value));
}

function clampPosition(x: number, y: number): BallPosition {
    const { width, height } = viewportSize();
    return {
        x: clamp(x, EDGE_MARGIN, Math.max(EDGE_MARGIN, width - BALL_SIZE - EDGE_MARGIN)),
        y: clamp(y, EDGE_MARGIN, Math.max(EDGE_MARGIN, height - BALL_SIZE - EDGE_MARGIN)),
    };
}

function defaultPosition(): BallPosition {
    const { width, height } = viewportSize();
    return clampPosition(width - BALL_SIZE - DEFAULT_OFFSET, height - BALL_SIZE - DEFAULT_OFFSET);
}

/** 按登录用户分别记住悬浮球位置；未登录时使用公共键。 */
function positionStorageKey(): string {
    const user = (window as unknown as { currentUser?: { user_id?: number | string } | null }).currentUser;
    const userId = user?.user_id;
    return userId === undefined || userId === null ? POSITION_KEY_PREFIX : `${POSITION_KEY_PREFIX}.${String(userId)}`;
}

function loadPosition(key: string): BallPosition {
    const fallback = defaultPosition();
    try {
        const raw = window.localStorage.getItem(key);
        if (!raw) return fallback;
        const data = JSON.parse(raw) as Partial<BallPosition>;
        if (![data.x, data.y].every((value) => typeof value === "number" && Number.isFinite(value))) {
            return fallback;
        }
        return clampPosition(Number(data.x), Number(data.y));
    } catch {
        return fallback;
    }
}

function persistPosition(key: string, position: BallPosition): void {
    try {
        window.localStorage.setItem(key, JSON.stringify(position));
    } catch {
        // 隐私模式下 localStorage 可能不可写，只影响偏好持久化。
    }
}

/** 松手后吸附到最近的左右边缘，纵向位置保持不变。 */
function snapToSide(position: BallPosition): BallPosition {
    const { width } = viewportSize();
    const centerX = position.x + BALL_SIZE / 2;
    const x = centerX < width / 2 ? EDGE_MARGIN : width - BALL_SIZE - EDGE_MARGIN;
    return clampPosition(x, position.y);
}

export function AgentBall() {
    const { canUse, ballVisible, unread, windowOpen, toggleWindow, openPage, setBallVisible } = useAgent();
    const [menuOpen, setMenuOpen] = useState(false);
    const [position, setPosition] = useState<BallPosition>(defaultPosition);
    const [dragging, setDragging] = useState(false);
    const [menuPlacement, setMenuPlacement] = useState<MenuPlacement | null>(null);
    const menuRef = useRef<HTMLDivElement | null>(null);
    const dragRef = useRef<DragState | null>(null);
    const suppressClickRef = useRef(false);
    const positionRef = useRef(position);
    const loadedKeyRef = useRef<string | null>(null);
    positionRef.current = position;

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

    // 登录状态就绪后（或切换用户后）加载对应用户记住的位置；在绘制前完成，避免首帧跳动。
    useLayoutEffect(() => {
        if (!canUse) return;
        const key = positionStorageKey();
        if (loadedKeyRef.current === key) return;
        loadedKeyRef.current = key;
        setPosition(loadPosition(key));
    }, [canUse]);

    // 窗口尺寸变化时把悬浮球收回可视区域，并把新位置写回偏好。
    useEffect(() => {
        const handleResize = (): void => {
            const current = positionRef.current;
            const next = clampPosition(current.x, current.y);
            if (next.x === current.x && next.y === current.y) return;
            setPosition(next);
            const key = loadedKeyRef.current;
            if (key) persistPosition(key, next);
        };
        window.addEventListener("resize", handleResize);
        return () => window.removeEventListener("resize", handleResize);
    }, []);

    // 拖动时禁止页面文本选中，抬起后立即恢复。
    useLayoutEffect(() => {
        if (!dragging) return;
        const root = document.documentElement;
        root.classList.add("agent-ball-dragging");
        return () => root.classList.remove("agent-ball-dragging");
    }, [dragging]);

    // 菜单跟随悬浮球：优先显示在上方，空间不足时翻到下方，并限制在视口内。
    useLayoutEffect(() => {
        if (!menuOpen) {
            setMenuPlacement(null);
            return;
        }
        const menu = menuRef.current;
        if (!menu) return;
        const width = menu.offsetWidth;
        const height = menu.offsetHeight;
        const { width: viewportWidth, height: viewportHeight } = viewportSize();
        const left = clamp(
            position.x + BALL_SIZE - width,
            MENU_MARGIN,
            Math.max(MENU_MARGIN, viewportWidth - width - MENU_MARGIN),
        );
        let top = position.y - height - MENU_GAP;
        if (top < MENU_MARGIN) {
            top = position.y + BALL_SIZE + MENU_GAP;
        }
        top = clamp(top, MENU_MARGIN, Math.max(MENU_MARGIN, viewportHeight - height - MENU_MARGIN));
        setMenuPlacement({ left: left - position.x, top: top - position.y });
    }, [menuOpen, position]);

    const handlePointerDown = useCallback((event: ReactPointerEvent<HTMLButtonElement>) => {
        if (event.button !== 0) return;
        const target = event.currentTarget;
        const rect = target.getBoundingClientRect();
        dragRef.current = {
            pointerId: event.pointerId,
            startX: event.clientX,
            startY: event.clientY,
            originX: rect.left,
            originY: rect.top,
            moved: false,
            startedAt: performance.now(),
        };
        suppressClickRef.current = false;
        try {
            target.setPointerCapture(event.pointerId);
        } catch {
            // 指针捕获失败时仍能通过按钮自身的事件完成短距离拖动。
        }
    }, []);

    const handlePointerMove = useCallback((event: ReactPointerEvent<HTMLButtonElement>) => {
        const drag = dragRef.current;
        if (!drag || drag.pointerId !== event.pointerId) return;
        const deltaX = event.clientX - drag.startX;
        const deltaY = event.clientY - drag.startY;
        if (!drag.moved) {
            // 超过位移阈值才进入拖拽，避免轻点/轻微抖动被当成拖动。
            if (Math.hypot(deltaX, deltaY) < DRAG_THRESHOLD) return;
            drag.moved = true;
            setMenuOpen(false);
            setDragging(true);
        }
        const next = clampPosition(drag.originX + deltaX, drag.originY + deltaY);
        positionRef.current = next;
        setPosition(next);
    }, []);

    const finishDrag = useCallback((pointerId: number, currentTarget: HTMLButtonElement, cancelled: boolean) => {
        const drag = dragRef.current;
        if (!drag || drag.pointerId !== pointerId) return;
        dragRef.current = null;
        setDragging(false);
        try {
            if (currentTarget.hasPointerCapture(pointerId)) {
                currentTarget.releasePointerCapture(pointerId);
            }
        } catch {
            // 捕获可能已被浏览器回收，忽略即可。
        }
        if (cancelled) {
            suppressClickRef.current = true;
            return;
        }
        if (drag.moved) {
            // 拖拽结束不算点击；松手后吸附到最近的左右边缘。
            suppressClickRef.current = true;
            const next = snapToSide(positionRef.current);
            positionRef.current = next;
            setPosition(next);
            const key = loadedKeyRef.current;
            if (key) persistPosition(key, next);
            return;
        }
        if (performance.now() - drag.startedAt > CLICK_MAX_DURATION) {
            // 长按不移动也不视为点击，避免按住悬浮球时误触展开/收起。
            suppressClickRef.current = true;
        }
    }, []);

    const handlePointerUp = useCallback(
        (event: ReactPointerEvent<HTMLButtonElement>) => {
            finishDrag(event.pointerId, event.currentTarget, false);
        },
        [finishDrag],
    );

    const handlePointerCancel = useCallback(
        (event: ReactPointerEvent<HTMLButtonElement>) => {
            finishDrag(event.pointerId, event.currentTarget, true);
        },
        [finishDrag],
    );

    const handleClick = useCallback((event: ReactMouseEvent<HTMLButtonElement>) => {
        const keyboardActivation = event.detail === 0;
        if (!keyboardActivation && suppressClickRef.current) {
            suppressClickRef.current = false;
            return;
        }
        suppressClickRef.current = false;
        toggleWindow();
    }, [toggleWindow]);

    if (!canUse || !ballVisible) return null;

    return (
        <div
            className={`agent-ball-layer${dragging ? " agent-ball-layer-dragging" : ""}`}
            style={{ transform: `translate3d(${position.x}px, ${position.y}px, 0)` }}
        >
            {menuOpen ? (
                <div
                    ref={menuRef}
                    className="agent-ball-menu"
                    role="menu"
                    style={
                        menuPlacement
                            ? { left: menuPlacement.left, top: menuPlacement.top }
                            : { visibility: "hidden" }
                    }
                >
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
                title={windowOpen ? "关闭 Agent 窗口" : "拖动可移动；单击打开/关闭 Agent 窗口"}
                onPointerDown={handlePointerDown}
                onPointerMove={handlePointerMove}
                onPointerUp={handlePointerUp}
                onPointerCancel={handlePointerCancel}
                onClick={handleClick}
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
