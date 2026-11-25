import os, re, json, tempfile, uuid, shutil, threading
from io import BytesIO
from concurrent.futures import ThreadPoolExecutor
from flask import Flask, render_template, request, jsonify
import fitz
from PIL import Image
import pytesseract

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 25 * 1024 * 1024
executor = ThreadPoolExecutor(max_workers=2)
jobs = {}
jobs_lock = threading.Lock()

PROJECT_PATTERNS = {
    'Solar EPC': [r'\bsolar\b', r'pv module', r'photovoltaic', r'inverter'],
    'CBG / Biogas': [r'\bcbg\b', r'biogas', r'compressed biogas', r'\banaerobic\b', r'\bdigester\b'],
    'STP / ETP': [r'\bstp\b', r'\betp\b', r'sewage treatment', r'effluent treatment'],
    'Pyrolysis': [r'pyrolysis', r'pyrolytic'],
}

CHECKS = [
    ('Scope', [r'scope of work', r'scope', r'included', r'excluded', r'exclusion']),
    ('Technical details', [r'capacity', r'kwh', r'kwp', r'tpd', r'process', r'equipment', r'specification']),
    ('Commercial price', [r'total project cost', r'project cost', r'grand total', r'total amount', r'₹', r'inr']),
    ('Payment terms', [r'payment terms', r'advance', r'milestone', r'payment schedule', r'commissioning payment']),
    ('Warranty', [r'warranty', r'defect liability', r'guarantee']),
    ('Timeline', [r'timeline', r'completion', r'delivery period', r'months?', r'days?']),
    ('Taxes', [r'gst', r'tax', r'cgst', r'sgst', r'igst']),
    ('Exclusions', [r'exclusion', r'not included', r'by client', r'client scope']),
    ('Performance / output', [r'performance guarantee', r'guaranteed output', r'generation', r'yield', r'efficiency', r'uptime']),
]

QUESTIONS = {
    'Solar EPC': [
        'What exact module make/model, inverter make/model and system capacity are included?',
        'Is the quoted generation an estimate or a contractual performance commitment?',
        'What is included for civil work, evacuation, approvals, net metering and testing?',
        'What are the warranty terms for modules, inverters, structures and workmanship?',
    ],
    'CBG / Biogas': [
        'What feedstock quality and quantity assumptions are used for the stated CBG output?',
        'What exact plant capacity, guaranteed production and uptime are committed?',
        'Which gas purification, compression, bottling and utility systems are included?',
        'What happens if actual feedstock or gas output is below the quoted assumption?',
    ],
    'STP / ETP': [
        'What inlet and outlet parameters are assumed, and what treated-water quality is guaranteed?',
        'Which process technology and equipment capacities are included?',
        'Are civil works, electricals, instrumentation, sludge handling and commissioning included?',
        'What performance guarantee and testing procedure apply after commissioning?',
    ],
    'Pyrolysis': [
        'What feedstock specification and daily throughput are assumed?',
        'What product yield, energy consumption and operating conditions are guaranteed?',
        'Which emission-control, safety and utility systems are included?',
        'What operating data or reference plants support the quoted output?',
    ],
    'General EPC': [
        'What exactly is included in EPC scope and what remains the client’s responsibility?',
        'Which technical specifications and performance commitments are contractual?',
        'What payment milestones are linked to measurable deliverables?',
        'What warranties, exclusions, taxes and statutory approvals are included or excluded?',
    ]
}

def extract_pdf_bytes(file_bytes, job_id=None):
    doc = fitz.open(stream=file_bytes, filetype='pdf')
    pages = []
    total_pages = len(doc)
    if total_pages > 100:
        doc.close()
        raise ValueError('This quotation has more than 100 pages. Please split it into smaller PDF files and upload one part at a time.')
    for i, page in enumerate(doc):
        if job_id:
            with jobs_lock:
                jobs[job_id].update({'progress': i + 1, 'total': total_pages, 'message': f'Reading page {i + 1} of {total_pages}…'})
        text = page.get_text('text') or ''
        # OCR pages with little/no embedded text (supports scanned and mixed PDFs).
        if len(text.strip()) < 30:
            if not shutil.which('tesseract'):
                raise RuntimeError('OCR engine is not installed on the server.')
            pix = page.get_pixmap(dpi=150, colorspace=fitz.csGRAY, alpha=False)
            image = Image.open(BytesIO(pix.tobytes('png')))
            text = pytesseract.image_to_string(image, lang='eng', config='--oem 1 --psm 6', timeout=30) or ''
        pages.append({'page': i + 1, 'text': text})
    doc.close()
    return pages

def detect_project(text):
    scores = {}
    for name, pats in PROJECT_PATTERNS.items():
        scores[name] = sum(bool(re.search(p, text, re.I)) for p in pats)
    best = max(scores, key=scores.get) if scores else 'General EPC'
    return best if scores.get(best, 0) else 'General EPC'

def first_match(text, patterns):
    return any(re.search(p, text, re.I) for p in patterns)

def find_amounts(text):
    vals = re.findall(r'(?:₹|Rs\.?|INR)\s*[0-9][0-9,]*(?:\.\d+)?', text, re.I)
    return list(dict.fromkeys(vals))[:12]

def evidence_for_term(pages, patterns, limit=260):
    """Return a short, page-labelled quotation excerpt supporting a keyword match."""
    for p in pages:
        source = p['text'] or ''
        for pattern in patterns:
            match = re.search(pattern, source, re.I)
            if not match:
                continue
            start = max(source.rfind('\n', 0, match.start()), source.rfind('.', 0, match.start()), source.rfind(';', 0, match.start())) + 1
            ends = [x for x in (source.find('\n', match.end()), source.find('.', match.end()), source.find(';', match.end())) if x >= 0]
            end = min(ends) + 1 if ends else len(source)
            excerpt = re.sub(r'\s+', ' ', source[start:end]).strip()
            if len(excerpt) > limit:
                left = max(0, match.start() - start - 75)
                excerpt = '…' + excerpt[left:left + limit - 1].strip() + '…'
            if excerpt:
                return {'page': p['page'], 'text': excerpt}
    return None

def page_for_term(pages, patterns):
    for p in pages:
        if first_match(p['text'], patterns):
            return p['page']
    return None


IMPACT_GUIDANCE = {
    'Scope': ('A scope reference appears, but the exact inclusions and interfaces still need to be matched to the final scope.', 'Which civil, electrical, utility, approval, testing and handover activities are included, and which remain with the client?', 'Signed scope matrix, battery limits, interface list and exclusions schedule.'),
    'Technical details': ('Technical terms appear, but their presence alone does not confirm that the design is complete or suitable.', 'Can you provide the final capacity, make/model, datasheets, applicable standards and design assumptions?', 'Approved technical datasheets, process/design basis, equipment list and compliance documents.'),
    'Commercial price': ('A price reference appears. The extracted figures are not a full reconciliation of the quotation.', 'Please share the item-wise price build-up and confirm the complete amount payable, including taxes and optional items.', 'Detailed BOQ, priced schedule, tax calculation and a written list of optional/additional costs.'),
    'Payment terms': ('Payment wording appears. The amount and timing should be tied to clear, verifiable deliverables.', 'What deliverable and acceptance evidence is linked to each payment, especially the advance?', 'Stage-wise payment schedule, purchase-order terms, delivery evidence and milestone acceptance criteria.'),
    'Warranty': ('A warranty or guarantee reference appears, but coverage and exclusions may vary by equipment and work type.', 'What is covered, for how long, from which start date, and how quickly will service issues be addressed?', 'Warranty certificates, equipment-wise coverage, exclusions, response-time commitment and service process.'),
    'Timeline': ('A time-related reference appears, but the project start trigger and completion conditions need to be clear.', 'From which date is the completion period counted, and what events allow an extension?', 'Baseline project schedule, delivery milestones, commissioning criteria and extension provisions.'),
    'Taxes': ('A tax reference appears, but the extracted text does not establish the final tax treatment or total payable.', 'Is GST included or extra, which rate applies to each supply/service, and what is the tax-inclusive total?', 'Tax breakup, invoice structure and written confirmation of the total landed cost.'),
    'Exclusions': ('An exclusion or client-responsibility reference appears. Confirm that all interfaces and dependencies are accounted for.', 'Please list every excluded item and identify who will arrange, pay for and complete it.', 'Exclusions schedule, responsibility matrix and interface/utility requirements.'),
    'Performance / output': ('Performance-related wording appears, but the conditions and method used to measure performance matter.', 'What output is committed, under what feedstock/site/operating conditions, and how will it be tested?', 'Guaranteed-performance schedule, assumptions, test protocol, acceptance criteria and remedy provisions.'),
    'Item-wise BOQ / price breakup': ('An item-wise breakup reference appears. Check whether quantities, unit rates, specifications and totals are all provided.', 'Can you provide a complete BOQ with quantities, unit rates, makes/specifications and line totals?', 'Priced BOQ, technical specification cross-reference and reconciled grand total.'),
    'GST treatment': ('A tax reference appears, but it does not by itself confirm whether the quoted total includes all applicable GST.', 'Please confirm the applicable GST rate and taxable value for each major supply/service, and whether it is included or extra.', 'GST breakup, tax invoice assumptions and tax-inclusive total.'),
    'Freight, installation and other charges': ('Additional-charge wording appears. Confirm the exact boundary between included and separately billed work.', 'Are packing, freight, transit insurance, unloading, erection, installation, testing and commissioning included?', 'Written inclusion list, logistics/installation scope and schedule of extra charges.'),
    'Payment milestones': ('Payment-related wording appears. Each instalment should correspond to an observable project milestone.', 'What evidence and acceptance sign-off are required before each payment becomes due?', 'Milestone-wise payment table, delivery/inspection records and acceptance procedure.'),
    'Completion and delay terms': ('Completion or delay wording appears. The contract should define the completion trigger and how delays are treated.', 'What is the committed completion date, what extensions are permitted, and what remedy applies to attributable delay?', 'Project schedule, extension clauses, delay-remedy terms and commissioning/handover definition.'),
    'Payment percentage cross-check': ('The extracted percentage figures are only a preliminary text check and may include unrelated percentages.', 'Please confirm the complete payment-stage percentages and the amount due at each stage.', 'Original payment table, stage-wise amount calculation and confirmation that all stages reconcile to the contract total.')
}

def add_impact_guidance(item):
    why, ask, verify = IMPACT_GUIDANCE.get(item['label'], (
        'This point can affect project cost, delivery, performance or responsibility if it is not clearly defined.',
        'Please explain the exact commitment, assumptions and responsibility for this item.',
        'Written contractual wording and supporting technical/commercial documents.'
    ))
    item['guidance'] = {
        'what_found': item.get('note', 'Review this item in the quotation.'),
        'why_it_matters': why,
        'ask_epc': ask,
        'verify_before_signing': verify
    }
    return item

def commercial_review(pages, text):
    checks = []
    def add(label, patterns, good_note, missing_note):
        ev = evidence_for_term(pages, patterns)
        checks.append(add_impact_guidance({
            'label': label,
            'status': 'Found in quotation' if ev else 'Not clearly found',
            'note': good_note if ev else missing_note,
            'page': ev['page'] if ev else None,
            'evidence': ev['text'] if ev else None
        }))

    add('Item-wise BOQ / price breakup',
        [r'\bBOQ\b', r'bill of quantities', r'price break.?up', r'item.?wise', r'quantity\s+.*\brate\b'],
        'A BOQ or item-wise breakup appears in the quotation. Check quantities, unit rates and totals line by line.',
        'No clear item-wise BOQ or price breakup reference was found. Ask for quantities, unit rates and line totals.')
    add('GST treatment',
        [r'\bGST\b', r'\bCGST\b', r'\bSGST\b', r'\bIGST\b'],
        'A tax reference appears. Confirm the applicable rate, taxable value and whether GST is included in the quoted total.',
        'No clear GST/tax reference was found. Ask for the applicable tax rate and whether taxes are included or extra.')
    add('Freight, installation and other charges',
        [r'freight', r'transportation', r'packing', r'insurance', r'installation charges', r'erection', r'commissioning charges'],
        'One or more additional-charge terms appear. Confirm which are included and which will be billed separately.',
        'No clear freight/installation/other-charge detail was found. Ask for a written list of all additional charges.')
    add('Payment milestones',
        [r'payment schedule', r'payment terms', r'milestone', r'advance payment', r'on dispatch', r'on delivery', r'on commissioning'],
        'Payment-related wording appears. Link every instalment to a measurable deliverable and acceptance milestone.',
        'No clear payment milestone schedule was found. Ask for stage-wise amounts, due dates and acceptance conditions.')
    add('Completion and delay terms',
        [r'liquidated damages', r'\bLD\b', r'delay penalty', r'delay damages', r'time is of the essence', r'completion period'],
        'Completion/delay wording appears. Confirm the start date, completion trigger, extensions and any delay remedy.',
        'No clear delay remedy or completion trigger was found. Ask how delays and extensions will be handled.')

    pct = [float(x) for x in re.findall(r'(\d+(?:\.\d+)?)\s*%', text)]
    payment_context = bool(re.search(r'payment|advance|milestone|dispatch|delivery|commissioning', text, re.I))
    payment_pcts = pct if payment_context else []
    if payment_pcts:
        total = sum(payment_pcts)
        note = f'Percentage figures appear near payment-related wording and add to {total:g}%. Confirm these are all payment stages and that the schedule totals 100%.'
        if abs(total - 100) > 0.01:
            note += ' The extracted percentage total is not 100%; check the original table carefully.'
        checks.append(add_impact_guidance({'label':'Payment percentage cross-check','status':'Review figures','note':note,'page':None,'evidence':None}))
    else:
        checks.append(add_impact_guidance({'label':'Payment percentage cross-check','status':'Not calculated','note':'No payment percentages could be confidently isolated from the extracted text. Check the payment table manually.','page':None,'evidence':None}))
    return checks

def build_report(pages):
    text = '\n'.join(p['text'] for p in pages)
    project = detect_project(text)
    findings = []
    found_count = 0
    attention_count = 0
    question_count = len(QUESTIONS.get(project, QUESTIONS['General EPC']))

    for label, patterns in CHECKS:
        present = first_match(text, patterns)
        evidence = evidence_for_term(pages, patterns)
        page = evidence['page'] if evidence else None
        if present:
            found_count += 1
            status = 'Information found'
            note = f'{label} is mentioned in the quotation.'
            if label in ('Performance / output', 'Payment terms', 'Scope', 'Exclusions'):
                status = 'Needs clarification'
                attention_count += 1
                note = f'{label} is mentioned, but the wording should be checked against the final scope/contract.'
        else:
            attention_count += 1
            status = 'Information not found'
            note = f'No clear {label.lower()} reference was found in the extracted quotation text.'
        findings.append(add_impact_guidance({'label': label, 'status': status, 'note': note, 'page': page, 'evidence': evidence['text'] if evidence else None}))

    amounts = find_amounts(text)
    commercial_checks = commercial_review(pages, text)
    if len(amounts) >= 2:
        arithmetic = 'Multiple monetary figures found; line-item reconciliation is recommended.'
    elif len(amounts) == 1:
        arithmetic = 'A total/monetary figure was found; reconcile it with the detailed BOQ before approval.'
    else:
        arithmetic = 'No clear monetary figure was extracted.'

    return {
        'id': str(uuid.uuid4()),
        'project': project,
        'pages': len(pages),
        'text_chars': len(text),
        'summary': f'Preliminary review of the uploaded quotation for {project}. This report highlights information found, areas needing clarification, and questions to discuss with the EPC provider.',
        'metrics': {'found': found_count, 'attention': attention_count, 'questions': question_count},
        'findings': findings,
        'questions': QUESTIONS.get(project, QUESTIONS['General EPC']),
        'amounts': amounts,
        'commercial_checks': commercial_checks,
        'arithmetic': arithmetic,
        'extracted_text': text[:30000],
    }

@app.get('/')
def home():
    return render_template('index.html')

def run_analysis(job_id, file_bytes):
    try:
        with jobs_lock:
            jobs[job_id].update({'status': 'processing', 'message': 'Opening quotation…'})
        pages = extract_pdf_bytes(file_bytes, job_id)
        total_text = sum(len(p['text'].strip()) for p in pages)
        if total_text < 80:
            raise ValueError('We could not recognise enough text in this PDF, even after OCR. Please upload a clearer scan or a text-based PDF.')
        report = build_report(pages)
        with jobs_lock:
            jobs[job_id].update({'status': 'done', 'message': 'Jaanch complete', 'report': report})
    except Exception as exc:
        app.logger.exception('PDF analysis failed')
        if isinstance(exc, pytesseract.pytesseract.TesseractNotFoundError):
            message = 'The OCR engine is missing from the server.'
        elif isinstance(exc, RuntimeError) and 'timeout' in str(exc).lower():
            message = 'OCR took too long on one page. Please split the PDF into smaller parts or upload a clearer copy.'
        elif isinstance(exc, ValueError):
            message = str(exc)
        elif isinstance(exc, fitz.FileDataError):
            message = 'This PDF appears damaged or invalid. Please open and re-save it as a PDF, then upload again.'
        else:
            message = 'PDF processing failed (' + type(exc).__name__ + '). Please try a smaller PDF.'
        with jobs_lock:
            jobs[job_id].update({'status': 'error', 'message': message})

@app.post('/api/analyze')
def analyze():
    file = request.files.get('quotation')
    if not file or not file.filename:
        return jsonify({'error': 'Please upload an EPC quotation PDF.'}), 400
    if not file.filename.lower().endswith('.pdf'):
        return jsonify({'error': 'Please upload a PDF quotation.'}), 400
    file_bytes = file.read()
    if not file_bytes.startswith(b'%PDF'):
        return jsonify({'error': 'This file does not appear to be a valid PDF.'}), 400
    job_id = str(uuid.uuid4())
    with jobs_lock:
        jobs[job_id] = {'status': 'queued', 'progress': 0, 'total': 0, 'message': 'Upload received. Preparing your quotation…'}
    executor.submit(run_analysis, job_id, file_bytes)
    return jsonify({'job_id': job_id}), 202

@app.get('/api/status/<job_id>')
def analysis_status(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            return jsonify({'error': 'Analysis session not found. Please upload the PDF again.'}), 404
        return jsonify({k: v for k, v in job.items() if k != 'report'} | ({'report': job['report']} if job['status'] == 'done' else {}))

@app.get('/health')
def health():
    tesseract_path = shutil.which('tesseract')
    return jsonify({'status': 'ok', 'ocr_available': bool(tesseract_path), 'ocr_engine': tesseract_path or 'not found'})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', '5000')))
