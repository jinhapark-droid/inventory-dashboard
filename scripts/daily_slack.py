import json
import re
import urllib.request
from datetime import date, datetime

SHEET_ID = '1ykrQdlyTKAHmf3qgtfAwHLiLgNeFJ5WD3n0wmjxU4I0'
GID_MAIN = '1461767551'
GID_RATE = '1388188128'
SLACK_CHANNEL = 'C03B2KKBVT6'  # #1_사업개발팀

import os
SLACK_TOKEN = os.environ['SLACK_BOT_TOKEN']

def fetch_gviz(gid):
    url = f'https://docs.google.com/spreadsheets/d/{SHEET_ID}/gviz/tq?tqx=out:json&gid={gid}'
    with urllib.request.urlopen(url) as r:
        raw = r.read().decode('utf-8')
    raw = re.sub(r'^[^(]+\(', '', raw).rstrip(');')
    return json.loads(raw)

def cell(c):
    if c is None: return None
    v = c.get('v')
    return v

def num(c): return float(cell(c) or 0)
def s(c):   return str(cell(c) or '').strip()

data = fetch_gviz(GID_MAIN)
rows_raw = data['table']['rows']
header = rows_raw[0]['c']

# 날짜 컬럼 18~32 인덱스 찾기
date_cols = list(range(18, 33))
valid_date_cols = [i for i in date_cols if i < len(header) and header[i] and header[i].get('v') is not None]

SPECIAL = re.compile(r'^\[리퍼\]|^\[B급\]|^\[전시\]', re.I)

rows = [r['c'] for r in rows_raw[1:]]  # 헤더 제외

total_stock_val = 0
total_wh = 0
today_sold = 0
total_14 = 0
cat_map = {}

for r in rows:
    if len(r) <= 14: continue
    name = s(r[0])
    if not name or SPECIAL.search(name): continue
    wh    = num(r[14])
    price = num(r[5])
    cat   = s(r[6]) or '기타'
    total_stock_val += wh * price
    total_wh        += wh

    if len(valid_date_cols) >= 2:
        prev_val = num(r[valid_date_cols[-2]]) if valid_date_cols[-2] < len(r) else 0
        last_val = num(r[valid_date_cols[-1]]) if valid_date_cols[-1] < len(r) else 0
        today_sold += max(0, prev_val - last_val)

    if len(valid_date_cols) >= 1:
        first_val = num(r[valid_date_cols[0]])  if valid_date_cols[0]  < len(r) else 0
        last_val2 = num(r[valid_date_cols[-1]]) if valid_date_cols[-1] < len(r) else 0
        total_14 += max(0, first_val - last_val2)

    if cat in ('어패럴', '기어', '텐트'):
        if cat not in cat_map:
            cat_map[cat] = {'sold14': 0, 'wh': 0, 'stock_val': 0}
        cat_map[cat]['sold14']    += max(0, (num(r[valid_date_cols[0]]) if valid_date_cols else 0) - (num(r[valid_date_cols[-1]]) if valid_date_cols else 0))
        cat_map[cat]['wh']        += wh
        cat_map[cat]['stock_val'] += wh * price

span_days = max(len(valid_date_cols) - 1, 1)
daily_avg = round(total_14 / span_days)
diff_pct  = round((today_sold - daily_avg) / daily_avg * 100) if daily_avg > 0 else 0

# 소진율 시트
rate_data = fetch_gviz(GID_RATE)
rate_rows = rate_data['table']['rows'][1:]
tot26 = wh26_rate = 0
for r in rate_rows:
    c = r['c']
    if len(c) > 2 and s(c[2]) == '26SS':
        tot26     += num(c[5])
        wh26_rate += num(c[7])

rate26 = round((tot26 - wh26_rate) / tot26 * 100) if tot26 > 0 else 0

# 26SS 창고재고액 (메인시트)
val26 = sum(num(r[14]) * num(r[5]) for r in rows if len(r) > 14 and s(r[8]) == '26SS' and not SPECIAL.search(s(r[0])))
val26_man = round(val26 / 10000)

# 시간 진행률
today = date.today()
start = date(2026, 4, 1)
end   = date(2026, 8, 31)
time_pct = round((today - start).days / (end - start).days * 100)
d_left    = (end - today).days
gap       = time_pct - rate26

# 메시지 조합
cats_sorted = sorted(cat_map.items(), key=lambda x: -x[1]['sold14'])
cat_lines = ''
for cat, d in cats_sorted:
    daily = round(d['sold14'] / span_days, 1)
    cat_lines += f"\n{cat} {daily}개/일 — 재고 {d['wh']:,}개 · ₩{round(d['stock_val']/10000):,}만"

diff_str = f"+{diff_pct}%" if diff_pct >= 0 else f"{diff_pct}%"
gap_str  = f"{gap}%p 지연" if gap > 0 else f"{abs(gap)}%p 선행"

# 어제 카테고리별 판매 집계
cat_sold_yday = {}
line_sold_yday = {}
if len(valid_date_cols) >= 2:
    prev_col = valid_date_cols[-2]
    last_col = valid_date_cols[-1]
    for r in rows:
        if len(r) <= 14: continue
        name = s(r[0])
        if not name or SPECIAL.search(name): continue
        cat  = s(r[6]) or '기타'
        line = s(r[9]) or name
        price = num(r[5])
        sold = max(0, (num(r[prev_col]) if prev_col < len(r) else 0) - (num(r[last_col]) if last_col < len(r) else 0))
        if sold == 0: continue
        if cat not in cat_sold_yday:
            cat_sold_yday[cat] = {'qty': 0, 'val': 0}
        cat_sold_yday[cat]['qty'] += sold
        cat_sold_yday[cat]['val'] += sold * price
        if line not in line_sold_yday:
            line_sold_yday[line] = {'cat': cat, 'qty': 0, 'val': 0}
        line_sold_yday[line]['qty'] += sold
        line_sold_yday[line]['val'] += sold * price

yday_label = f"{today.month}/{today.day - 1}" if today.day > 1 else "어제"

# 카테고리 요약줄
yday_cat_lines = ''
for cat in ['어패럴', '텐트', '기어']:
    d = cat_sold_yday.get(cat)
    if d and d['qty'] > 0:
        yday_cat_lines += f"\n{cat} {d['qty']}개 — ₩{round(d['val']/10000, 1)}만"

# 판매 상위 품목 (수량 기준 top 5)
top_lines_yday = sorted(line_sold_yday.items(), key=lambda x: -x[1]['qty'])[:5]
top_lines_str = '\n'.join(
    f"· {line} {d['qty']}개 (₩{round(d['val']/10000, 1)}만)"
    for line, d in top_lines_yday
)

# 인사이트 코멘트
app_qty  = cat_sold_yday.get('어패럴', {}).get('qty', 0)
tent_qty = cat_sold_yday.get('텐트', {}).get('qty', 0)
app_val2 = round(cat_sold_yday.get('어패럴', {}).get('val', 0) / 10000, 1)
tent_val2= round(cat_sold_yday.get('텐트', {}).get('val', 0) / 10000, 1)

if tent_qty > 0 and tent_val2 >= app_val2:
    yday_comment = f"수량은 어패럴({app_qty}개)이 많았지만 텐트({tent_qty}개)가 단가 우위로 매출 비슷. 텐트 단가 레버리지 주목."
elif app_qty > tent_qty * 3:
    yday_comment = f"어패럴이 수량 기준 전체의 {round(app_qty/(app_qty+tent_qty+1)*100)}% 차지. 텐트는 {tent_qty}개로 부진 — 7월 프로모션 연계 필요."
else:
    yday_comment = f"어패럴 {app_qty}개·텐트 {tent_qty}개 고른 판매. 26SS 마감까지 {d_left}일, 소진율 관리 중요한 시점."

insight = f"어패럴·텐트 중심 전일 판매를 분석했어요. {yday_comment}"

today_str = f"{today.month}/{today.day}"
message = f"""[재고 현황 업데이트] {today_str} 오후 7시

📊 전사 재고 요약
총 재고액 ₩{round(total_stock_val/10000):,}만 | 창고재고 {int(total_wh):,}개
14일 일평균 소진 {daily_avg}개/일 · 오늘 {int(today_sold)}개 판매 (평균 대비 {diff_str})

📦 카테고리별 소진 속도 (최근 14일){cat_lines}

📅 26SS 시즌 현황 (D-{d_left}, 마감 8/31)
소진율 {rate26}% vs 시간 {time_pct}% 경과 → {gap_str} · 잔여 재고액 ₩{val26_man:,}만

💡 인사이트
{insight}

📈 어제({yday_label}) 판매 현황{yday_cat_lines}

판매 상위 품목
{top_lines_str}

🔗 https://jinhapark-droid.github.io/inventory-dashboard/"""

# Slack 전송
payload = json.dumps({'channel': SLACK_CHANNEL, 'text': message}).encode()
req = urllib.request.Request(
    'https://slack.com/api/chat.postMessage',
    data=payload,
    headers={'Content-Type': 'application/json', 'Authorization': f'Bearer {SLACK_TOKEN}'}
)
with urllib.request.urlopen(req) as r:
    result = json.loads(r.read())

if result.get('ok'):
    print(f"✅ Slack 전송 완료: {today_str}")
else:
    raise Exception(f"Slack 전송 실패: {result.get('error')}")
