import { useCallback, useEffect, useMemo, useState } from "react";
import { capitalDerived, verifiedProof } from "./proof";
import type { MetricsResponse, NumericValue } from "./types";

const metricsUrl =
  import.meta.env.VITE_METRICS_API_URL?.trim() || "/.netlify/functions/metrics";

const proofTimeline = [
  "AI INTENT",
  "POLICY PASS",
  "AUTO",
  "$5,800 RESERVED",
  "ONE PAPER SUBMIT",
  "FILLED",
  "SHORT_PUT_OPEN",
  "RECONCILED",
];

function useLiveMetrics() {
  const [data, setData] = useState<MetricsResponse | null>(null);
  const [error, setError] = useState(false);
  const [loading, setLoading] = useState(true);

  const refresh = useCallback(async (signal?: AbortSignal) => {
    try {
      const response = await fetch(metricsUrl, {
        headers: { Accept: "application/json" },
        signal,
      });
      if (!response.ok) throw new Error("metrics unavailable");
      const payload = (await response.json()) as Partial<MetricsResponse>;
      if (payload.status !== "online") throw new Error("metrics offline");
      setData(payload as MetricsResponse);
      setError(false);
    } catch (cause) {
      if (cause instanceof DOMException && cause.name === "AbortError") return;
      setError(true);
    } finally {
      if (!signal?.aborted) setLoading(false);
    }
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    void refresh(controller.signal);
    const interval = window.setInterval(() => void refresh(), 7000);
    return () => {
      controller.abort();
      window.clearInterval(interval);
    };
  }, [refresh]);

  return { data, error, loading, refresh: () => void refresh() };
}

function money(value: NumericValue, digits = 2) {
  if (value === null || value === undefined || value === "") return "—";
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return "—";
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  }).format(numeric);
}

function quantity(value: NumericValue) {
  if (value === null || value === undefined || value === "") return "—";
  return String(value);
}

function secondsAgo(updatedAt: string | undefined) {
  if (!updatedAt) return "—";
  const age = Math.max(0, Math.round((Date.now() - Date.parse(updatedAt)) / 1000));
  return `${age}s ago`;
}

function Icon({ name }: { name: "pulse" | "lock" | "arrow" | "refresh" }) {
  if (name === "pulse") {
    return (
      <svg aria-hidden="true" className="icon" viewBox="0 0 24 24">
        <path d="M3 12h4l2.1-6 4.2 12L15.5 12H21" />
      </svg>
    );
  }
  if (name === "lock") {
    return (
      <svg aria-hidden="true" className="icon" viewBox="0 0 24 24">
        <rect x="5" y="10" width="14" height="10" rx="2" />
        <path d="M8 10V7a4 4 0 0 1 8 0v3M12 14v2" />
      </svg>
    );
  }
  if (name === "refresh") {
    return (
      <svg aria-hidden="true" className="icon" viewBox="0 0 24 24">
        <path d="M20 11a8 8 0 0 0-14.7-4L3 10M3 5v5h5M4 13a8 8 0 0 0 14.7 4L21 14m0 5v-5h-5" />
      </svg>
    );
  }
  return (
    <svg aria-hidden="true" className="icon arrow-icon" viewBox="0 0 24 24">
      <path d="M5 12h13M13 6l6 6-6 6" />
    </svg>
  );
}

function LiveBadge({ stale, offline }: { stale: boolean; offline: boolean }) {
  const label = offline ? (stale ? "STALE" : "LIVE API OFFLINE") : "LIVE";
  return (
    <span className={`live-badge ${offline ? "is-offline" : ""}`}>
      <span className="status-dot" /> {label}
    </span>
  );
}

function Metric({ label, value, detail, accent = false, muted = false }: {
  label: string;
  value: string;
  detail?: string;
  accent?: boolean;
  muted?: boolean;
}) {
  return (
    <div className={`metric ${accent ? "metric-accent" : ""}`}>
      <span className="metric-label">{label}</span>
      <span className={`metric-value ${muted ? "metric-muted" : ""}`}>{value}</span>
      {detail && <span className="metric-detail">{detail}</span>}
    </div>
  );
}

function SectionHeading({ index, title, detail }: { index: string; title: string; detail: string }) {
  return (
    <div className="section-heading">
      <span className="section-index">{index}</span>
      <div>
        <h2>{title}</h2>
        <p>{detail}</p>
      </div>
    </div>
  );
}

function App() {
  const { data, error, loading, refresh } = useLiveMetrics();
  const stale = Boolean(data && error);
  const offline = error;
  const liveContract = data?.wheelContract;
  const livePositionLabel = !data ? "UNKNOWN" : liveContract?.present ? "PRESENT" : "ABSENT";
  const liveSide = liveContract?.side || "—";
  const liveUpdated = secondsAgo(data?.updatedAt);
  const quoteMid = useMemo(() => {
    const bid = Number(data?.quote?.bid);
    const ask = Number(data?.quote?.ask);
    return Number.isFinite(bid) && Number.isFinite(ask) ? (bid + ask) / 2 : null;
  }, [data?.quote]);

  return (
    <div className="app-shell">
      <header className="topbar">
        <a className="brand" href="#top" aria-label="OPACA home">
          <span className="brand-mark" aria-hidden="true"><i /><i /><i /><i /></span>
          <span>OPACA</span>
        </a>
        <div className="topbar-meta">
          <span className="topbar-note">PUBLIC COMPETITION SURFACE</span>
          <span className="topbar-rule" />
          <span className="topbar-mode">PAPER / READ ONLY</span>
        </div>
      </header>

      <main id="top">
        <section className="hero section-wrap">
          <div className="hero-copy">
            <p className="eyebrow">LIVE PAPER METRICS</p>
            <h1>Risk-Governed<br /><em>Autonomous Wheel Agent</em></h1>
            <p className="hero-subtitle">AI reasons. Software enforces. Alpaca executes.</p>
            <p className="hero-description">A public, read-only view of the competition PAPER lane — with live broker facts kept separate from verified execution proof.</p>
          </div>
          <div className="hero-status-panel">
            <div className="hero-status-top">
              <LiveBadge stale={stale} offline={offline} />
              <button className="refresh-button" onClick={refresh} type="button" aria-label="Refresh live metrics">
                <Icon name="refresh" /> REFRESH
              </button>
            </div>
            <div className="status-ledger">
              <div><span>ENVIRONMENT</span><strong>PAPER</strong></div>
              <div><span>ACCESS</span><strong>READ ONLY</strong></div>
              <div><span>MARKET DATA</span><strong className="amber-text">INDICATIVE</strong></div>
            </div>
            <div className="updated-line">
              <span>LAST UPDATED</span>
              <strong>{loading && !data ? "CONNECTING" : liveUpdated}</strong>
              {offline && <span className="offline-inline">LIVE API OFFLINE</span>}
            </div>
          </div>
        </section>

        {offline && (
          <section className="offline-banner section-wrap" role="status">
            <span className="offline-marker" />
            <div>
              <strong>LIVE API OFFLINE</strong>
              <span>{stale ? "Showing the last fetched values as STALE." : "Live broker values are unavailable."}</span>
            </div>
            <button onClick={refresh} type="button">RETRY <Icon name="arrow" /></button>
          </section>
        )}

        <section className="metrics-section section-wrap">
          <SectionHeading index="01" title="Capital at a glance" detail="Live account facts alongside the verified assignment-capital envelope." />
          <div className="metrics-grid">
            <Metric label="CASH" value={money(data?.account.cash ?? null)} detail="LIVE BROKER STATE" />
            <Metric label="EQUITY" value={money(data?.account.equity ?? null)} detail="LIVE BROKER STATE" />
            <Metric label="RISK CAPITAL BASE" value={money(data?.capital.riskCapitalBase ?? verifiedProof.riskCapitalBase)} detail="VERIFIED BOOTSTRAP" accent />
            <Metric label="RESERVED CAPITAL" value={money(data?.capital.reservedCapital ?? verifiedProof.assignmentCapital)} detail="VERIFIED RESERVATION" accent />
            <Metric label="AVAILABLE CAPITAL" value={money(data?.capital.availableCapital ?? capitalDerived.available)} detail="BASE LESS VERIFIED RESERVATION" />
            <Metric label="CAPITAL USED %" value={data?.capital.capitalUsedPercent ? `${data.capital.capitalUsedPercent}%` : `${capitalDerived.usedPercent}%`} detail="ASSIGNMENT UTILIZATION" />
          </div>
        </section>

        <section className="state-section section-wrap">
          <div className="state-grid">
            <article className="panel live-panel">
              <div className="panel-kicker"><span className="panel-dot live-dot" /> LIVE BROKER STATE</div>
              <div className="panel-title-row"><h2>Current wheel position</h2><span className={`state-tag ${livePositionLabel === "PRESENT" ? "state-present" : ""}`}>{livePositionLabel}</span></div>
              <div className="contract-lockup">
                <span className="contract-underlying">{verifiedProof.underlying}</span>
                <span className="contract-symbol">{verifiedProof.occSymbol}</span>
              </div>
              <dl className="data-list">
                <div><dt>LIVE QTY / SIDE</dt><dd>{quantity(liveContract?.qty ?? null)} <small>{liveSide}</small></dd></div>
                <div><dt>MARKET VALUE</dt><dd>{money(liveContract?.marketValue ?? null)}</dd></div>
                <div><dt>UNREALIZED P/L</dt><dd className={Number(liveContract?.unrealizedPL) >= 0 ? "positive" : "negative"}>{money(liveContract?.unrealizedPL ?? null)}</dd></div>
                <div><dt>OPTION QUOTE</dt><dd>{quoteMid === null ? "—" : money(quoteMid)} <small>{data?.quote ? "MID / INDICATIVE" : "UNAVAILABLE"}</small></dd></div>
              </dl>
              <p className="panel-footnote">The browser reads a sanitized snapshot only. It has no broker credentials and no execution controls.</p>
            </article>

            <article className="panel proof-panel">
              <div className="panel-kicker"><span className="panel-dot proof-dot" /> VERIFIED EXECUTION PROOF</div>
              <div className="panel-title-row"><h2>XLF cash-secured put</h2><span className="state-tag state-verified">RECONCILED</span></div>
              <div className="proof-contract"><strong>{verifiedProof.occSymbol}</strong><span>{verifiedProof.expiry} · ${verifiedProof.strike} PUT · {verifiedProof.contracts} CONTRACT</span></div>
              <div className="proof-facts">
                <div><span>ASSIGNMENT CAPITAL</span><strong>{money(verifiedProof.assignmentCapital)}</strong></div>
                <div><span>VERIFIED AUTHORITY</span><strong>{verifiedProof.authority}</strong></div>
                <div><span>VERIFIED STATE</span><strong>{verifiedProof.state}</strong></div>
                <div><span>DATA FEED</span><strong className="amber-text">{verifiedProof.feed}</strong></div>
              </div>
              <div className="honesty-callout"><Icon name="lock" /><span>Historical proof is immutable. Current broker state is fetched separately above.</span></div>
            </article>
          </div>
        </section>

        <section className="timeline-section section-wrap">
          <SectionHeading index="02" title="The verified path" detail="One bounded PAPER execution, captured and reconciled." />
          <div className="timeline" aria-label="Verified XLF paper proof timeline">
            {proofTimeline.map((step, index) => (
              <div className="timeline-step" key={step}>
                <div className="timeline-node">{String(index + 1).padStart(2, "0")}</div>
                <span>{step}</span>
                {index < proofTimeline.length - 1 && <div className="timeline-connector"><Icon name="arrow" /></div>}
              </div>
            ))}
          </div>
          <div className="timeline-caption"><span>VERIFIED XLF PAPER PROOF</span><span>NOT A LIVE LIFECYCLE CLAIM</span></div>
        </section>

        <section className="controls-section section-wrap">
          <div className="controls-intro">
            <SectionHeading index="03" title="Risk / control" detail="The software boundary that governed the proof." />
            <div className="utilization"><span>VERIFIED ASSIGNMENT UTILIZATION</span><strong>5.8<small>%</small></strong><div className="utilization-bar"><span /></div></div>
          </div>
          <div className="controls-grid">
            <div className="control-rail"><span>HARD PER-NAME CAP</span><strong>25%</strong></div>
            <div className="control-rail"><span>AUTO PROPOSAL CAP</span><strong>10%</strong></div>
            <div className="control-rail"><span>AUTO AGGREGATE CAP</span><strong>20%</strong></div>
            <div className="checks"><span className="checks-label">VERIFIED EXECUTION CHECKS</span><strong>6 / 6 <small>PASS</small></strong><div className="check-dots">{["CHECK-17", "CHECK-18", "CHECK-19", "CHECK-20", "CHECK-21", "CHECK-22"].map((check) => <span key={check} title={check} />)}</div></div>
          </div>
        </section>

        <section className="system-section section-wrap">
          <SectionHeading index="04" title="System status" detail="Operational boundaries are visible, not implied." />
          <div className="system-table">
            <div><span>ALPACA TRADING API</span><strong className={offline ? "offline-text" : "positive"}>{offline ? "OFFLINE" : "CONNECTED"}</strong></div>
            <div><span>EXECUTION MODE</span><strong>PAPER ONLY</strong></div>
            <div><span>MCP BOUNDARY</span><strong>READ ONLY</strong></div>
            <div><span>MARKET DATA</span><strong className="amber-text">INDICATIVE</strong></div>
            <div><span>PRODUCTION-GRADE DATA</span><strong>NO</strong></div>
            <div><span>BROKER MUTATION UI</span><strong>DISABLED</strong></div>
            <div><span>LATEST PROOF</span><strong className="positive">RECONCILED</strong></div>
            <div><span>OPEN ORDERS</span><strong>{data ? data.openOrdersCount : "—"}</strong></div>
          </div>
        </section>

        <section className="disclaimer section-wrap">
          <div className="disclaimer-mark"><Icon name="pulse" /></div>
          <p>Competition PAPER execution used Alpaca <strong>INDICATIVE</strong> option data. It is not OPRA and is not presented as production-grade market data.</p>
        </section>
      </main>

      <footer className="footer section-wrap">
        <span>OPACA · RISK-GOVERNED AUTONOMOUS WHEEL AGENT</span>
        <span>PUBLIC READ-ONLY OBSERVABILITY · {new Date().getFullYear()}</span>
      </footer>
    </div>
  );
}

export default App;
