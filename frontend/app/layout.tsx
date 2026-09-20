import type { Metadata } from "next";
import Link from "next/link";
import "./globals.css";

export const metadata: Metadata = {
  title: "Ledger Bridge console",
  description: "Migration and reconciliation console for Ledger Bridge",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en">
      <body>
        <div className="app-shell">
          <header className="topbar">
            <div className="topbar-inner">
              <Link href="/" className="brand">
                <span className="brand-mark">LB</span>
                <span>Ledger Bridge</span>
                <span className="brand-sub">migration console</span>
              </Link>
              <nav className="topbar-nav">
                <Link href="/">Operations</Link>
              </nav>
            </div>
          </header>
          <main className="page">{children}</main>
        </div>
      </body>
    </html>
  );
}
