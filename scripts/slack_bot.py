import os
import json
import re
import hmac
import hashlib
import time
import urllib.request
from datetime import date
from flask import Flask, request, jsonify

app = Flask(__name__)

SLACK_BOT_TOKEN      = os.environ['SLACK_BOT_TOKEN']
SLACK_SIGNING_SECRET = os.environ['SLACK_SIGNING_SECRET']
ANTHROPIC_API_KEY    = os.environ['ANTHROPIC_API_KEY']
SHEET_ID  = '1ykrQdlyTKAHmf3qgtfAwHLiLgNeFJ5WD3n0wmjxU4I0'
GID_MAIN  = '1461767551'

processed_events = set()

def verify_slack(req):
    ts  = req.headers.get('X-Slack-Request-Timestamp', '')
    sig = req.headers.get('X-Slack-Signature', '')
    if not ts or abs(time.time() - int(ts)) > 300:
        return False
    base = f'v0:{ts}:{req.get_data(as_text=True)}'
    expected = 'v0=' + hmac.new(
        SLACK_SIGNING_SECRET.encode(), base.encode(), hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, sig)

def fetch_gviz(gid):
    url = f'https://docs.google.com/spreadsheets/d/{SHEET_ID}/gviz/tq?tqx=out:json&gid={gid}'
    with urllib.request.urlopen(url, timeout=10) as r:
        raw = r.read().decode('utf-8')
    raw = re.sub(r'^[^(]+\(', '', raw).rstrip(');')
    return json.loads(raw)

def get_inventory():
    data = fetch_gviz(GID_MAIN)
    rows_raw = data['table']['rows']
    header   = rows_raw[0]['c']
    date_cols = [i for i in range(18, 33) if i < len(header) and header[i] and header[i].get('v')]
    SPECIAL  = re.compile(r'^\[리퍼\]|^\[B급\]|^\[전시\]', re.I)
    rows = [r['c'] for r in rows_raw[1:]]

    def s(c): return str(c.get('v') or '').strip() if c else ''
    def n(c): return float(c.get('v') or 0) if c else 0

    total_wh = total_val = 0
    cat_map  = {}
    line_map = {}
    season_map = {}

    for r in rows:
        if len(r) <= 14: continue
        name   = s(r[0]);
        if not name or SPECIAL.search(name): continue
        wh     = n(r[14]); price = n(r[5])
        cat    = s(r[6]) or '기타'
        line   = s(r[9]) or name
        season = s(r[8])
        total_wh  += wh; total_val += wh * price

        sold14 = 0
        if len(date_cols) >= 2:
            sold14 = max(0, (n(r[date_cols[0]]) if date_cols[0] < len(r) else 0)
                           - (n(r[date_cols[-1]]) if date_cols[-1] < len(r) else 0))

        if cat not in cat_map:
            cat_map[cat] = {'wh': 0, 'val': 0, 'sold14': 0}
        cat_map[cat]['wh']    += wh
        cat_map[cat]['val']   += wh * price
        cat_map[cat]['sold14'] += sold14

        key = f'{line}||{season}'
        if key not in line_map:
            line_map[key] = {'line': line, 'season': season, 'cat': cat, 'wh': 0, 'val': 0, 'daily': 0}
        line_map[key]['wh']    += wh
        line_map[key]['val']   += wh * price
        line_map[key]['daily'] += sold14

        if season not in season_map:
            season_map[season] = {'wh': 0, 'val': 0, 'sold14': 0}
        season_map[season]['wh']    += wh
        season_map[season]['val']   += wh * price
        season_map[season]['sold14'] += sold14

    span = max(len(date_cols) - 1, 1)

    today    = date.today()
    d_left   = (date(2026, 8, 31) - today).days
    start    = date(2026, 4, 1)
    end      = date(2026, 8, 31)
    time_pct = round((today - start).days / (end - start).days * 100)

    # 26SS 잔여재고 / 일평균 소진 (메인시트 기반)
    val26_wh = val26_sold = 0
    for v in season_map.get('26SS', {}).values():
        pass
    s26 = season_map.get('26SS', {'wh': 0, 'val': 0, 'sold14': 0})
    val26_man   = round(s26['val'] / 10000)
    daily26_val = round(s26['sold14'] / span / 10000, 1)
    days_to_zero = round(s26['val'] / (s26['sold14'] / span)) if s26['sold14'] > 0 else 999

    # daily 누적값을 span으로 나눠 개/일로 변환
    for v in line_map.values():
        v['daily'] = round(v['daily'] / span, 1)

    top_lines = sorted(line_map.values(), key=lambda x: -x['val'])[:10]
    top_sold  = sorted(line_map.values(), key=lambda x: -x['daily'])[:10]

    return {
        'total_wh': int(total_wh),
        'total_val_man': round(total_val / 10000),
        'cat_map': {k: {'wh': int(v['wh']), 'val_man': round(v['val']/10000),
                        'daily': round(v['sold14']/span, 1)} for k, v in cat_map.items()
                    if k not in ('', None)},
        'season_map': {k: {'wh': int(v['wh']), 'val_man': round(v['val']/10000),
                           'daily': round(v['sold14']/span, 1)} for k, v in season_map.items()},
        'line_map': line_map,
        'top_lines': top_lines, 'top_sold': top_sold,
        'val26_man': val26_man, 'daily26_val': daily26_val, 'days_to_zero': days_to_zero,
        'd_left': d_left, 'time_pct': time_pct,
        'span': span,
    }

def build_context(inv):
    """재고 데이터를 Claude에게 넘길 텍스트로 변환"""
    today = date.today()
    lines = [
        f"[기준일: {today.strftime('%Y-%m-%d')}]",
        f"전사 합계: 창고재고 {inv['total_wh']:,}개 · 재고액 ₩{inv['total_val_man']:,}만",
        "",
        "■ 카테고리별 (창고재고 | 재고액 | 일평균소진)",
    ]
    for cat, d in sorted(inv['cat_map'].items(), key=lambda x: -x[1]['val_man']):
        lines.append(f"  {cat}: {d['wh']:,}개 · ₩{d['val_man']:,}만 · {d['daily']}개/일")

    lines += ["", "■ 시즌별"]
    for season, d in sorted(inv['season_map'].items(), key=lambda x: -x[1]['val_man']):
        lines.append(f"  {season}: {d['wh']:,}개 · ₩{d['val_man']:,}만 · {d['daily']}개/일")

    d2z = inv['days_to_zero']
    pace = f"현재 속도로 {d2z}일 후 소진 예상" if d2z < 999 else "소진 속도 미미"
    lines += [
        "",
        f"■ 26SS 시즌 (마감 2026-08-31, D-{inv['d_left']})",
        f"  잔여재고액 ₩{inv['val26_man']:,}만 · 일평균 ₩{inv['daily26_val']}만 소진",
        f"  {pace} ({'마감 전 소진 불가' if d2z > inv['d_left'] else '마감 전 소진 가능'})",
    ]

    lines += ["", "■ 품목별 재고 (라인명 | 시즌 | 카테고리 | 창고재고 | 재고액 | 일평균소진)"]
    for v in sorted(inv['line_map'].values(), key=lambda x: -x['val']):
        lines.append(
            f"  {v['line']} | {v['season']} | {v['cat']} | "
            f"{int(v['wh'])}개 | ₩{round(v['val']/10000)}만 | {v['daily']}개/일"
        )

    return '\n'.join(lines)

def call_claude(question, context):
    system = (
        "당신은 아웃도어 브랜드 '어반사이드'의 실시간 재고 현황을 답변하는 AI 어시스턴트입니다.\n"
        "아래 재고 데이터를 기반으로 질문에 간결하고 정확하게 한국어로 답변하세요.\n"
        "- 숫자는 데이터 그대로 사용하고 추측하지 마세요.\n"
        "- 답변은 5줄 이내로 핵심만 말하세요.\n"
        "- 재고 관련 없는 질문에는 '재고 관련 질문만 답변할 수 있어요'라고 답하세요.\n\n"
        f"=== 현재 재고 데이터 ===\n{context}"
    )
    payload = json.dumps({
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 400,
        "system": system,
        "messages": [{"role": "user", "content": question}]
    }).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=payload,
        headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json"
        }
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        data = json.loads(r.read())
    return data["content"][0]["text"]

def answer(text, inv):
    context = build_context(inv)
    return call_claude(text, context)

def slack_reply(channel, thread_ts, text):
    payload = json.dumps({'channel': channel, 'thread_ts': thread_ts, 'text': text}).encode()
    req = urllib.request.Request(
        'https://slack.com/api/chat.postMessage', data=payload,
        headers={'Content-Type': 'application/json',
                 'Authorization': f'Bearer {SLACK_BOT_TOKEN}'}
    )
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())

def is_bot_parent(channel, thread_ts):
    url = (f'https://slack.com/api/conversations.replies'
           f'?channel={channel}&ts={thread_ts}&limit=1&inclusive=true')
    req = urllib.request.Request(url, headers={'Authorization': f'Bearer {SLACK_BOT_TOKEN}'})
    with urllib.request.urlopen(req) as r:
        data = json.loads(r.read())
    messages = data.get('messages', [])
    if not messages:
        return False
    parent = messages[0]
    return bool(parent.get('bot_id') or parent.get('subtype') == 'bot_message')

@app.route('/slack/events', methods=['POST'])
def slack_events():
    if not verify_slack(request):
        return jsonify({'error': 'invalid signature'}), 403

    body = request.get_json()
    if body.get('type') == 'url_verification':
        return jsonify({'challenge': body['challenge']})

    event    = body.get('event', {})
    event_id = body.get('event_id', '')
    if event_id in processed_events:
        return jsonify({'ok': True})
    processed_events.add(event_id)
    if len(processed_events) > 1000:
        processed_events.clear()

    if event.get('bot_id') or event.get('subtype'):
        return jsonify({'ok': True})

    thread_ts = event.get('thread_ts')
    if not thread_ts:
        return jsonify({'ok': True})

    text    = event.get('text', '').strip()
    channel = event.get('channel', '')
    if not text:
        return jsonify({'ok': True})

    if not is_bot_parent(channel, thread_ts):
        return jsonify({'ok': True})

    try:
        inv  = get_inventory()
        reply = answer(text, inv)
        slack_reply(channel, thread_ts, reply)
    except Exception as e:
        slack_reply(channel, thread_ts, f"⚠️ 데이터 조회 중 오류: {str(e)}")

    return jsonify({'ok': True})

@app.route('/health', methods=['GET'])
def health():
    return jsonify({'status': 'ok'})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)
