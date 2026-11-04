// noVNC ships no type declarations; this covers the part of RFB the viewer uses.
declare module '@novnc/novnc' {
  export default class RFB extends EventTarget {
    constructor(target: HTMLElement, url: string, options?: { wsProtocols?: string[] })
    scaleViewport: boolean
    resizeSession: boolean
    viewOnly: boolean
    /** CSS background behind the scaled screen. */
    background: string
    focus(): void
    disconnect(): void
    addEventListener(type: 'connect', listener: (event: CustomEvent) => void): void
    addEventListener(type: 'disconnect', listener: (event: CustomEvent<{ clean: boolean }>) => void): void
  }
}
