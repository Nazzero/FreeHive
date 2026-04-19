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

// Remove stale sidecar binary before building so a failed/interrupted
// build never packages an old artifact.
const sidecarExe = path.join(
  repoRoot, "src-tauri", "sidecar",
  process.platform === "win32" ? "freehive-backend.exe" : "freehive-backend"
);
if (fs.existsSync(sidecarExe)) {
  const stat = fs.statSync(sidecarExe);
  if (stat.isDirectory()) {
    fs.rmSync(sidecarExe, { recursive: true, force: true });
  } else {
    fs.unlinkSync(sidecarExe);
  }
  console.log("[build:sidecar] Removed stale sidecar before rebuild.");
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

  lastStatus = typeof res.status === "number" ? res.status : 1;

  if (lastStatus !== 0) {
    if (fs.existsSync(sidecarExe)) {
      const s = fs.statSync(sidecarExe);
      if (s.isDirectory()) fs.rmSync(sidecarExe, { recursive: true, force: true });
      else fs.unlinkSync(sidecarExe);
    }
    console.error("[build:sidecar] ❌ Build failed — partial output cleaned up.");
  } else {
    console.log("[build:sidecar] ✅ Sidecar built successfully.");
  }

  process.exit(lastStatus);
}

console.error(
  "No Python runtime found. Install Python 3 and retry `npm run build:sidecar`."
);
process.exit(lastStatus);
