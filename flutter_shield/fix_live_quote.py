with open('lib/shield_controller.dart', 'r', encoding='utf-8') as f:
    ctrl = f.read()

ctrl = ctrl.replace("final qs = await _quoteFor(sym);", "final qs = await _liveQuote(sym);")

with open('lib/shield_controller.dart', 'w', encoding='utf-8') as f:
    f.write(ctrl)
print("[OK] Replaced _quoteFor with _liveQuote")
