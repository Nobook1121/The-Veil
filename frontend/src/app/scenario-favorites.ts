/**
 * 剧本收藏：按当前账号保存在本机 localStorage。
 * 收藏结果会通过 trpg:favorites-changed 事件通知剧本广场、个人主页与创建房间选择器。
 */
namespace ScenarioFavorites {
    const STORAGE_PREFIX = "trpg_scenario_favorites_";
    export const CHANGED_EVENT = "trpg:favorites-changed";

    function storageKey(): string {
        const userId = window.currentUser?.user_id;
        return `${STORAGE_PREFIX}${userId || "guest"}`;
    }

    export function list(): number[] {
        try {
            const raw = window.localStorage.getItem(storageKey());
            const parsed: unknown = raw ? JSON.parse(raw) : [];
            if (!Array.isArray(parsed)) return [];
            return parsed
                .map((value) => Number(value))
                .filter((id) => Number.isFinite(id) && id > 0);
        } catch {
            return [];
        }
    }

    export function has(id: number): boolean {
        return list().includes(id);
    }

    /** 切换收藏状态，返回切换后是否已收藏。 */
    export function toggle(id: number): boolean {
        const current = list();
        const exists = current.includes(id);
        const next = exists ? current.filter((item) => item !== id) : [...current, id];
        try {
            window.localStorage.setItem(storageKey(), JSON.stringify(next));
        } catch {
            // 浏览器禁用存储时只影响本机收藏，不阻断其它操作。
        }
        window.dispatchEvent(new CustomEvent(CHANGED_EVENT, { detail: { id, favorited: !exists } }));
        return !exists;
    }

    export function remove(id: number): void {
        if (has(id)) toggle(id);
    }

    /** 丢弃已经不存在的剧本 id（剧本被删除或归档后自动消失）。 */
    export function prune(validIds: number[]): void {
        const valid = new Set(validIds);
        const current = list();
        const next = current.filter((id) => valid.has(id));
        if (next.length === current.length) return;
        try {
            window.localStorage.setItem(storageKey(), JSON.stringify(next));
        } catch {
            // 忽略存储失败。
        }
    }
}
