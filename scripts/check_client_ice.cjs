// Execute the pinned client's actual batcher and its tests without editing it.
// Node 24: node --experimental-transform-types scripts/check_client_ice.cjs PATH [--write]
const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { spawnSync } = require("node:child_process");

async function main() {
  const root = path.resolve(process.argv[2]);
  const source = path.join(root, "packages/client/src/rtc/IceCandidateBatcher.ts");
  const { IceCandidateBatcher, ICE_BATCH_INTERVAL_MS, ICE_BATCH_MAX_SIZE } = require(source);
  assert.equal(ICE_BATCH_INTERVAL_MS, 25);
  assert.equal(ICE_BATCH_MAX_SIZE, 16);
  const candidate = (i) => ({
    candidate: `candidate:${i} 1 udp 1 192.0.2.1 ${4000 + i} typ host`,
    sdpMid: "0", sdpMLineIndex: 0, usernameFragment: "sg08",
  });
  const cases = [];
  for (const [name, count, complete] of [["single", 1, false], ["sixteen", 16, false], ["final-three", 19, true], ["empty-complete", 0, true]]) {
    const batches = [];
    const timers = new Map();
    let next = 0;
    const batcher = new IceCandidateBatcher({
      now: () => 0,
      setTimer: (callback) => { const id = ++next; timers.set(id, callback); return id; },
      clearTimer: (id) => timers.delete(id),
      send: async (candidates, completed) => batches.push({ candidates, completed }),
    });
    for (let i = 1; i <= count; i++) batcher.add(candidate(i));
    if (complete) await batcher.complete();
    else await batcher.flush("timer");
    cases.push({ name, batches });
  }
  assert.deepEqual(cases.map(c => c.batches.map(b => [b.candidates.length, b.completed])), [
    [[1, false]], [[16, false]], [[16, false], [3, true]], [[0, true]],
  ]);
  const fixture = {
    repository: "Leydotpy/synq.js",
    revision: "6521187857da9f8d9223e67b445d71114e935ab0",
    source_sha256: crypto.createHash("sha256").update(fs.readFileSync(source)).digest("hex"),
    cases,
  };
  const destination = path.join(__dirname, "../tests/fixtures/client-ice.json");
  if (process.argv.includes("--write")) fs.writeFileSync(destination, JSON.stringify(fixture, null, 2) + "\n");
  else assert.deepEqual(fixture, JSON.parse(fs.readFileSync(destination, "utf8")));

  // Run the upstream suite against the same actual source, redirecting only its
  // compiled-module import into a disposable test file outside the client tree.
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "sg09-client-"));
  try {
    const original = fs.readFileSync(path.join(root, "packages/client/test/ice-candidate-batcher.test.cjs"), "utf8");
    const compiledImport = '"../../../node_modules/.cache/synq-meet-client-test/rtc/IceCandidateBatcher.js"';
    assert.ok(original.includes(compiledImport));
    const test = path.join(directory, "batcher.test.cjs");
    fs.writeFileSync(test, original.replace(compiledImport, JSON.stringify(source)));
    const result = spawnSync(process.execPath, ["--experimental-transform-types", "--test", test], { stdio: "inherit" });
    assert.equal(result.status, 0);
  } finally {
    fs.rmSync(directory, { recursive: true, force: true });
  }
  console.log("Pinned client batcher: four fixtures match; upstream tests passed.");
}
main().catch(error => { console.error(error); process.exitCode = 1; });
