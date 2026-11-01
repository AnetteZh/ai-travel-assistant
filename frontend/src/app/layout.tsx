import type { Metadata } from "next";
import "./globals.css";
import { TripProvider } from "@/components/trip-provider";

export const metadata: Metadata = {
  title: "О!дин запрос — AI-помощник по путешествиям",
  description: "Путешествие от одной идеи до плана",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="ru">
      <body><TripProvider>{children}</TripProvider></body>
    </html>
  );
}
