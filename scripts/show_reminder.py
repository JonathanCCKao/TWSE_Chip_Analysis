import ctypes
msg = "【籌碼分析年度排程提醒】\n\n自動化排程即將於 2026/12/31 到期！\n請與 Jonathan 討論進行 2026 年度成效檢討，並規劃 2027 年度的自動化安排。"
ctypes.windll.user32.MessageBoxW(0, msg, "【分點籌碼分析】年度排程到期檢討提醒", 0x00000040)
