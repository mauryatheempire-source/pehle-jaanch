# Pehle Jaanch — Render-ready MVP

## Deploy on Render
1. Put this folder in a GitHub repository named `pehle-jaanch`.
2. In Render choose **New > Web Service** and select the repository.
3. Render can use `render.yaml`, or set:
   - Build Command: `pip install -r requirements.txt`
   - Start Command: `gunicorn app:app`
4. Deploy.
5. Test `/health`, then upload a real text-based EPC quotation PDF.

## Current MVP
- Mobile-friendly landing page
- PDF upload up to 25 MB
- Server-side PDF text extraction
- Project type detection: Solar, CBG/Biogas, STP/ETP, Pyrolysis, General EPC
- Checks scope, technical details, price, payment, warranty, timeline, taxes, exclusions and performance/output
- Generates neutral findings and project-specific questions
- Detects scanned/image-only PDFs and clearly asks for a text PDF

## Next production upgrades
- OCR for scanned PDFs
- Page-level evidence snippets for every finding
- Stronger number/BOQ reconciliation
- Better project-specific rule sets
- Secure temporary-file lifecycle and rate limiting
- Optional downloadable report and user history
