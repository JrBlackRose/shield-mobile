with open('lib/shield_controller.dart', 'r', encoding='utf-8') as f:
    ctrl = f.read()

old_line = "      if (pick == null) return; // no high-conviction setup this round"
new_line = """      if (pick == null) {
        _log('🔍 Scout scanned ${candidates.length} live pairs — no pullback setup met entry criteria.', level: 'info', notify: false);
        return;
      }"""

if old_line in ctrl:
    ctrl = ctrl.replace(old_line, new_line, 1)
    with open('lib/shield_controller.dart', 'w', encoding='utf-8') as f:
        f.write(ctrl)
    print("[OK] Added visible 3-minute Scout heartbeat log to Activity Timeline!")
else:
    print("[INFO] Heartbeat already present or line modified.")
