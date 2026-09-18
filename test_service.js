"use strict";

const { spawnSync } = require("node:child_process");

const suites = ["service_contract", "test_domain"];
let failed = false;

for (const suite of suites) {
  const result = spawnSync("python3", ["-m", "unittest", "-v", suite], { stdio: "inherit" });
  if (result.error) {
    console.error(result.error.message);
    process.exit(1);
  }
  if (result.status !== 0) {
    failed = true;
  }
}

process.exit(failed ? 1 : 0);
