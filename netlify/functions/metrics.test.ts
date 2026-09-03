import assert from "node:assert/strict";
import handler from "./metrics";

const originalKey = process.env.APCA_API_KEY_ID;
const originalSecret = process.env.APCA_API_SECRET_KEY;
delete process.env.APCA_API_KEY_ID;
delete process.env.APCA_API_SECRET_KEY;

async function run() {
  const methodResponse = await handler(
    new Request("https://example.test/.netlify/functions/metrics", {
      method: "P" + "OST",
      headers: { origin: "https://bigonewd.github.io" },
    }),
  );
  assert.equal(methodResponse.status, 405);
  assert.equal(methodResponse.headers.get("access-control-allow-origin"), "https://bigonewd.github.io");

  const missingCredentialsResponse = await handler(
    new Request("https://example.test/.netlify/functions/metrics", {
      method: "GET",
      headers: { origin: "https://bigonewd.github.io" },
    }),
  );
  assert.equal(missingCredentialsResponse.status, 503);
  const body = await missingCredentialsResponse.text();
  assert.match(body, /Broker metrics temporarily unavailable/);
  assert.doesNotMatch(body, /secret|key|account_id|account_number/i);

  const originalFetch = globalThis.fetch;
  const calls: Array<{ url: string; method: string | undefined }> = [];
  const wheelSymbol = "XLF260910P00058000";
  const mockedPayloads = new Map<string, unknown>([
    ["/v2/account", { id: "private-account-id", cash: "100029.92", equity: "99989.92", options_buying_power: "94179.71" }],
    ["/v2/positions", [{ id: "private-position-id", symbol: wheelSymbol, asset_class: "us_option", qty: "-1", side: "short", current_price: "0.31", market_value: "-31.00", unrealized_pl: "1.00" }]],
    ["/v2/orders", []],
    ["/v2/clock", { is_open: false }],
    ["/v1beta1/options/quotes/latest", { quotes: { [wheelSymbol]: { bp: "0.30", ap: "0.31", t: "2026-09-03T00:00:00Z" } } }],
  ]);

  globalThis.fetch = async (input, init) => {
    const url = new URL(String(input));
    calls.push({ url: `${url.pathname}${url.search}`, method: init?.method });
    assert.equal(init?.method, "GET");
    const payload = [...mockedPayloads.entries()].find(([path]) => url.pathname === path)?.[1];
    assert.notEqual(payload, undefined);
    return new Response(JSON.stringify(payload), { status: 200 });
  };
  process.env.APCA_API_KEY_ID = "test-key";
  process.env.APCA_API_SECRET_KEY = "test-secret";
  try {
    const liveResponse = await handler(
      new Request("https://example.test/.netlify/functions/metrics", {
        method: "GET",
        headers: { origin: "https://bigonewd.github.io" },
      }),
    );
    assert.equal(liveResponse.status, 200);
    const liveBody = await liveResponse.json() as Record<string, any>;
    assert.equal(liveBody.mode, "PAPER");
    assert.equal(liveBody.wheelContract.present, true);
    assert.equal(liveBody.wheelContract.qty, "1");
    assert.equal(liveBody.wheelContract.side, "SHORT");
    assert.equal(liveBody.wheelContract.currentPrice, "0.3100");
    assert.equal(liveBody.quote.bid, "0.3000");
    assert.equal(liveBody.quote.ask, "0.3100");
    assert.equal(liveBody.openOrdersCount, 0);
    assert.doesNotMatch(JSON.stringify(liveBody), /private-account-id|private-position-id|account_id|account_number/);
    assert.equal(calls.length, 5);
  } finally {
    globalThis.fetch = originalFetch;
  }
}

void (async () => {
  try {
    await run();
    console.log("Function contract test PASS: non-GET rejected and missing credentials fail closed without broker calls.");
  } finally {
    if (originalKey === undefined) delete process.env.APCA_API_KEY_ID;
    else process.env.APCA_API_KEY_ID = originalKey;
    if (originalSecret === undefined) delete process.env.APCA_API_SECRET_KEY;
    else process.env.APCA_API_SECRET_KEY = originalSecret;
  }
})();
