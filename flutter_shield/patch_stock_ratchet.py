with open('lib/shield_controller.dart', 'r', encoding='utf-8') as f:
    ctrl = f.read()

# Track high-water mark bps for open stocks so a +1.1%+ winner never turns into a loss
if "_stockPeakBps" not in ctrl:
    ctrl = ctrl.replace(
        "  final Map<String, DateTime> _entryTimes = <String, DateTime>{};",
        "  final Map<String, DateTime> _entryTimes = <String, DateTime>{};\n  final Map<String, int> _stockPeakBps = <String, int>{};"
    )

old_stock_loop = """      if (gainBps >= 185 || (!hasActiveSell && gainBps <= -150)) {"""
new_stock_loop = """      final prevPeak = _stockPeakBps[sym] ?? gainBps;
      final peakBps = gainBps > prevPeak ? gainBps : prevPeak;
      _stockPeakBps[sym] = peakBps;
      // If a stock reached >= +1.10% (110 bps) and pulls back 45 bps from its peak, lock in the green win!
      final trailTriggered = peakBps >= 110 && gainBps >= 35 && (peakBps - gainBps) >= 45;
      if (gainBps >= 185 || trailTriggered || (!hasActiveSell && gainBps <= -150)) {"""

if old_stock_loop in ctrl:
    ctrl = ctrl.replace(old_stock_loop, new_stock_loop, 1)

# Also fix protectionSummary bug where legs of other orders were read without symbol check
old_legs = """      final legs = o['legs'];
      if (legs is List) {"""
new_legs = """      final legs = normSym('${o['symbol'] ?? ''}') == sym ? o['legs'] : null;
      if (legs is List) {"""
if old_legs in ctrl:
    ctrl = ctrl.replace(old_legs, new_legs, 1)

with open('lib/shield_controller.dart', 'w', encoding='utf-8') as f:
    f.write(ctrl)
print("[OK] Added Stock Peak-Profit Trail Ratchet & fixed TP/SL leg display!")
