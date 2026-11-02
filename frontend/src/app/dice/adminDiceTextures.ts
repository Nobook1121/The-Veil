/**
 * 管理员骰子贴图库：上传桌面/骰子图片，维护桌面皮肤与逐面骰子皮肤。
 * 仅管理员可见；所有写入接口在服务端再次校验权限。
 */
(function initAdminDiceTextures(global: Window) {
    "use strict";

    const CONTAINER_ID = "adminDiceTextures";
    const DIE_TYPES = ["d4", "d6", "d8", "d10", "d12", "d20", "d100"];
    const KIND_LABELS: Record<string, string> = { table: "桌面贴图", dice: "骰子贴图", face: "骰面贴图" };
    // 与服务端 dice_textures.py 的限制保持一致：尺寸 128–2048px，单文件 3MB。
    const MIN_DIMENSION = 128;
    const MAX_DIMENSION = 2048;
    const MAX_BYTES = 3 * 1024 * 1024;
    const SUPPORTED_TYPES = ["image/png", "image/jpeg", "image/webp"];
    // 骰面裁剪：框内正方形即最终贴到骰面上的图案。
    const CROP_FRAME_SIZE = 260;
    const CROP_MAX_ZOOM = 4;
    const CROP_MAX_OUTPUT = 1024;

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

    /** 服务端 reason 转成可读提示，避免用户看到 bad_dimensions 这类内部代码。 */
    function describeFailure(reason: string): string {
        const labels: Record<string, string> = {
            bad_dimensions: `图片尺寸需在 ${MIN_DIMENSION}–${MAX_DIMENSION} 像素之间`,
            too_large: `图片体积不能超过 ${Math.round(MAX_BYTES / (1024 * 1024))}MB`,
            unsupported_type: "仅支持 PNG / JPEG / WebP 图片",
        };
        return labels[reason] || reason;
    }

    function loadImage(file: File): Promise<HTMLImageElement> {
        return new Promise((resolve, reject) => {
            const url = URL.createObjectURL(file);
            const image = new Image();
            image.onload = () => {
                URL.revokeObjectURL(url);
                resolve(image);
            };
            image.onerror = () => {
                URL.revokeObjectURL(url);
                reject(new Error("图片无法解析，请确认文件未损坏"));
            };
            image.src = url;
        });
    }

    /** 服务端只接受 128–2048px 的图片，超出范围时按原始比例缩放。 */
    function fitDimensions(width: number, height: number): { width: number; height: number } {
        const longest = Math.max(width, height);
        const shortest = Math.min(width, height);
        let scale = 1;
        if (longest > MAX_DIMENSION) scale = MAX_DIMENSION / longest;
        else if (shortest < MIN_DIMENSION) scale = MIN_DIMENSION / shortest;
        let nextWidth = width * scale;
        let nextHeight = height * scale;
        // 极端长宽比下放大后仍可能超上限，再按上限压一次。
        const overflow = Math.max(nextWidth, nextHeight) / MAX_DIMENSION;
        if (overflow > 1) {
            nextWidth /= overflow;
            nextHeight /= overflow;
        }
        return {
            width: Math.max(1, Math.round(nextWidth)),
            height: Math.max(1, Math.round(nextHeight)),
        };
    }

    function encodeCanvas(canvas: HTMLCanvasElement, type: string, quality: number): Promise<Blob | null> {
        return new Promise((resolve) => canvas.toBlob(resolve, type, quality));
    }

    /** 上传前把图片缩放进服务端允许的尺寸范围，并确保体积不超过 3MB。 */
    async function prepareUpload(file: File): Promise<File> {
        if (!SUPPORTED_TYPES.includes(file.type.toLowerCase())) {
            throw new Error("仅支持 PNG / JPEG / WebP 图片");
        }
        const image = await loadImage(file);
        const sourceWidth = image.naturalWidth || image.width;
        const sourceHeight = image.naturalHeight || image.height;
        if (!sourceWidth || !sourceHeight) throw new Error("图片无法解析，请确认文件未损坏");
        const target = fitDimensions(sourceWidth, sourceHeight);
        if (target.width < MIN_DIMENSION || target.height < MIN_DIMENSION) {
            throw new Error(`图片长宽比过于极端，请裁剪到短边不小于 ${MIN_DIMENSION} 像素`);
        }
        if (target.width === sourceWidth && target.height === sourceHeight && file.size <= MAX_BYTES) {
            return file;
        }
        const canvas = document.createElement("canvas");
        canvas.width = target.width;
        canvas.height = target.height;
        const context = canvas.getContext("2d");
        if (!context) throw new Error("无法处理该图片");
        context.imageSmoothingEnabled = true;
        context.imageSmoothingQuality = "high";
        context.drawImage(image, 0, 0, target.width, target.height);
        let blob = await encodeCanvas(canvas, file.type, 0.92);
        // 缩放后仍偏大时改用 WebP 压缩，服务端同样基于文件头识别。
        if (blob && blob.size > MAX_BYTES) {
            blob = (await encodeCanvas(canvas, "image/webp", 0.9)) || blob;
        }
        if (!blob) throw new Error("无法处理该图片");
        if (blob.size > MAX_BYTES) {
            throw new Error(`图片体积不能超过 ${Math.round(MAX_BYTES / (1024 * 1024))}MB`);
        }
        return new File([blob], file.name, { type: blob.type || file.type });
    }

    /** 骰子/骰面贴图：用正方形取景框让管理员自行缩放、平移并截取。 */
    function openCropper(file: File, onConfirm: (cropped: File) => void): void {
        const objectUrl = URL.createObjectURL(file);
        const source = new Image();
        source.onerror = () => {
            URL.revokeObjectURL(objectUrl);
            notify("上传失败：图片无法解析，请确认文件未损坏", "error");
        };
        source.onload = () => {
            const frameSize = CROP_FRAME_SIZE;
            const baseScale = Math.max(frameSize / source.naturalWidth, frameSize / source.naturalHeight);

            const overlay = document.createElement("div");
            overlay.className = "dice-crop-overlay";
            overlay.innerHTML = `
                <div class="dice-crop-panel" role="dialog" aria-modal="true" aria-label="裁剪骰子贴图">
                    <h6 class="dice-crop-title">裁剪骰子贴图</h6>
                    <p class="dice-crop-hint">拖动图片调整位置，滚轮或滑块缩放；框内正方形就是最终贴到骰面上的图案。</p>
                    <div class="dice-crop-frame" data-crop-frame>
                        <img class="dice-crop-image" data-crop-image alt="" draggable="false">
                        <span class="dice-crop-grid" aria-hidden="true"></span>
                    </div>
                    <div class="dice-crop-zoom-row">
                        <span class="small text-muted">缩放</span>
                        <input type="range" class="form-range" min="100" max="${CROP_MAX_ZOOM * 100}" step="1" value="100" data-crop-zoom>
                    </div>
                    <div class="dice-crop-actions">
                        <button type="button" class="btn btn-sm btn-outline-secondary" data-crop-cancel>取消</button>
                        <button type="button" class="btn btn-sm btn-primary" data-crop-confirm>确定裁剪并上传</button>
                    </div>
                </div>`;
            const frame = overlay.querySelector<HTMLElement>("[data-crop-frame]");
            const imageElement = overlay.querySelector<HTMLImageElement>("[data-crop-image]");
            const zoomInput = overlay.querySelector<HTMLInputElement>("[data-crop-zoom]");
            if (!frame || !imageElement || !zoomInput) {
                URL.revokeObjectURL(objectUrl);
                return;
            }
            frame.style.width = `${frameSize}px`;
            frame.style.height = `${frameSize}px`;
            imageElement.src = objectUrl;

            let zoom = 1;
            let offsetX = 0;
            let offsetY = 0;

            const applyTransform = (): void => {
                const scale = baseScale * zoom;
                const scaledWidth = source.naturalWidth * scale;
                const scaledHeight = source.naturalHeight * scale;
                const maxOffsetX = Math.max(0, (scaledWidth - frameSize) / 2);
                const maxOffsetY = Math.max(0, (scaledHeight - frameSize) / 2);
                offsetX = Math.min(maxOffsetX, Math.max(-maxOffsetX, offsetX));
                offsetY = Math.min(maxOffsetY, Math.max(-maxOffsetY, offsetY));
                imageElement.style.width = `${scaledWidth}px`;
                imageElement.style.height = `${scaledHeight}px`;
                imageElement.style.transform = `translate(-50%, -50%) translate(${offsetX}px, ${offsetY}px)`;
            };

            const setZoom = (value: number): void => {
                zoom = Math.min(CROP_MAX_ZOOM, Math.max(1, value));
                zoomInput.value = String(Math.round(zoom * 100));
                applyTransform();
            };

            const close = (): void => {
                overlay.remove();
                document.removeEventListener("keydown", onKeydown);
                URL.revokeObjectURL(objectUrl);
            };

            const onKeydown = (event: KeyboardEvent): void => {
                if (event.key === "Escape") close();
            };

            let dragging = false;
            let dragStartX = 0;
            let dragStartY = 0;
            let dragBaseX = 0;
            let dragBaseY = 0;
            frame.addEventListener("pointerdown", (event) => {
                dragging = true;
                dragStartX = event.clientX;
                dragStartY = event.clientY;
                dragBaseX = offsetX;
                dragBaseY = offsetY;
                frame.setPointerCapture(event.pointerId);
            });
            frame.addEventListener("pointermove", (event) => {
                if (!dragging) return;
                offsetX = dragBaseX + (event.clientX - dragStartX);
                offsetY = dragBaseY + (event.clientY - dragStartY);
                applyTransform();
            });
            const endDrag = (event: PointerEvent): void => {
                dragging = false;
                if (frame.hasPointerCapture(event.pointerId)) frame.releasePointerCapture(event.pointerId);
            };
            frame.addEventListener("pointerup", endDrag);
            frame.addEventListener("pointercancel", endDrag);
            frame.addEventListener("wheel", (event) => {
                event.preventDefault();
                setZoom(zoom * (event.deltaY < 0 ? 1.12 : 0.89));
            }, { passive: false });
            zoomInput.addEventListener("input", () => setZoom(Number(zoomInput.value) / 100));
            overlay.querySelector("[data-crop-cancel]")?.addEventListener("click", close);
            overlay.addEventListener("click", (event) => {
                if (event.target === overlay) close();
            });
            overlay.querySelector("[data-crop-confirm]")?.addEventListener("click", () => {
                const scale = baseScale * zoom;
                const left = frameSize / 2 - (source.naturalWidth * scale) / 2 + offsetX;
                const top = frameSize / 2 - (source.naturalHeight * scale) / 2 + offsetY;
                const sourceSize = frameSize / scale;
                const outputSize = Math.min(CROP_MAX_OUTPUT, Math.max(MIN_DIMENSION, Math.round(sourceSize)));
                const canvas = document.createElement("canvas");
                canvas.width = outputSize;
                canvas.height = outputSize;
                const context = canvas.getContext("2d");
                if (!context) {
                    notify("裁剪失败，请重试", "error");
                    return;
                }
                context.imageSmoothingQuality = "high";
                context.drawImage(source, -left / scale, -top / scale, sourceSize, sourceSize, 0, 0, outputSize, outputSize);
                canvas.toBlob((blob) => {
                    close();
                    if (!blob) {
                        notify("裁剪失败，请重试", "error");
                        return;
                    }
                    onConfirm(new File([blob], file.name, { type: blob.type || "image/png" }));
                }, "image/webp", 0.92);
            });

            document.addEventListener("keydown", onKeydown);
            document.body.appendChild(overlay);
            applyTransform();
        };
        source.src = objectUrl;
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
                <p class="text-muted small mb-0 mt-1">支持 PNG / JPEG / WebP，单文件不超过 3MB；骰子/骰面贴图会弹出取景框自行裁剪，桌面贴图自动等比缩放（128–2048px）。</p>
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
        const kind = container.querySelector<HTMLSelectElement>("[data-upload-kind]")?.value || "table";
        const name = container.querySelector<HTMLInputElement>("[data-upload-name]")?.value || "";
        if (!SUPPORTED_TYPES.includes(file.type.toLowerCase())) {
            notify("上传失败：仅支持 PNG / JPEG / WebP 图片", "error");
            return;
        }
        if (kind === "table") {
            // 桌面贴图铺满桌面，保持原始比例缩放即可。
            let uploadFile: File;
            try {
                uploadFile = await prepareUpload(file);
            } catch (error) {
                notify(`上传失败：${error instanceof Error ? error.message : String(error)}`, "error");
                return;
            }
            await submitUpload(uploadFile, kind, name);
            return;
        }
        // 骰子/骰面贴图先进取景框，让管理员自己决定截取范围。
        openCropper(file, (cropped) => {
            void submitUpload(cropped, kind, name);
        });
    }

    async function submitUpload(file: File, kind: string, name: string): Promise<void> {
        const formData = new FormData();
        formData.append("file", file);
        formData.append("kind", kind);
        formData.append("name", name);
        const response = await TrpgApi.post<ApiResponse<DiceTextureAsset>>("/api/dice-textures/assets", formData);
        if (!response?.success) {
            notify(`上传失败：${describeFailure(response?.message || response?.error || "未知错误")}`, "error");
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
