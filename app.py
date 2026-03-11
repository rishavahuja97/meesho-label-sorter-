from flask import Flask, request, jsonify, send_file, render_template, session
from pypdf import PdfReader, PdfWriter
import pdfplumber
import os, re, json, uuid, shutil
from pathlib import Path

app = Flask(__name__)
app.secret_key = 'meesho-label-sorter-2024'

UPLOAD_DIR = Path('uploads')
OUTPUT_DIR = Path('output')
UPLOAD_DIR.mkdir(exist_ok=True)
OUTPUT_DIR.mkdir(exist_ok=True)

SIZE_ORDER = ['XS', 'S', 'M', 'L', 'XL', 'XXL', '2XL', '3XL', '4XL', '5XL']

def size_rank(size):
    s = size.upper().replace('XXXL','3XL').replace('XXxl','3XL')
    try:
        return SIZE_ORDER.index(s)
    except ValueError:
        return 99

def extract_label_info(page_text):
    """
    Extract SKU ID and size from a Meesho label page.
    
    Meesho label format (Product Details section):
      SKU Size Qty Color Order No.
      <sku_part1> <SIZE> <qty> <color> <order>
      <sku_part2?>           (if SKU wrapped to next line)
    
    The size column always contains one of: S M L XL XXL 2XL 3XL XS
    The SKU is everything before the size on that data row (and any continuation).
    """
    sku = None
    size = None
    
    # Find the Product Details table header
    # After "SKU Size Qty Color Order No." the next line(s) contain data
    header_match = re.search(
        r'SKU\s+Size\s+Qty\s+Color\s+Order\s*No\.?\s*\n(.*?)(?:\nTAX INVOICE|\Z)',
        page_text, re.DOTALL | re.IGNORECASE
    )
    
    if header_match:
        data_block = header_match.group(1).strip()
        lines = [l.strip() for l in data_block.split('\n') if l.strip()]
        
        # The size token is an exact size value — find it in first line
        size_re = re.compile(r'\b(3XL|XXL|2XL|XL|XS|S|M|L)\b')
        
        # First line should be: <SKU_start> <SIZE> <qty> <color> <order>
        if lines:
            first_line = lines[0]
            size_match = size_re.search(first_line)
            if size_match:
                size = size_match.group(1).upper()
                # SKU = everything before the size token on this line
                sku_part = first_line[:size_match.start()].strip()
                # Check if there's a second line continuing the SKU
                # (second line won't have a size/qty/color pattern)
                if len(lines) > 1:
                    second_line = lines[1]
                    # Second line is SKU continuation if it has no size token and no digits (qty) early on
                    if not size_re.search(second_line) and not re.match(r'^\d+\s', second_line):
                        sku_part = (sku_part + second_line).strip()
                sku = sku_part if sku_part else None
    
    # Fallback: scan whole text for size
    if size is None:
        m = re.search(r'\b(3XL|XXL|2XL|XL|XS|S|M|L)\b', page_text)
        if m:
            size = m.group(1).upper()
    
    # Clean SKU: remove internal spaces (PDF wrapping artifact)
    if sku:
        sku = re.sub(r'\s+', '', sku)  # remove all whitespace from SKU
    
    return sku, size

def process_pdfs(file_paths):
    """Read all PDFs and return list of {page_obj, sku, size, source_file}"""
    labels = []
    
    for fpath in file_paths:
        # Use pdfplumber for text extraction, pypdf for page objects
        try:
            plumber_pdf = pdfplumber.open(fpath)
            pypdf_reader = PdfReader(fpath)
            
            for i, (plumber_page, pypdf_page) in enumerate(zip(plumber_pdf.pages, pypdf_reader.pages)):
                text = plumber_page.extract_text() or ''
                sku, size = extract_label_info(text)
                labels.append({
                    'page': pypdf_page,
                    'sku': sku or 'UNKNOWN',
                    'size': size or 'UNKNOWN',
                    'source': Path(fpath).name,
                    'page_num': i,
                    'text_snippet': text[:200]
                })
            plumber_pdf.close()
        except Exception as e:
            print(f"Error reading {fpath}: {e}")
    
    return labels

def write_pdf(pages, output_path):
    writer = PdfWriter()
    for page in pages:
        writer.add_page(page)
    with open(output_path, 'wb') as f:
        writer.write(f)

# ─── Session store (in-memory, keyed by session_id) ───
sessions = {}

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/upload', methods=['POST'])
def upload():
    files = request.files.getlist('pdfs')
    if not files:
        return jsonify({'error': 'No files uploaded'}), 400
    
    session_id = str(uuid.uuid4())
    sess_dir = UPLOAD_DIR / session_id
    sess_dir.mkdir()
    
    saved_paths = []
    for f in files:
        if f.filename.endswith('.pdf'):
            dest = sess_dir / f.filename
            f.save(dest)
            saved_paths.append(str(dest))
    
    labels = process_pdfs(saved_paths)
    
    # Build SKU summary
    sku_map = {}
    for lbl in labels:
        sku = lbl['sku']
        if sku not in sku_map:
            sku_map[sku] = {'sizes': [], 'count': 0}
        sku_map[sku]['sizes'].append(lbl['size'])
        sku_map[sku]['count'] += 1
    
    # Deduplicate sizes per SKU
    for sku in sku_map:
        sizes = list(dict.fromkeys(sku_map[sku]['sizes']))
        sizes_sorted = sorted(sizes, key=size_rank)
        sku_map[sku]['sizes'] = sizes_sorted
    
    sessions[session_id] = {
        'labels': [{
            'sku': l['sku'],
            'size': l['size'],
            'source': l['source'],
            'page_num': l['page_num']
        } for l in labels],
        'file_paths': saved_paths,
        'sku_map': sku_map,
        'groups': {}  # manual groups: {group_name: [sku1, sku2, ...]}
    }
    
    return jsonify({
        'session_id': session_id,
        'total_labels': len(labels),
        'skus': sku_map,
        'files': [Path(p).name for p in saved_paths]
    })

@app.route('/save_groups', methods=['POST'])
def save_groups():
    data = request.json
    sid = data.get('session_id')
    groups = data.get('groups', {})
    
    if sid not in sessions:
        return jsonify({'error': 'Session not found'}), 404
    
    sessions[sid]['groups'] = groups
    return jsonify({'ok': True})

@app.route('/download', methods=['POST'])
def download():
    data = request.json
    sid = data.get('session_id')
    mode = data.get('mode')  # 'single_sku' | 'mix_skus' | 'group' | 'by_size'
    
    if sid not in sessions:
        return jsonify({'error': 'Session expired, please re-upload'}), 404
    
    sess = sessions[sid]
    file_paths = sess['file_paths']
    groups = sess.get('groups', {})
    
    # Re-read pages
    all_labels = []
    for fpath in file_paths:
        try:
            plumber_pdf = pdfplumber.open(fpath)
            pypdf_reader = PdfReader(fpath)
            for i, (pp, pyp) in enumerate(zip(plumber_pdf.pages, pypdf_reader.pages)):
                text = pp.extract_text() or ''
                sku, size = extract_label_info(text)
                all_labels.append({
                    'page': pyp,
                    'sku': sku or 'UNKNOWN',
                    'size': size or 'UNKNOWN',
                })
            plumber_pdf.close()
        except Exception as e:
            return jsonify({'error': str(e)}), 500
    
    out_id = str(uuid.uuid4())[:8]
    
    if mode == 'single_sku':
        target_sku = data.get('sku')
        pages = [l for l in all_labels if l['sku'] == target_sku]
        pages_sorted = sorted(pages, key=lambda x: size_rank(x['size']))
        if not pages_sorted:
            return jsonify({'error': f'No labels found for SKU {target_sku}'}), 404
        out_path = OUTPUT_DIR / f'SKU_{target_sku}_{out_id}.pdf'
        write_pdf([p['page'] for p in pages_sorted], out_path)
        return send_file(out_path, as_attachment=True, download_name=f'SKU_{target_sku}_sorted.pdf')
    
    elif mode == 'mix_skus':
        selected_skus = data.get('skus', [])
        pages = [l for l in all_labels if l['sku'] in selected_skus]
        pages_sorted = sorted(pages, key=lambda x: (size_rank(x['size']), x['sku']))
        if not pages_sorted:
            return jsonify({'error': 'No labels found for selected SKUs'}), 404
        name = 'MIX_' + '_'.join(selected_skus[:3])
        out_path = OUTPUT_DIR / f'{name}_{out_id}.pdf'
        write_pdf([p['page'] for p in pages_sorted], out_path)
        return send_file(out_path, as_attachment=True, download_name=f'{name}_sorted_by_size.pdf')
    
    elif mode == 'group':
        group_name = data.get('group_name')
        group_skus = groups.get(group_name, [])
        if not group_skus:
            return jsonify({'error': f'Group "{group_name}" has no SKUs'}), 404
        pages = [l for l in all_labels if l['sku'] in group_skus]
        pages_sorted = sorted(pages, key=lambda x: size_rank(x['size']))
        if not pages_sorted:
            return jsonify({'error': 'No labels found for this group'}), 404
        safe_name = re.sub(r'[^A-Za-z0-9_\-]', '_', group_name)
        out_path = OUTPUT_DIR / f'GROUP_{safe_name}_{out_id}.pdf'
        write_pdf([p['page'] for p in pages_sorted], out_path)
        return send_file(out_path, as_attachment=True, download_name=f'GROUP_{safe_name}_sorted.pdf')
    
    elif mode == 'by_size':
        target_size = data.get('size')
        selected_skus = data.get('skus', [])  # empty = all SKUs
        pages = [l for l in all_labels if l['size'] == target_size and (not selected_skus or l['sku'] in selected_skus)]
        if not pages:
            return jsonify({'error': f'No labels found for size {target_size}'}), 404
        out_path = OUTPUT_DIR / f'SIZE_{target_size}_{out_id}.pdf'
        write_pdf([p['page'] for p in pages], out_path)
        return send_file(out_path, as_attachment=True, download_name=f'SIZE_{target_size}_all_skus.pdf')
    
    return jsonify({'error': 'Unknown mode'}), 400

@app.route('/cleanup/<sid>', methods=['POST'])
def cleanup(sid):
    if sid in sessions:
        sess_dir = UPLOAD_DIR / sid
        if sess_dir.exists():
            shutil.rmtree(sess_dir)
        del sessions[sid]
    return jsonify({'ok': True})

if __name__ == '__main__':
    print("\n✅ Meesho Label Sorter running at: http://localhost:5050\n")
    app.run(debug=False, port=5050)
