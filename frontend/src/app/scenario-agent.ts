/**
 * 剧本弹窗内的网站 Agent 侧栏（设计文档 §8.4）。
 *
 * 只向服务端发送 `scenario_id / selected_node_id / base_version` 与用户指令，
 * 不发送整份剧本；写操作走 Agent 的 preview → confirm → apply，发布仍由用户完成。
 */
(function (window: Window): void {
    "use strict";

    interface AgentAttachment {
        kind?: string;
        action_id?: string;
        tool?: string;
        risk?: string;
        summary?: string;
        status?: string;
        diff?: Record<string, unknown>;
    }

    interface AgentMessage {
        role: string;
        content: string;
        attachments?: AgentAttachment[];
        notice?: string;
    }

    interface AgentConversation {
        id: string;
        messages?: AgentMessage[];
    }

    interface AgentTurnPayload {
        conversation: AgentConversation;
        message: AgentMessage;
    }

    const state = {
        scenarioId: "",
        conversationId: null as string | null,
        nodeId: "",
        nodeTitle: "",
        sending: false,
    };

    function byId<T extends HTMLElement = HTMLElement>(id: string): T | null {
        return document.getElementById(id) as T | null;
    }

    function t(key: string, fallback: string): string {
        return window.TrpgI18n?.t(key, fallback) || fallback;
    }

    function escapeHtml(value: string): string {
        return value
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;")
            .replace(/'/g, "&#39;");
    }

    function modal(): HTMLElement | null {
        return byId("scenarioModal");
    }

    function currentScenarioId(): string {
        return modal()?.dataset.scenarioId || "";
    }

    function updateContext(): void {
        const label = byId("scenarioAgentContext");
        if (!label) return;
        const scenarioId = currentScenarioId();
        if (!scenarioId) {
            label.textContent = t("scenario.narrative.agent.context_empty", "打开一个已保存的剧本后可使用助手。");
            return;
        }
        const parts = [t("scenario.narrative.agent.scenario", "剧本"), scenarioId];
        if (state.nodeId) parts.push(t("scenario.narrative.agent.node", "节点"), state.nodeTitle || state.nodeId);
        label.textContent = parts.join(" · ");
    }

    function appendMessage(role: string, content: string, attachments?: AgentAttachment[]): void {
        const list = byId("scenarioAgentMessages");
        if (!list) return;
        const empty = list.querySelector(".scenario-agent-empty");
        if (empty) empty.remove();
        const bubble = document.createElement("div");
        bubble.className = `scenario-agent-message is-${role === "user" ? "user" : "assistant"}`;
        bubble.innerHTML = `<div class="scenario-agent-bubble">${escapeHtml(content)}</div>`;
        const cards = (attachments || []).filter((item) => item.action_id);
        if (cards.length) {
            const wrap = document.createElement("div");
            wrap.className = "scenario-agent-actions";
            for (const card of cards) {
                const actionId = card.action_id || "";
                const element = document.createElement("div");
                element.className = "scenario-agent-action";
                element.innerHTML = `
                    <div class="small">${escapeHtml(card.summary || card.tool || t("scenario.narrative.agent.patch", "草稿改动"))}</div>
                    <div class="scenario-agent-action-footer">
                        <span class="badge bg-secondary">${escapeHtml(card.risk || "write")}</span>
                        <button type="button" class="btn btn-sm btn-primary" data-confirm-action="${escapeHtml(actionId)}">${escapeHtml(t("scenario.narrative.agent.apply", "应用到草稿"))}</button>
                        <button type="button" class="btn btn-sm btn-outline-secondary" data-reject-action="${escapeHtml(actionId)}">${escapeHtml(t("scenario.narrative.agent.reject", "忽略"))}</button>
                    </div>`;
                wrap.appendChild(element);
            }
            bubble.appendChild(wrap);
        }
        if (attachments && attachments.length === 0 && content) bubble.classList.add("is-plain");
        list.appendChild(bubble);
        list.scrollTop = list.scrollHeight;
    }

    function clearMessages(): void {
        const list = byId("scenarioAgentMessages");
        if (!list) return;
        list.innerHTML = `<p class="text-muted small mb-0 scenario-agent-empty">${escapeHtml(t("scenario.narrative.agent.empty", "在世界树/图谱页签选中节点后，可以让助手补写正文、物件、结果或关键词。"))}</p>`;
    }

    function resetForScenario(scenarioId: string): void {
        if (state.scenarioId === scenarioId) return;
        state.scenarioId = scenarioId;
        state.conversationId = null;
        state.nodeId = "";
        state.nodeTitle = "";
        clearMessages();
        updateContext();
    }

    function openPanel(): void {
        const panel = byId("scenarioAgentPanel");
        if (!panel) return;
        const scenarioId = currentScenarioId();
        if (!scenarioId) {
            showNotification(t("scenario.tree.save_first", "请先保存剧本，再查看世界树。"), "warning");
            return;
        }
        if (window.ScenarioNarrative && !window.ScenarioNarrative.canEdit()) {
            showNotification(t("scenario.narrative.agent.readonly", "该剧本当前为只读，无法使用创作助手修改。"), "warning");
            return;
        }
        resetForScenario(scenarioId);
        panel.hidden = false;
        updateContext();
        byId<HTMLTextAreaElement>("scenarioAgentInput")?.focus();
    }

    function closePanel(): void {
        const panel = byId("scenarioAgentPanel");
        if (panel) panel.hidden = true;
    }

    function setSending(sending: boolean): void {
        state.sending = sending;
        const button = byId<HTMLButtonElement>("scenarioAgentSend");
        if (button) {
            button.disabled = sending;
            button.textContent = sending ? t("scenario.narrative.agent.sending", "生成中…") : t("scenario.narrative.agent.send", "发送");
        }
    }

    async function send(): Promise<void> {
        if (state.sending) return;
        const input = byId<HTMLTextAreaElement>("scenarioAgentInput");
        const content = (input?.value || "").trim();
        if (!content) return;
        const scenarioId = Number.parseInt(currentScenarioId(), 10);
        if (!Number.isFinite(scenarioId) || scenarioId <= 0) {
            showNotification(t("scenario.tree.save_first", "请先保存剧本，再查看世界树。"), "warning");
            return;
        }
        resetForScenario(String(scenarioId));
        appendMessage("user", content);
        if (input) input.value = "";
        setSending(true);
        try {
            const { response, data } = await TrpgApi.requestWithResponse<ApiResponse<AgentTurnPayload>>(
                "/api/agent/turn",
                {
                    method: "POST",
                    body: {
                        content,
                        conversation_id: state.conversationId,
                        scenario_id: scenarioId,
                        selected_node_id: state.nodeId || undefined,
                        base_version: window.ScenarioNarrative?.revision() ?? undefined,
                        tab: document.querySelector<HTMLElement>("#scenarioTabs .nav-link.active")?.dataset.bsTarget || undefined,
                    },
                },
            );
            if (!response.ok || !data?.success || !data.data) {
                throw new Error(data?.message || `HTTP ${response.status}`);
            }
            state.conversationId = data.data.conversation?.id || state.conversationId;
            appendMessage("assistant", data.data.message?.content || "", data.data.message?.attachments);
            if (data.data.message?.notice) {
                appendMessage("assistant", data.data.message.notice);
            }
        } catch (error) {
            const message = error instanceof Error ? error.message : String(error);
            appendMessage("assistant", message);
            showNotification(message, "error");
        } finally {
            setSending(false);
        }
    }

    async function confirmAction(actionId: string, apply: boolean): Promise<void> {
        try {
            const path = `/api/agent/actions/${encodeURIComponent(actionId)}/${apply ? "confirm" : "reject"}`;
            const { response, data } = await TrpgApi.requestWithResponse<ApiResponse<unknown>>(path, { method: "POST", body: {} });
            if (!response.ok || !data?.success) throw new Error(data?.message || `HTTP ${response.status}`);
            showNotification(
                apply
                    ? t("scenario.narrative.agent.applied", "已应用到草稿，请检查后“保存并发布”。")
                    : t("scenario.narrative.agent.rejected", "已忽略该改动。"),
                "success",
            );
            const scenarioId = Number.parseInt(currentScenarioId(), 10);
            if (apply && Number.isFinite(scenarioId) && scenarioId > 0) {
                await window.ScenarioNarrative?.load(scenarioId);
                await window.ScenarioTriggers?.load(scenarioId);
            }
        } catch (error) {
            showNotification(error instanceof Error ? error.message : String(error), "error");
        }
    }

    function init(): void {
        byId("scenarioAgentToggle")?.addEventListener("click", openPanel);
        byId("scenarioAgentClose")?.addEventListener("click", closePanel);
        byId("scenarioAgentForm")?.addEventListener("submit", (event) => {
            event.preventDefault();
            void send();
        });
        byId("scenarioAgentMessages")?.addEventListener("click", (event) => {
            const target = event.target as HTMLElement;
            const confirmButton = target.closest<HTMLElement>("[data-confirm-action]");
            if (confirmButton?.dataset.confirmAction) {
                void confirmAction(confirmButton.dataset.confirmAction, true);
                return;
            }
            const rejectButton = target.closest<HTMLElement>("[data-reject-action]");
            if (rejectButton?.dataset.rejectAction) {
                void confirmAction(rejectButton.dataset.rejectAction, false);
            }
        });
        document.addEventListener("scenario:node-selected", (event) => {
            const detail = (event as CustomEvent<{ nodeId?: string; title?: string }>).detail || {};
            state.nodeId = detail.nodeId || "";
            state.nodeTitle = detail.title || "";
            updateContext();
        });
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
    } else {
        init();
    }
})(window);
