const fixture = {
  operator: null,
  deviceSummary: {
    ready: false,
    issues: ["左侧传感器需要重新检查"],
  },
  uploadSummary: {
    pending: 0,
    uploaded: 12,
  },
  recentRecords: [
    { subjectLabel: "**1234", status: "已完成" },
    { subjectLabel: "**2345", status: "已完成" },
    { subjectLabel: "**3456", status: "待上传" },
    { subjectLabel: "**4567", status: "已完成" },
    { subjectLabel: "**5678", status: "已完成" },
  ],
};

function copiedSnapshot() {
  const snapshot = {
    ...fixture,
    operator: fixture.operator && { ...fixture.operator },
    deviceSummary: {
      ...fixture.deviceSummary,
      issues: [...fixture.deviceSummary.issues],
    },
    uploadSummary: { ...fixture.uploadSummary },
    recentRecords: fixture.recentRecords.map((record) => ({ ...record })),
  };

  if (snapshot.operator) {
    Object.freeze(snapshot.operator);
  }
  Object.freeze(snapshot.deviceSummary.issues);
  Object.freeze(snapshot.deviceSummary);
  Object.freeze(snapshot.uploadSummary);
  for (const record of snapshot.recentRecords) {
    Object.freeze(record);
  }
  Object.freeze(snapshot.recentRecords);
  return Object.freeze(snapshot);
}

export const mockTerminalAdapter = Object.freeze({
  async snapshot() {
    return copiedSnapshot();
  },

  async login({ organization, password }) {
    if (!organization || !password) {
      throw new Error("组织和密码均为必填项");
    }

    fixture.operator = { organization };
    return copiedSnapshot();
  },

  async recheckDevices() {
    fixture.deviceSummary.ready = true;
    fixture.deviceSummary.issues = [];
    return copiedSnapshot();
  },
});
