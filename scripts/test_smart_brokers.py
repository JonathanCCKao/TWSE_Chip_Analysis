import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yfinance as yf
from src.db import ChipDatabase

db = ChipDatabase('data/chips.db')

for stock_id in ['6782', '2301', '1303', '3005']:
    sym = f'{stock_id}.TW'
    df = yf.Ticker(sym).history(period='3mo')
    if df.empty:
        df = yf.Ticker(f'{stock_id}.TWO').history(period='3mo')
    if df.empty or len(df) < 20:
        continue
    
    p_min = float(df['Low'].min())
    p_max = float(df['High'].max())
    p_range = p_max - p_min if p_max > p_min else 1.0
    ref_price = float(df['Close'].iloc[-1])
    current_pct = (ref_price - p_min) / p_range * 100
    
    print(f"=== {stock_id} (現價: {ref_price:.1f}, 60日區間: {p_min:.1f} ~ {p_max:.1f}, 當前分位: {current_pct:.1f}%) ===")
    
    # 1. 檢驗長線囤貨大戶 (240日 或 120日)
    long_buyers_240 = db.get_period_ranks(stock_id, 240, side='BUY', limit=5)
    long_buyers_120 = {b['broker_name']: b for b in db.get_period_ranks(stock_id, 120, side='BUY', limit=5)}
    
    hoarders = []
    for b in long_buyers_240:
        name = b['broker_name']
        net = b['net_shares']
        tot = b['buy_shares'] + b['sell_shares']
        purity = (b['buy_shares'] / tot) if tot > 0 else 0
        if purity >= 0.55 or name in long_buyers_120:
            hoarders.append(f"{name}(淨買{net:,}張/純度{purity:.0%})")
    print("  🔒 囤貨大戶:", ", ".join(hoarders) if hoarders else "無")
    
    # 2. 檢驗波段買底 / 賣頂神算子
    b20 = db.get_period_ranks(stock_id, 20, side='BUY', limit=5)
    s20 = db.get_period_ranks(stock_id, 20, side='SELL', limit=5)
    b1 = db.get_period_ranks(stock_id, 1, side='BUY', limit=5)
    s1 = db.get_period_ranks(stock_id, 1, side='SELL', limit=5)
    
    swing_masters = []
    for b in b20:
        if current_pct >= 50 and b['ratio'] >= 3.0:
            swing_masters.append(f"{b['broker_name']}(20日低檔吸籌/佔比{b['ratio']}%)")
    for s in s1:
        if current_pct >= 75 and s['ratio'] >= 5.0:
            swing_masters.append(f"{s['broker_name']}(當日高檔獲利了結/佔比{s['ratio']}%)")
    print("  🎯 波段神算:", ", ".join(swing_masters) if swing_masters else "觀察中")
