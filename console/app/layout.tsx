import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Automation Console",
  description: "Watch runs, take control of a live session, hand it back.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
