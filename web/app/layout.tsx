import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "WC × Kalshi — Paper Gambling Simulator",
  description: "Replay the in-play model paper-betting World Cup matches, and tune the strategy.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
