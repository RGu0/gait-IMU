/**
 * Renderer-owned terminal adapter contract.
 *
 * Implementations provide asynchronous `snapshot()`,
 * `login({ organization, password })`, `logout()`, and `recheckDevices()` methods.
 */
export const terminalAdapterContract = Object.freeze([
  "snapshot",
  "login",
  "logout",
  "recheckDevices",
]);
