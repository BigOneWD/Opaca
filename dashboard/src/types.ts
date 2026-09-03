export type NumericValue = string | number | null;

export interface MetricsPosition {
  symbol: string;
  qty: string;
  side: string;
  marketValue: NumericValue;
  unrealizedPL: NumericValue;
}

export interface MetricsResponse {
  mode: "PAPER";
  status: "online";
  updatedAt: string;
  account: {
    cash: NumericValue;
    equity: NumericValue;
    optionsBuyingPower: NumericValue;
  };
  market: {
    isOpen: boolean;
  };
  capital: {
    riskCapitalBase: NumericValue;
    reservedCapital: NumericValue;
    availableCapital: NumericValue;
    capitalUsedPercent: NumericValue;
  };
  positions: MetricsPosition[];
  wheelContract: {
    symbol: string;
    present: boolean;
    qty: NumericValue;
    side: string | null;
    currentPrice: NumericValue;
    marketValue: NumericValue;
    unrealizedPL: NumericValue;
  };
  quote: {
    bid: NumericValue;
    ask: NumericValue;
    timestamp: string | null;
    feed: "INDICATIVE";
  } | null;
  openOrdersCount: number;
}
