import json
import re
import urllib.request
from datetime import date

SHEET_ID = '1ykrQdlyTKAHmf3qgtfAwHLiLgNeFJ5WD3n0wmjxU4I0'
GID_MAIN = '1461767551'
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

date_cols = list(range(18, 33))
valid_date_cols = [i for i in date_cols if i < len(header) and header[i] and header[i].get('v') is not None]

SPECIAL = re.compile(r'^\[리퍼\]|^\[B급\]|^\[전시\]', re.I)

rows = [r['c'] for r in rows_raw[1:]]

total_stock_val = 0
total_wh = 0
today_sold = 0
total_14 = 0
cat_map = {}          # 카테고리별 재고
app_season_map = {}   # 어패럴 시즌별 재고
line_sold7 = {}       # 라인명별 7일 실판매수량
line_order = {}       # 라인명별 사전구매(접수건, wh=0) 수량
preorder_by_line = {} # 사전구매 재고: 라인명 → [(상품명, qty), ...]

# 7일 기준 컬럼: 마지막 8개 (7일 span)
seven_cols = valid_date_cols[-8:] if len(valid_date_cols) >= 8 else valid_date_cols

for r in rows:
    if len(r) <= 14: continue
    name = s(r[0])
    if not name or SPECIAL.search(name): continue
    wh    = num(r[14])
    price = num(r[5])
    cat   = s(r[6]) or '기타'
    line  = s(r[9]) or name
    season = s(r[8])
    order = num(r[13]) if len(r) > 13 else 0

    total_stock_val += wh * price
    total_wh        += wh

    # 오늘 판매 (전날 대비)
    if len(valid_date_cols) >= 2:
        prev_val = num(r[valid_date_cols[-2]]) if valid_date_cols[-2] < len(r) else 0
        last_val = num(r[valid_date_cols[-1]]) if valid_date_cols[-1] < len(r) else 0
        today_sold += max(0, prev_val - last_val)

    # 14일 판매 (일평균 계산용)
    sold14 = 0
    if len(valid_date_cols) >= 1:
        first_val = num(r[valid_date_cols[0]])  if valid_date_cols[0]  < len(r) else 0
        last_val2 = num(r[valid_date_cols[-1]]) if valid_date_cols[-1] < len(r) else 0
        sold14 = max(0, first_val - last_val2)
        total_14 += sold14

    # 카테고리별 재고
    if cat in ('어패럴', '기어', '텐트'):
        if cat not in cat_map:
            cat_map[cat] = {'wh': 0, 'stock_val': 0}
        cat_map[cat]['wh']        += wh
        cat_map[cat]['stock_val'] += wh * price

    # 어패럴 시즌별 재고
    if cat == '어패럴':
        if season not in app_season_map:
            app_season_map[season] = {'wh': 0, 'stock_val': 0}
        app_season_map[season]['wh']        += wh
        app_season_map[season]['stock_val'] += wh * price

    if cat not in ('어패럴', '기어', '텐트'):
        continue

    # 실판매: 7일간 창고재고 감소 (창고재고 있는 품목)
    if wh > 0 and len(seven_cols) >= 2:
        first7 = num(r[seven_cols[0]])  if seven_cols[0]  < len(r) else 0
        last7  = num(r[seven_cols[-1]]) if seven_cols[-1] < len(r) else 0
        sold7  = max(0, first7 - last7)
        if sold7 > 0:
            line_sold7[line] = line_sold7.get(line, 0) + sold7

    # 사전구매 증가: wh=0이고 접수건 있는 품목
    if wh == 0 and order > 0:
        line_order[line] = line_order.get(line, 0) + int(order)
        # 하위 품목 상세 (사전구매 재고 섹션용)
        sub_name = name.replace(line, '').strip() or '본품'
        if not sub_name:
            sub_name = '본품'
        if line not in preorder_by_line:
            preorder_by_line[line] = []
        preorder_by_line[line].append((sub_name, int(order)))

span_days = max(len(valid_date_cols) - 1, 1)
daily_avg = round(total_14 / span_days)
diff_pct  = round((today_sold - daily_avg) / daily_avg * 100) if daily_avg > 0 else 0
diff_str  = f"+{diff_pct}%" if diff_pct >= 0 else f"{diff_pct}%"

today = date.today()
today_str = today.strftime('%Y-%m-%d')

# 카테고리별 재고 현황 (어패럴, 기어, 텐트 순)
cat_lines = ''
for cat in ['어패럴', '기어', '텐트']:
    d = cat_map.get(cat)
    if d:
        cat_lines += f"\n{cat} {int(d['wh']):,}개 · ₩{round(d['stock_val']/10000):,}만"

# 어패럴 시즌별 재고액
SEASON_ORDER = ['24FW', '25SS', '25FW', '26SS', '26FW', '27SS']
season_lines = ''
for season in SEASON_ORDER:
    d = app_season_map.get(season)
    if d and d['wh'] > 0:
        season_lines += f"\n{season} {int(d['wh']):,}개 · ₩{round(d['stock_val']/10000):,}만"
for season, d in sorted(app_season_map.items(), key=lambda x: x[0]):
    if season not in SEASON_ORDER and d['wh'] > 0:
        season_lines += f"\n{season} {int(d['wh']):,}개 · ₩{round(d['stock_val']/10000):,}만"

# 소진 활발 품목 top5 (7일 기준, 실판매 + 사전구매 혼합)
activity = []
for line, qty in line_sold7.items():
    activity.append((line, qty, '실판매'))
for line, qty in line_order.items():
    if line not in line_sold7:  # 실판매에 없는 경우만 사전구매로 추가
        activity.append((line, qty, '사전구매 증가'))
    else:
        # 둘 다 있으면 사전구매 수량도 별도 항목으로
        activity.append((line, qty, '사전구매 증가'))

activity_sorted = sorted(activity, key=lambda x: -x[1])
# 라인명 중복 제거 (같은 라인이 실판매+사전구매 둘 다 있으면 큰 쪽 우선)
seen_lines = {}
for line, qty, kind in activity_sorted:
    if line not in seen_lines:
        seen_lines[line] = (qty, kind)
top5 = sorted(seen_lines.items(), key=lambda x: -x[1][0])[:5]

top_sold_str = ''
for i, (line, (qty, kind)) in enumerate(top5, 1):
    if kind == '사전구매 증가':
        top_sold_str += f"\n{i}. {line} +{qty}개 ({kind})"
    else:
        top_sold_str += f"\n{i}. {line} {qty}개 ({kind})"

# 사전구매 재고 (라인명별 합산 + 하위 항목)
preorder_lines = ''
preorder_summary = {
    line: sum(q for _, q in items)
    for line, items in preorder_by_line.items()
}
for line, total in sorted(preorder_summary.items(), key=lambda x: -x[1])[:5]:
    preorder_lines += f"\n{line} {total}개 발주 · 창고 미입고"
    items = sorted(preorder_by_line[line], key=lambda x: -x[1])
    if len(items) > 1:
        sub_str = ', '.join(f"{sn} {sq}개" for sn, sq in items)
        preorder_lines += f"\nㄴ {sub_str}"
if not preorder_lines:
    preorder_lines = '\n해당 없음'

message = f"""[재고 현황 업데이트] {today_str} 오후 7시

📊 재고 요약
총 재고액 ₩{round(total_stock_val/10000):,}만 | 창고재고 {int(total_wh):,}개
14일 일평균 소진 {daily_avg}개/일 · 오늘 {int(today_sold)}개 판매 (평균 대비 {diff_str})

📦 카테고리별 재고 현황{cat_lines}

👕 어패럴 시즌별 재고액{season_lines}

🔥 소진 활발 품목 (최근 7일){top_sold_str}

📥 사전 구매 재고{preorder_lines}

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
