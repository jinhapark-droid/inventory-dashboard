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

def search_lines(text, line_map):
    """질문에서 품목명 키워드 추출해 line_map 검색"""
    # 불용어 제거
    stopwords = {'재고', '현황', '알려줘', '알려', '줘', '얼마', '몇', '개', '수량',
                 '추이', '판매', '소진', '속도', '빠른', '많은', '남은', '이번주', '지난',
                 '7일간', '14일', '최근', '전체', '전사', '요약'}
    words = [w for w in re.split(r'[\s\[\]()]+', text) if len(w) >= 2 and w not in stopwords]
    if not words:
        return []
    results = []
    for v in line_map.values():
        name_lower = v['line'].lower()
        if any(w.lower() in name_lower for w in words):
            results.append(v)
    return sorted(results, key=lambda x: -x['val'])

def answer(text, inv):
    t = text.lower()
    line_map = inv['line_map']

    # ── 특정 품목명 검색 (최우선) ──────────────────────────────
    matched = search_lines(text, line_map)
    # 카테고리/시즌 키워드가 아닌 경우에만 품목 검색 결과 사용
    generic_keywords = {'전체', '전사', '요약', '현황', '총', '텐트', '어패럴', '의류', '기어',
                        '많이', '잘팔', '재고많', '소진율', '마감', '26ss', '25fw', '역시즌'}
    is_generic = any(k in t for k in generic_keywords)

    if matched and not is_generic:
        if len(matched) == 1:
            l = matched[0]
            daily = l['daily']
            days_left = round(l['wh'] / daily) if daily > 0 else None
            days_str = f" · 현재 속도로 약 {days_left}일치 재고" if days_left else ""
            return (f"📦 {l['line']} ({l['season']}) [{l['cat']}]\n"
                    f"창고재고 {int(l['wh']):,}개 · ₩{round(l['val']/10000):,}만\n"
                    f"최근 14일 일평균 {daily}개/일{days_str}")
        else:
            lines = '\n'.join(
                f"· {l['line']} ({l['season']}) — {int(l['wh']):,}개 · ₩{round(l['val']/10000):,}만 · {l['daily']}개/일"
                for l in matched[:7]
            )
            total_wh = sum(l['wh'] for l in matched)
            total_val = sum(l['val'] for l in matched)
            return (f"🔍 검색 결과 ({len(matched)}개 라인)\n"
                    f"합계 {int(total_wh):,}개 · ₩{round(total_val/10000):,}만\n\n"
                    f"{lines}")

    # ── 카테고리별 ────────────────────────────────────────────
    if '텐트' in t and not any(k in t for k in ['전체', '전사']):
        d = inv['cat_map'].get('텐트', {})
        tops = [l for l in inv['top_lines'] if l['cat'] == '텐트'][:5]
        rows = '\n'.join(f"  · {l['line']} ({l['season']}) {int(l['wh']):,}개 ₩{round(l['val']/10000):,}만 · {l['daily']}개/일" for l in tops)
        return (f"⛺ 텐트 재고\n총 {d.get('wh',0):,}개 · ₩{d.get('val_man',0):,}만 · {d.get('daily',0)}개/일\n\n재고액 상위:\n{rows}")

    if any(k in t for k in ['어패럴', '의류', '옷']):
        d = inv['cat_map'].get('어패럴', {})
        tops = [l for l in inv['top_lines'] if l['cat'] == '어패럴'][:5]
        rows = '\n'.join(f"  · {l['line']} ({l['season']}) {int(l['wh']):,}개 ₩{round(l['val']/10000):,}만 · {l['daily']}개/일" for l in tops)
        return (f"👕 어패럴 재고\n총 {d.get('wh',0):,}개 · ₩{d.get('val_man',0):,}만 · {d.get('daily',0)}개/일\n\n재고액 상위:\n{rows}")

    if any(k in t for k in ['기어', 'gear']):
        d = inv['cat_map'].get('기어', {})
        tops = [l for l in inv['top_lines'] if l['cat'] == '기어'][:5]
        rows = '\n'.join(f"  · {l['line']} ({l['season']}) {int(l['wh']):,}개 ₩{round(l['val']/10000):,}만 · {l['daily']}개/일" for l in tops)
        return (f"🎒 기어 재고\n총 {d.get('wh',0):,}개 · ₩{d.get('val_man',0):,}만 · {d.get('daily',0)}개/일\n\n재고액 상위:\n{rows}")

    # ── 시즌별 ───────────────────────────────────────────────
    if '26ss' in t or '26시즌' in t or '26 ss' in t:
        s = inv['season_map'].get('26SS', {})
        d2z = inv['days_to_zero']
        pace = f"현재 속도로 D-{d2z}일 소진 예상 ({'마감 전 소진 불가' if d2z > inv['d_left'] else '마감 전 소진 가능'})"
        return (f"📅 26SS 시즌 현황 (D-{inv['d_left']}, 마감 8/31)\n"
                f"잔여재고 {s.get('wh',0):,}개 · ₩{inv['val26_man']:,}만\n"
                f"일평균 소진 ₩{inv['daily26_val']}만 · {pace}")

    if '25fw' in t or '역시즌' in t or '25 fw' in t:
        s = inv['season_map'].get('25FW', {})
        return (f"❄️ 25FW 역시즌 재고\n"
                f"재고 {s.get('wh',0):,}개 · ₩{s.get('val_man',0):,}만 · {s.get('daily',0)}개/일")

    # ── 판매 / 소진 랭킹 ─────────────────────────────────────
    if any(k in t for k in ['많이 팔', '잘 팔', '잘팔', '소진 빠', '판매 순', '판매순']):
        tops = inv['top_sold'][:7]
        rows = '\n'.join(f"  {i+1}. {l['line']} ({l['season']}) — {l['daily']}개/일 · 재고 {int(l['wh']):,}개" for i, l in enumerate(tops))
        return f"🔥 일평균 소진 상위 품목 (최근 14일)\n{rows}"

    if any(k in t for k in ['재고 많', '많은 재고', '남은 재고', '쌓인', '고재고']):
        tops = inv['top_lines'][:7]
        rows = '\n'.join(f"  {i+1}. {l['line']} ({l['season']}) — ₩{round(l['val']/10000):,}만 · {int(l['wh']):,}개" for i, l in enumerate(tops))
        return f"📦 재고액 상위 품목\n{rows}"

    # ── 전사 현황 ────────────────────────────────────────────
    if any(k in t for k in ['전체', '요약', '현황', '전사', '총']):
        cats = '\n'.join(
            f"  {cat}: {d['wh']:,}개 · ₩{d['val_man']:,}만 · {d['daily']}개/일"
            for cat, d in sorted(inv['cat_map'].items(), key=lambda x: -x[1]['val_man'])
        )
        d2z = inv['days_to_zero']
        pace = f"D-{d2z}일 소진 예상" if d2z < 999 else "소진 속도 미미"
        return (f"📊 전사 재고 현황\n"
                f"총 창고재고 {inv['total_wh']:,}개 · ₩{inv['total_val_man']:,}만\n\n"
                f"카테고리별:\n{cats}\n\n"
                f"26SS 잔여 ₩{inv['val26_man']:,}만 · {pace} (D-{inv['d_left']} 마감)")

    # ── 기본 안내 ────────────────────────────────────────────
    return ("질문 예시:\n"
            "• 이지팝 TC 재고 현황\n"
            "• 스태고 돔텐트 재고\n"
            "• 텐트 / 어패럴 / 기어 전체\n"
            "• 26SS 현황\n"
            "• 많이 팔린 품목\n"
            "• 재고 많은 품목")

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
