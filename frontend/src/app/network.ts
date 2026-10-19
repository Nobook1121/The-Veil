interface NetworkConfig {
    port?: number;
    discovery_enabled?: boolean;
    /** 生成房间邀请链接/二维码时使用的对外基础地址，留空表示跟随访问者当前地址。 */
    share_base_url?: string;
}

interface ShareAddressResponse {
    addresses?: string[];
    share_base_url?: string;
    port?: number;
}

let networkShareBaseUrl = "";

/** 供其它模块（房间分享）读取管理员配置的邀请地址；未配置时返回空串，由调用方回退到当前 origin。 */
function getShareBaseUrl(): string {
    return networkShareBaseUrl;
}

interface PenetrationConfig {
    enabled?: boolean;
}

interface NetworkStatus {
    server_status?: string;
    lan_ip?: string;
    external_access?: string;
}

interface NetworkTestResult {
    local_ip?: string;
    port?: number;
    connection_success?: boolean;
    error_message?: string;
    discovery_success?: boolean;
    discovery_error?: string;
}

async function initNetworkConfig(): Promise<void> {
    await loadNetworkConfig();
    await loadShareAddressOptions();

    const shareBaseUrlSelect = document.getElementById("shareBaseUrlSelect") as HTMLSelectElement | null;
    const shareBaseUrlInput = document.getElementById("shareBaseUrlInput") as HTMLInputElement | null;
    if (shareBaseUrlSelect && shareBaseUrlInput) {
        shareBaseUrlSelect.addEventListener("change", () => {
            if (shareBaseUrlSelect.value === "__custom__") {
                shareBaseUrlInput.disabled = false;
                shareBaseUrlInput.focus();
                return;
            }
            shareBaseUrlInput.disabled = true;
            shareBaseUrlInput.value = shareBaseUrlSelect.value;
        });
    }

    const saveNetworkConfigBtn = document.getElementById("saveNetworkConfig");
    if (saveNetworkConfigBtn) {
        saveNetworkConfigBtn.addEventListener("click", () => {
            void saveNetworkConfig();
        });
    }

    const testNetworkConnectionBtn = document.getElementById("testNetworkConnection");
    if (testNetworkConnectionBtn) {
        testNetworkConnectionBtn.addEventListener("click", () => {
            void testNetworkConnection();
        });
    }

    // 网络状态面板每 5 秒静默刷新一次，失败只在控制台记录，不打扰用户。
    setInterval(() => {
        void updateNetworkStatus();
    }, 5000);
    console.log("网络配置初始化成功");
}

async function loadNetworkConfig(): Promise<void> {
    try {
        const data = await TrpgApi.get<ApiResponse<NetworkConfig>>("/api/network/config");

        if (data.success && data.data) {
            const config = data.data;
            const networkPort = document.getElementById("networkPort") as HTMLInputElement | null;
            if (networkPort) {
                networkPort.value = String(config.port || 8086);
            }

            const enableDiscovery = document.getElementById("enableDiscovery") as HTMLInputElement | null;
            if (enableDiscovery) {
                enableDiscovery.checked = config.discovery_enabled !== false;
            }

            networkShareBaseUrl = (config.share_base_url || "").trim();

            await loadPenetrationConfig();
        }
    } catch (error) {
        console.error("加载网络配置失败:", error);
    }
}

/** 拉取本机多网卡候选地址并填充设置页的「邀请地址」下拉框。 */
async function loadShareAddressOptions(): Promise<void> {
    const select = document.getElementById("shareBaseUrlSelect") as HTMLSelectElement | null;
    const input = document.getElementById("shareBaseUrlInput") as HTMLInputElement | null;
    if (!select || !input) return;
    try {
        const data = await TrpgApi.get<ApiResponse<ShareAddressResponse>>("/api/network/addresses");
        const payload = data.success ? data.data : undefined;
        const addresses = payload?.addresses || [];
        const configured = (payload?.share_base_url || "").trim();

        select.innerHTML = "";
        const defaultOption = document.createElement("option");
        defaultOption.value = "";
        defaultOption.textContent = "使用访问者当前地址（默认）";
        select.appendChild(defaultOption);
        addresses.forEach((address) => {
            const option = document.createElement("option");
            option.value = address;
            option.textContent = address;
            select.appendChild(option);
        });
        const customOption = document.createElement("option");
        customOption.value = "__custom__";
        customOption.textContent = "自定义地址…";
        select.appendChild(customOption);

        if (!configured) {
            select.value = "";
            input.value = "";
        } else if (addresses.includes(configured)) {
            select.value = configured;
            input.value = configured;
        } else {
            select.value = "__custom__";
            input.value = configured;
        }
        input.disabled = select.value !== "__custom__";
    } catch (error) {
        console.error("加载邀请地址列表失败:", error);
    }
}

/** 读取当前设置页里用户选择的邀请地址（下拉选中项优先，自定义模式取输入框）。 */
function currentShareBaseUrlInput(): string {
    const select = document.getElementById("shareBaseUrlSelect") as HTMLSelectElement | null;
    const input = document.getElementById("shareBaseUrlInput") as HTMLInputElement | null;
    if (select && select.value && select.value !== "__custom__") {
        return select.value;
    }
    return (input?.value || "").trim();
}

async function loadPenetrationConfig(): Promise<void> {
    try {
        const data = await TrpgApi.get<ApiResponse<PenetrationConfig>>("/api/network/penetration");

        if (data.success && data.data) {
            const enablePenetration = document.getElementById("enablePenetration") as HTMLInputElement | null;
            if (enablePenetration) {
                enablePenetration.checked = data.data.enabled === true;
            }
        }
    } catch (error) {
        console.error("加载网络穿透配置失败:", error);
    }
}

async function saveNetworkConfig(): Promise<void> {
    try {
        const networkPort = document.getElementById("networkPort") as HTMLInputElement | null;
        const enableDiscovery = document.getElementById("enableDiscovery") as HTMLInputElement | null;
        if (!networkPort || !enableDiscovery) {
            showNotification("网络配置表单不完整", "error");
            return;
        }

        const shareBaseUrl = currentShareBaseUrlInput();
        if (shareBaseUrl && !/^https?:\/\//i.test(shareBaseUrl)) {
            showNotification("邀请地址需以 http:// 或 https:// 开头", "error");
            return;
        }

        const data = await TrpgApi.post<ApiResponse>("/api/network/config", {
            port: Number.parseInt(networkPort.value, 10),
            discovery_enabled: enableDiscovery.checked,
            share_base_url: shareBaseUrl,
        });

        if (data.success) {
            networkShareBaseUrl = shareBaseUrl;
            showNotification("网络配置保存成功", "success");
            await updateNetworkStatus();
        } else {
            showNotification(`网络配置保存失败: ${data.message || data.error || "未知错误"}`, "error");
        }
    } catch (error) {
        console.error("保存网络配置失败:", error);
        showNotification(`保存网络配置失败: ${networkErrorMessage(error)}`, "error");
    }
}

async function testNetworkConnection(): Promise<void> {
    try {
        const data = await TrpgApi.post<ApiResponse<NetworkTestResult>>("/api/network/test", {});

        if (data.success && data.data) {
            const testData = data.data;
            let message = `网络连接测试完成\n本地IP: ${testData.local_ip || "-"}\n端口: ${testData.port || "-"}\n连接状态: ${testData.connection_success ? "成功" : "失败"}`;
            if (!testData.connection_success) {
                message += `\n错误信息: ${testData.error_message || "未知"}`;
            }
            message += `\n服务发现: ${testData.discovery_success ? "成功" : "失败"}`;
            if (!testData.discovery_success) {
                message += `\n发现错误: ${testData.discovery_error || "未知"}`;
            }
            showNotification(message, testData.connection_success ? "success" : "error");
            await updateNetworkStatus();
        } else {
            showNotification(`网络连接测试失败: ${data.message || data.error || "未知错误"}`, "error");
        }
    } catch (error) {
        console.error("测试网络连接失败:", error);
        showNotification(`测试网络连接失败: ${networkErrorMessage(error)}`, "error");
    }
}

async function updateNetworkStatus(): Promise<void> {
    try {
        const data = await TrpgApi.get<ApiResponse<NetworkStatus>>("/api/network/status");

        if (data.success && data.data) {
            const status = data.data;
            const serverStatus = document.getElementById("serverStatus");
            if (serverStatus) {
                serverStatus.textContent = status.server_status || "未知";
                serverStatus.className = `status-value ${status.server_status === "运行中" ? "status-success" : "status-error"}`;
            }

            const lanIp = document.getElementById("lanIp");
            if (lanIp) {
                lanIp.textContent = status.lan_ip || "未知";
            }

            const externalAccess = document.getElementById("externalAccess");
            if (externalAccess) {
                externalAccess.textContent = status.external_access || "未知";
                externalAccess.className = `status-value ${status.external_access === "可访问" ? "status-success" : "status-error"}`;
            }
        }
    } catch (error) {
        console.error("更新网络状态失败:", error);
    }
}

function networkErrorMessage(error: unknown): string {
    return error instanceof Error ? error.message : String(error);
}
