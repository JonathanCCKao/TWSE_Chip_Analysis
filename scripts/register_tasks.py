import os
import sys
import subprocess
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

# 專案絕對路徑
PROJECT_DIR = str(Path(__file__).resolve().parent.parent)
PYTHONW_EXE = r"C:\Users\jonathancc_kao\pythonw.exe"
RUNNER_SCRIPT = os.path.join(PROJECT_DIR, "scripts", "scheduler_runner.py")
REMINDER_SCRIPT = os.path.join(PROJECT_DIR, "scripts", "show_reminder.py")

# 建立專屬的提醒彈窗 python 腳本
with open(REMINDER_SCRIPT, "w", encoding="utf-8") as f:
    f.write(r'''import ctypes
msg = "【籌碼分析年度排程提醒】\n\n自動化排程即將於 2026/12/31 到期！\n請與 Jonathan 討論進行 2026 年度成效檢討，並規劃 2027 年度的自動化安排。"
ctypes.windll.user32.MessageBoxW(0, msg, "【分點籌碼分析】年度排程到期檢討提醒", 0x00000040)
''')

# 1. 每日日報 Task XML (週一至週五 21:45，至 2026-12-31)
daily_xml = f'''<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.3" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Daily Tactical Chip Brief at 21:45 (Mon-Fri) until 2026-12-31</Description>
    <URI>\\TWSE_Chip_Daily_Report</URI>
  </RegistrationInfo>
  <Principals>
    <Principal id="Author">
      <LogonType>InteractiveToken</LogonType>
    </Principal>
  </Principals>
  <Settings>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <StartWhenAvailable>true</StartWhenAvailable>
    <UseUnifiedSchedulingEngine>true</UseUnifiedSchedulingEngine>
  </Settings>
  <Triggers>
    <CalendarTrigger>
      <StartBoundary>2026-10-15T21:45:00</StartBoundary>
      <EndBoundary>2026-12-31T23:59:59</EndBoundary>
      <ScheduleByWeek>
        <WeeksInterval>1</WeeksInterval>
        <DaysOfWeek>
          <Monday />
          <Tuesday />
          <Wednesday />
          <Thursday />
          <Friday />
        </DaysOfWeek>
      </ScheduleByWeek>
    </CalendarTrigger>
  </Triggers>
  <Actions Context="Author">
    <Exec>
      <Command>{PYTHONW_EXE}</Command>
      <Arguments>"{RUNNER_SCRIPT}" --mode daily</Arguments>
      <WorkingDirectory>{PROJECT_DIR}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>'''

# 2. 每週週報 Task XML (每週五 22:00，至 2026-12-31)
weekly_xml = f'''<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.3" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Weekly Strategic Chip Report at 22:00 (Fri) until 2026-12-31</Description>
    <URI>\\TWSE_Chip_Weekly_Report</URI>
  </RegistrationInfo>
  <Principals>
    <Principal id="Author">
      <LogonType>InteractiveToken</LogonType>
    </Principal>
  </Principals>
  <Settings>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <StartWhenAvailable>true</StartWhenAvailable>
    <UseUnifiedSchedulingEngine>true</UseUnifiedSchedulingEngine>
  </Settings>
  <Triggers>
    <CalendarTrigger>
      <StartBoundary>2026-10-16T22:00:00</StartBoundary>
      <EndBoundary>2026-12-31T23:59:59</EndBoundary>
      <ScheduleByWeek>
        <WeeksInterval>1</WeeksInterval>
        <DaysOfWeek>
          <Friday />
        </DaysOfWeek>
      </ScheduleByWeek>
    </CalendarTrigger>
  </Triggers>
  <Actions Context="Author">
    <Exec>
      <Command>{PYTHONW_EXE}</Command>
      <Arguments>"{RUNNER_SCRIPT}" --mode weekly</Arguments>
      <WorkingDirectory>{PROJECT_DIR}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>'''

# 3. 年度檢討提醒 Task XML (2026-12-28 與 12-29 上午 09:30)
reminder_xml = f'''<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.3" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Annual Review Reminder on 2026-12-28 and 12-29</Description>
    <URI>\\TWSE_Chip_2026_Review_Reminder</URI>
  </RegistrationInfo>
  <Principals>
    <Principal id="Author">
      <LogonType>InteractiveToken</LogonType>
    </Principal>
  </Principals>
  <Settings>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <StartWhenAvailable>true</StartWhenAvailable>
    <UseUnifiedSchedulingEngine>true</UseUnifiedSchedulingEngine>
  </Settings>
  <Triggers>
    <TimeTrigger>
      <StartBoundary>2026-12-28T09:30:00</StartBoundary>
      <EndBoundary>2026-12-31T23:59:59</EndBoundary>
    </TimeTrigger>
    <TimeTrigger>
      <StartBoundary>2026-12-29T09:30:00</StartBoundary>
      <EndBoundary>2026-12-31T23:59:59</EndBoundary>
    </TimeTrigger>
  </Triggers>
  <Actions Context="Author">
    <Exec>
      <Command>{PYTHONW_EXE}</Command>
      <Arguments>"{REMINDER_SCRIPT}"</Arguments>
      <WorkingDirectory>{PROJECT_DIR}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>'''

def save_and_register(task_name: str, xml_content: str):
    temp_xml_path = os.path.join(PROJECT_DIR, "scripts", f"{task_name}.xml")
    with open(temp_xml_path, "w", encoding="utf-16") as f:
        f.write(xml_content)
    
    cmd = ["schtasks", "/create", "/tn", task_name, "/xml", temp_xml_path, "/f"]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode == 0:
        print(f"✅ 成功註冊排程: {task_name}")
    else:
        print(f"❌ 註冊失敗: {task_name}, 錯誤: {res.stderr}")
    
    # 註冊後刪除暫存 XML
    if os.path.exists(temp_xml_path):
        os.remove(temp_xml_path)

if __name__ == "__main__":
    print(f"正在重新註冊排程任務至 Windows Task Scheduler (工作路徑: {PROJECT_DIR})...")
    save_and_register("TWSE_Chip_Daily_Report", daily_xml)
    save_and_register("TWSE_Chip_Weekly_Report", weekly_xml)
    save_and_register("TWSE_Chip_2026_Review_Reminder", reminder_xml)
    print("完成！")
