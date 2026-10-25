#!/usr/bin/env python3
"""Build the deterministic synthetic report used by the quality check."""

import argparse
import io
from pathlib import Path
from typing import cast

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen.canvas import Canvas


def build_pdf() -> bytes:
    output = io.BytesIO()
    canvas = Canvas(output, pagesize=A4, invariant=1, pageCompression=1)
    canvas.setTitle("Synthetic operating report")
    canvas.setAuthor("Synthetic quality fixture")
    canvas.setFont("Helvetica-Bold", 18)
    canvas.drawString(54, 780, "Synthetic operating report")
    canvas.setFont("Helvetica", 10)
    canvas.drawString(54, 758, "Fictional numbers for document-processing checks.")
    canvas.setFont("Helvetica-Bold", 14)
    canvas.drawString(54, 714, "Revenue by region")

    rows = (
        ("Measure", "Amount (USD)"),
        ("North revenue", "USD 120,000"),
        ("South revenue", "USD 80,000"),
        ("Total revenue", "USD 200,000"),
    )
    for index in range(len(rows) + 1):
        y = 690 - index * 36
        canvas.line(54, y, 540, y)
    for x in (54, 330, 540):
        canvas.line(x, 690, x, 546)
    for index, (label, value) in enumerate(rows):
        canvas.setFont("Helvetica-Bold" if index in (0, 3) else "Helvetica", 12)
        canvas.drawString(66, 666 - index * 36, label)
        canvas.drawString(342, 666 - index * 36, value)
    canvas.setFont("Helvetica", 10)
    canvas.drawString(54, 54, "Synthetic fixture | Page 1 of 2")
    canvas.showPage()

    canvas.setFont("Helvetica-Bold", 18)
    canvas.drawString(54, 780, "Synthetic operating report")
    canvas.setFont("Helvetica", 10)
    canvas.drawString(54, 758, "Fictional numbers for document-processing checks.")
    canvas.setFont("Helvetica-Bold", 14)
    canvas.drawString(54, 714, "Operating costs and profit")
    canvas.setFont("Helvetica", 12)
    canvas.drawString(54, 680, "Operating costs: USD 140,000")
    canvas.drawString(54, 650, "Operating profit: USD 60,000")
    canvas.setFont("Helvetica", 10)
    canvas.drawString(
        54, 610, "All figures are synthetic. This is not a company report."
    )
    canvas.drawString(54, 54, "Synthetic fixture | Page 2 of 2")
    canvas.showPage()
    canvas.save()
    return output.getvalue()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "destination",
        nargs="?",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "fixtures" / "quality-report.pdf",
    )
    destination = cast(Path, parser.parse_args().destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(build_pdf())
    print("quality_fixture_generated")


if __name__ == "__main__":
    main()
