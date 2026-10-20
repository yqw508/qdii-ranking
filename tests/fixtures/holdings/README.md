# Cross-page holding fixture

`019454-cross-page.txt` is extracted from section 7.10 of the 2026 midyear report:
https://pdf.dfcfw.com/pdf/H2_AN202608291828681989_1.pdf

The fixture retains line breaks inside the amount and fund name, manager fields,
the report title and numbered-page boundary, and the following report notes.
Only trailing whitespace is trimmed. Tests run without downloading the PDF.

`019524-wrapped-amount.txt` is extracted from section 7.10 of the 2026 midyear report:
https://pdf.dfcfw.com/pdf/H2_AN202608291828684974_1.pdf

It retains the amount split before a comma (`1,379,117` / `,189.04`), the explicit
91.04% holding weight, and the legal fund-name continuation across pages 47–48.
