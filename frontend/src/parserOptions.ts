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
      "Better suited to complex layouts and tables. It sends the PDF to Google, needs a Gemini key, and may add latency and usage cost.",
  },
  {
    value: "mistral",
    label: "Scanned document / OCR / Mistral",
    description:
      "Use this for scans and image-heavy PDFs. It sends the PDF to Mistral, needs a Mistral key, and may add latency and usage cost.",
  },
] as const;
