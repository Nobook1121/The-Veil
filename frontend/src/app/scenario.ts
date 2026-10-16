interface ScenarioCoverUploadResponse {
    cover_url: string;
}

let scenarioController: ScenarioController | null = null;

function initScenarioManagement(): void {
    if (!scenarioController) scenarioController = new ScenarioController();

    document.addEventListener("change", (event) => {
        const target = event.target as HTMLElement | null;
        if (target?.id === "coverUpload") {
            void handleCoverUpload(event);
        }
    });

    // 未上传封面时，封面预览跟随剧本标题实时刷新为「剧本名」文字封面。
    document.addEventListener("input", (event) => {
        const target = event.target as HTMLInputElement | null;
        if (target?.id !== "scenarioTitle") return;
        const coverUrl = (document.getElementById("scenarioCoverUrl") as HTMLInputElement | null)?.value.trim();
        if (coverUrl) return;
        const preview = document.getElementById("coverPreview") as HTMLImageElement | null;
        if (preview) preview.src = TrpgAvatar.scenarioCoverTextDataUri(target.value);
    });
}

function refreshScenarioManagement(): void {
    scenarioController?.refresh();
}

window.refreshScenarioManagement = refreshScenarioManagement;

async function handleCoverUpload(event: Event): Promise<void> {
    const input = event.target as HTMLInputElement | null;
    const file = input?.files?.[0];
    if (!file) return;

    if (file.size > 5 * 1024 * 1024) {
        showNotification("封面文件大小不能超过 5MB", "error");
        return;
    }

    if (!file.type.startsWith("image/")) {
        showNotification("请上传图片文件", "error");
        return;
    }

    const reader = new FileReader();
    reader.onload = () => {
        const preview = document.getElementById("coverPreview") as HTMLImageElement | null;
        if (preview) preview.src = String(reader.result || "");
    };
    reader.readAsDataURL(file);

    window.uploadedCoverFile = file;

    const formData = new FormData();
    formData.append("cover", file);
    const scenarioTitle = (document.getElementById("scenarioTitle") as HTMLInputElement | null)?.value.trim() || "unknown_scenario";
    formData.append("scenario_title", scenarioTitle);

    try {
        const { response, data } = await TrpgApi.requestWithResponse<ApiResponse<ScenarioCoverUploadResponse>>("/api/scenarios/cover", {
            method: "POST",
            body: formData,
        });
        if (!response.ok || !data.success || !data.data) {
            throw new Error(data.message || data.error || "上传失败");
        }

        showNotification("封面上传成功", "success");
        const coverUrlInput = document.getElementById("scenarioCoverUrl") as HTMLInputElement | null;
        if (coverUrlInput) coverUrlInput.value = data.data.cover_url;
    } catch (error) {
        console.error("上传封面失败:", error);
        showNotification(`上传封面失败: ${scenarioUploadErrorMessage(error)}`, "error");
    }
}

function scenarioUploadErrorMessage(error: unknown): string {
    return error instanceof Error ? error.message : String(error);
}
