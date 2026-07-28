import type { Metadata } from "next";

import { AppFrame } from "@/components/app-frame";

import "./globals.css";

export const metadata: Metadata = {
  title: "PRD Agent Workbench",
  description: "Grounded PRD workflow, visible and under control.",
};

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="zh-CN">
      <body>
        <AppFrame>{children}</AppFrame>
      </body>
    </html>
  );
}
