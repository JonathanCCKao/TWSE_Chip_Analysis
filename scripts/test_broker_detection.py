import sys
import os
sys.path.insert(0, os.path.abspath("."))
import json
from src.db import ChipDatabase

db = ChipDatabase('data/chips.db')

for stock_id in ['6782', '2301', '1303', '3005']:
    print(f"\n=================== {stock_id} ===================")
    w_ranks = {}
    for w in [1, 5, 20, 60, 120, 240]:
        w_ranks[w] = {
            'BUY': db.get_period_ranks(stock_id, w, side='BUY', limit=5),
            'SELL': db.get_period_ranks(stock_id, w, side='SELL', limit=5),
            'summary': db.get_period_summary(stock_id, w)
        }
    
    # 1. 偵測長線囤貨大戶 (240日與120日)
    hoarders = []
    seen_hoarders = set()
    b60_names = {b['broker_name']: b for b in w_ranks[60]['BUY']}
    b120_names = {b['broker_name']: b for b in w_ranks[120]['BUY']}
    b240_names = {b['broker_name']: b for b in w_ranks[240]['BUY']}
    
    # 先檢視 240 日
    for name, b240 in b240_names.items():
        net = b240['net_shares']
        tot = b240['buy_shares'] + b240['sell_shares']
        purity = (b240['buy_shares'] / tot) if tot > 0 else 0
        is_in_120 = name in b120_names
        is_in_60 = name in b60_names
        
        if (purity >= 0.55 or is_in_120) and b240['ratio'] >= 0.3:
            hoarders.append({
                'name': name,
                'period': 240,
                'net_shares': net,
                'ratio': b240['ratio'],
                'purity': purity,
                'continuous': is_in_120 or is_in_60
            })
            seen_hoarders.add(name)

    # 再檢視 120 日 (若未在 240 日出現，但 120 日積極鎖碼)
    for name, b120 in b120_names.items():
        if name in seen_hoarders:
            continue
        net = b120['net_shares']
        tot = b120['buy_shares'] + b120['sell_shares']
        purity = (b120['buy_shares'] / tot) if tot > 0 else 0
        is_in_60 = name in b60_names
        if (purity >= 0.58 or is_in_60) and b120['ratio'] >= 0.5:
            hoarders.append({
                'name': name,
                'period': 120,
                'net_shares': net,
                'ratio': b120['ratio'],
                'purity': purity,
                'continuous': is_in_60
            })
            seen_hoarders.add(name)
            
    print("【🔒 長線囤貨大戶】:")
    for h in hoarders:
        cont_str = "跨週期持續建倉" if h['continuous'] else "獨立鎖碼"
        print(f"  * {h['name']}: {h['period']}日淨買 {h['net_shares']:,} 張 (佔比 {h['ratio']}%, 囤貨純度 {h['purity']:.1%}, {cont_str})")
    if not hoarders:
        print("  無明顯長線囤貨分點")

    # 2. 偵測波段神算子
    s20_summary = w_ranks[20]['summary']
    s1_summary = w_ranks[1]['summary']
    ref_price = s1_summary['avg_buy_cost'] if s1_summary else 0.0
    
    swing_masters = []
    seen_swing = set()
    
    # A. 買底高手：20日或60日買超主力，成本大幅低於現價 (獲利已拉開 >= 2%)
    for b in w_ranks[20]['BUY']:
        name = b['broker_name']
        cost = s20_summary['avg_buy_cost'] if s20_summary else 0.0
        bias = ((ref_price - cost) / cost * 100) if cost > 0 else 0.0
        if b['ratio'] >= 2.5 and bias >= 2.0:
            swing_masters.append({
                'name': name,
                'role': '🎯買底神手 (獲利中)',
                'period': 20,
                'ratio': b['ratio'],
                'bias': bias
            })
            seen_swing.add(name)

    # B. 高檔逃頂/獲利了結神手：在 1日或 5日大賣 (佔比 >= 6%)，且先前是 60/120/240 日長線大買方
    for w in [1, 5]:
        for s in w_ranks[w]['SELL'][:4]:
            name = s['broker_name']
            if name in seen_swing:
                continue
            is_long_buyer = (name in b60_names or name in b120_names or name in b240_names)
            if s['ratio'] >= 6.0 and is_long_buyer:
                swing_masters.append({
                    'name': name,
                    'role': f'🎯高檔了結神手 ({w}日大賣)',
                    'period': w,
                    'ratio': s['ratio'],
                    'bias': 0.0
                })
                seen_swing.add(name)
            
    print("【🎯 波段神算高手】:")
    for sm in swing_masters:
        detail = f"波段獲利 {sm['bias']:+.1f}%" if sm['bias'] > 0 else f"{sm['period']}日賣超佔比 {sm['ratio']}%"
        print(f"  * {sm['name']}: {sm['role']} (佔比 {sm['ratio']}%, {detail})")
    if not swing_masters:
        print("  觀察中 (近期未見極端波段轉折動作)")
