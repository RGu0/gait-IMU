import path from "node:path";

import { describe, expect, it, vi } from "vitest";

import {
  defaultReportFileName,
  EXPORT_PDF_CHANNEL,
  exportReportPdf,
  PDF_OPTIONS,
  PRINT_CHANNEL,
  printReport,
  registerReportExport,
} from "../reportExport.js";

const NOW = new Date(2026, 8, 15, 9, 5);
const PDF = Buffer.from("%PDF-1.4 fake");

function fakes({ dialogResult = { canceled: false, filePath: "/Users/op/Documents/x.pdf" } } = {}) {
  return {
    webContents: { printToPDF: vi.fn(async () => PDF) },
    window: { id: 1 },
    dialog: { showSaveDialog: vi.fn(async () => dialogResult) },
    writeFile: vi.fn(async () => {}),
  };
}

describe("default file name carries no subject identifier", () => {
  it("is built from export time and the report id tail only", () => {
    expect(defaultReportFileName({ reportId: "20260915T010203Z-a1b2c3d4", now: NOW })).toBe(
      "步态报告-20260915-0905-a1b2c3d4.pdf",
    );
  });

  it("strips anything that could become a path out of the report id", () => {
    const name = defaultReportFileName({ reportId: "../../etc/pa ss", now: NOW });
    expect(name).toBe("步态报告-20260915-0905-etcpass.pdf");
    expect(name).not.toMatch(/[/\\]/);
  });

  it("falls back when there is no report id", () => {
    expect(defaultReportFileName({ now: NOW })).toBe("步态报告-20260915-0905-unknown.pdf");
  });

  it("ignores a subject label the renderer tries to pass", async () => {
    const f = fakes();
    await exportReportPdf({
      ...f,
      documentsDir: "/Users/op/Documents",
      meta: { reportId: "R-2026-0823-0031", subjectLabel: "**2781", suggestedName: "张三-2781" },
      now: NOW,
    });
    const { defaultPath } = f.dialog.showSaveDialog.mock.calls[0][1];
    // 用 path.join 拼期望值：Windows 上主进程拼出来的是反斜杠。
    expect(defaultPath).toBe(path.join("/Users/op/Documents", "步态报告-20260915-0905-08230031.pdf"));
    expect(defaultPath).not.toMatch(/2781|张三/);
  });
});

describe("export to PDF", () => {
  it("prints with A4 / background / CSS page size and writes where the operator chose", async () => {
    const f = fakes();
    const result = await exportReportPdf({ ...f, documentsDir: "/d", meta: { reportId: "abc" }, now: NOW });
    expect(f.webContents.printToPDF).toHaveBeenCalledWith(PDF_OPTIONS);
    expect(PDF_OPTIONS).toMatchObject({ pageSize: "A4", printBackground: true, preferCSSPageSize: true, margins: { marginType: "none" } });
    const [parent, opts] = f.dialog.showSaveDialog.mock.calls[0];
    expect(parent).toBe(f.window);
    expect(opts.filters).toEqual([{ name: "PDF", extensions: ["pdf"] }]);
    expect(f.writeFile).toHaveBeenCalledWith("/Users/op/Documents/x.pdf", PDF);
    expect(result).toEqual({ status: "saved", path: "/Users/op/Documents/x.pdf" });
  });

  it("adds the .pdf extension when the operator removed it", async () => {
    const f = fakes({ dialogResult: { canceled: false, filePath: "/d/report" } });
    const result = await exportReportPdf({ ...f, meta: {}, now: NOW });
    expect(result).toEqual({ status: "saved", path: "/d/report.pdf" });
  });

  it("reports a cancel as canceled and writes nothing", async () => {
    const f = fakes({ dialogResult: { canceled: true, filePath: "" } });
    expect(await exportReportPdf({ ...f, meta: {}, now: NOW })).toEqual({ status: "canceled" });
    expect(f.writeFile).not.toHaveBeenCalled();
  });

  it("surfaces a printToPDF failure without asking where to save", async () => {
    const f = fakes();
    f.webContents.printToPDF.mockRejectedValue(new Error("render crashed"));
    expect(await exportReportPdf({ ...f, meta: {}, now: NOW })).toEqual({ status: "error", message: "render crashed" });
    expect(f.dialog.showSaveDialog).not.toHaveBeenCalled();
  });

  it("surfaces a write failure instead of claiming it saved", async () => {
    const f = fakes();
    f.writeFile.mockRejectedValue(new Error("EACCES: permission denied"));
    expect(await exportReportPdf({ ...f, meta: {}, now: NOW })).toEqual({
      status: "error",
      message: "EACCES: permission denied",
    });
  });
});

describe("print", () => {
  const withCallback = (...args) => ({ print: vi.fn((_opts, cb) => cb(...args)) });

  it("prints with backgrounds", async () => {
    const webContents = withCallback(true, "");
    expect(await printReport({ webContents })).toEqual({ status: "printed" });
    expect(webContents.print.mock.calls[0][0]).toEqual({ printBackground: true });
  });

  it("tells a cancel apart from a failure", async () => {
    expect(await printReport({ webContents: withCallback(false, "cancelled") })).toEqual({ status: "canceled" });
    expect(await printReport({ webContents: withCallback(false, "failed") })).toEqual({ status: "error", message: "failed" });
  });

  it("turns a throw into an error result", async () => {
    const webContents = { print: vi.fn(() => { throw new Error("no printer"); }) };
    expect(await printReport({ webContents })).toEqual({ status: "error", message: "no printer" });
  });
});

describe("registration", () => {
  it("wires both channels to the sender's webContents", async () => {
    const handlers = {};
    const ipcMain = { handle: (channel, fn) => { handlers[channel] = fn; } };
    const f = fakes();
    registerReportExport({
      ipcMain,
      dialog: f.dialog,
      getWindow: () => f.window,
      getDocumentsDir: () => "/Users/op/Documents",
      writeFile: f.writeFile,
      clock: () => NOW,
    });
    expect(Object.keys(handlers).sort()).toEqual([EXPORT_PDF_CHANNEL, PRINT_CHANNEL].sort());

    const result = await handlers[EXPORT_PDF_CHANNEL]({ sender: f.webContents }, { reportId: "abc12345" });
    expect(result.status).toBe("saved");
    expect(f.dialog.showSaveDialog.mock.calls[0][1].defaultPath).toBe(
      path.join("/Users/op/Documents", "步态报告-20260915-0905-abc12345.pdf"),
    );

    const printing = { print: vi.fn((_o, cb) => cb(true)) };
    expect(await handlers[PRINT_CHANNEL]({ sender: printing })).toEqual({ status: "printed" });
  });
});
