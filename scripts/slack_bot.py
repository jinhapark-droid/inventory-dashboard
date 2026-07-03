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

SLACK_BOT_TOKEN     = os.environ['SLACK_BOT_TOKEN']
SLACK_SIGNING_SECRET = os.environ['SLACK_SIGNING_SECRET']
SHEET_ID  = '1ykrQdlyTKAHmf3qgtfAwHLiLgNeFJ5WD3n0wmjxU4I0'
GID_MAIN  = '1461767551'
GID_RATE  = '1388188128'

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

    rate_data  = fetch_gviz(GID_RATE)
    rate_rows  = rate_data['table']['rows'][1:]
    tot26 = wh26 = 0
    for r in rate_rows:
        c = r['c']
        def s2(x): return str(x.get('v') or '').strip() if x else ''
        def n2(x): return float(x.get('v') or 0) if x else 0
        if len(c) > 7 and s2(c[2]) == '26SS':
            tot26 += n2(c[5]); wh26 += n2(c[7])
    rate26 = round((tot26 - wh26) / tot26 * 100) if tot26 > 0 else 0

    today  = date.today()
    d_left = (date(2026, 8, 31) - today).days
    start  = date(2026, 4, 1)
    end    = date(2026, 8, 31)
    time_pct = round((today - start).days / (end - start).days * 100)

    # daily 누적값을 span으로 나눠 개/일로 변환
    for v in line_map.values():
        v['daily'] = round(v['daily'] / span, 1)

    top_lines = sorted(line_map.values(), key=lambda x: -x['val'])[:10]
    top_sold  = sorted(line_map.values(), key=lambda x: -x['daily'])[:10]

    return {
        'total_wh': int(total_wh),
        'total_val_man': round(total_val / 10000),
        'cat_map': {k: {'wh': int(v['wh']), 'val_man': round(v['val']/10000),
                        'daily': round(v['sold14']/span, 1)} for k, v in cat_map.items()},
        'season_map': {k: {'wh': int(v['wh']), 'val_man': round(v['val']/10000),
                           'daily': round(v['sold14']/span, 1)} for k, v in season_map.items()},
        'top_lines': top_lines, 'top_sold': top_sold,
        'rate26': rate26, 'd_left': d_left, 'time_pct': time_pct,
        'span': span,
    }

def answer(text, inv):
    t = text.lower()

    # 전체 요약
    if any(k in t for k in ['전체', '요약', '현황', '전사', '총']):
        cats = '\n'.join(
            f"  {cat}: {d['wh']:,}개 · ₩{d['val_man']:,}만 · {d['daily']}개/일"
            for cat, d in sorted(inv['cat_map'].items(), key=lambda x: -x[1]['val_man'])
        )
        return (f"📊 전사 재고 현황\n"
                f"총 창고재고 {inv['total_wh']:,}개 · ₩{inv['total_val_man']:,}만\n\n"
                f"카테고리별:\n{cats}\n\n"
                f"26SS 소진율 {inv['rate26']}% / 시간 {inv['time_pct']}% 경과 (D-{inv['d_left']})")

    # 26SS
    if '26ss' in t or '26 ss' in t or '26시즌' in t:
        s = inv['season_map'].get('26SS', {})
        gap = inv['time_pct'] - inv['rate26']
        gap_str = f"{gap}%p 지연" if gap > 0 else f"{abs(gap)}%p 선행"
        return (f"📅 26SS 시즌 현황\n"
                f"재고 {s.get('wh',0):,}개 · ₩{s.get('val_man',0):,}만\n"
                f"소진율 {inv['rate26']}% vs 시간 {inv['time_pct']}% → {gap_str}\n"
                f"마감까지 D-{inv['d_left']} (8/31)")

    # 25FW
    if '25fw' in t or '25 fw' in t or '역시즌' in t:
        s = inv['season_map'].get('25FW', {})
        return (f"❄️ 25FW 역시즌 재고\n"
                f"재고 {s.get('wh',0):,}개 · ₩{s.get('val_man',0):,}만\n"
                f"일평균 소진 {s.get('daily',0)}개/일")

    # 텐트
    if '텐트' in t:
        d = inv['cat_map'].get('텐트', {})
        tops = [l for l in inv['top_lines'] if l['cat'] == '텐트'][:5]
        lines = '\n'.join(f"  · {l['line']} ({l['season']}) {l['wh']:,}개 ₩{round(l['val']/10000):,}만" for l in tops)
        return (f"⛺ 텐트 재고\n"
                f"총 {d.get('wh',0):,}개 · ₩{d.get('val_man',0):,}만 · {d.get('daily',0)}개/일\n\n"
                f"재고액 상위:\n{lines}")

    # 어패럴
    if '어패럴' in t or '의류' in t or '옷' in t:
        d = inv['cat_map'].get('어패럴', {})
        tops = [l for l in inv['top_lines'] if l['cat'] == '어패럴'][:5]
        lines = '\n'.join(f"  · {l['line']} ({l['season']}) {l['wh']:,}개 ₩{round(l['val']/10000):,}만" for l in tops)
        return (f"👕 어패럴 재고\n"
                f"총 {d.get('wh',0):,}개 · ₩{d.get('val_man',0):,}만 · {d.get('daily',0)}개/일\n\n"
                f"재고액 상위:\n{lines}")

    # 기어
    if '기어' in t or 'gear' in t:
        d = inv['cat_map'].get('기어', {})
        tops = [l for l in inv['top_lines'] if l['cat'] == '기어'][:5]
        lines = '\n'.join(f"  · {l['line']} ({l['season']}) {l['wh']:,}개" for l in tops)
        return (f"🎒 기어 재고\n"
                f"총 {d.get('wh',0):,}개 · ₩{d.get('val_man',0):,}만 · {d.get('daily',0)}개/일\n\n"
                f"재고액 상위:\n{lines}")

    # 많이 팔린 / 소진 빠른
    if any(k in t for k in ['많이 팔', '판매', '소진 빠', '잘 팔', '잘팔']):
        tops = inv['top_sold'][:7]
        lines = '\n'.join(
            f"  {i+1}. {l['line']} ({l['season']}) — {l['daily']}개/일 · 재고 {l['wh']:,}개"
            for i, l in enumerate(tops)
        )
        return f"🔥 일평균 소진 상위 품목 (최근 14일)\n{lines}"

    # 재고 많은
    if any(k in t for k in ['재고 많', '많은 재고', '남은 재고', '쌓인']):
        tops = inv['top_lines'][:7]
        lines = '\n'.join(
            f"  {i+1}. {l['line']} ({l['season']}) — ₩{round(l['val']/10000):,}만 · {l['wh']:,}개"
            for i, l in enumerate(tops)
        )
        return f"📦 재고액 상위 품목\n{lines}"

    # 소진율 / D-day
    if any(k in t for k in ['소진율', 'd-', 'd남', '마감', '몇일']):
        gap = inv['time_pct'] - inv['rate26']
        gap_str = f"{gap}%p 지연" if gap > 0 else f"{abs(gap)}%p 선행"
        return (f"📅 26SS 소진율 {inv['rate26']}% (시간 {inv['time_pct']}% 경과)\n"
                f"→ {gap_str} · D-{inv['d_left']} (8/31 마감)")

    # 기본
    return (f"아래 질문에 답할 수 있어요:\n"
            f"• 전체/전사 현황\n"
            f"• 텐트 / 어패럴 / 기어 재고\n"
            f"• 26SS / 25FW 현황\n"
            f"• 많이 팔린 품목\n"
            f"• 재고 많은 품목\n"
            f"• 소진율 / 마감 D-day")

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
