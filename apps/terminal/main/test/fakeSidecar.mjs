// 一个可以被指挥着崩溃的假 sidecar。它说的是同一套 JSON Lines 协议。
// 用它而不是真 Python，是因为「崩溃」要能被精确地、反复地触发；真 sidecar 的
// 往返另有一条测试专门验。
import process from "node:process";

const mode = process.argv[2] ?? "ok";
if (mode === "crash-on-start") {
  process.stderr.write("fake sidecar: dying immediately\n");
  process.exit(9);
}

let buffer = "";
process.stdin.setEncoding("utf8");
process.stdin.on("data", (chunk) => {
  buffer += chunk;
  let index;
  while ((index = buffer.indexOf("\n")) >= 0) {
    const line = buffer.slice(0, index).trim();
    buffer = buffer.slice(index + 1);
    if (!line) continue;
    const message = JSON.parse(line);
    if (message.method === "die") {
      process.exit(9); // 不回应就死 —— 模拟请求在飞时崩溃
    }
    if (message.method === "emit") {
      process.stdout.write(
        `${JSON.stringify({ kind: "event", v: "1.0", topic: "session.tick", seq: 1, payload: { remainingSeconds: 7 } })}\n`,
      );
      continue;
    }
    process.stdout.write(
      `${JSON.stringify({ kind: "response", v: "1.0", id: message.id, status: "ok", result: { echoed: message.method } })}\n`,
    );
  }
});
