import proofArtifact from "../../docs/evidence/wheel-csp-proof-2026-09-03.json";

type RecordValue = Record<string, unknown>;

const PAPER_TRADING_BASE = "https://paper-api.alpaca.markets/v2";
const OPTIONS_DATA_BASE = "https://data.alpaca.markets/v1beta1";
const PUBLIC_ORIGIN = "https://bigonewd.github.io";
const LOCAL_ORIGIN = "http://localhost:5173";
const WHEEL_SYMBOL = String(proofArtifact.occ_symbol);
const RISK_CAPITAL_BASE = "99999.94";
const RESERVED_CAPITAL = String(proofArtifact.reserved_assignment_capital);

function isRecord(value: unknown): value is RecordValue {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function asRecords(value: unknown): RecordValue[] {
  if (!Array.isArray(value) || !value.every(isRecord)) throw new Error("invalid broker collection");
  return value;
}

function numeric(value: unknown, digits = 2): string | null {
  if (typeof value !== "string" && typeof value !== "number") return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed.toFixed(digits) : null;
}

function requiredNumeric(value: unknown): string {
  const normalized = numeric(value);
  if (normalized === null) throw new Error("missing broker account metric");
  return normalized;
}

function side(value: unknown, qty: string | null): string {
  if (typeof value === "string" && value.trim()) return value.toUpperCase();
  return Number(qty) < 0 ? "SHORT" : "LONG";
}

function absoluteQuantity(value: unknown): string | null {
  if (typeof value !== "string" && typeof value !== "number") return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? Math.abs(parsed).toString() : null;
}

function responseHeaders(origin: string | null): HeadersInit {
  const headers: Record<string, string> = {
    "Cache-Control": "public, max-age=5, stale-while-revalidate=10",
    "Content-Type": "application/json; charset=utf-8",
    Vary: "Origin",
  };
  if (origin === PUBLIC_ORIGIN || origin === LOCAL_ORIGIN) {
    headers["Access-Control-Allow-Origin"] = origin;
  }
  return headers;
}

function json(body: RecordValue, status: number, origin: string | null): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: responseHeaders(origin),
  });
}

async function brokerGet<T>(url: string, keyId: string, secretKey: string): Promise<T> {
  const response = await fetch(url, {
    method: "GET",
    headers: {
      Accept: "application/json",
      "APCA-API-KEY-ID": keyId,
      "APCA-API-SECRET-KEY": secretKey,
    },
  });
  if (!response.ok) throw new Error(`broker read ${response.status}`);
  return (await response.json()) as T;
}

function sanitizePosition(position: RecordValue) {
  const qty = absoluteQuantity(position.qty);
  return {
    symbol: typeof position.symbol === "string" ? position.symbol : "UNKNOWN",
    qty,
    side: side(position.side, qty),
    marketValue: numeric(position.market_value),
    unrealizedPL: numeric(position.unrealized_pl),
  };
}

function quoteFromPayload(payload: unknown) {
  if (!isRecord(payload) || !isRecord(payload.quotes)) return null;
  const rawQuote = payload.quotes[WHEEL_SYMBOL];
  if (!isRecord(rawQuote)) return null;
  const bid = numeric(rawQuote.bp ?? rawQuote.bid_price, 4);
  const ask = numeric(rawQuote.ap ?? rawQuote.ask_price, 4);
  const timestamp = typeof rawQuote.t === "string" ? rawQuote.t : typeof rawQuote.timestamp === "string" ? rawQuote.timestamp : null;
  if (bid === null && ask === null) return null;
  return { bid, ask, timestamp, feed: "INDICATIVE" as const };
}

async function buildMetrics(keyId: string, secretKey: string) {
  const [account, positionsPayload, ordersPayload, clock] = await Promise.all([
    brokerGet<RecordValue>(`${PAPER_TRADING_BASE}/account`, keyId, secretKey),
    brokerGet<unknown>(`${PAPER_TRADING_BASE}/positions`, keyId, secretKey),
    brokerGet<unknown>(`${PAPER_TRADING_BASE}/orders?status=open&limit=500`, keyId, secretKey),
    brokerGet<RecordValue>(`${PAPER_TRADING_BASE}/clock`, keyId, secretKey),
  ]);

  let quote = null;
  try {
    const query = new URLSearchParams({ feed: "indicative", symbols: WHEEL_SYMBOL });
    const payload = await brokerGet<unknown>(`${OPTIONS_DATA_BASE}/options/quotes/latest?${query}`, keyId, secretKey);
    quote = quoteFromPayload(payload);
  } catch {
    quote = null;
  }

  const rawPositions = asRecords(positionsPayload);
  const positions = rawPositions.map(sanitizePosition);
  const wheelPosition = positions.find((position) => position.symbol === WHEEL_SYMBOL);
  const wheelPositionRaw = rawPositions.find((position) => position.symbol === WHEEL_SYMBOL);
  const reserved = Number(RESERVED_CAPITAL);
  const riskBase = Number(RISK_CAPITAL_BASE);
  const quoteMid = quote && quote.bid !== null && quote.ask !== null
    ? ((Number(quote.bid) + Number(quote.ask)) / 2).toFixed(4)
    : null;

  return {
    mode: "PAPER" as const,
    status: "online" as const,
    updatedAt: new Date().toISOString(),
    account: {
      cash: requiredNumeric(account.cash),
      equity: requiredNumeric(account.equity),
      optionsBuyingPower: numeric(account.options_buying_power),
    },
    market: {
      isOpen: clock.is_open === true,
    },
    capital: {
      riskCapitalBase: RISK_CAPITAL_BASE,
      reservedCapital: reserved.toFixed(2),
      availableCapital: (riskBase - reserved).toFixed(2),
      capitalUsedPercent: ((reserved / riskBase) * 100).toFixed(4),
    },
    positions,
    wheelContract: {
      symbol: WHEEL_SYMBOL,
      present: Boolean(wheelPosition),
      qty: wheelPosition?.qty ?? null,
      side: wheelPosition?.side ?? null,
      currentPrice: wheelPosition
        ? numeric(wheelPositionRaw?.current_price, 4) ?? quoteMid
        : null,
      marketValue: wheelPosition?.marketValue ?? null,
      unrealizedPL: wheelPosition?.unrealizedPL ?? null,
    },
    quote,
    openOrdersCount: asRecords(ordersPayload).length,
  };
}

export default async function handler(request: Request): Promise<Response> {
  const origin = request.headers.get("origin");
  if (request.method !== "GET") {
    return json({ status: "method_not_allowed" }, 405, origin);
  }

  const keyId = process.env.APCA_API_KEY_ID?.trim();
  const secretKey = process.env.APCA_API_SECRET_KEY?.trim();
  if (!keyId || !secretKey) {
    return json({ mode: "PAPER", status: "offline", updatedAt: new Date().toISOString(), error: "Broker metrics temporarily unavailable" }, 503, origin);
  }

  try {
    return json(await buildMetrics(keyId, secretKey), 200, origin);
  } catch {
    return json({ mode: "PAPER", status: "offline", updatedAt: new Date().toISOString(), error: "Broker metrics temporarily unavailable" }, 503, origin);
  }
}
