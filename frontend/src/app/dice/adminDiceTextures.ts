/**
 * 管理员骰子贴图库：上传桌面/骰子图片，维护桌面皮肤与逐面骰子皮肤。
 * 仅管理员可见；所有写入接口在服务端再次校验权限。
 */
(function initAdminDiceTextures(global: Window) {
    "use strict";

    const CONTAINER_ID = "adminDiceTextures";
    const DIE_TYPES = ["d4", "d6", "d8", "d10", "d12", "d20", "d100"];
    const KIND_LABELS: Record<string, string> = { table: "桌面贴图", dice: "骰子贴图", face: "骰面贴图" };

    let library: DiceTextureLibrary = { assets: [], table_skins: [], dice_skins: [] };
    let dieType = "d6";
    let editingSkinId = "";
    let loaded = false;

    function escapeHtml(value: unknown): string {
        return String(value ?? "")
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;")
            .replace(/'/g, "&#039;");
    }

    function notify(message: string, type = "info"): void {
        global.showNotification?.(message, type);
    }

    function faceKeysFor(type: string): string[] {
        const sides = Number(type.replace("d", "")) || 6;
        if (type === "d10" || type === "d100") return Array.from({ length: 10 }, (_, index) => String(index));
        return Array.from({ length: sides }, (_, index) => String(index + 1));
    }

    function options(values: Array<{ value: string; label: string }>, selected: string): string {
        return values.map((item) => `<option value="${escapeHtml(item.value)}"${item.value === selected ? " selected" : ""}>${escapeHtml(item.label)}</option>`).join("");
    }

    function assetOptions(selected: string): string {
        return options(
            [{ value: "", label: "未选择" }].concat(library.assets.map((asset) => ({
                value: asset.id,
                label: `${asset.name}（${KIND_LABELS[asset.kind] || asset.kind}）`,
            }))),
            selected,
        );
    }

    function renderAssets(): string {
        if (!library.assets.length) return `<p class="text-muted small">暂无贴图。上传后会出现在这里。</p>`;
        return library.assets.map((asset) => `
            <div class="admin-dice-asset" data-asset-id="${escapeHtml(asset.id)}">
                <img src="${escapeHtml(diceAssetUrl(asset.id))}" alt="${escapeHtml(asset.name)}" loading="lazy">
                <div class="admin-dice-asset-meta">
                    <strong>${escapeHtml(asset.name)}</strong>
                    <span class="small text-muted">${escapeHtml(KIND_LABELS[asset.kind] || asset.kind)} · ${asset.width}×${asset.height} · ${Math.round(asset.size / 1024)}KB</span>
                </div>
                <button type="button" class="btn btn-sm btn-outline-danger" data-delete-asset="${escapeHtml(asset.id)}">删除</button>
            </div>`).join("");
    }

    function renderTableSkins(): string {
        if (!library.table_skins.length) return `<p class="text-muted small">暂无桌面皮肤。</p>`;
        return library.table_skins.map((skin) => {
            const asset = library.assets.find((item) => item.id === skin.asset_id);
            return `
                <div class="admin-dice-skin-row" data-skin-id="${escapeHtml(skin.id)}">
                    <span>${escapeHtml(skin.name)}${skin.enabled ? "" : "（已停用）"}</span>
                    <span class="small text-muted">${escapeHtml(asset?.name || "贴图缺失")}</span>
                    <button type="button" class="btn btn-sm btn-outline-danger" data-delete-skin="${escapeHtml(skin.id)}">删除</button>
                </div>`;
        }).join("");
    }

    function renderDiceSkins(): string {
        const skins = library.dice_skins.filter((skin) => skin.die_type === dieType);
        if (!skins.length) return `<p class="text-muted small">该骰型暂无皮肤。</p>`;
        return skins.map((skin) => `
            <div class="admin-dice-skin-row" data-skin-id="${escapeHtml(skin.id)}">
                <span>${escapeHtml(skin.name)}${skin.enabled ? "" : "（已停用）"}</span>
                <button type="button" class="btn btn-sm btn-outline-secondary" data-edit-skin="${escapeHtml(skin.id)}">编辑</button>
                <button type="button" class="btn btn-sm btn-outline-danger" data-delete-skin="${escapeHtml(skin.id)}">删除</button>
            </div>`).join("");
    }

    function renderFaceEditor(): string {
        const editing = library.dice_skins.find((skin) => skin.id === editingSkinId && skin.die_type === dieType);
        const keys = faceKeysFor(dieType);
        const rows = keys.map((key) => `
            <label class="admin-dice-face-row">
                <span>${escapeHtml(key)}</span>
                <select class="form-select form-select-sm" data-face-key="${escapeHtml(key)}">${assetOptions(editing?.faces?.[key] || "")}</select>
            </label>`).join("");
        return `
            <div class="admin-dice-face-editor">
                <div class="row g-2 align-items-end mb-2">
                    <div class="col-md-3">
                        <label class="form-label small">骰型</label>
                        <select class="form-select form-select-sm" data-die-type>${options(DIE_TYPES.map((value) => ({ value, label: value.toUpperCase() })), dieType)}</select>
                    </div>
                    <div class="col-md-3">
                        <label class="form-label small">编辑已有皮肤</label>
                        <select class="form-select form-select-sm" data-edit-select>${options(
                            [{ value: "", label: "新建皮肤" }].concat(library.dice_skins.filter((skin) => skin.die_type === dieType).map((skin) => ({ value: skin.id, label: skin.name }))),
                            editingSkinId,
                        )}</select>
                    </div>
                    <div class="col-md-4">
                        <label class="form-label small">皮肤名称</label>
                        <input type="text" class="form-control form-control-sm" data-skin-name value="${escapeHtml(editing?.name || "")}" maxlength="60">
                    </div>
                    <div class="col-md-2">
                        <button type="button" class="btn btn-primary btn-sm w-100" data-save-dice-skin>保存皮肤</button>
                    </div>
                </div>
                <details class="admin-dice-faces">
                    <summary>逐面贴图（留空表示纯色）</summary>
                    ${rows}
                </details>
            </div>`;
    }

    function renderAll(): void {
        const container = document.getElementById(CONTAINER_ID);
        if (!container) return;
        container.innerHTML = `
            <div class="admin-dice-upload">
                <div class="row g-2 align-items-end">
                    <div class="col-md-3">
                        <label class="form-label small">贴图类型</label>
                        <select class="form-select form-select-sm" data-upload-kind>
                            <option value="table">桌面贴图</option>
                            <option value="dice">骰子贴图</option>
                            <option value="face">骰面贴图</option>
                        </select>
                    </div>
                    <div class="col-md-3">
                        <label class="form-label small">名称</label>
                        <input type="text" class="form-control form-control-sm" data-upload-name maxlength="60" placeholder="例如：古旧木桌">
                    </div>
                    <div class="col-md-4">
                        <label class="form-label small">图片文件</label>
                        <input type="file" class="form-control form-control-sm" data-upload-file accept="image/png,image/jpeg,image/webp">
                    </div>
                    <div class="col-md-2">
                        <button type="button" class="btn btn-primary btn-sm w-100" data-upload>上传贴图</button>
                    </div>
                </div>
                <p class="text-muted small mb-0 mt-1">支持 PNG / JPEG / WebP，尺寸 128–2048px，单文件不超过 3MB；服务端按文件头校验。</p>
            </div>
            <h5 class="mt-3">贴图资产</h5>
            <div class="admin-dice-assets" data-assets>${renderAssets()}</div>
            <h5 class="mt-3">桌面皮肤</h5>
            <div class="admin-dice-skins" data-table-skins>${renderTableSkins()}</div>
            <div class="row g-2 align-items-end mt-2">
                <div class="col-md-3">
                    <label class="form-label small">皮肤名称</label>
                    <input type="text" class="form-control form-control-sm" data-table-skin-name maxlength="60" placeholder="例如：深色木纹">
                </div>
                <div class="col-md-6">
                    <label class="form-label small">桌面贴图</label>
                    <select class="form-select form-select-sm" data-table-skin-asset>${assetOptions("")}</select>
                </div>
                <div class="col-md-3">
                    <button type="button" class="btn btn-primary btn-sm w-100" data-save-table-skin>保存桌面皮肤</button>
                </div>
            </div>
            <h5 class="mt-3">骰子皮肤</h5>
            <div class="admin-dice-skins" data-dice-skins>${renderDiceSkins()}</div>
            ${renderFaceEditor()}
        `;
        loaded = true;
        bind(container);
    }

    function bind(container: HTMLElement): void {
        container.querySelector<HTMLElement>("[data-upload]")?.addEventListener("click", () => {
            void upload(container);
        });
        container.querySelectorAll<HTMLElement>("[data-delete-asset]").forEach((button) => {
            button.addEventListener("click", () => {
                void removeAsset(button.dataset.deleteAsset || "");
            });
        });
        container.querySelectorAll<HTMLElement>("[data-delete-skin]").forEach((button) => {
            button.addEventListener("click", () => {
                void removeSkin(button.dataset.deleteSkin || "");
            });
        });
        container.querySelectorAll<HTMLElement>("[data-edit-skin]").forEach((button) => {
            button.addEventListener("click", () => {
                editingSkinId = button.dataset.editSkin || "";
                renderAll();
            });
        });
        container.querySelector<HTMLElement>("[data-save-table-skin]")?.addEventListener("click", () => {
            void saveTableSkin(container);
        });
        container.querySelector<HTMLSelectElement>("[data-die-type]")?.addEventListener("change", (event) => {
            dieType = (event.target as HTMLSelectElement).value;
            editingSkinId = "";
            renderAll();
        });
        container.querySelector<HTMLSelectElement>("[data-edit-select]")?.addEventListener("change", (event) => {
            editingSkinId = (event.target as HTMLSelectElement).value;
            renderAll();
        });
        container.querySelector<HTMLElement>("[data-save-dice-skin]")?.addEventListener("click", () => {
            void saveDiceSkin(container);
        });
    }

    async function upload(container: HTMLElement): Promise<void> {
        const fileInput = container.querySelector<HTMLInputElement>("[data-upload-file]");
        const file = fileInput?.files?.[0];
        if (!file) {
            notify("请先选择要上传的图片", "warning");
            return;
        }
        const formData = new FormData();
        formData.append("file", file);
        formData.append("kind", container.querySelector<HTMLSelectElement>("[data-upload-kind]")?.value || "table");
        formData.append("name", container.querySelector<HTMLInputElement>("[data-upload-name]")?.value || "");
        const response = await TrpgApi.post<ApiResponse<DiceTextureAsset>>("/api/dice-textures/assets", formData);
        if (!response?.success) {
            notify(`上传失败：${response?.message || response?.error || "未知错误"}`, "error");
            return;
        }
        notify("贴图已上传", "success");
        await refresh();
    }

    async function removeAsset(assetId: string): Promise<void> {
        if (!assetId) return;
        const confirmed = await global.TrpgDialog?.confirm({ message: "删除贴图资产？被皮肤引用的资产无法删除。", danger: true });
        if (!confirmed) return;
        const response = await TrpgApi.del<ApiResponse<unknown>>(`/api/dice-textures/assets/${encodeURIComponent(assetId)}`);
        if (!response?.success) {
            notify(`删除失败：${response?.message || response?.error || "未知错误"}`, "error");
            return;
        }
        notify("贴图已删除", "success");
        await refresh();
    }

    async function removeSkin(skinId: string): Promise<void> {
        if (!skinId) return;
        const confirmed = await global.TrpgDialog?.confirm({ message: "删除该皮肤？已选择该皮肤的玩家会自动回退到纯色。", danger: true });
        if (!confirmed) return;
        const response = await TrpgApi.del<ApiResponse<unknown>>(`/api/dice-textures/skins/${encodeURIComponent(skinId)}`);
        if (!response?.success) {
            notify(`删除失败：${response?.message || response?.error || "未知错误"}`, "error");
            return;
        }
        if (editingSkinId === skinId) editingSkinId = "";
        notify("皮肤已删除", "success");
        await refresh();
    }

    async function saveTableSkin(container: HTMLElement): Promise<void> {
        const assetId = container.querySelector<HTMLSelectElement>("[data-table-skin-asset]")?.value || "";
        if (!assetId) {
            notify("请选择桌面贴图", "warning");
            return;
        }
        const response = await TrpgApi.post<ApiResponse<DiceTableSkin>>("/api/dice-textures/table-skins", {
            name: container.querySelector<HTMLInputElement>("[data-table-skin-name]")?.value || "",
            asset_id: assetId,
        });
        if (!response?.success) {
            notify(`保存失败：${response?.message || response?.error || "未知错误"}`, "error");
            return;
        }
        notify("桌面皮肤已保存", "success");
        await refresh();
    }

    async function saveDiceSkin(container: HTMLElement): Promise<void> {
        const faces: Record<string, string | null> = {};
        container.querySelectorAll<HTMLSelectElement>("[data-face-key]").forEach((select) => {
            const key = select.dataset.faceKey || "";
            if (key) faces[key] = select.value || null;
        });
        const response = await TrpgApi.post<ApiResponse<DiceSkin>>("/api/dice-textures/dice-skins", {
            id: editingSkinId || undefined,
            name: container.querySelector<HTMLInputElement>("[data-skin-name]")?.value || "",
            die_type: dieType,
            faces,
        });
        if (!response?.success) {
            notify(`保存失败：${response?.message || response?.error || "未知错误"}`, "error");
            return;
        }
        notify("骰子皮肤已保存", "success");
        await refresh();
    }

    async function refresh(): Promise<void> {
        const response = await TrpgApi.get<ApiResponse<DiceTextureLibrary>>("/api/dice-textures?include_disabled=1");
        library = response?.data || { assets: [], table_skins: [], dice_skins: [] };
        renderAll();
        // 让玩家侧外观选择的缓存与服务端保持一致。
        void diceTextureLibrary(true);
    }

    function init(): void {
        if (loaded || !document.getElementById(CONTAINER_ID)) return;
        const role = global.currentUser?.role;
        if (role !== "ADMIN" && role !== "OWNER") return;
        void refresh();
    }

    global.TrpgAdminDiceTextures = { init, refresh };
})(window);
