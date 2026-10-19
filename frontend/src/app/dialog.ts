// 统一的「应用内弹窗」：确认与文本输入。
//
// 项目规则禁止使用浏览器原生 alert() / confirm() / prompt()（外观无法统一、阻塞
// 主线程且易被浏览器拦截）。所有需要用户确认或输入的交互都走这里，基于 Bootstrap
// 模态框渲染，返回 Promise 便于在 async 流程中 await。
(function initializeDialog(global: Window) {
    "use strict";

    interface DialogOptions {
        title?: string;
        message: string;
        confirmText?: string;
        cancelText?: string;
        danger?: boolean;
    }

    interface PromptOptions extends DialogOptions {
        defaultValue?: string;
    }

    let resolver: ((value: string | null) => void) | null = null;

    function escapeHtml(value: string): string {
        return String(value ?? "")
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;");
    }

    function open(options: DialogOptions, withInput: boolean, defaultValue: string): Promise<string | null> {
        return new Promise<string | null>((resolve) => {
            resolver = resolve;
            const confirmClass = options.danger ? "btn-danger" : "btn-primary";
            const modalElement = document.createElement("div");
            modalElement.className = "modal fade";
            modalElement.tabIndex = -1;
            modalElement.innerHTML = `
                <div class="modal-dialog modal-dialog-centered">
                    <div class="modal-content">
                        <div class="modal-header">
                            <h5 class="modal-title">${escapeHtml(options.title || "请确认")}</h5>
                            <button type="button" class="btn-close" data-bs-dismiss="modal" aria-label="Close"></button>
                        </div>
                        <div class="modal-body">
                            <p class="mb-0" style="white-space: pre-line;">${escapeHtml(options.message)}</p>
                            ${withInput ? '<input type="text" class="form-control mt-3" data-dialog-input>' : ""}
                        </div>
                        <div class="modal-footer">
                            <button type="button" class="btn btn-secondary" data-dialog-cancel>${escapeHtml(options.cancelText || "取消")}</button>
                            <button type="button" class="btn ${confirmClass}" data-dialog-confirm>${escapeHtml(options.confirmText || "确定")}</button>
                        </div>
                    </div>
                </div>`;
            document.body.appendChild(modalElement);

            const input = modalElement.querySelector<HTMLInputElement>("[data-dialog-input]");
            if (input) input.value = defaultValue;

            let settled = false;
            const finish = (value: string | null): void => {
                if (settled) return;
                settled = true;
                const resolveCurrent = resolver;
                resolver = null;
                if (resolveCurrent) resolveCurrent(value);
                instance.hide();
            };

            modalElement.querySelector("[data-dialog-confirm]")?.addEventListener("click", () => {
                finish(withInput ? (input?.value ?? "") : "confirmed");
            });
            modalElement.querySelector("[data-dialog-cancel]")?.addEventListener("click", () => {
                finish(null);
            });
            input?.addEventListener("keydown", (event) => {
                if (event.key === "Enter") {
                    event.preventDefault();
                    finish(input.value);
                }
            });
            // 通过 ESC / 遮罩 / 关闭按钮关闭时视为取消。
            modalElement.addEventListener("hidden.bs.modal", () => {
                finish(null);
                modalElement.remove();
            });

            const instance = new bootstrap.Modal(modalElement, { backdrop: "static" });
            instance.show();
            if (input) window.setTimeout(() => input.focus(), 150);
        });
    }

    global.TrpgDialog = {
        confirm(options: DialogOptions): Promise<boolean> {
            return open(options, false, "").then((value) => value !== null);
        },
        prompt(options: PromptOptions): Promise<string | null> {
            return open(options, true, options.defaultValue || "");
        },
    };
})(window);
