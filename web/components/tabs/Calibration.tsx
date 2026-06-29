"use client";

// The honesty dashboard. Reads the manifest the Python export stamps (P1–P3): the
// fixed-stake edge headline (edge_eval), the Kelly cross-check (aggregate), the
// match-clustered CLV CIs + verdict, per-outcome calibration, capture coverage, and the
// reproducibility provenance. Core rule: a number whose CI brackets zero renders NEUTRAL
// (grey) — green/red is reserved for results that actually clear zero. We measure the
// edge; we don't sell it.

import type { ReactNode } from "react";
import type { Aggregate, Manifest } from "../../lib/data";

const OUTS: { key: string; label: string; color: string }[] = [
  { key: "home", label: "Home", color: "var(--home)" },
  { key: "draw", label: "Draw", color: "var(--draw)" },
  { key: "away", label: "Away", color: "var(--away)" },
];

const pp = (x: number, d = 3) => `${x >= 0 ? "+" : "−"}${Math.abs(x).toFixed(d)}`;
const pct = (x: number) => `${(x * 100).toFixed(0)}%`;

function verdictMeta(v: string) {
  if (v === "negative") return { color: "var(--red)", label: "negative edge" };
  if (v === "positive") return { color: "var(--green)", label: "positive edge" };
  return { color: "var(--muted)", label: "no demonstrated edge" };
}

/** Interval [lo,hi] + point estimate over a symmetric domain, with a 0 line. Colour
 *  follows the honesty rule: neutral when the interval brackets zero. */
function CiBar({ lo, hi, point, domain = 0.18 }: { lo: number; hi: number; point: number; domain: number }) {
  const xp = (v: number) => Math.max(0, Math.min(100, ((v + domain) / (2 * domain)) * 100));
  const bracketsZero = lo <= 0 && hi >= 0;
  const color = bracketsZero ? "var(--muted)" : hi < 0 ? "var(--red)" : "var(--green)";
  return (
    <div style={{ position: "relative", height: 28, background: "var(--bg2)", borderRadius: 8, border: "1px solid var(--border)" }}>
      <div style={{ position: "absolute", left: "50%", top: 0, bottom: 0, width: 1, background: "var(--border2)" }} />
      <div style={{ position: "absolute", left: `${xp(lo)}%`, width: `${xp(hi) - xp(lo)}%`, top: 9, height: 10, background: color, opacity: 0.5, borderRadius: 5 }} />
      <div style={{ position: "absolute", left: `${xp(point)}%`, top: 6, width: 2, height: 16, background: color, transform: "translateX(-1px)" }} />
      <span style={{ position: "absolute", left: 6, bottom: 2, fontSize: 9.5, color: "var(--faint)" }} className="mono">−{domain.toFixed(2)}</span>
      <span style={{ position: "absolute", left: "50%", bottom: 2, fontSize: 9.5, color: "var(--faint)", transform: "translateX(-50%)" }} className="mono">0</span>
      <span style={{ position: "absolute", right: 6, bottom: 2, fontSize: 9.5, color: "var(--faint)" }} className="mono">+{domain.toFixed(2)}</span>
    </div>
  );
}

function Pill({ color, children }: { color: string; children: ReactNode }) {
  return (
    <span style={{ display: "inline-flex", alignItems: "center", gap: 6, fontSize: 11.5, color, background: "color-mix(in srgb, currentColor 12%, transparent)", padding: "3px 9px", borderRadius: 20 }}>
      <span style={{ width: 7, height: 7, borderRadius: "50%", background: color }} />{children}
    </span>
  );
}

export function Calibration({ manifest }: { manifest: Manifest }) {
  const kelly = manifest.aggregate as unknown as Aggregate;
  const fixed = (manifest.edge_eval as unknown as Aggregate) ?? kelly; // fixed = the headline
  const cal = kelly.calibration;
  const cov = manifest.coverage_summary;
  const prov = manifest.provenance;
  const po = kelly.calibration_per_outcome;

  const fv = verdictMeta(fixed.edge_verdict);
  const kv = verdictMeta(kelly.edge_verdict);
  const wellCal = cal.ece < 0.05;
  // P&L: indistinguishable from zero when its CI brackets zero -> render neutral, never red.
  const pnlNoise = kelly.pnl_ci[0] <= 0 && kelly.pnl_ci[1] >= 0;

  const refs = [
    { label: "pre-off line", v: kelly.avg_clv_preoff, note: "the opening line — the primary, non-degenerate edge signal" },
    { label: "+5 min", v: kelly.avg_clv_5m, note: "in-play drift ~5 minutes after entry" },
    { label: "close", v: kelly.avg_clv, note: "vs the last tick — degenerate near full time (mostly restates who won)" },
  ];

  return (
    <div>
      <div className="tabhead">
        <h1>Calibration &amp; honesty</h1>
        <div className="sub">We measure the edge — we don&apos;t sell it. Every number below regenerates from the stamped code + data.</div>
      </div>

      {/* Verdict strip */}
      <div className="scoreboard" style={{ gridTemplateColumns: "repeat(3, 1fr)" }}>
        <div className="cell">
          <span className="k">Calibration · ECE</span>
          <span className="v" style={{ color: wellCal ? "var(--green)" : "var(--text)" }}>{cal.ece.toFixed(3)}</span>
          <span className="sub2">Brier {cal.brier.toFixed(3)} vs 0.667 uniform{wellCal ? " · well calibrated" : ""}</span>
        </div>
        <div className="cell">
          <span className="k">Edge · pre-off CLV (fixed stake)</span>
          <span className="v" style={{ color: fv.color }}>{pp(fixed.avg_clv_preoff)}</span>
          <span className="sub2 mono">95% CI [{pp(fixed.clv_ci_preoff[0])}, {pp(fixed.clv_ci_preoff[1])}] · {fixed.n_fills} fills</span>
        </div>
        <div className="cell">
          <span className="k">Sample power</span>
          <span className="v">{kelly.n_matches}</span>
          <span className="sub2">matches · {fixed.n_fills} fills · {fixed.n_clusters_preoff} clusters</span>
        </div>
      </div>

      <div style={{ display: "flex", gap: 8, margin: "10px 0 0", flexWrap: "wrap" }}>
        <Pill color={fv.color}>headline (fixed stake): {fv.label}</Pill>
        <Pill color="var(--muted)">P&amp;L indistinguishable from zero (t {kelly.t_stat.toFixed(2)})</Pill>
      </div>

      {/* Honest verdict */}
      <div className="panel" style={{ marginTop: 14 }}>
        <h2>The honest verdict</h2>
        <p style={{ color: "var(--muted)", lineHeight: 1.65, margin: 0 }}>
          The model reads games <span style={{ color: "var(--text)", fontWeight: 600 }}>well</span> — pooled ECE {cal.ece.toFixed(3)},
          Brier {cal.brier.toFixed(3)} vs 0.667 for a uniform guess. But it does <span style={{ color: "var(--text)", fontWeight: 600 }}>not beat the market</span>:
          fixed-stake pre-off CLV is <span style={{ color: fv.color }} className="mono">{pp(fixed.avg_clv_preoff)}</span>,
          95% CI <span className="mono">[{pp(fixed.clv_ci_preoff[0])}, {pp(fixed.clv_ci_preoff[1])}]</span> — a small but
          {fixed.edge_verdict === "negative" ? " statistically negative" : " indistinguishable"} edge (the Kelly cross-check agrees: <span className="mono">{pp(kelly.avg_clv_preoff)}</span>).
          Realized P&amp;L (<span className="mono" style={{ color: pnlNoise ? "var(--muted)" : undefined }}>{pp(kelly.realized_pnl, 2)}</span>) is statistical
          noise (t {kelly.t_stat.toFixed(2)}). A well-calibrated model with no positive edge — the expected, honest outcome.
        </p>
      </div>

      {/* Match-clustered CI — the centerpiece */}
      <div className="panel" style={{ marginTop: 12 }}>
        <h2>Is the edge distinguishable from zero?</h2>
        <p className="note" style={{ margin: "0 0 12px" }}>
          Match-clustered bootstrap — resamples whole matches, not fills (fills in one match are correlated), so the interval reflects the real sample size.
        </p>
        {[
          { tag: "fixed stake (headline)", a: fixed, v: fv },
          { tag: "Kelly (interactive sim)", a: kelly, v: kv },
        ].map((row) => (
          <div key={row.tag} style={{ marginBottom: 12 }}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 5 }}>
              <span style={{ fontSize: 12.5, color: "var(--muted)" }}>{row.tag}</span>
              <Pill color={row.v.color}>{row.v.label}</Pill>
            </div>
            <CiBar lo={row.a.clv_ci_preoff[0]} hi={row.a.clv_ci_preoff[1]} point={row.a.avg_clv_preoff} domain={0.18} />
          </div>
        ))}
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(280px, 1fr))", gap: 12, marginTop: 12 }}>
        {/* Per-outcome calibration */}
        <div className="panel" style={{ margin: 0 }}>
          <h2>Calibration by outcome</h2>
          <p className="note" style={{ margin: "0 0 12px" }}>Pooled ECE can hide per-class bias that cancels. Here it doesn&apos;t — each class&apos;s predicted vs actual:</p>
          {po
            ? OUTS.map(({ key, label, color }) => {
                const c = po[key];
                if (!c) return null;
                return (
                  <div key={key} style={{ marginBottom: 11 }}>
                    <div style={{ display: "flex", justifyContent: "space-between", fontSize: 12.5, marginBottom: 4 }}>
                      <span style={{ color }}>{label}</span>
                      <span className="mono" style={{ color: Math.abs(c.bias) < 0.03 ? "var(--muted)" : "var(--text)" }}>
                        pred {pct(c.mean_predicted)} · actual {pct(c.empirical_freq)} · bias {pp(c.bias, 3)}
                      </span>
                    </div>
                    <div style={{ position: "relative", height: 8, background: "var(--bg2)", borderRadius: 4 }}>
                      <div style={{ position: "absolute", left: 0, top: 0, bottom: 0, width: `${c.mean_predicted * 100}%`, background: color, opacity: 0.5, borderRadius: 4 }} />
                      <div style={{ position: "absolute", left: `${c.empirical_freq * 100}%`, top: -2, width: 2, height: 12, background: "var(--text)" }} title="actual frequency" />
                    </div>
                  </div>
                );
              })
            : <p className="note" style={{ margin: 0 }}>Per-outcome calibration not in this manifest.</p>}
        </div>

        {/* CLV references */}
        <div className="panel" style={{ margin: 0 }}>
          <h2>CLV reference lines</h2>
          <p className="note" style={{ margin: "0 0 12px" }}>Three references; if their signs disagree, the late drift is noise — not a late edge.</p>
          {refs.map((r) => {
            const c = r.v >= 0 ? "var(--green)" : "var(--red)";
            return (
              <div key={r.label} style={{ marginBottom: 11 }}>
                <div style={{ display: "flex", justifyContent: "space-between", fontSize: 12.5 }}>
                  <span style={{ color: "var(--muted)" }}>{r.label}</span>
                  <span className="mono" style={{ color: c }}>{pp(r.v)}</span>
                </div>
                <div style={{ fontSize: 11, color: "var(--faint)", marginTop: 2 }}>{r.note}</div>
              </div>
            );
          })}
        </div>
      </div>

      {/* Coverage + provenance */}
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(280px, 1fr))", gap: 12, marginTop: 12 }}>
        {cov && (
          <div className="panel" style={{ margin: 0 }}>
            <h2>Capture coverage</h2>
            <div style={{ position: "relative", height: 10, background: "var(--bg2)", borderRadius: 5, margin: "4px 0 10px" }}>
              <div style={{ position: "absolute", left: 0, top: 0, bottom: 0, width: `${(cov.n_kickoff / cov.n_matches) * 100}%`, background: "var(--green)", opacity: 0.55, borderRadius: 5 }} />
            </div>
            <p style={{ color: "var(--muted)", lineHeight: 1.6, margin: 0, fontSize: 13 }}>
              <span style={{ color: "var(--text)", fontWeight: 600 }}>{cov.n_kickoff} of {cov.n_matches}</span> matches captured from kickoff.
              The other <span style={{ color: "var(--yellow)" }}>{cov.n_mid_game_start}</span> started mid-game — their &quot;pre-off&quot; line is
              really a mid-match price, so they inflate the pre-off CLV. Honest headlines should exclude them.
            </p>
          </div>
        )}
        {prov && (
          <div className="panel" style={{ margin: 0 }}>
            <h2>Reproducible</h2>
            <p className="note" style={{ margin: "0 0 8px" }}>These numbers regenerate exactly from the stamped code + data:</p>
            <table style={{ width: "100%", fontSize: 12.5 }}>
              <tbody>
                {[
                  ["model git", prov.model_git_sha ?? "—"],
                  ["config sha", prov.config_sha256],
                  ["db sha", prov.db?.sha256 ?? "—"],
                  ["generated", prov.generated_at?.replace("T", " ").replace("+00:00", "Z")],
                ].map(([k, v]) => (
                  <tr key={k}>
                    <td style={{ color: "var(--muted)", padding: "3px 0" }}>{k}</td>
                    <td className="mono" style={{ textAlign: "right", color: "var(--text)" }}>{v}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}
