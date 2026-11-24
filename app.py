import os, re, json, tempfile, uuid
from io import BytesIO
from flask import Flask, render_template, request, jsonify
import fitz
from PIL import Image
import pytesseract

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 25 * 1024 * 1024

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

def extract_pdf(file):
    doc = fitz.open(stream=file.read(), filetype='pdf')
    pages = []
    for i, page in enumerate(doc):
        text = page.get_text('text') or ''
        # OCR pages with little/no embedded text (supports scanned and mixed PDFs).
        if len(text.strip()) < 30:
            pix = page.get_pixmap(dpi=200, alpha=False)
            image = Image.open(BytesIO(pix.tobytes('png')))
            text = pytesseract.image_to_string(image, config='--psm 3') or ''
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

def page_for_term(pages, patterns):
    for p in pages:
        if first_match(p['text'], patterns):
            return p['page']
    return None

def build_report(pages):
    text = '\n'.join(p['text'] for p in pages)
    project = detect_project(text)
    findings = []
    found_count = 0
    attention_count = 0
    question_count = len(QUESTIONS.get(project, QUESTIONS['General EPC']))

    for label, patterns in CHECKS:
        present = first_match(text, patterns)
        page = page_for_term(pages, patterns)
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
        findings.append({'label': label, 'status': status, 'note': note, 'page': page})

    amounts = find_amounts(text)
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
        'arithmetic': arithmetic,
        'extracted_text': text[:30000],
    }

@app.get('/')
def home():
    return render_template('index.html')

@app.post('/api/analyze')
def analyze():
    file = request.files.get('quotation')
    if not file or not file.filename:
        return jsonify({'error': 'Please upload an EPC quotation PDF.'}), 400
    if not file.filename.lower().endswith('.pdf'):
        return jsonify({'error': 'Please upload a PDF quotation.'}), 400
    try:
        pages = extract_pdf(file)
        total_text = sum(len(p['text'].strip()) for p in pages)
        if total_text < 80:
            return jsonify({'error': 'We could not recognise enough text in this PDF, even after OCR. Please upload a clearer scan or a text-based PDF.'}), 422
        return jsonify({'ok': True, 'report': build_report(pages)})
    except Exception:
        app.logger.exception('PDF analysis failed')
        return jsonify({'error': 'We could not read this PDF. Please try another PDF quotation.'}), 500

@app.get('/health')
def health():
    return jsonify({'status': 'ok'})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', '5000')))
