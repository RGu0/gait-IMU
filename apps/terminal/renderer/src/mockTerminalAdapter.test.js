import { describe, expect, it } from "vitest";

import { mockTerminalAdapter } from "./mockTerminalAdapter.js";

describe("mockTerminalAdapter", () => {
  it("returns exactly five masked recent records", async () => {
    const snapshot = await mockTerminalAdapter.snapshot();

    expect(snapshot.recentRecords).toHaveLength(5);
    expect(snapshot.recentRecords[0].subjectLabel).toMatch(/^\*\*\d{4}$/);
  });

  it("returns immutable copied snapshots", async () => {
    const snapshot = await mockTerminalAdapter.snapshot();
    const nextSnapshot = await mockTerminalAdapter.snapshot();

    expect(snapshot).not.toBe(nextSnapshot);
    expect(snapshot.deviceSummary).not.toBe(nextSnapshot.deviceSummary);
    expect(Object.isFrozen(snapshot)).toBe(true);
    expect(Object.isFrozen(snapshot.deviceSummary)).toBe(true);
    expect(Object.isFrozen(snapshot.recentRecords)).toBe(true);
    expect(Object.isFrozen(snapshot.recentRecords[0])).toBe(true);
  });

  it("rejects login when either supplied value is empty", async () => {
    await expect(
      mockTerminalAdapter.login({ organization: "", password: "secret" }),
    ).rejects.toThrow();
    await expect(
      mockTerminalAdapter.login({ organization: "Clinic", password: "" }),
    ).rejects.toThrow();
  });

  it("returns an operator-bearing snapshot after successful login", async () => {
    const snapshot = await mockTerminalAdapter.login({
      organization: "Clinic",
      password: "secret",
    });

    expect(snapshot.operator).toBeTruthy();
  });

  it("moves the fixture from needs-attention to ready when rechecked", async () => {
    expect((await mockTerminalAdapter.snapshot()).deviceSummary.ready).toBe(false);
    await mockTerminalAdapter.recheckDevices();
    expect((await mockTerminalAdapter.snapshot()).deviceSummary.ready).toBe(true);
  });
});
