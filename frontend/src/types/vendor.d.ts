interface BootstrapModalInstance {
    show(): void;
    hide(): void;
}

interface BootstrapModalConstructor {
    new(element: Element, options?: { backdrop?: boolean | "static" }): BootstrapModalInstance;
    getInstance(element: Element | null): BootstrapModalInstance | null;
}

declare const bootstrap: {
    Modal: BootstrapModalConstructor;
};

interface MarkedParser {
    (content: string, options?: Record<string, unknown>): string;
    parse(content: string): string;
    setOptions(options: Record<string, unknown>): void;
}

declare const marked: MarkedParser;

interface DomPurifySanitizer {
    sanitize(dirty: string, config?: Record<string, unknown>): string;
}

declare const DOMPurify: DomPurifySanitizer;

interface KatexRenderer {
    render(expression: string, element: HTMLElement, options?: Record<string, unknown>): void;
    renderToString(expression: string, options?: Record<string, unknown>): string;
}

declare const katex: KatexRenderer;

interface SocketLike {
    connected: boolean;
    on(eventName: string, handler: (payload: unknown) => void): void;
    emit(eventName: string, payload?: unknown): void;
    disconnect(): void;
}

declare function io(options?: Record<string, unknown>): SocketLike;
