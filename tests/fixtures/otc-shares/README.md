# OTC primary-share fixtures

`160213-product-overview.txt` preserves the extracted text of the product overview
and investment-strategy section of the 160213 product summary published 2026-09-21:
https://pdf.dfcfw.com/pdf/H2_AN202609211829696259_1.pdf

Trailing whitespace from PDF extraction is trimmed. Line wrapping, managers,
currency, operation mode and the following investment
section are retained. USD and ETF mentions outside the overview must not influence
share eligibility. Tests consume this local text and never download a PDF.

`candidates-20260922.json` freezes the 16 existing OTC Nasdaq-100 record identities
plus the omitted 160213 metadata. Tests compare original discovery with supplemental
verification; expected ordering and returns are not hardcoded.
