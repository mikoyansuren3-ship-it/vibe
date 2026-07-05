"use client";

// App-level error boundary: a malformed or zero-tick bundle (or any render throw) shows a
// recoverable panel instead of white-screening the whole app. `reset` re-renders the segment.
export default function Error({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  return (
    <div style={{ maxWidth: 560, margin: "80px auto", padding: "0 20px" }}>
      <div className="panel">
        <h2 style={{ marginTop: 0 }}>Something went wrong</h2>
        <div className="note" style={{ marginTop: 8 }}>
          A match view failed to render. This is usually a stale or incomplete feed — the rest
          of the app is fine.
        </div>
        <pre
          style={{
            marginTop: 12,
            padding: 10,
            background: "var(--bg2, rgba(255,255,255,0.04))",
            borderRadius: 6,
            fontSize: 12,
            color: "var(--faint)",
            overflowX: "auto",
            whiteSpace: "pre-wrap",
          }}
        >
          {error.message || "Unknown error"}
        </pre>
        <button className="primary" style={{ marginTop: 14 }} onClick={() => reset()}>
          Try again
        </button>
      </div>
    </div>
  );
}
