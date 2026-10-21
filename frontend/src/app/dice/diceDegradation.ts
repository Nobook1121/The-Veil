/**
 * 逐层降级：3D → 简易 → 纯文本。
 * 全局性失败（bundle / WebGL）每个会话只尝试一次并记住降级；
 * 单次失败也记录，但由服务端做 30 条/分钟限流。
 */
const DiceDegradation = (() => {
    const sessionLevels = new Map<string, DiceAnimationLevel>();
    const reportedSession = new Set<string>();

    function webglAvailable(): boolean {
        try {
            const canvas = document.createElement("canvas");
            const context = canvas.getContext("webgl2") || canvas.getContext("webgl") || canvas.getContext("experimental-webgl");
            return Boolean(context);
        } catch {
            return false;
        }
    }

    function currentRoomId(): string {
        return String(window.currentRoom?.id || "");
    }

    function effectiveLevel(roomId: string, baseLevel: DiceAnimationLevel): DiceAnimationLevel {
        const downgraded = sessionLevels.get(roomId);
        if (!downgraded) return baseLevel;
        const order: DiceAnimationLevel[] = ["text", "simple", "3d"];
        return order.indexOf(downgraded) < order.indexOf(baseLevel) ? downgraded : baseLevel;
    }

    function report(
        roomId: string,
        fromLevel: DiceAnimationLevel,
        toLevel: DiceAnimationLevel,
        reason: string,
        options: { scope?: "session" | "roll"; error?: string; dice?: Record<string, unknown>; requestId?: string; toolName?: string } = {},
    ): void {
        if (!roomId || fromLevel === toLevel) return;
        const scope = options.scope === "session" ? "session" : "roll";
        if (scope === "session") {
            const key = `${roomId}:${fromLevel}:${toLevel}:${reason}`;
            if (reportedSession.has(key)) return;
            reportedSession.add(key);
            sessionLevels.set(roomId, toLevel);
        }
        console.warn(`骰子动画降级 ${fromLevel} → ${toLevel} (${reason})`, options.error || "");
        void TrpgApi.post<ApiResponse<{ logged: boolean }>>(
            `/api/rooms/${encodeURIComponent(roomId)}/dice-animation/degraded`,
            {
                from_level: fromLevel,
                to_level: toLevel,
                reason,
                scope,
                error: options.error || "",
                dice: options.dice || {},
                request_id: options.requestId || null,
                tool_name: options.toolName || "",
            },
        ).catch(() => {
            // 降级日志失败不再打扰玩家。
        });
    }

    function resetSession(roomId?: string): void {
        if (roomId) {
            sessionLevels.delete(roomId);
            Array.from(reportedSession).forEach((key) => {
                if (key.startsWith(`${roomId}:`)) reportedSession.delete(key);
            });
            return;
        }
        sessionLevels.clear();
        reportedSession.clear();
    }

    return { webglAvailable, effectiveLevel, report, resetSession, currentRoomId };
})();
