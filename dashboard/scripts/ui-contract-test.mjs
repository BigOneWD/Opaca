import { readFileSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

const root = fileURLToPath(new URL("..", import.meta.url));
const app = readFileSync(join(root, "src", "App.tsx"), "utf8");
const proof = readFileSync(join(root, "src", "proof.ts"), "utf8");

const requiredText = [
  "Risk-Governed",
  "Autonomous Wheel Agent",
  "LIVE PAPER METRICS",
  "READ ONLY",
  "INDICATIVE",
  "LIVE BROKER STATE",
  "VERIFIED EXECUTION PROOF",
  "SHORT_PUT_OPEN",
  "RECONCILED",
  "VERIFIED EXECUTION CHECKS",
];

for (const text of requiredText) {
  if (!app.includes(text) && !proof.includes(text)) throw new Error(`UI contract missing: ${text}`);
}
if (!app.includes("7000")) throw new Error("UI contract missing seven-second refresh interval");
if (/localStorage|sessionStorage|VITE_APCA_|APCA_API_SECRET_KEY|APCA_API_KEY_ID/.test(app + proof)) {
  throw new Error("UI contract contains a private browser-side credential or storage surface");
}

console.log("UI contract test PASS: required public observability copy, refresh cadence, and browser boundary are present.");
