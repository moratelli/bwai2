import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { App } from "./App";

describe("App", () => {
  it("renders aligned parser guidance with Fast text selected by default", () => {
    render(<App />);

    expect(screen.getByRole("heading", { name: "Async document processor" })).toBeVisible();
    expect(screen.getByRole("radio", { name: /Fast text \/ PyPDF/ })).toBeChecked();
    expect(screen.getByRole("radio", { name: /Rich Markdown \/ Gemini/ })).not.toBeChecked();
    expect(
      screen.getByRole("radio", { name: /Scanned document \/ OCR \/ Mistral/ }),
    ).not.toBeChecked();
    expect(screen.getByText(/does not OCR scanned pages/i)).toBeVisible();
    expect(screen.getByText(/every completed document uses Gemini for its summary/i)).toBeVisible();
  });
});
