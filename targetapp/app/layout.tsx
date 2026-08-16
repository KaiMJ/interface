import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Meridian CU — Member Services Console",
  description: "Mock back-office application. Not a real financial institution.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
