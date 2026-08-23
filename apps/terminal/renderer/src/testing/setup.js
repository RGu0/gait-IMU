import "@testing-library/jest-dom/vitest";

// jsdom ships no matchMedia. Without it, any component that asks about the
// viewport throws on render — which is how it surfaced: nineteen unrelated
// assertions went red at once because one screen could not mount.
//
// The stub reports "not matching" and never fires a change, so a test that
// cares about a breakpoint must pass the state in explicitly rather than lean
// on this. That is deliberate: a stub that guessed would let a test claim to
// exercise a breakpoint it never reached.
if (!window.matchMedia) {
  window.matchMedia = (query) => ({
    media: query,
    matches: false,
    onchange: null,
    addEventListener: () => {},
    removeEventListener: () => {},
    addListener: () => {},
    removeListener: () => {},
    dispatchEvent: () => false,
  });
}
