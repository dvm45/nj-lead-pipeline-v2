"""
PDF text extraction using PyMuPDF.

This module's job is simple: open a PDF and pull out the text from each page.
It does NOT parse or interpret that text — that's parser.py's job.

PyMuPDF is imported as 'fitz' (its internal name). It's fast and handles most
text-based PDFs well. For scanned pages (images with no embedded text),
it returns an empty string — those are flagged as 'ocr_needed'.

Main function: extract_pdf(path) → ExtractedDocument
"""

from dataclasses import dataclass, field
from pathlib import Path

import fitz  # PyMuPDF


@dataclass
class ExtractedPage:
    """
    The result of extracting one page from a PDF.

    page_num  : 0-indexed page number (page 0 = first page)
    raw_text  : the text content, or empty string if blank/scanned
    is_blank  : True if PyMuPDF found no text (likely a scanned image)
    """

    page_num: int
    raw_text: str
    is_blank: bool


@dataclass
class ExtractedDocument:
    """
    The result of extracting a whole PDF.

    file_path  : the PDF file that was extracted
    pages      : list of ExtractedPage, one per page
    error      : if something went wrong opening/reading the file, the error message
    """

    file_path: Path
    pages: list[ExtractedPage] = field(default_factory=list)
    error: str | None = None

    @property
    def page_count(self) -> int:
        return len(self.pages)

    @property
    def blank_page_count(self) -> int:
        return sum(1 for p in self.pages if p.is_blank)

    @property
    def failed(self) -> bool:
        """True if the file couldn't be opened at all."""
        return self.error is not None


def extract_pdf(path: Path) -> ExtractedDocument:
    """
    Open a PDF and extract text from every page.

    Returns an ExtractedDocument. If the file can't be opened (corrupt,
    password-protected, wrong format), the .error field will be set and
    .pages will be empty — the caller logs this to the issues table.

    How it works:
      1. Open the PDF with fitz.open()
      2. Loop through each page
      3. Call page.get_text("text") — returns a plain-text string
      4. If the string is empty or only whitespace → mark as blank (scanned)
    """
    result = ExtractedDocument(file_path=path)

    try:
        # Open the PDF file
        doc = fitz.open(str(path))
    except Exception as e:
        # Can't open the file — record the error and return early
        result.error = f"Could not open PDF: {e}"
        return result

    try:
        for page_num in range(len(doc)):
            page = doc[page_num]

            # Extract text as a plain string.
            # "text" mode preserves line breaks; good enough for regex parsing.
            raw_text = page.get_text("text")

            # Strip whitespace to check if the page is actually empty
            is_blank = len(raw_text.strip()) == 0

            result.pages.append(
                ExtractedPage(
                    page_num=page_num,
                    raw_text=raw_text,
                    is_blank=is_blank,
                )
            )
    except Exception as e:
        # Something went wrong mid-document — return what we have so far
        # and record the error. Partial data is better than no data.
        result.error = f"Error reading pages: {e}"
    finally:
        # Always close the file handle, even if an exception occurred
        doc.close()

    return result
