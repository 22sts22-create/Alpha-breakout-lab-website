import type { Context, Config } from "@netlify/functions";

// ── Bible v1.0 Strategy Parameters ──────────────────────────────────────────
const MA_FAST    = 15;
const MA_ENTRY   = 65;
const MA_TREND   = 200;
const LOOKBACK   = 20;
const SIDEWAYS   = 0.005;
const TRAIL_ABOVE = 0.07;
const TRAIL_BELOW = 0.03;
const CAPITAL    = 4000;

const DYN_TRAIL: [number, number][] = [
  [0.30, 0.03],
  [0.20, 0.04],
  [0.10, 0.05],
];

interface Bar {
  date: string; open: number; high: number; low: number; close: number; volume: number;
}

interface Trade {
  entryDate: string; exitDate: string; entryPrice: number; exitPrice: number;
  shares: number; pnl: number; pnlPct: number; exitReason: string; win: boolean;
}

function sma(arr: number[], period: number): (number | null)[] {
  return arr.map((_, i) => {
    if (i < period - 1) return null;
    const slice = arr.slice(i - period + 1, i + 1);
    return slice.reduce((a, b) => a + b, 0) / period;
  });
}

function isSideways(ma200: (number | null)[], i: number): boolean {
  if (i < LOOKBACK) return true;
  const start = ma200[i - LOOKBACK];
  const end   = ma200[i];
  if (start == null || end == null) return true;
  return Math.abs(end - start) / start < SIDEWAYS;
}

function getTrailPct(entryPrice: number, peakPrice: number): number {
  const gain = (peakPrice - entryPrice) / entryPrice;
  for (const [threshold, pct] of DYN_TRAIL) {
    if (gain >= threshold) return pct;
  }
  return TRAIL_ABOVE;
}

function runBacktest(bars: Bar[]): { trades: Trade[]; equity: number[] } {
  const closes   = bars.map(b => b.close);
  const ma15arr  = sma(closes, MA_FAST);
  const ma65arr  = sma(closes, MA_ENTRY);
  const ma200arr = sma(closes, MA_TREND);
  const trades: Trade[] = [];
  const equity: number[] = [];
  let inTrade = false, entryPrice = 0, entryDate = "", shares = 0, peakPrice = 0, cash = CAPITAL, portfolio = CAPITAL;

  for (let i = 1; i < bars.length; i++) {
    const price = bars[i].close;
    const ma15  = ma15arr[i];
    const ma65  = ma65arr[i];
    const ma200 = ma200arr[i];
    if (ma15 == null || ma65 == null || ma200 == null) { equity.push(portfolio); continue; }

    if (!inTrade) {
      if (price > ma65 && ma15 > ma65 && !isSideways(ma200arr, i)) {
        shares = Math.floor(CAPITAL / price);
        if (shares > 0) { inTrade = true; entryPrice = price; entryDate = bars[i].date; peakPrice = price; cash = CAPITAL - shares * price; }
      }
    } else {
      if (price > peakPrice) peakPrice = price;
      const trailPct  = price >= ma200 ? getTrailPct(entryPrice, peakPrice) : TRAIL_BELOW;
      const trailStop = peakPrice * (1 - trailPct);
      let exitReason: string | null = null;
      if (ma15 < ma65) exitReason = "MA cross";
      else if (price <= trailStop) exitReason = `Trail stop (${(trailPct*100).toFixed(0)}%)`;
      if (exitReason) {
        const pnl = (price - entryPrice) * shares;
        trades.push({ entryDate, exitDate: bars[i].date, entryPrice: +entryPrice.toFixed(2), exitPrice: +price.toFixed(2), shares, pnl: +pnl.toFixed(2), pnlPct: +((price-entryPrice)/entryPrice*100).toFixed(2), exitReason, win: pnl > 0 });
        portfolio = CAPITAL + pnl; cash = portfolio; inTrade = false; peakPrice = 0;
      }
    }
    portfolio = inTrade ? cash + shares * price : cash;
    equity.push(+portfolio.toFixed(2));
  }
  return { trades, equity };
}

async function fetchBars(ticker: string): Promise<Bar[]> {
  const apiKey = Netlify.env.get("TWELVE_DATA_API_KEY");
  const url = `https://api.twelvedata.com/time_series?symbol=${ticker}&interval=30min&outputsize=5000&apikey=${apiKey}`;
  const res  = await fetch(url);
  const data = await res.json() as any;
  if (data.status === "error" || !data.values) throw new Error(data.message ?? "Twelve Data error");
  return (data.values as any[]).reverse().map(v => ({ date: v.datetime, open: parseFloat(v.open), high: parseFloat(v.high), low: parseFloat(v.low), close: parseFloat(v.close), volume: parseInt(v.volume ?? "0") }));
}

export default async (req: Request, _context: Context) => {
  const ticker = new URL(req.url).searchParams.get("ticker")?.toUpperCase().trim();
  if (!ticker) return Response.json({ error: "Missing ticker parameter" }, { status: 400 });
  try {
    const bars = await fetchBars(ticker);
    if (bars.length < MA_TREND) return Response.json({ error: `Not enough data for ${ticker}. Need ${MA_TREND} bars, got ${bars.length}.` }, { status: 400 });
    const { trades, equity } = runBacktest(bars);
    const wins = trades.filter(t => t.win), losses = trades.filter(t => !t.win);
    const totalPnl = trades.reduce((s, t) => s + t.pnl, 0);
    return Response.json({ ticker, bars: bars.map(b => ({ date: b.date, close: b.close })), equity, trades, summary: { totalTrades: trades.length, wins: wins.length, losses: losses.length, winRate: trades.length ? +(wins.length/trades.length*100).toFixed(1) : 0, totalPnl: +totalPnl.toFixed(2), returnPct: +(totalPnl/CAPITAL*100).toFixed(1), startCapital: CAPITAL, endCapital: +(CAPITAL+totalPnl).toFixed(2), avgWin: wins.length ? +(wins.reduce((s,t)=>s+t.pnl,0)/wins.length).toFixed(2) : 0, avgLoss: losses.length ? +(losses.reduce((s,t)=>s+t.pnl,0)/losses.length).toFixed(2) : 0 } });
  } catch (err: any) {
    return Response.json({ error: err.message ?? "Unknown error" }, { status: 500 });
  }
};

export const config: Config = { path: "/api/backtest" };
