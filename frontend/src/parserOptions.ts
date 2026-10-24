export const parserOptions = [
  {
    value: "pypdf",
    label: "Fast text / PyPDF",
    description:
      "Fast, local extraction for digital PDFs. It has no parser usage cost and keeps the PDF local, but it does not OCR scanned pages.",
  },
  {
    value: "gemini",
    label: "Rich Markdown / Gemini",
    description:
      "For complex layouts, tables, and scans. It sends the PDF to Google, needs a Gemini key, and may add latency and usage cost. Scan quality affects results.",
  },
] as const;
