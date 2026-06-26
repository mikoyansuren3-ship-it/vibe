"use client";

export type TabId = "overview" | "replay" | "bets" | "sandbox" | "games" | "about";
export type Mode = "basic" | "advanced";

const NAV: { id: TabId; label: string; icon: string; advanced?: boolean }[] = [
  { id: "overview", label: "Overview", icon: "◉" },
  { id: "replay", label: "Replay", icon: "▶" },
  { id: "bets", label: "Bets", icon: "▦" },
  { id: "sandbox", label: "Sandbox", icon: "⚙", advanced: true },
  { id: "games", label: "Games", icon: "▤" },
  { id: "about", label: "About", icon: "ⓘ" },
];

export function Sidebar({
  active, setActive, mode, setMode, betCount,
}: {
  active: TabId; setActive: (t: TabId) => void;
  mode: Mode; setMode: (m: Mode) => void;
  betCount?: number;
}) {
  const items = NAV.filter((n) => !n.advanced || mode === "advanced");
  return (
    <aside className="sidebar">
      <div className="brandrow">
        <span className="logo">K</span>
        <div>
          <div style={{ fontWeight: 680, fontSize: 13.5, lineHeight: 1.2 }}>WC × Kalshi</div>
          <div style={{ fontSize: 11, color: "var(--faint)" }}>paper sim</div>
        </div>
      </div>

      {items.map((n) => (
        <div key={n.id} className={`navitem ${active === n.id ? "active" : ""}`} onClick={() => setActive(n.id)}>
          <span className="ic">{n.icon}</span>
          <span>{n.label}</span>
          {n.id === "bets" && betCount != null && <span className="badge2">{betCount}</span>}
        </div>
      ))}

      <div className="spacer" />
      <div className="seclabel">View mode</div>
      <div className="modetoggle">
        <button className={mode === "basic" ? "on" : ""} onClick={() => setMode("basic")}>Basic</button>
        <button className={mode === "advanced" ? "on" : ""} onClick={() => setMode("advanced")}>Advanced</button>
      </div>
      <div className="modehint">
        {mode === "basic"
          ? "Plain-English: watch the bot bet & see what won."
          : "Full quant: CLV, edges, Kelly knobs, calibration."}
      </div>
    </aside>
  );
}
