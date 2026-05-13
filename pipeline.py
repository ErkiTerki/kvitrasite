import socket
import threading
import time
import json
from datetime import datetime
from flask import Flask, jsonify, render_template_string
from smartcard.System import readers
from smartcard.Exceptions import NoCardException
from supabase import create_client
import anthropic

ANTHROPIC_KEY = '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'
SUPABASE_URL  = 'https://whvrazrcoytddafpdujg.supabase.co'
SUPABASE_KEY  = 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6IndodnJhenJjb3l0ZGRhZnBkdWpnIiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc3ODQwNTcyOSwiZXhwIjoyMDkzOTgxNzI5fQ.bIhlT9cTeWo7bO0Hu5HkL72Ndu7Z3FTc9vVKpE4UKjU'
PRINTER_IP    = '192.168.0.100'
PRINTER_PORT  = 9100
SITE_URL      = 'https://zingy-rugelach-b39619.netlify.app'
NFC_TIMEOUT   = 60

sb               = create_client(SUPABASE_URL, SUPABASE_KEY)
anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
app              = Flask(__name__)

# ── Shared state ──────────────────────────────────────────────────────────────

nfc_customer_id = None
nfc_tap_time    = None
nfc_timer       = None
pending_receipt = None
events          = []
raw_store       = {}
event_counter   = 0
lock            = threading.Lock()

# ── Events ────────────────────────────────────────────────────────────────────

def add_event(etype, **kwargs):
    global event_counter
    raw = kwargs.pop('raw', None)
    with lock:
        eid = event_counter
        event_counter += 1
        events.insert(0, {
            'id':   eid,
            'type': etype,
            'time': datetime.now().strftime('%H:%M:%S'),
            **kwargs
        })
        if len(events) > 50:
            events.pop()
        if raw is not None:
            raw_store[eid] = raw
            for k in [k for k in list(raw_store) if k < eid - 100]:
                del raw_store[k]

# ── NFC ───────────────────────────────────────────────────────────────────────

def clear_nfc():
    global nfc_customer_id, nfc_tap_time, nfc_timer
    with lock:
        uuid            = nfc_customer_id
        nfc_customer_id = None
        nfc_tap_time    = None
        nfc_timer       = None
    if uuid:
        add_event('nfc_timeout', uuid=uuid)
        print(f"NFC timeout — forgot {uuid}")

def set_customer(uuid):
    global nfc_customer_id, nfc_tap_time, nfc_timer
    with lock:
        if nfc_timer:
            nfc_timer.cancel()
        nfc_customer_id = uuid
        nfc_tap_time    = time.time()
        nfc_timer       = threading.Timer(NFC_TIMEOUT, clear_nfc)
        nfc_timer.daemon = True
        nfc_timer.start()
    add_event('nfc_tap', uuid=uuid)
    print(f"Customer tapped: {uuid}")

def read_uuid_from_conn(conn):
    data = []
    for page in [4, 8, 12]:
        resp, sw1, _ = conn.transmit([0xFF, 0xB0, 0x00, page, 0x10])
        if sw1 == 0x90:
            data += resp
    return bytes(data).rstrip(b'\x00').decode('ascii', errors='replace') or None

def write_url_ndef(conn, url):
    if url.startswith('https://'):
        prefix, body = 0x04, url[8:]
    elif url.startswith('http://'):
        prefix, body = 0x03, url[7:]
    else:
        prefix, body = 0x00, url
    payload     = bytes([prefix]) + body.encode('utf-8')
    ndef_record = bytes([0xD1, 0x01, len(payload), 0x55]) + payload
    tlv         = bytes([0x03, len(ndef_record)]) + ndef_record + bytes([0xFE])
    if len(tlv) % 4:
        tlv += bytes(4 - len(tlv) % 4)
    for i in range(0, len(tlv), 4):
        _, sw1, _ = conn.transmit([0xFF, 0xD6, 0x00, 4 + i // 4, 0x04] + list(tlv[i:i+4]))
        if sw1 != 0x90:
            raise Exception(f'Write failed at page {4 + i // 4}')

def set_pending_receipt(url):
    global pending_receipt
    with lock:
        pending_receipt = {'url': url}
    print("Receipt URL pending write to NFC sticker.")

def poll_nfc():
    global pending_receipt
    r = readers()
    if not r:
        print("No NFC reader found.")
        return
    reader   = r[0]
    last_uid = None
    print("NFC reader ready.")
    while True:
        try:
            conn = reader.createConnection()
            conn.connect()
            try:
                resp, sw1, _ = conn.transmit([0xFF, 0xCA, 0x00, 0x00, 0x00])
                uid = bytes(resp).hex() if sw1 == 0x90 else None
                if uid:
                    with lock:
                        pr = pending_receipt
                    if pr:
                        try:
                            write_url_ndef(conn, pr['url'])
                            with lock:
                                pending_receipt = None
                            add_event('url_relayed', url=pr['url'])
                            print("Receipt URL written to NFC sticker.")
                        except Exception as e:
                            print(f"NDEF write failed: {e}")
                            add_event('db_error', message=f"NFC write failed: {e}")
                    elif uid != last_uid:
                        uuid = read_uuid_from_conn(conn)
                        if uuid:
                            set_customer(uuid)
                    last_uid = uid
                else:
                    last_uid = None
            finally:
                conn.disconnect()
        except NoCardException:
            last_uid = None
        except Exception:
            pass  # transient PC/SC error — keep last_uid so we don't re-trigger set_customer
        time.sleep(0.3)

# ── ESC/POS parsing ───────────────────────────────────────────────────────────

def strip_telnet(data):
    out = bytearray()
    i   = 0
    while i < len(data):
        if data[i] == 0xFF and i + 1 < len(data):
            if data[i+1] == 0xFA:
                end = data.find(b'\xff\xf0', i + 2)
                i   = (end + 2) if end >= 0 else len(data)
            else:
                i += 2
        else:
            out.append(data[i])
            i += 1
    return bytes(out)

def parse_escpos(raw):
    data = strip_telnet(raw)
    text = bytearray()
    i    = 0
    while i < len(data):
        b = data[i]
        if b == 0x1b:
            if i + 1 >= len(data): break
            cmd = data[i+1]
            if   cmd == 0x40: i += 2
            elif cmd == 0x70: i += 5
            elif cmd == 0x69: i += 2
            else:             i += 3
        elif b == 0x1d:
            if i + 1 >= len(data): break
            cmd = data[i+1]
            if cmd == 0x76 and i + 7 < len(data):
                xl, xh = data[i+4], data[i+5]
                yl, yh = data[i+6], data[i+7]
                i += 8 + (xl + xh*256) * (yl + yh*256)
            else:
                i += 3
        elif b in (0x0d, 0x0a):
            text.append(b); i += 1
        elif 0x20 <= b <= 0x7e:
            text.append(b); i += 1
        else:
            i += 1
    return text.decode('latin-1')

def parse_with_claude(text):
    prompt = (
        'Extract the receipt data from the following ESC/POS decoded text and return ONLY valid JSON '
        'with this exact structure: '
        '{"store_name": "...", "items": [{"name": "...", "qty": "1", "value": "6,00"}], "total_amount": "8,50"}\n\n'
        'Rules: qty and value are strings. value is the line total for that item. '
        'total_amount is the final amount due. No extra text, just the JSON object.\n\n'
        f'Receipt:\n{text}'
    )
    try:
        msg = anthropic_client.messages.create(
            model='claude-haiku-4-5',
            max_tokens=512,
            messages=[{'role': 'user', 'content': prompt}]
        )
        raw = msg.content[0].text.strip()
        if raw.startswith('```'):
            lines = raw.splitlines()
            raw = '\n'.join(lines[1:-1] if lines[-1].strip() == '```' else lines[1:])
        d            = json.loads(raw)
        store_name   = d.get('store_name', 'Unknown')
        items        = d.get('items', [])
        total_amount = str(d.get('total_amount', ''))
        print(f"Claude parsed: store={store_name!r} total={total_amount!r} items={len(items)}")
        return store_name, items, total_amount
    except Exception as e:
        print(f"Claude parse failed: {e}")
        return None, None, None

# ── Printer forward ───────────────────────────────────────────────────────────

def forward_to_printer(data):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.settimeout(5)
        s.connect((PRINTER_IP, PRINTER_PORT))
        s.sendall(data)
        print("Forwarded to printer.")
    except Exception as e:
        print(f"Printer error: {e}")
    finally:
        s.close()

# ── Supabase ──────────────────────────────────────────────────────────────────

def save_receipt(customer_id, store_name, items, total_amount, raw_hex):
    try:
        result = sb.table('receipts').insert({
            'customer_id':  customer_id,
            'store_name':   store_name,
            'items':        items,
            'total_amount': total_amount,
            'raw_data':     raw_hex
        }).execute()
        if not result.data:
            msg = "Supabase returned no data — INSERT may have been blocked by RLS"
            print(msg)
            add_event('db_error', message=msg)
            return None
        receipt_id  = result.data[0]['id']
        receipt_url = f"{SITE_URL}/receipt.html?id={receipt_id}"
        print(f"Saved. URL: {receipt_url}")
        return receipt_url
    except Exception as e:
        msg = str(e)
        print(f"Supabase error: {msg}")
        add_event('db_error', message=msg)
        return None

# ── TCP listener ──────────────────────────────────────────────────────────────

def listen_escpos():
    global nfc_customer_id, nfc_tap_time, nfc_timer
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(('0.0.0.0', 9100))
    server.listen(5)
    print("Listening for ESC/POS on port 9100...")
    while True:
        conn, _ = server.accept()
        conn.settimeout(3.0)
        data = b''
        try:
            while True:
                chunk = conn.recv(4096)
                if not chunk: break
                data += chunk
        except socket.timeout:
            pass
        finally:
            conn.close()
        if not data:
            continue

        print(f"Received {len(data)} bytes")

        with lock:
            customer_id     = nfc_customer_id
            nfc_customer_id = None
            nfc_tap_time    = None
            timer           = nfc_timer
            nfc_timer       = None
        if timer:
            timer.cancel()

        text = parse_escpos(data)
        store_name, items, total_amount = parse_with_claude(text)
        if store_name is None:
            store_name, items, total_amount = 'Unknown', [], ''
        print(f"Parsed: store={store_name!r} total={total_amount!r} items={len(items)}")
        print(f"Decoded text:\n{text}")

        raw_hex     = data.hex(' ')
        raw_payload = raw_hex + '\n\n--- DECODED TEXT ---\n' + text

        if customer_id:
            receipt_url = save_receipt(customer_id, store_name, items, total_amount, raw_hex)
            add_event('receipt_database',
                customer=customer_id,
                store=store_name,
                total=total_amount,
                items=items,
                receipt_url=receipt_url or '',
                raw=raw_payload
            )
        else:
            forward_to_printer(data)
            receipt_url = save_receipt(None, store_name, items, total_amount, raw_hex)
            if receipt_url:
                set_pending_receipt(receipt_url)
            add_event('receipt_printer',
                store=store_name,
                total=total_amount,
                items=items,
                receipt_url=receipt_url or '',
                raw=raw_payload
            )

# ── Web UI ────────────────────────────────────────────────────────────────────

PAGE = """
<!DOCTYPE html>
<html>
<head>
  <title>Kvitra Pipeline</title>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: monospace; background: #111; color: #eee; padding: 2rem; }
    h1 { font-size: 1.1rem; color: #888; margin-bottom: 1.5rem; letter-spacing: .05em; text-transform: uppercase; }
    .nfc-box { background: #1a1a1a; border-radius: 8px; padding: 1.2rem 1.5rem; margin-bottom: 1.5rem; display: flex; align-items: center; gap: 1rem; }
    .dot { width: 12px; height: 12px; border-radius: 50%; flex-shrink: 0; }
    .dot-gray   { background: #444; }
    .dot-yellow { background: #ff9800; box-shadow: 0 0 8px #ff9800; }
    .nfc-label { font-size: .95rem; }
    .nfc-uuid  { font-size: .78rem; color: #888; margin-top: .25rem; word-break: break-all; }
    .nfc-cd    { font-size: .78rem; color: #ff9800; margin-top: .2rem; }
    table { width: 100%; border-collapse: collapse; }
    th { text-align: left; padding: .5rem; border-bottom: 1px solid #222; color: #555; font-size: .75rem; text-transform: uppercase; }
    td { padding: .6rem .5rem; border-bottom: 1px solid #1a1a1a; font-size: .85rem; vertical-align: top; }
    .tag { padding: .15rem .55rem; border-radius: 3px; font-size: .75rem; white-space: nowrap; }
    .db      { background: #0d2137; color: #64b5f6; }
    .printer { background: #0d2a0d; color: #81c784; }
    .tap     { background: #2a1f00; color: #ffb74d; }
    .timeout { background: #2a0d0d; color: #e57373; }
    .relayed { background: #001a1a; color: #4dd0e1; }
    a { color: #64b5f6; text-decoration: none; }
    a:hover { text-decoration: underline; }
    .detail { color: #aaa; }
    .items { margin-top: .4rem; }
    .item-row { display: flex; gap: 1rem; font-size: .8rem; color: #aaa; padding: .1rem 0; }
    .item-row span:first-child { flex: 1; }
    .item-row.total { color: #eee; font-weight: bold; border-top: 1px solid #333; margin-top: .2rem; padding-top: .2rem; }
    .raw-btn { background: #1e1e1e; border: 1px solid #333; color: #666; border-radius: 3px; padding: .1rem .45rem; font-size: .7rem; cursor: pointer; font-family: monospace; margin-left: .5rem; }
    .raw-btn:hover { color: #aaa; border-color: #555; }
    .overlay { display: none; position: fixed; inset: 0; background: rgba(0,0,0,.75); z-index: 100; align-items: center; justify-content: center; }
    .overlay.open { display: flex; }
    .modal { background: #1a1a1a; border-radius: 10px; padding: 1.5rem; width: 90vw; max-width: 780px; max-height: 80vh; display: flex; flex-direction: column; }
    .modal-head { display: flex; justify-content: space-between; align-items: center; margin-bottom: 1rem; }
    .modal-title { color: #555; font-size: .75rem; text-transform: uppercase; letter-spacing: .06em; }
    .modal-close { background: none; border: none; color: #555; font-size: 1.1rem; cursor: pointer; line-height: 1; }
    .modal-close:hover { color: #eee; }
    .hex-dump { overflow: auto; flex: 1; }
    .hex-dump pre { font-size: .72rem; color: #7ec8a0; white-space: pre-wrap; word-break: break-all; line-height: 1.6; }
  </style>
</head>
<body>
  <h1>Kvitra Pipeline Monitor</h1>

  <div class="nfc-box">
    <div class="dot dot-gray" id="dot"></div>
    <div>
      <div class="nfc-label" id="nfc-label">Waiting for NFC tap...</div>
      <div class="nfc-uuid"  id="nfc-uuid"></div>
      <div class="nfc-cd"    id="nfc-cd"></div>
    </div>
  </div>

  <div class="overlay" id="overlay" onclick="closeRaw(event)">
    <div class="modal">
      <div class="modal-head">
        <span class="modal-title">Raw ESC/POS bytes &amp; decoded text</span>
        <button class="modal-close" onclick="closeRaw()">&#x2715;</button>
      </div>
      <div class="hex-dump"><pre id="hex-content">loading...</pre></div>
    </div>
  </div>

  <table>
    <thead><tr><th>Time</th><th>Event</th><th>Details</th></tr></thead>
    <tbody id="events"></tbody>
  </table>

  <script>
    let tapTime = null;

    function buildItemsHtml(e) {
      if (!e.items || !e.items.length) return '';
      return '<div class="items">' + e.items.map(i =>
        `<div class="item-row"><span>${i.name}</span><span>x${i.qty}</span><span>${i.value}</span></div>`
      ).join('') +
      `<div class="item-row total"><span>Total</span><span></span><span>${e.total}</span></div></div>`;
    }

    function showRaw(id) {
      document.getElementById('hex-content').textContent = 'loading...';
      document.getElementById('overlay').classList.add('open');
      fetch('/api/raw/' + id).then(r=>r.json()).then(d => {
        const hex = d.hex || '';
        const bytes = hex.split(' ');
        let out = '';
        for (let i = 0; i < bytes.length; i += 16) {
          const chunk = bytes.slice(i, i + 16);
          const addr  = String(i).padStart(5, '0');
          const hex16 = chunk.join(' ').padEnd(47, ' ');
          const ascii = chunk.map(b => {
            const c = parseInt(b, 16);
            return c >= 0x20 && c <= 0x7e ? String.fromCharCode(c) : '.';
          }).join('');
          out += addr + '  ' + hex16 + '  ' + ascii + '\\n';
        }
        document.getElementById('hex-content').textContent = out || '(no data)';
      });
    }

    function closeRaw(evt) {
      if (evt && evt.target !== document.getElementById('overlay')) return;
      document.getElementById('overlay').classList.remove('open');
    }

    function refresh() {
      fetch('/api/status').then(r=>r.json()).then(s => {
        const dot   = document.getElementById('dot');
        const label = document.getElementById('nfc-label');
        const uuid  = document.getElementById('nfc-uuid');
        if (s.customer_id) {
          dot.className     = 'dot dot-yellow';
          label.textContent = 'Customer identified';
          uuid.textContent  = s.customer_id;
          tapTime = s.tap_time;
        } else {
          dot.className     = 'dot dot-gray';
          label.textContent = 'Waiting for NFC tap...';
          uuid.textContent  = '';
          document.getElementById('nfc-cd').textContent = '';
          tapTime = null;
        }
      });
      fetch('/api/events').then(r=>r.json()).then(evts => {
        const tbody = document.getElementById('events');
        tbody.innerHTML = '';
        evts.forEach(e => {
          const tr = document.createElement('tr');
          let tag = '', detail = '';
          if (e.type === 'receipt_database') {
            tag    = '<span class="tag db">Database</span>';
            detail = `<strong>${e.store}</strong>` +
                     (e.receipt_url ? ` &mdash; <a href="${e.receipt_url}" target="_blank">View receipt</a>` : '') +
                     `<button class="raw-btn" onclick="showRaw(${e.id})">view raw</button>` +
                     buildItemsHtml(e);
          } else if (e.type === 'receipt_printer') {
            tag    = '<span class="tag printer">Printer</span>';
            detail = `<strong>${e.store}</strong>` +
                     (e.receipt_url ? ` &mdash; <a href="${e.receipt_url}" target="_blank">View receipt</a>` : '') +
                     `<button class="raw-btn" onclick="showRaw(${e.id})">view raw</button>` +
                     buildItemsHtml(e);
          } else if (e.type === 'nfc_tap') {
            tag    = '<span class="tag tap">NFC Tap</span>';
            detail = `<span class="detail">${e.uuid}</span>`;
          } else if (e.type === 'nfc_timeout') {
            tag    = '<span class="tag timeout">Timeout</span>';
            detail = `<span class="detail">${e.uuid}</span>`;
          } else if (e.type === 'db_error') {
            tag    = '<span class="tag timeout">DB Error</span>';
            detail = `<span class="detail">${e.message}</span>`;
          } else if (e.type === 'url_relayed') {
            tag    = '<span class="tag relayed">Relayed</span>';
            detail = `Receipt URL written to tag &mdash; <a href="${e.url}" target="_blank">view receipt</a>`;
          }
          tr.innerHTML = `<td>${e.time}</td><td>${tag}</td><td>${detail}</td>`;
          tbody.appendChild(tr);
        });
      });
    }

    function updateCountdown() {
      const cd = document.getElementById('nfc-cd');
      if (tapTime) {
        const left = 60 - (Date.now()/1000 - tapTime);
        cd.textContent = left > 0 ? `Expires in ${Math.ceil(left)}s` : '';
      }
    }

    setInterval(refresh, 1000);
    setInterval(updateCountdown, 500);
    refresh();
  </script>
</body>
</html>
"""

@app.route('/')
def index(): return render_template_string(PAGE)

@app.route('/api/status')
def api_status():
    with lock:
        return jsonify({'customer_id': nfc_customer_id, 'tap_time': nfc_tap_time})

@app.route('/api/events')
def api_events():
    return jsonify(list(events))

@app.route('/api/raw/<int:eid>')
def api_raw(eid):
    with lock:
        return jsonify({'hex': raw_store.get(eid, '')})

# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    threading.Thread(target=poll_nfc,      daemon=True).start()
    threading.Thread(target=listen_escpos, daemon=True).start()
    app.run(host='0.0.0.0', port=8080, debug=False)
