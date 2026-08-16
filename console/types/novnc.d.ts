/**
 * noVNC ships no type declarations. Rather than `any`, declare the slice of RFB
 * this console actually uses — which doubles as documentation of the handoff
 * contract: connect to a display, toggle who may send input, disconnect.
 */
declare module "@novnc/novnc" {
  export interface RFBOptions {
    credentials?: { username?: string; password?: string; target?: string };
    shared?: boolean;
    repeaterID?: string;
    wsProtocols?: string[];
  }

  export default class RFB extends EventTarget {
    constructor(target: HTMLElement, url: string | WebSocket, options?: RFBOptions);

    /** The control token, made visible. True = the operator may watch only. */
    viewOnly: boolean;
    scaleViewport: boolean;
    resizeSession: boolean;
    focusOnClick: boolean;

    disconnect(): void;
    focus(): void;
    blur(): void;
    sendCtrlAltDel(): void;
    clipboardPasteFrom(text: string): void;
  }
}
