/**
 * noVNC ships no type declarations, so this declares the slice of RFB the console uses:
 * connect to a display, toggle who may send input, disconnect.
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
