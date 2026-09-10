import '@testing-library/jest-dom/vitest'

// jsdom implements neither of these, and both are used by components under test:
// the floor-plan editor and the panorama canvas observe their container size, and the
// panorama canvas needs a WebGL context that jsdom cannot provide.
class ResizeObserverStub implements ResizeObserver {
  observe(): void {}
  unobserve(): void {}
  disconnect(): void {}
}
globalThis.ResizeObserver = globalThis.ResizeObserver ?? (ResizeObserverStub as never)

if (!HTMLCanvasElement.prototype.getContext) {
  HTMLCanvasElement.prototype.getContext = (() => null) as never
}

if (!globalThis.matchMedia) {
  globalThis.matchMedia = ((query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: () => {},
    removeListener: () => {},
    addEventListener: () => {},
    removeEventListener: () => {},
    dispatchEvent: () => false,
  })) as never
}

// Pointer capture is part of the Pointer Events spec that jsdom does not implement. The
// floor-plan editor and the panorama canvas both rely on it to keep receiving move events when
// the pointer leaves the element mid-drag.
if (!HTMLElement.prototype.setPointerCapture) {
  const captured = new Set<number>()
  HTMLElement.prototype.setPointerCapture = function setPointerCapture(id: number) {
    captured.add(id)
  }
  HTMLElement.prototype.releasePointerCapture = function releasePointerCapture(id: number) {
    captured.delete(id)
  }
  HTMLElement.prototype.hasPointerCapture = function hasPointerCapture(id: number) {
    return captured.has(id)
  }
}
