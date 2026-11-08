"""Udyam Digital — local, consent-first business launchpad.
Only Python's standard library is required. Native OCR uses tools/ocr on macOS;
Tesseract is an optional cross-platform fallback. No document is retained.
"""
import base64
import collections
import csv
import datetime as dt
from difflib import SequenceMatcher
import hashlib
import html
import io
import json
import mimetypes
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import urllib.request
import gemini
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
DATA = ROOT / 'data'
DB_PATH = ROOT / 'workspace.sqlite3'
REFERENCE_DATE = dt.date(2026, 7, 15)
SEGMENTS = {'micro': 'Retail & services', 'prof': 'Professional office', 'multi': 'Multi-site business', 'mfg': 'Manufacturing'}
NEEDS = {'internet', 'billing', 'email', 'security', 'customers', 'backup', 'cctv', 'machines', 'mobile'}

def rows(name):
    with (DATA / name).open(encoding='utf-8-sig', newline='') as file:
        return list(csv.DictReader(file))

CAT = rows('product_catalogue.csv')
PRODUCTS = {p['product_id']: p for p in CAT}
GST = {r['gstin']: r for r in rows('mock_gstin_registry.csv')}
UDY = {r['udyam_number']: r for r in rows('mock_udyam_registry.csv')}
LEADS = rows('smb_leads.csv')

def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()

def connect():
    c = sqlite3.connect(DB_PATH, timeout=20)
    c.row_factory = sqlite3.Row
    c.execute('PRAGMA foreign_keys=ON')
    return c

def init_db():
    with connect() as c:
        c.executescript('''CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY, created TEXT, updated TEXT, payload TEXT);
            CREATE TABLE IF NOT EXISTS orders (
            id TEXT PRIMARY KEY, session_id TEXT NOT NULL, created TEXT, payload TEXT,
            FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE CASCADE);
            CREATE TABLE IF NOT EXISTS callbacks (
            id TEXT PRIMARY KEY, session_id TEXT, created TEXT, payload TEXT,
            FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE CASCADE);''')
        cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=24)).isoformat()
        c.execute('DELETE FROM sessions WHERE updated < ?', (cutoff,))

def empty_session():
    return {'profile': {}, 'items': {}, 'consent': False, 'documents': {}, 'registry': None, 'ai_consent': False}

def mask(value):
    return value[:2] + '•' * max(0, len(value) - 6) + value[-4:] if value else ''

def normalise_profile(raw):
    p = dict(raw)
    if p.get('segment') not in SEGMENTS:
        raise ValueError('Choose your business type first.')
    for key, default, maximum in [('employees', 5, 10000), ('sites', 1, 100), ('seats', p.get('employees', 5), 1000), ('budget', 3000, 10000000), ('current_spend', 0, 10000000)]:
        try:
            number = float(p.get(key, default))
            if not number.is_integer():
                raise ValueError()
            p[key] = int(number)
        except (ValueError, TypeError):
            raise ValueError(f'Enter a whole number for {key.replace("_", " ")}.')
        minimum = 0 if key == 'current_spend' else 1
        if not minimum <= p[key] <= maximum:
            raise ValueError(f'{key.replace("_", " ").capitalize()} must be between {minimum} and {maximum}.')
    p['tier'] = str(p.get('tier', '1'))
    if p['tier'] not in ['1', '2']:
        raise ValueError('This pilot supports tier 1 and tier 2 cities.')
    p['needs'] = [n for n in p.get('needs', ['internet']) if n in NEEDS]
    if not p['needs']:
        raise ValueError('Choose at least one business goal.')
    p['language'] = 'Hindi' if p.get('language') == 'Hindi' else 'English'
    p['city'] = str(p.get('city', 'Your city'))[:60]
    p['name'] = str(p.get('name', 'Your business'))[:100]
    p['fibre'] = p.get('fibre', 'available')
    if p['fibre'] not in ['available', 'unavailable', 'existing']:
        raise ValueError('Choose your current internet setup.')
    p['has_gstin'] = p.get('has_gstin') is True
    return p

def valid_gstin(value):
    if not re.fullmatch(r'[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][A-Z0-9]Z[A-Z0-9]', value):
        return False
    alphabet = '0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ'
    total, factor = 0, 2
    for ch in reversed(value[:14]):
        product = alphabet.index(ch) * factor
        total += product // 36 + product % 36
        factor = 1 if factor == 2 else 2
    return alphabet[(36 - total % 36) % 36] == value[-1]


def verified_gst(registry):
    return bool(registry and registry.get('kind') == 'gst' and registry.get('status') == 'Active')

def eligible(product, profile, has_gst=False):
    return (str(profile['tier']) in product['available_city_tiers'].split(';')
            and profile['employees'] >= int(product['min_employees'])
            and profile['sites'] >= int(product['min_sites'])
            and (product['requires_gstin'] != 'Y' or has_gst))

def quote(raw_profile, items, has_gst=False, provisional=False):
    profile = normalise_profile(raw_profile)
    if not isinstance(items, dict) or not items:
        raise ValueError('Add at least one service to your plan.')
    result = []
    pending_gst = False
    for key, quantity in items.items():
        if key not in PRODUCTS or type(quantity) is not int or not 1 <= quantity <= 1000:
            raise ValueError('Use a valid service and a quantity between 1 and 1,000.')
        p = PRODUCTS[key]
        if key in ['P01', 'P02', 'P03', 'P04', 'P05', 'P06'] and quantity < profile['sites']:
            raise ValueError('Your plan covers ' + str(profile['sites']) + ' locations. Keep at least one primary connection per location, or edit your locations.')
        allow_gst = has_gst or (provisional and profile['has_gstin'])
        if not eligible(p, profile, allow_gst):
            raise ValueError(p['product_name'] + ' does not match your team size, locations, city tier, or GST status.')
        if p['requires_one_of'] and not set(p['requires_one_of'].split('|')).intersection(items):
            names = ', '.join(PRODUCTS[k]['product_name'] for k in p['requires_one_of'].split('|'))
            raise ValueError(p['product_name'] + ' needs one of these in the same plan: ' + names + '.')
        pending_gst |= p['requires_gstin'] == 'Y' and not has_gst
        result.append({'id': key, 'name': p['product_name'], 'category': p['category'], 'unit': p['unit'],
                       'description': p['description'], 'quantity': quantity,
                       'rate': int(p['price_min_inr_per_month']),
                       'monthly': int(p['price_min_inr_per_month']) * quantity,
                       'setup': int(p['one_time_setup_inr']) * quantity,
                       'contract': int(p['min_contract_months']), 'days': int(p['typical_install_days'])})
    subtotal = sum(r['monthly'] for r in result)
    setup = sum(r['setup'] for r in result)
    total = round(subtotal * 1.18, 2)
    return {'items': result, 'subtotal': subtotal, 'tax': round(subtotal * .18, 2), 'total': total,
            'setup': setup, 'setup_tax': round(setup * .18, 2), 'setup_total': round(setup * 1.18, 2),
            'first_invoice': round(total + setup * 1.18, 2),
            'contract': max(r['contract'] for r in result), 'days': max(r['days'] for r in result),
            'annual': round(total * 12 + setup * 1.18, 2), 'gst_pending': pending_gst,
            'budget_gap': round(max(0, total - profile['budget']), 2),
            'headroom': round(max(0, profile['budget'] - total), 2)}

REASONS = {
 'P01': ('Reliable payments & daily work', '100 Mbps business fibre for UPI, billing and everyday browsing.'),
 'P02': ('Room for a busy team', '300 Mbps supports concurrent video calls, shared files and cloud apps.'),
 'P04': ('Connect without a fibre line', 'Wireless broadband is an alternative where fibre is unavailable; coverage still needs a site check.'),
 'P05': ('Dedicated capacity for operations', 'A dedicated 1:1 link suits production systems; the longer installation lead time is shown upfront.'),
 'P07': ('Keep your team reachable', 'A business mobile line separates work calls from a personal number.'),
 'P09': ('Keep working during an outage', 'An automatic backup connection supports your primary business link.'),
 'P15': ('Bring customers back', 'A business WhatsApp channel supports customer communication; platform and registration approvals still apply.'),
 'P16': ('Work under your own name', 'Business email and shared documents for the number of users you selected.'),
 'P17': ('Protect work from accidental loss', 'A 1 TB cloud backup account keeps a second copy of important files.'),
 'P18': ('Be discoverable online', 'A starter website and domain create a simple online presence.'),
 'P19': ('Make billing easier', 'A single billing counter supports inventory and GST-ready invoicing.'),
 'P20': ('Protect your work devices', 'Endpoint protection is sized to the devices you asked to cover.'),
 'P23': ('See your business from anywhere', 'One cloud-connected camera per location; change quantities if needed.'),
 'P24': ('Understand machine uptime', 'One starter monitoring device per site; sensor suitability requires review.')}

def recommend(raw_profile, registry=None, use_ai=False):
    if use_ai:
        return recommend_gemini(raw_profile, registry)
    p = normalise_profile(raw_profile)
    has_gst = verified_gst(registry)
    provisional_gst = has_gst or p['has_gstin']
    candidates = []
    deferred_primary = []
    if 'internet' in p['needs'] or set(p['needs']).intersection({'backup', 'cctv'}):
        primary = 'P04' if p['fibre'] == 'unavailable' else ('P05' if p['segment'] == 'mfg' and p['employees'] >= 10 and provisional_gst else ('P01' if p['segment'] == 'micro' else 'P02'))
        original_primary = primary
        if primary != 'P04' and int(PRODUCTS[primary]['price_min_inr_per_month']) * p['sites'] * 1.18 > p['budget']:
            alternatives = ['P02', 'P01'] if primary == 'P05' else ['P01']
            primary = next((key for key in alternatives if int(PRODUCTS[key]['price_min_inr_per_month']) * p['sites'] * 1.18 <= p['budget']), 'P01')
            if primary != original_primary:
                prod = PRODUCTS[original_primary]
                deferred_primary.append({'id': original_primary, 'name': prod['product_name'], 'reason': 'A higher-capacity connection exceeds the starting budget; review this as an upgrade later.', 'monthly': int(prod['price_min_inr_per_month']) * p['sites']})
        candidates.append((primary, p['sites'], 'internet'))
    by_need = {'billing': 'P19', 'email': 'P16', 'security': 'P20', 'customers': 'P15' if provisional_gst else 'P18', 'backup': 'P09', 'cctv': 'P23', 'machines': 'P24', 'mobile': 'P07'}
    for need in p['needs']:
        if need in by_need:
            key = by_need[need]
            qty = p['seats'] if key in ['P16', 'P20'] else p['sites'] if PRODUCTS[key]['unit'] in ['per site', 'per camera', 'per device'] else 1
            candidates.append((key, qty, need))
    items, later = {}, deferred_primary
    for key, qty, need in candidates:
        prod = PRODUCTS[key]
        reason = REASONS.get(key, (prod['product_name'], prod['description']))[1]
        if not eligible(prod, p, provisional_gst):
            later.append({'id': key, 'name': prod['product_name'], 'reason': 'Needs an active GST registration or a larger team/site count.', 'monthly': int(prod['price_min_inr_per_month']) * qty})
            continue
        trial = dict(items, **{key: qty})
        try:
            q = quote(p, trial, has_gst, True)
        except ValueError:
            later.append({'id': key, 'name': prod['product_name'], 'reason': 'Needs a compatible primary internet service in this plan.', 'monthly': int(prod['price_min_inr_per_month']) * qty})
            continue
        if not items or q['total'] <= p['budget']:
            items[key] = qty
        else:
            later.append({'id': key, 'name': prod['product_name'], 'reason': 'Keep for phase two to respect your monthly budget.', 'monthly': int(prod['price_min_inr_per_month']) * qty})
    if not items:
        raise ValueError('These goals need a registration or setup we cannot fulfil online yet. Request a partner review, or choose another goal.')
    q = quote(p, items, has_gst, True)
    reasons = [{'id': key, 'title': REASONS.get(key, (PRODUCTS[key]['product_name'], ''))[0], 'text': REASONS.get(key, ('', PRODUCTS[key]['description']))[1]} for key in items]
    explanation = f"Start with {len(items)} services for your {SEGMENTS[p['segment']].lower()}. "
    explanation += 'The essentials exceed your budget, so review the gap before continuing.' if q['budget_gap'] else 'This keeps your recurring bill within the budget you set, including estimated GST.'
    if p['language'] == 'Hindi':
        explanation = f"आपके व्यवसाय के लिए {len(items)} सेवाओं से शुरुआत करें। " + ('ज़रूरी सेवाएँ आपके बजट से ऊपर हैं। आगे बढ़ने से पहले खर्च की समीक्षा करें।' if q['budget_gap'] else 'अनुमानित GST सहित मासिक खर्च आपके बजट के अंदर है। बाकी सेवाएँ बाद में जोड़ सकते हैं।')
    mode = 'guided'
    return {'profile': p, 'items': items, 'quote': q, 'reasons': reasons, 'later': later, 'explanation': explanation, 'mode': mode}

def recommend_gemini(raw_profile, registry=None):
    if any(not raw_profile.get(k) for k in ['segment', 'employees', 'needs', 'budget']):
        raise ValueError('Add your business type, team size, goals and budget before building a Gemini plan.')
    p = normalise_profile(raw_profile)
    active_gst = verified_gst(registry)
    provisional = active_gst or p['has_gstin']
    allowed = set()
    goal_products = gemini.GOAL_PRODUCTS
    for goal in p['needs']: allowed.update(goal_products[goal])
    if set(p['needs']).intersection({'backup','cctv'}): allowed.update(goal_products['internet'])
    catalogue = [prod for prod in CAT if prod['product_id'] in allowed and eligible(prod, p, provisional)]
    proposal = gemini.propose(p, catalogue)
    suggested = proposal.get('items')
    if not isinstance(suggested, list) or not 1 <= len(suggested) <= 12:
        raise gemini.AIUnavailable('Gemini could not propose a supported plan. Try again or switch to guided mode.')
    items, reasons, later, seen = {}, [], [], set()
    for entry in suggested:
        if not isinstance(entry, dict):
            raise gemini.AIUnavailable('Gemini returned an unsupported plan. Try again or switch to guided mode.')
        key, qty, reason = entry.get('id'), entry.get('quantity'), entry.get('reason')
        if key not in PRODUCTS or key in seen or type(qty) is not int or not 1 <= qty <= 1000 or not isinstance(reason, str):
            raise gemini.AIUnavailable('Gemini returned an unsupported service or quantity. Try again or switch to guided mode.')
        if key not in {prod['product_id'] for prod in catalogue} or (key in ['P16','P20'] and qty != p['seats']):
            raise gemini.AIUnavailable('Gemini’s proposal did not match your stated goals or device counts. Try again or use guided mode.')
        seen.add(key)
        try:
            trial = quote(p, dict(items, **{key: qty}), active_gst, True)
        except ValueError:
            raise gemini.AIUnavailable('Gemini’s proposal did not pass catalogue checks. Your previous plan is saved. Try again or switch to guided mode.') from None
        prod = PRODUCTS[key]
        if items and trial['total'] > p['budget']:
            later.append({'id': key, 'name': prod['product_name'], 'reason': 'Deferred by the pricing engine to keep the monthly total within your budget.', 'monthly': int(prod['price_min_inr_per_month']) * qty})
        else:
            items[key] = qty
            reasons.append({'id': key, 'title': prod['product_name'], 'text': reason[:350]})
    uncovered = [goal for goal in p['needs'] if not seen.intersection(goal_products[goal])]
    if uncovered:
        raise gemini.AIUnavailable('Gemini did not cover every requested goal. Try again or switch to guided mode.')
    if not items:
        raise gemini.AIUnavailable('Gemini did not find a valid starting plan. Choose guided mode or ask a partner for help.')
    q = quote(p, items, active_gst, True)
    explanation = ('Gemini selected these services from your stated needs. The server checked quantities, dependencies and every price. '
                   + ('The essentials exceed your budget; review the gap before reserving.' if q['budget_gap'] else 'Your recurring bill fits the budget you set, including estimated GST.'))
    if p['language'] == 'Hindi':
        explanation = 'Gemini ने आपकी ज़रूरतों के अनुसार सेवाएँ चुनी हैं। सर्वर ने संख्या, निर्भरता और सभी कीमतों की जाँच की है। ' + ('ज़रूरी सेवाएँ बजट से ऊपर हैं। आगे बढ़ने से पहले अंतर देखें।' if q['budget_gap'] else 'अनुमानित GST सहित मासिक खर्च आपके बजट में है।')
    return {'profile': p, 'items': items, 'quote': q, 'reasons': reasons, 'later': later,
            'explanation': explanation, 'mode': 'gemini', 'model': os.getenv('GEMINI_MODEL', gemini.DEFAULT_MODEL)}


def parse_message(text, previous):
    p = dict(previous)
    t = text.lower()[:1500]
    if any(w in t for w in ['clinic', 'office', 'dental', 'coaching', 'accountant', 'क्लिनिक', 'ऑफिस']): p['segment'] = 'prof'
    elif any(w in t for w in ['manufactur', 'factory', 'unit', 'फैक्ट्री']): p['segment'] = 'mfg'
    elif any(w in t for w in ['chain', 'multi-site', 'branches']): p['segment'] = 'multi'
    elif any(w in t for w in ['shop', 'retail', 'kirana', 'salon', 'store', 'दुकान', 'किराना']): p['segment'] = 'micro'
    for key, pattern in [('employees', r'(\d+)\s*(?:staff|employees|people|team members|कर्मचारी|लोग)'), ('sites', r'(\d+)\s*(?:sites?|locations?|branches|stores|शाखा)'), ('budget', r'(?:budget|बजट|₹|rs\.?|inr)(?:\s+(?:of|including|incl\.?|gst|is|per|month|monthly|in|rupees|taxes|tax))*[\s:₹]*([\d,]+)')]:
        match = re.search(pattern, t)
        if match: p[key] = int(match.group(1).replace(',', ''))
    found = []
    for key, words in {'internet': ['internet', 'wifi', 'wi-fi', 'broadband', 'इंटरनेट'], 'billing': ['billing', 'pos', 'बिलिंग'], 'email': ['email', 'ईमेल'], 'security': ['security', 'antivirus', 'सुरक्षा'], 'customers': ['whatsapp', 'marketing', 'customers', 'ग्राहक'], 'backup': ['backup', 'outage', 'failover'], 'cctv': ['cctv', 'camera'], 'machines': ['sensor', 'monitoring'], 'mobile': ['sim', 'mobile plan']}.items():
        if any(w in t for w in words): found.append(key)
    if found: p['needs'] = list(dict.fromkeys(p.get('needs', []) + found))
    for city, tier in [('Mumbai', '1'), ('Delhi', '1'), ('Bengaluru', '1'), ('Chennai', '1'), ('Pune', '1'), ('Hyderabad', '1'), ('Jaipur', '2'), ('Lucknow', '2'), ('Kochi', '2'), ('Indore', '2')]:
        if city.lower() in t: p.update(city=city, tier=tier)
    if any(w in t for w in ['no gst', 'without gst', 'gst नहीं']): p['has_gstin'] = False
    elif 'gst' in t: p['has_gstin'] = True
    return p

# OCR runs entirely on this computer. Source files are removed immediately after use.
def read_document(path):
    native = ROOT / 'tools' / 'ocr'
    if sys.platform == 'darwin' and native.exists() and os.access(native, os.X_OK):
        result = subprocess.run([str(native), str(path)], capture_output=True, text=True, timeout=40, check=True)
        lines = json.loads(result.stdout)['lines']
        return lines, 'Apple Vision · on-device'
    if shutil.which('tesseract'):
        result = subprocess.run(['tesseract', str(path), 'stdout'], capture_output=True, text=True, timeout=40, check=True)
        return [{'text': line, 'confidence': .7} for line in result.stdout.splitlines() if line.strip()], 'Tesseract · local'
    raise ValueError('Local OCR is not installed. On macOS run: swiftc tools/ocr.swift -o tools/ocr. Else install Tesseract. Registry verification still works.')

def extract_document(lines, kind):
    texts = [r['text'] for r in lines]
    text = '\n'.join(texts)
    patterns = {'gst': r'\b[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][A-Z0-9]Z[A-Z0-9]\b', 'udyam': r'UDYAM[-\s][A-Z]{2}[-\s]\d{2}[-\s]\d{7}'}
    match = re.search(patterns.get(kind, r'(?!)'), text.upper())
    identifier = match.group(0).replace(' ', '-') if match else ''
    # Layout-aware extraction preserves a value on the right of its printed label.
    def value(label):
        for i, line in enumerate(lines):
            if label.lower() in line['text'].lower():
                if 'x' in line:
                    candidates = [r for r in lines if r.get('x', 0) > .35 and abs(r.get('y', 0) - line.get('y', 0)) < .018 and r is not line]
                    if candidates: return ' '.join(r['text'] for r in candidates)
                if i + 1 < len(lines): return lines[i + 1]['text']
        return ''
    def block(label, stop):
        start = next((r for r in lines if label.lower() in r['text'].lower()), None)
        end = next((r for r in lines if stop.lower() in r['text'].lower()), None)
        if start and end and 'y' in start and 'y' in end:
            return ' '.join(r['text'] for r in lines if r.get('x', 0) > .35 and end['y'] + .018 < r.get('y', 0) <= start['y'] + .018)
        return value(label)
    fields = {}
    if kind == 'gst': fields = {'business_name': value('Legal name'), 'trade_name': value('Trade name'), 'address': block('Address of principal', 'Date of liability')}
    elif kind == 'udyam': fields = {'business_name': value('Enterprise name')}
    elif kind == 'address': fields = {'business_name': value('Billed to') or value('Consumer name') or value('Customer name') or value('Name'), 'date': value('Bill date') or value('Issue date') or value('Date of issue'), 'address': block('Supply address', 'Bill date')}
    elif kind == 'signatory': fields = {'signatory': value('Name'), 'business_name': value('Business')}
    dates = re.findall(r'\b20\d{2}-\d{2}-\d{2}\b', text)
    if kind == 'address' and not fields.get('date') and dates: fields['date'] = max(dates)
    checks = []
    expected = {'gst': ['gst', 'gstin'], 'udyam': ['udyam'], 'address': ['electricity', 'utility', 'bill date'], 'signatory': ['signatory', 'id no.']}
    if not any(term in text.lower() for term in expected[kind]):
        checks.append({'status': 'error', 'title': 'This looks like a different document type', 'detail': 'Check the selected document category and upload the matching file.'})
    if kind in ['gst', 'udyam']:
        registry = (GST if kind == 'gst' else UDY).get(identifier)
        if not identifier: checks.append({'status': 'warning', 'title': 'Registration number needs a second look', 'detail': 'OCR could not confidently read the number. Enter it in the registry check above.'})
        elif not registry: checks.append({'status': 'error', 'title': 'Registration not found', 'detail': 'The extracted identifier does not match the supplied synthetic registry.'})
        else:
            status = registry.get('status', 'Registered')
            checks.append({'status': 'pass' if status in ['Active', 'Registered'] else 'error', 'title': 'Registry status: ' + status, 'detail': 'Matched against the supplied mock registry.'})
            canonical = registry.get('legal_name', registry.get('enterprise_name', ''))
            if fields.get('business_name') and not names_match(fields['business_name'], canonical):
                checks.append({'status': 'error', 'title': 'Certificate name differs from the registry', 'detail': 'Confirm the legal name and upload the matching certificate.'})
            fields.setdefault('business_name', canonical)
    if kind == 'address':
        try:
            date = dt.date.fromisoformat(fields.get('date', '')[:10])
            age = (REFERENCE_DATE - date).days
            checks.append({'status': 'pass' if 0 <= age <= 90 else 'error', 'title': 'Address proof is recent' if 0 <= age <= 90 else 'Address proof needs replacing', 'detail': f'{age} days old against the fixture date 15 Jul 2026; the allowed window is 0–90 days.'})
        except ValueError:
            checks.append({'status': 'warning', 'title': 'Bill date could not be confirmed', 'detail': 'Upload a sharper copy with a visible issue date, or ask for a review.'})
    if kind == 'signatory':
        expiry = value('Valid till')
        try:
            expires = dt.date.fromisoformat(expiry[:10])
            checks.append({'status': 'pass' if expires >= REFERENCE_DATE else 'error', 'title': 'Signatory ID is in date' if expires >= REFERENCE_DATE else 'Signatory ID has expired', 'detail': 'Checked against the specimen reference date, 15 Jul 2026.'})
        except ValueError:
            checks.append({'status': 'warning', 'title': 'ID validity needs review', 'detail': 'The expiry date could not be read clearly.'})
        checks.append({'status': 'pass' if fields.get('signatory') else 'warning', 'title': 'Signatory name extracted' if fields.get('signatory') else 'Signatory name needs review', 'detail': 'Name matching is a triage check, not an identity or authenticity decision.'})
    fields = {k: v for k, v in fields.items() if v}
    confidence = round(sum(r.get('confidence', .7) for r in lines) / max(1, len(lines)), 2)
    return {'kind': kind, 'fields': fields, 'identifier': mask(identifier), 'identifier_hash': hashlib.sha256(identifier.encode()).hexdigest() if identifier else '', 'confidence': confidence, 'checks': checks, 'extracted_at': now()}

def name_tokens(name):
    return set(re.findall(r'[a-z]{3,}', name.lower())) - {'prop', 'proprietor', 'private', 'limited', 'pvt', 'ltd', 'the', 'name', 'llp'}

def names_match(a, b):
    ta, tb = name_tokens(a), name_tokens(b)
    return bool(ta and tb and (ta <= tb or tb <= ta or len(ta & tb) / len(ta | tb) >= .65 or SequenceMatcher(None, ' '.join(sorted(ta)), ' '.join(sorted(tb))).ratio() > .88))


def addresses_match(a, b):
    pa, pb = re.findall(r'\b[0-9]{6}\b', a), re.findall(r'\b[0-9]{6}\b', b)
    if pa and pb and pa[-1] != pb[-1]: return False
    clean = lambda value: re.sub(r'[^a-z0-9]', '', value.lower())
    return SequenceMatcher(None, clean(a), clean(b)).ratio() >= .82


def kyc_summary(session):
    documents = session.get('documents', {})
    reg = session.get('registry')
    checks = []
    if reg:
        checks.append({'status': 'pass' if reg['status'] in ['Active', 'Registered'] else 'error', 'title': 'Business registration', 'detail': reg['name'] + ' · ' + reg['status']})
    else:
        checks.append({'status': 'warning', 'title': 'Verify a business registration', 'detail': 'Use an active GSTIN or an Udyam number.'})
    for kind in ['address', 'signatory']:
        if kind not in documents:
            checks.append({'status': 'warning', 'title': ('Address proof' if kind == 'address' else 'Signatory ID') + ' is still needed', 'detail': 'Upload an image or use a supplied specimen.'})
    for kind, document in documents.items():
        checks.extend(document['checks'])
        if reg and kind in ['gst', 'udyam'] and document.get('identifier_hash'):
            if reg['kind'] == kind and reg['identifier_hash'] != document['identifier_hash']:
                checks.append({'status': 'error', 'title': 'Registration numbers do not match', 'detail': 'The uploaded certificate belongs to a different registry record.'})
        if reg and document['fields'].get('business_name'):
            if not names_match(reg['name'], document['fields']['business_name']):
                checks.append({'status': 'error', 'title': 'Business names differ across documents', 'detail': 'Check that all files belong to the same business. A partner can review legal-name differences.'})
        if reg and reg.get('address') and kind in ['gst', 'address'] and document['fields'].get('address'):
            if not addresses_match(reg['address'], document['fields']['address']):
                checks.append({'status': 'error', 'title': 'Business addresses do not match', 'detail': 'The extracted address differs from the registered principal address. Replace the file or request help with a different installation address.'})
    errors = sum(c['status'] == 'error' for c in checks)
    warnings = sum(c['status'] == 'warning' for c in checks)
    return {'state': 'attention' if errors else 'incomplete' if warnings else 'ready', 'checks': checks, 'errors': errors, 'warnings': warnings,
            'label': 'Action needed' if errors else 'A few details left' if warnings else 'Ready for a partner review'}

def analytics():
    active = [l for l in LEADS if l['outcome'] == 'Activated']
    groups = []
    for segment in sorted({l['segment'] for l in LEADS}):
        ls = [l for l in LEADS if l['segment'] == segment]
        ac = [l for l in ls if l['outcome'] == 'Activated']
        groups.append({'segment': segment, 'leads': len(ls), 'activated': len(ac), 'conversion': round(100 * len(ac) / len(ls), 2),
                       'revenue': round(sum(float(l['monthly_bundle_value_inr'] or 0) for l in ac)),
                       'cac': round(sum(float(l['acquisition_cost_inr'] or 0) for l in ls) / max(1, len(ac)))})
    stages = ['Lead', 'Contacted', 'Needs assessed', 'Quote accepted', 'KYC complete', 'Activated']
    funnel = [{'stage': stage, 'count': sum(stages.index(l['stage_reached']) >= i for l in LEADS)} for i, stage in enumerate(stages)]
    drops = collections.Counter(l['drop_reason'] for l in LEADS if l['drop_reason'])
    def priority(l):
        stage = stages.index(l['stage_reached'])
        return min(99, 20 + stage * 13 + (8 if l['drop_reason'] in ['Document details did not match', 'Missing GST or Udyam document', 'Installation delayed beyond 21 days', 'First payment failed'] else 0))
    queue = []
    for l in sorted((l for l in LEADS if l['outcome'] != 'Activated'), key=priority, reverse=True)[:30]:
        reason = l['drop_reason']
        action = 'Review the document mismatch' if 'match' in reason else 'Offer a new installation slot' if 'Installation' in reason else 'Resolve the failed payment' if 'payment' in reason else 'Help complete registration' if 'document' in reason.lower() else 'Call to resolve the last blocker'
        queue.append({'id': l['lead_id'], 'business': l['business_name'], 'city': l['city'], 'stage': l['stage_reached'], 'reason': reason, 'priority': priority(l), 'action': action})
    return {'leads': len(LEADS), 'activated': len(active), 'conversion': round(100 * len(active) / len(LEADS), 2),
            'days': round(sum(float(l['days_lead_to_activation']) for l in active) / len(active), 1),
            'cac': round(sum(float(l['acquisition_cost_inr'] or 0) for l in LEADS) / len(active)),
            'churn': round(100 * sum(l['churned_within_6_months'] in ['Y', '1', 'Yes'] for l in active) / len(active), 1),
            'segments': groups, 'funnel': funnel, 'drop_reasons': [{'reason': reason, 'count': count} for reason, count in drops.most_common(8)], 'queue': queue}

class Handler(BaseHTTPRequestHandler):
    server_version = 'Udyam/2'

    def log_message(self, *args):
        pass

    def session(self):
        if hasattr(self, '_session'):
            return self._session
        cookie = SimpleCookie()
        try: cookie.load(self.headers.get('Cookie', ''))
        except Exception: pass
        sid = cookie.get('udyam_session')
        sid = sid.value if sid else ''
        with connect() as c:
            cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=24)).isoformat()
            c.execute('DELETE FROM sessions WHERE updated < ?', (cutoff,))
            record = c.execute('SELECT payload FROM sessions WHERE id=?', (sid,)).fetchone()
            if record:
                state = json.loads(record['payload'])
                c.execute('UPDATE sessions SET updated=? WHERE id=?', (now(), sid))
            else:
                sid = secrets.token_urlsafe(32)
                state = empty_session()
                c.execute('INSERT INTO sessions VALUES (?,?,?,?)', (sid, now(), now(), json.dumps(state)))
        self.sid = sid
        self._session = state
        return state

    def save_session(self, state):
        with connect() as c:
            c.execute('UPDATE sessions SET updated=?,payload=? WHERE id=?', (now(), json.dumps(state), self.sid))
        self._session = state

    def send(self, body, status=200, content_type='application/json; charset=utf-8'):
        raw = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(raw)))
        self.send_header('Cache-Control', 'no-store' if '/api/' in self.path else 'no-cache')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'same-origin')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' blob: data:; font-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
        if hasattr(self, 'sid'):
            self.send_header('Set-Cookie', f'udyam_session={self.sid}; HttpOnly; SameSite=Strict; Path=/; Max-Age=86400')
        self.end_headers()
        self.wfile.write(raw)

    def public_state(self, state):
        with connect() as c:
            orders = [json.loads(r['payload']) for r in c.execute('SELECT payload FROM orders WHERE session_id=? ORDER BY created DESC', (self.sid,))]
        return dict(state, kyc=kyc_summary(state), orders=orders)

    def do_GET(self):
        try:
            path = urlparse(self.path).path
            if path == '/api/bootstrap':
                state = self.session()
                return self.send({'state': self.public_state(state), 'catalogue': CAT,
                                  'capabilities': {'ai': gemini.configured(), 'ai_provider': 'Gemini', 'ai_model': os.getenv('GEMINI_MODEL', gemini.DEFAULT_MODEL), 'ocr': (sys.platform == 'darwin' and (ROOT / 'tools' / 'ocr').exists()) or bool(shutil.which('tesseract'))},
                                  'reference_date': REFERENCE_DATE.isoformat()})
            if path == '/api/analytics':
                self.session()
                result = analytics()
                with connect() as c:
                    result['orders'] = [json.loads(r['payload']) for r in c.execute('SELECT payload FROM orders WHERE session_id=? ORDER BY created DESC', (self.sid,))]
                    result['callbacks'] = [dict(id=r['id'], **json.loads(r['payload'])) for r in c.execute('SELECT * FROM callbacks WHERE session_id=? ORDER BY created DESC', (self.sid,))]
                return self.send(result)
            if path.startswith('/api/order/'):
                self.session()
                oid = path.split('/')[-1]
                with connect() as c:
                    r = c.execute('SELECT payload FROM orders WHERE id=? AND session_id=?', (oid, self.sid)).fetchone()
                return self.send(json.loads(r['payload']) if r else {'error': 'Order not found in this workspace.'}, 200 if r else 404)
            if path == '/api/catalogue': return self.send(CAT)
            if path == '/api/health': return self.send({'ok': True, 'version': 2})
            if path == '/favicon.svg': file = ROOT / 'static' / 'favicon.svg'
            elif path in ['/', '/index.html']: file = ROOT / 'static' / 'index.html'
            else: file = ROOT / 'static' / path.lstrip('/')
            file = file.resolve()
            if not file.is_relative_to((ROOT / 'static').resolve()) or not file.is_file():
                return self.send({'error': 'Not found'}, 404)
            self.send(file.read_bytes(), content_type=mimetypes.guess_type(file.name)[0] or 'application/octet-stream')
        except Exception:
            self.send({'error': 'Could not load the workspace.'}, 500)

    def do_POST(self):
        try:
            origin = self.headers.get('Origin')
            if origin and origin not in ['http://' + self.headers.get('Host', '')]:
                return self.send({'error': 'This workspace only accepts requests from its own page.'}, 403)
            size = int(self.headers.get('Content-Length', 0))
            if size < 1 or size > 12000000:
                raise ValueError('Use a request smaller than 12 MB.')
            if not self.headers.get('Content-Type', '').startswith('application/json'):
                raise ValueError('JSON requests are required.')
            b = json.loads(self.rfile.read(size))
            if not isinstance(b, dict): raise ValueError('Invalid request.')
            path = self.path
            state = self.session()
            if path == '/api/profile':
                state['profile'] = normalise_profile(b['profile'])
                self.save_session(state)
                return self.send(self.public_state(state))
            if path == '/api/ai-consent':
                if type(b.get('consent')) is not bool: raise ValueError('Choose whether to use Gemini assistance.')
                state['ai_consent'] = b['consent']
                if not b['consent']: state['advisor_reply'] = ''
                self.save_session(state)
                return self.send(self.public_state(state))
            if path == '/api/advisor':
                if state.get('ai_consent'):
                    result = gemini.discover(str(b.get('message', '')), b.get('profile', state.get('profile', {})))
                    literal = parse_message(str(b.get('message', '')), {})
                    for field in ['employees', 'sites', 'budget', 'city', 'tier']:
                        if field in literal: result['profile'][field] = literal[field]
                    if literal.get('needs') and not re.search(r'\b(no|not|without|remove|stop|don.t)\b', str(b.get('message', '')).lower()):
                        result['profile']['needs'] = list(dict.fromkeys(result['profile'].get('needs', []) + literal['needs']))
                    missing = [k for k in ['segment','employees','sites','current_tools','needs','budget'] if not result['profile'].get(k)]
                    result['ready'], result['next'] = not missing, missing[0] if missing else 'ready'
                    if not missing:
                        result['reply'] = 'आपकी ज़रूरतें दर्ज हैं। सारांश देखें और अपना प्लान बनाएँ।' if result['profile'].get('language') == 'Hindi' else 'Your needs are captured. Review the summary, then build your plan.'
                    state['advisor_draft'], state['advisor_reply'] = gemini.safe_profile(result['profile']), result['reply']
                    self.save_session(state)
                    return self.send(result)
                profile = parse_message(str(b.get('message', '')), b.get('profile', state.get('profile', {})))
                updates = b.get('updates', {})
                if not isinstance(updates, dict): raise ValueError('Invalid profile details.')
                for key in ['segment', 'employees', 'sites', 'seats', 'needs', 'budget', 'current_spend', 'fibre', 'tier', 'city', 'language', 'has_gstin', 'name']:
                    if key in updates: profile[key] = updates[key]
                missing = [k for k in ['segment', 'employees', 'needs', 'budget'] if not profile.get(k)]
                return self.send({'profile': profile, 'next': missing[0] if missing else 'ready', 'ready': not missing})
            if path in ['/api/recommend', '/api/advise']:
                result = recommend(b.get('profile', state['profile']), state.get('registry'), use_ai=state.get('ai_consent', False))
                state['profile'], state['items'], state['plan'] = result['profile'], result['items'], result
                self.save_session(state)
                return self.send(result)
            if path == '/api/quote':
                p = b.get('profile', state['profile'])
                q = quote(p, b['items'], verified_gst(state.get('registry')), True)
                state['profile'], state['items'] = normalise_profile(p), b['items']
                self.save_session(state)
                return self.send(q)
            if path == '/api/consent':
                state['consent'] = b.get('consent') is True
                if state['consent']: state['consent_at'] = now()
                self.save_session(state)
                return self.send(self.public_state(state))
            if path in ['/api/lookup', '/api/document', '/api/specimen']:
                if state.get('consent') is not True:
                    raise ValueError('Please give consent before business or document verification.')
                if path == '/api/lookup':
                    kind = b.get('kind')
                    if kind not in ['gst', 'udyam']: raise ValueError('Choose GSTIN or Udyam.')
                    value = str(b.get('value', '')).strip().upper()
                    if kind == 'gst' and not valid_gstin(value):
                        raise ValueError('That GSTIN has an invalid format or checksum. Check all 15 characters.')
                    if kind == 'udyam' and not re.fullmatch(r'UDYAM-[A-Z]{2}-[0-9]{2}-[0-9]{7}', value):
                        raise ValueError('Use the Udyam format UDYAM-XX-00-0000000.')
                    registry = (GST if kind == 'gst' else UDY).get(value)
                    if not registry: raise ValueError('No match in the synthetic registry. Check the number and try again.')
                    state['registry'] = {'kind': kind, 'name': registry.get('legal_name', registry.get('enterprise_name')), 'status': registry.get('status', 'Registered'), 'masked': mask(value), 'identifier_hash': hashlib.sha256(value.encode()).hexdigest(), 'address': registry.get('principal_place_of_business', ''), 'checked_at': now()}
                    self.save_session(state)
                    return self.send(self.public_state(state))
                kind = b.get('kind')
                if kind not in ['gst', 'udyam', 'address', 'signatory']: raise ValueError('Choose a document type.')
                if path == '/api/specimen':
                    applicant = b.get('applicant', '')
                    if not re.fullmatch(r'K(?:0[1-9]|1[0-2])', applicant): raise ValueError('Choose a supplied specimen.')
                    suffix = {'gst': 'gst_certificate', 'udyam': 'udyam_certificate', 'address': 'address_proof', 'signatory': 'signatory_id'}[kind]
                    file = DATA / 'kyc_documents' / f'{applicant}_{suffix}.png'
                    if not file.exists(): raise ValueError('This applicant has no specimen of that type.')
                    lines, engine = read_document(file)
                else:
                    try: raw = base64.b64decode(b.get('data', ''), validate=True)
                    except Exception: raise ValueError('The image could not be read.')
                    if not raw or len(raw) > 8000000: raise ValueError('Choose an image smaller than 8 MB.')
                    if not (raw.startswith(b'\x89PNG') or raw.startswith(b'\xff\xd8\xff')): raise ValueError('Use a PNG or JPEG image.')
                    with tempfile.TemporaryDirectory(prefix='udyam-ocr-') as directory:
                        file = Path(directory) / 'document.png'
                        file.write_bytes(raw)
                        lines, engine = read_document(file)
                doc = extract_document(lines, kind)
                doc['engine'] = engine
                state['documents'][kind] = doc
                self.save_session(state)
                return self.send(self.public_state(state))
            if path == '/api/callback':
                topic = str(b.get('topic', 'Help with my launch'))[:160]
                ticket = 'HELP-' + secrets.token_hex(3).upper()
                with connect() as c:
                    c.execute('INSERT INTO callbacks VALUES (?,?,?,?)', (ticket, self.sid, now(), json.dumps({'topic': topic, 'status': 'Requested', 'created': now(), 'name': state.get('profile', {}).get('name', 'Your business')})))
                return self.send({'id': ticket, 'status': 'Requested', 'message': 'Added to the local partner queue. No external call or message was sent.'}, 201)
            if path == '/api/order':
                if not state.get('consent'): raise ValueError('Consent is needed to submit an order.')
                if not state.get('registry') or state['registry']['status'] not in ['Active', 'Registered']:
                    raise ValueError('Verify an active GSTIN or Udyam registration first.')
                if b.get('accept') is not True: raise ValueError('Confirm that you have reviewed this demo quote.')
                # Recompute all prices and eligibility on the server, using verified GST only.
                q = quote(state['profile'], state['items'], verified_gst(state['registry']))
                if b.get('idempotency') and state.get('last_checkout') == b['idempotency']:
                    with connect() as c:
                        record = c.execute('SELECT payload FROM orders WHERE id=? AND session_id=?', (state['last_order'], self.sid)).fetchone()
                    if record: return self.send(json.loads(record['payload']))
                oid = 'UD-' + secrets.token_hex(4).upper()
                summary = kyc_summary(state)
                order = {'id': oid, 'name': state['registry']['name'], 'created': now(), 'quote': q,
                         'city': state['profile'].get('city'), 'status': 'review', 'kyc': summary,
                         'events': [{'status': 'submitted', 'at': now(), 'title': 'Your plan is reserved', 'detail': 'Demo order created. No money charged.'}, {'status': 'review', 'at': now(), 'title': 'Verification review', 'detail': summary['label']}],
                         'registration_hash': state['registry']['identifier_hash'], 'consent_version': '2.0', 'consent_at': state.get('consent_at'), 'installation': None}
                with connect() as c: c.execute('INSERT INTO orders VALUES (?,?,?,?)', (oid, self.sid, now(), json.dumps(order)))
                state.update(last_order=oid, last_checkout=str(b.get('idempotency', ''))[:100])
                self.save_session(state)
                return self.send(order, 201)
            if path == '/api/order/advance':
                if not state.get('consent'): raise ValueError('Consent has been paused. Restore consent before progressing this order.')
                oid = str(b.get('id', ''))
                with connect() as c:
                    record = c.execute('SELECT payload FROM orders WHERE id=? AND session_id=?', (oid, self.sid)).fetchone()
                    if not record: raise ValueError('Order not found in this workspace.')
                    order = json.loads(record['payload'])
                    target = {'review': 'verified', 'verified': 'scheduled', 'scheduled': 'active'}.get(order['status'])
                    if not target: raise ValueError('This order has no next step.')
                    note = str(b.get('note', '')).strip()[:500]
                    if len(note) < 8: raise ValueError('Add a meaningful review or completion note (at least 8 characters).')
                    if target == 'verified':
                        if order.get('registration_hash') != (state.get('registry') or {}).get('identifier_hash'):
                            raise ValueError('Verify the registration used for this order before approving it.')
                        summary = kyc_summary(state)
                        if summary['state'] != 'ready': raise ValueError('Resolve all KYC issues before approving verification.')
                        order['kyc'] = summary
                    if target == 'scheduled':
                        date = dt.date.fromisoformat(b.get('date', ''))
                        if date < dt.date.today(): raise ValueError('Choose today or a future installation date.')
                        order['installation'] = date.isoformat()
                    order['status'] = target
                    title = {'verified': 'Verification approved', 'scheduled': 'Installation scheduled', 'active': 'Services activated'}[target]
                    order['events'].append({'status': target, 'at': now(), 'title': title, 'detail': note, 'actor': 'Local demo operator'})
                    c.execute('UPDATE orders SET payload=? WHERE id=? AND session_id=?', (json.dumps(order), oid, self.sid))
                return self.send(order)
            if path == '/api/withdraw':
                with connect() as c:
                    c.execute('DELETE FROM sessions WHERE id=?', (self.sid,))
                self.sid = ''
                return self.send({'deleted': True})
            return self.send({'error': 'Not found'}, 404)
        except gemini.AIUnavailable as error:
            self.send({'error': str(error)}, 503)
        except (ValueError, KeyError, TypeError) as error:
            self.send({'error': str(error)}, 400)
        except (subprocess.SubprocessError, OSError):
            self.send({'error': 'The local document reader could not process that image. Try a clear, upright PNG or JPEG.'}, 422)
        except Exception:
            self.send({'error': 'Something interrupted this step. Your previous progress is still saved.'}, 500)

if __name__ == '__main__':
    os.umask(0o077)
    init_db()
    os.chmod(DB_PATH, 0o600)
    port = int(os.getenv('PORT', '8000'))
    print(f'Udyam Digital · http://localhost:{port}', flush=True)
    ThreadingHTTPServer(('127.0.0.1', port), Handler).serve_forever()
