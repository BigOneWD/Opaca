import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { spawnSync } from "node:child_process";

const dashboardRoot = fileURLToPath(new URL("..", import.meta.url));
const repoRoot = join(dashboardRoot, "..");
const srcRoot = join(dashboardRoot, "src");
const functionPath = join(repoRoot, "netlify", "functions", "metrics.ts");
const distRoot = join(dashboardRoot, "dist");

function readTree(root) {
  const result = spawnSync("rg", ["--files", root], { encoding: "utf8" });
  if (result.status !== 0 && result.status !== 1) throw new Error(result.stderr);
  return result.stdout.trim().split("\n").filter(Boolean).map((file) => readFileSync(file, "utf8")).join("\n");
}

function assert(condition, message) {
  if (!condition) throw new Error(`SECURITY SCAN FAILED: ${message}`);
}

const frontendSource = readTree(srcRoot);
const frontendBundle = existsSync(distRoot) ? readTree(distRoot) : "";
const functionSource = readFileSync(functionPath, "utf8");
const forbiddenBrowserTokens = /APCA_API_KEY_ID|APCA_API_SECRET_KEY|VITE_APCA_|account_id|account_number|localStorage|sessionStorage/;
const forbiddenMutationTokens = /submit_order|cancel|replace|exercise|close_position|DELETE|POST|PATCH|PUT/;

assert(!forbiddenBrowserTokens.test(frontendSource), "frontend source contains a credential, private identity, or browser storage token");
assert(!forbiddenBrowserTokens.test(frontendBundle), "production bundle contains a credential, private identity, or browser storage token");
assert(!/paper-api\.alpaca\.markets|data\.alpaca\.markets/.test(frontendBundle), "production bundle contains direct Alpaca endpoints");
assert(!forbiddenMutationTokens.test(functionSource), "metrics function contains a broker mutation token");
assert(functionSource.includes('method: "GET"'), "metrics function does not pin broker reads to GET");
assert(!/return\s+(raw|account|positions|orders)\b/.test(functionSource), "metrics function returns a raw broker object");
assert(!functionSource.includes("account_id") && !functionSource.includes("account_number"), "metrics DTO names private account identity fields");

for (const ignoredPath of [".env", "backend/opaca-wheel-paper.db"]) {
  const result = spawnSync("git", ["check-ignore", "-q", ignoredPath], { cwd: repoRoot });
  assert(result.status === 0, `${ignoredPath} is not ignored`);
}

console.log("Security scan PASS: browser credentials/private identity absent, function is GET-only, raw broker objects are not returned, and local secrets/runtime state remain ignored.");
