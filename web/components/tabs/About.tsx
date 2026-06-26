"use client";

export function About() {
  return (
    <div>
      <div className="tabhead">
        <h1>About</h1>
        <div className="sub">What this is, how it works, and why it&apos;s honest about having no edge.</div>
      </div>
      <div className="panel">
        <div className="prose">
          <p><span className="em">This is a paper-trading lab.</span> A model watches real 2026 World Cup matches in-play and decides whether the live Kalshi prices look mispriced. It places fake-money bets so you can watch its reasoning play out — and judge it honestly. No real money, ever.</p>

          <h3>How a game runs</h3>
          <p>For each recorded match we replay it tick-by-tick. At every moment the model estimates win / draw / loss probabilities (a Dixon-Coles in-play model fed by score, time, red cards, and a shot-based expected-goals proxy). It compares that to the market&apos;s de-vigged price. If its edge clears a threshold it sizes a fractional-Kelly bet; otherwise it waits.</p>

          <h3>Taken vs considered</h3>
          <p>The <span className="em">Bets</span> tab shows everything: bets actually <span className="em">taken</span>, and ones <span className="em">considered</span> but skipped — because the market was on a re-bet cooldown, the sized bet was too small, a strategy filter blocked it, or a stronger edge that moment won the one-bet-per-tick slot.</p>

          <h3>CLV — the honest scoreboard</h3>
          <p>Closing-line value asks: did a bet enter at a better price than the market&apos;s opening line? It&apos;s the metric sharp bettors trust because it doesn&apos;t depend on whether one game happened to win. This model&apos;s CLV sits <span className="em">slightly negative</span> — it&apos;s well-calibrated, but it does <span className="em">not</span> beat Kalshi. Positive paper P&amp;L is variance, not edge.</p>

          <h3>The sandbox finding</h3>
          <p>Slicing CLV by trade type showed the bleed is concentrated in <span className="em">backs</span> and <span className="em">late-game</span> entries, while fades enter roughly fair. The Advanced <span className="em">Sandbox</span> lets you toggle those filters and watch the pooled CLV move toward zero — though it never turns positive. That&apos;s the honest takeaway: a tool to learn from, not a tipster to follow.</p>

          <h3>Data</h3>
          <p>Model probabilities and market quotes are precomputed from a 24/7 recorder, then the whole betting policy runs in your browser. Everything you see is reproduced from the same engine that&apos;s validated against the Python backtest.</p>
        </div>
      </div>
    </div>
  );
}
