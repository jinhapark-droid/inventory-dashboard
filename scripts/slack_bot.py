import os
import json
import re
import hmac
import hashlib
import time
import urllib.request
from flask import Flask, request, jsonify
import anthropic

app = Flask(__name__)

SLACK_BOT_TOKEN    = os.environ['SLACK_BOT_TOKEN']
SLACK_SIGNING_SECRET = os.environ['SLACK_SIGNING_SECRET']
ANTHROPIC_API_KEY  = os.environ['ANTHROPIC_API_KEY']
SHEET_ID = '1ykrQdlyTKAHmf3qgtfAwHLiLgNeFJ5WD3n0wmjxU4I0'
GID_MAIN = '1461767551'
GID_RATE = '1388188128'

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# 중복 이벤트 방지
processed_events = set()

def verify_slack(req):
    ts = req.headers.get('X-Slack-Request-Timestamp', '')
    sig = req.headers.get('X-Slack-Signature', '')
    if abs(time.time() - int(ts)) > 300:
        return False
    base = f'v0:{ts}:{req.get_data(as_text=True)}'
    expected = 'v0=' + hmac.new(
        SLACK_SIGNING_SECRET.encode(), base.encode(), hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, sig)

def fetch_gviz(gid):
    url = f'https://docs.google.com/spreadsheets/d/{SHEET_ID}/gviz/tq?tqx=out:json&gid={gid}'
    with urllib.request.urlopen(url) as r:
        raw = r.read().decode('utf-8')
    raw = re.sub(r'^[^(]+\(', '', raw).rstrip(');')
    return json.loads(raw)

def get_inventory_summary():
    data = fetch_gviz(GID_MAIN)
    rows_raw = data['table']['rows']
    header = rows_raw[0]['c']
    date_cols = [i for i in range(18, 33) if i < len(header) and header[i] and header[i].get('v')]
    SPECIAL = re.compile(r'^\[리퍼\]|^\[B급\]|^\[전시\]', re.I)

    rows = [r['c'] for r in rows_raw[1:]]
    total_wh = 0
    total_val = 0
    cat_map = {}
    line_map = {}

    for r in rows:
        if len(r) <= 14: continue
        def s(c): return str(c.get('v') or '').strip() if c else ''
        def n(c): return float(c.get('v') or 0) if c else 0
        name = s(r[0])
        if not name or SPECIAL.search(name): continue
        wh    = n(r[14])
        price = n(r[5])
        cat   = s(r[6]) or '기타'
        line  = s(r[9]) or name
        season = s(r[8])
        total_wh  += wh
        total_val += wh * price

        sold14 = 0
        if len(date_cols) >= 2:
            first = n(r[date_cols[0]]) if date_cols[0] < len(r) else 0
            last  = n(r[date_cols[-1]]) if date_cols[-1] < len(r) else 0
            sold14 = max(0, first - last)

        if cat not in cat_map:
            cat_map[cat] = {'wh': 0, 'val': 0, 'sold14': 0}
        cat_map[cat]['wh']    += wh
        cat_map[cat]['val']   += wh * price
        cat_map[cat]['sold14'] += sold14

        key = f'{line}||{season}'
        if key not in line_map:
            line_map[key] = {'line': line, 'season': season, 'cat': cat, 'wh': 0, 'val': 0, 'sold14': 0}
        line_map[key]['wh']    += wh
        line_map[key]['val']   += wh * price
        line_map[key]['sold14'] += sold14

    span = max(len(date_cols) - 1, 1)

    # 26SS 소진율
    rate_data = fetch_gviz(GID_RATE)
    rate_rows = rate_data['table']['rows'][1:]
    tot26 = wh26 = 0
    for r in rate_rows:
        c = r['c']
        def s2(x): return str(x.get('v') or '').strip() if x else ''
        def n2(x): return float(x.get('v') or 0) if x else 0
        if len(c) > 7 and s2(c[2]) == '26SS':
            tot26 += n2(c[5])
            wh26  += n2(c[7])
    rate26 = round((tot26 - wh26) / tot26 * 100) if tot26 > 0 else 0

    from datetime import date
    today = date.today()
    d_left = (date(2026, 8, 31) - today).days

    # 상위 재고 품목 (재고액 기준 top 10)
    top_lines = sorted(line_map.values(), key=lambda x: -x['val'])[:10]

    summary = {
        'total_wh': int(total_wh),
        'total_val_man': round(total_val / 10000),
        'cat_map': {k: {
            'wh': int(v['wh']),
            'val_man': round(v['val'] / 10000),
            'daily': round(v['sold14'] / span, 1)
        } for k, v in cat_map.items()},
        'rate26': rate26,
        'd_left': d_left,
        'top_lines': [{
            'line': l['line'], 'season': l['season'], 'cat': l['cat'],
            'wh': int(l['wh']), 'val_man': round(l['val'] / 10000),
            'daily': round(l['sold14'] / span, 1)
        } for l in top_lines],
        'today': today.strftime('%Y-%m-%d'),
    }
    return summary

def ask_claude(question, inventory):
    context = f"""
당신은 어반사이드(Urban Side) 브랜드의 재고 분석 어시스턴트입니다.
아래는 현재 실시간 재고 현황입니다.

[전사 재고 요약]
- 총 창고재고: {inventory['total_wh']:,}개 / ₩{inventory['total_val_man']:,}만
- 26SS 소진율: {inventory['rate26']}% (마감까지 D-{inventory['d_left']})
- 기준일: {inventory['today']}

[카테고리별 현황]
""" + '\n'.join(
        f"- {cat}: {d['wh']:,}개 / ₩{d['val_man']:,}만 / 일평균 {d['daily']}개 소진"
        for cat, d in inventory['cat_map'].items()
    ) + """

[재고액 상위 품목 (Top 10)]
""" + '\n'.join(
        f"- [{l['season']}] {l['line']} ({l['cat']}): {l['wh']:,}개 / ₩{l['val_man']:,}만 / 일평균 {l['daily']}개"
        for l in inventory['top_lines']
    ) + f"""

팀원의 질문에 한국어로 간결하게 답변하세요. 데이터 기반으로 구체적인 수치를 포함해 주세요.
불필요한 서두 없이 바로 답변하세요.

질문: {question}
"""
    msg = client.messages.create(
        model='claude-sonnet-4-6',
        max_tokens=800,
        messages=[{'role': 'user', 'content': context}]
    )
    return msg.content[0].text

def slack_reply(channel, thread_ts, text):
    payload = json.dumps({
        'channel': channel,
        'thread_ts': thread_ts,
        'text': text
    }).encode()
    req = urllib.request.Request(
        'https://slack.com/api/chat.postMessage',
        data=payload,
        headers={
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {SLACK_BOT_TOKEN}'
        }
    )
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())

@app.route('/slack/events', methods=['POST'])
def slack_events():
    if not verify_slack(request):
        return jsonify({'error': 'invalid signature'}), 403

    body = request.get_json()

    # URL 검증
    if body.get('type') == 'url_verification':
        return jsonify({'challenge': body['challenge']})

    event = body.get('event', {})
    event_id = body.get('event_id', '')

    # 중복 방지
    if event_id in processed_events:
        return jsonify({'ok': True})
    processed_events.add(event_id)
    if len(processed_events) > 1000:
        processed_events.clear()

    # 봇 자신의 메시지 무시
    if event.get('bot_id') or event.get('subtype'):
        return jsonify({'ok': True})

    # 스레드 메시지만 처리
    thread_ts = event.get('thread_ts')
    if not thread_ts:
        return jsonify({'ok': True})

    text    = event.get('text', '').strip()
    channel = event.get('channel', '')
    ts      = event.get('ts', '')

    if not text:
        return jsonify({'ok': True})

    try:
        inventory = get_inventory_summary()
        answer = ask_claude(text, inventory)
        slack_reply(channel, thread_ts, answer)
    except Exception as e:
        slack_reply(channel, thread_ts, f"⚠️ 오류가 발생했어요: {str(e)}")

    return jsonify({'ok': True})

@app.route('/health', methods=['GET'])
def health():
    return jsonify({'status': 'ok'})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)
