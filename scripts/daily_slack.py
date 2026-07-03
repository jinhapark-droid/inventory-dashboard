import json
import re
import urllib.request
from datetime import date, datetime

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
order_cat_map = {}   # 접수건 카테고리별 합계
preorder_items = []  # 재고 0인데 접수건 있는 품목
line_map = {}        # 품목별 재고/판매 (소진 임박 계산용)

# 26SS 소진율 메인시트 직접 계산
ss26_initial = 0
ss26_current = 0

for r in rows:
    if len(r) <= 14: continue
    name = s(r[0])
    if not name or SPECIAL.search(name): continue
    wh    = num(r[14])
    price = num(r[5])
    cat   = s(r[6]) or '기타'
    line  = s(r[9]) or name
    season = s(r[8])
    order = num(r[13]) if len(r) > 13 else 0  # 접수건 (컬럼 N)
    total_stock_val += wh * price
    total_wh        += wh

    # 접수건 집계
    if order > 0:
        if cat not in order_cat_map:
            order_cat_map[cat] = 0
        order_cat_map[cat] += order
        if wh == 0:
            preorder_items.append((line, int(order), cat))

    if len(valid_date_cols) >= 2:
        prev_val = num(r[valid_date_cols[-2]]) if valid_date_cols[-2] < len(r) else 0
        last_val = num(r[valid_date_cols[-1]]) if valid_date_cols[-1] < len(r) else 0
        today_sold += max(0, prev_val - last_val)

    if len(valid_date_cols) >= 1:
        first_val = num(r[valid_date_cols[0]])  if valid_date_cols[0]  < len(r) else 0
        last_val2 = num(r[valid_date_cols[-1]]) if valid_date_cols[-1] < len(r) else 0
        total_14 += max(0, first_val - last_val2)

        # 26SS 소진율: 초기재고(최초날짜) vs 현재재고
        if season == '26SS' and not SPECIAL.search(name):
            ss26_initial += first_val
            ss26_current += last_val2

        # 품목별 소진 속도 (소진 임박 / 부진 고재고 계산용)
        sold14_item = max(0, first_val - last_val2)
        daily_item = sold14_item / max(len(valid_date_cols) - 1, 1)
        if wh > 0 and cat in ('어패럴', '텐트', '기어'):
            key = f'{line}||{cat}'
            if key not in line_map:
                line_map[key] = {'line': line, 'cat': cat, 'wh': 0, 'daily': 0, 'val': 0}
            line_map[key]['wh']    += wh
            line_map[key]['daily'] += daily_item
            line_map[key]['val']   += wh * price

    if cat in ('어패럴', '기어', '텐트'):
        if cat not in cat_map:
            cat_map[cat] = {'sold14': 0, 'wh': 0, 'stock_val': 0}
        cat_map[cat]['sold14']    += max(0, (num(r[valid_date_cols[0]]) if valid_date_cols else 0) - (num(r[valid_date_cols[-1]]) if valid_date_cols else 0))
        cat_map[cat]['wh']        += wh
        cat_map[cat]['stock_val'] += wh * price

span_days = max(len(valid_date_cols) - 1, 1)
daily_avg = round(total_14 / span_days)
diff_pct  = round((today_sold - daily_avg) / daily_avg * 100) if daily_avg > 0 else 0

# 26SS 현황 (메인시트 기반)
val26_wh   = 0
val26_sold_14d = 0
wh26_cnt   = 0
for r in rows:
    if len(r) <= 14: continue
    if s(r[8]) != '26SS' or SPECIAL.search(s(r[0])): continue
    wh    = num(r[14])
    price = num(r[5])
    val26_wh += wh * price
    wh26_cnt += wh
    if len(valid_date_cols) >= 2:
        fv = num(r[valid_date_cols[0]]) if valid_date_cols[0] < len(r) else 0
        lv = num(r[valid_date_cols[-1]]) if valid_date_cols[-1] < len(r) else 0
        val26_sold_14d += max(0, fv - lv) * price

val26_man      = round(val26_wh / 10000)
val26_sold_man = round(val26_sold_14d / 10000)
daily26_val    = round(val26_sold_14d / span_days / 10000, 1)  # 26SS 일평균 매출액(만원)
days_to_zero   = round(val26_wh / (val26_sold_14d / span_days)) if val26_sold_14d > 0 else 999

# 시간 진행률
today    = date.today()
start    = date(2026, 4, 1)
end      = date(2026, 8, 31)
time_pct = round((today - start).days / (end - start).days * 100)
d_left   = (end - today).days

# 메시지 조합
cats_sorted = sorted(cat_map.items(), key=lambda x: -x[1]['sold14'])
cat_lines = ''
for cat, d in cats_sorted:
    daily = round(d['sold14'] / span_days, 1)
    cat_lines += f"\n{cat} {daily}개/일 — 재고 {int(d['wh']):,}개 · ₩{round(d['stock_val']/10000):,}만"

diff_str = f"+{diff_pct}%" if diff_pct >= 0 else f"{diff_pct}%"

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
        yday_cat_lines += f"\n{cat} {int(d['qty'])}개 — ₩{round(d['val']/10000, 1)}만"

# 판매 상위 품목 (수량 기준 top 5)
top_lines_yday = sorted(line_sold_yday.items(), key=lambda x: -x[1]['qty'])[:5]
top_lines_str = '\n'.join(
    f"· {line} {int(d['qty'])}개 (₩{round(d['val']/10000, 1)}만)"
    for line, d in top_lines_yday
)

# 소진 임박 품목 (현재 속도로 30일 내 소진 예상, 재고액 500만 이상)
stockout_items = []
for key, d in line_map.items():
    if d['daily'] > 0 and d['val'] >= 5_000_000:
        days_left = d['wh'] / d['daily']
        if days_left <= 30:
            stockout_items.append((d['line'], d['cat'], int(d['wh']), round(days_left), round(d['val']/10000)))
stockout_items = sorted(stockout_items, key=lambda x: x[3])[:5]

stockout_section = ''
if stockout_items:
    for line, cat, wh, days, val_man in stockout_items:
        stockout_section += f"\n⏰ {line} [{cat}] — {wh}개 남음 · D-{days}일 소진 예상 (₩{val_man:,}만)"
else:
    stockout_section = '\n해당 없음'

# 부진 고재고 품목 (재고액 1000만 이상인데 14일간 판매 0)
slow_items = []
for key, d in line_map.items():
    if d['daily'] == 0 and d['val'] >= 10_000_000:
        slow_items.append((d['line'], d['cat'], int(d['wh']), round(d['val']/10000)))
slow_items = sorted(slow_items, key=lambda x: -x[3])[:5]

slow_section = ''
if slow_items:
    for line, cat, wh, val_man in slow_items:
        slow_section += f"\n🧊 {line} [{cat}] — {wh:,}개 · ₩{val_man:,}만 (14일 판매 0)"
else:
    slow_section = '\n해당 없음'

# 변동사항 감지 (입고 / 신규 품목)
restocked_items = []
new_items = []
if len(valid_date_cols) >= 2:
    prev_col  = valid_date_cols[-2]
    last_col  = valid_date_cols[-1]
    first_col = valid_date_cols[0]
    for r in rows:
        if len(r) <= 14: continue
        name = s(r[0])
        if not name or SPECIAL.search(name): continue
        line  = s(r[9]) or name
        prev_v = num(r[prev_col]) if prev_col < len(r) else 0
        last_v = num(r[last_col]) if last_col < len(r) else 0
        diff   = last_v - prev_v
        if diff >= 10:
            restocked_items.append((line, int(diff)))
        if last_v > 0:
            all_prev_zero = all(
                (num(r[i]) if i < len(r) and r[i] and r[i].get('v') is not None else 0) == 0
                for i in valid_date_cols[:-1]
            )
            if all_prev_zero:
                new_items.append((line, int(last_v)))

restocked_items = sorted(restocked_items, key=lambda x: -x[1])[:5]
# 신규 품목 라인명 기준 중복 제거
new_merged = {}
for line, qty in new_items:
    new_merged[line] = new_merged.get(line, 0) + qty
new_items = sorted(new_merged.items(), key=lambda x: -x[1])[:5]

changes_lines = ''
for line, qty in restocked_items:
    changes_lines += f"\n📥 {line} +{qty:,}개 (입고된 것 같아요)"
for line, qty in new_items:
    changes_lines += f"\n🆕 신규 품목: {line} ({qty:,}개)"
if not changes_lines:
    changes_lines = '\n변동사항 없음'

# 인사이트
tent_stock_val = round(cat_map.get('텐트', {}).get('stock_val', 0) / 10000)
app_stock_val  = round(cat_map.get('어패럴', {}).get('stock_val', 0) / 10000)
tent_daily_spd = round(cat_map.get('텐트', {}).get('sold14', 0) / span_days, 1)
if days_to_zero < 30:
    ss26_pace = f"⚠️ 현재 소진 속도 유지 시 {days_to_zero}일 내 소진 완료 예상"
elif days_to_zero < d_left:
    ss26_pace = f"현재 속도로 {days_to_zero}일 내 소진 — 시즌 마감({d_left}일 남음) 전 여유 있음"
else:
    ss26_pace = f"현재 속도로 마감 전 소진 불가 (예상 소진일 D-{days_to_zero}, 마감 D-{d_left})"

insight = (
    f"텐트 재고액(₩{tent_stock_val:,}만)이 어패럴(₩{app_stock_val:,}만)과 비슷한 수준인데 "
    f"소진 속도는 {tent_daily_spd}개/일로 절반에 불과해요. "
    f"26SS 잔여재고 ₩{val26_man:,}만 (일평균 ₩{daily26_val}만 소진) — {ss26_pace}."
)
if diff_pct < -20:
    insight += f" 오늘 판매량({int(today_sold)}개)은 최근 평균 대비 {abs(diff_pct)}% 저조했어요."
elif diff_pct >= 20:
    insight += f" 오늘 판매량({int(today_sold)}개)은 최근 평균 대비 {diff_pct}% 호조예요."

# 접수건 섹션
total_orders = sum(order_cat_map.values())
order_section = f"\n총 {int(total_orders)}건"
for cat in ['어패럴', '텐트', '기어']:
    qty = order_cat_map.get(cat, 0)
    if qty > 0:
        order_section += f"\n{cat} {int(qty)}건"
other_orders = sum(v for k, v in order_cat_map.items() if k not in ('어패럴', '텐트', '기어'))
if other_orders > 0:
    order_section += f"\n기타 {int(other_orders)}건"
if preorder_items:
    preorder_merged = {}
    for line, qty, cat in preorder_items:
        key = (line, cat)
        preorder_merged[key] = preorder_merged.get(key, 0) + qty
    order_section += "\n\n⚠️ 사전구매 중 (재고 0)"
    for (line, cat), qty in sorted(preorder_merged.items(), key=lambda x: -x[1])[:5]:
        order_section += f"\n· {line} {qty}건 [{cat}]"
if total_orders == 0:
    order_section = "\n접수건 없음"

today_str = f"{today.month}/{today.day}"
message = f"""[재고 현황 업데이트] {today_str} 오후 7시

📊 전사 재고 요약
총 재고액 ₩{round(total_stock_val/10000):,}만 | 창고재고 {int(total_wh):,}개
14일 일평균 소진 {daily_avg}개/일 · 오늘 {int(today_sold)}개 판매 (평균 대비 {diff_str})

📦 카테고리별 소진 속도 (최근 14일){cat_lines}

📅 26SS 시즌 현황 (D-{d_left}, 마감 8/31)
잔여 재고액 ₩{val26_man:,}만 · 시간 {time_pct}% 경과
일평균 소진 ₩{daily26_val}만 · {ss26_pace}

💡 인사이트
{insight}

📈 어제({yday_label}) 판매 현황{yday_cat_lines}

판매 상위 품목
{top_lines_str}

📬 금일 출고 접수{order_section}

⏱ 재고 소진 임박 (30일 내, 재고액 500만↑){stockout_section}

🧊 부진 고재고 품목 (14일 판매 0, 재고액 1000만↑){slow_section}

🔔 변동사항{changes_lines}

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
