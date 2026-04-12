#!/usr/bin/env node
const { spawnSync } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");

const repoRoot = path.resolve(__dirname, "..");
const buildScript = path.join(repoRoot, "scripts", "build_backend_sidecar.py");

const venvPython = process.platform === "win32"
  ? path.join(repoRoot, "venv", "Scripts", "python.exe")
  : path.join(repoRoot, "venv", "bin", "python");

const candidates = [];
if (fs.existsSync(venvPython)) {
  candidates.push([venvPython, [buildScript]]);
}

if (process.platform === "win32") {
  candidates.push(["py", ["-3", buildScript]]);
  candidates.push(["python", [buildScript]]);
  candidates.push(["python3", [buildScript]]);
} else {
  candidates.push(["python3", [buildScript]]);
  candidates.push(["python", [buildScript]]);
}

let lastStatus = 1;
for (const [cmd, args] of candidates) {
  const res = spawnSync(cmd, args, {
    cwd: repoRoot,
    stdio: "inherit",
  });

  if (res.error && res.error.code === "ENOENT") {
    continue;
  }

  if (typeof res.status === "number") {
    lastStatus = res.status;
  }

  process.exit(lastStatus);
}

console.error(
  "No Python runtime found. Install Python 3 and retry `npm run build:sidecar`."
);
process.exit(lastStatus);
