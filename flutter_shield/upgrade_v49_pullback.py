import re

# 1. Patch lib/shield_core.dart: Shift RSI targets from Breakout/Momentum to Pullback/Cool-off
with open('lib/shield_core.dart', 'r', encoding='utf-8') as f:
    core = f.read()

core = core.replace(
    "bool get rsiConstructive => rsiValue >= 50.0 && rsiValue < 70.0;",
    "bool get rsiConstructive => rsiValue >= 45.0 && rsiValue <= 60.0; // Pullback cooling zone"
)
core = core.replace(
    "bool get rsiOverbought => rsiValue >= 70.0;",
    "bool get rsiOverbought => rsiValue > 60.0; // Extended/Climax zone"
)

with open('lib/shield_core.dart', 'w', encoding='utf-8') as f:
    f.write(core)
print("[OK] Patched lib/shield_core.dart (RSI Pullback Bands)")

# 2. Patch lib/shield_controller.dart: Implement Wick Rejection & Support Proximity
with open('lib/shield_controller.dart', 'r', encoding='utf-8') as f:
    ctrl = f.read()

ctrl = ctrl.replace("currentVersion = '4.8.0';", "currentVersion = '4.9.0';")

old_conf = """  final range = last.h - last.l;
  final strongClose = range <= 0 ? true : ((last.c - last.l) / range) >= 0.45;
  final trendAligned = ema9 >= ema21 && price >= ema9 && strongClose;

  final vwap = sessionVwap(session);
  final vr = volumeRatio(done.map((c) => c.v).toList(), avgBars: ConfluenceRules.volAvgBars);
  var rsi = wilderRsi(closes, period: ConfluenceRules.rsiPeriod);
  if (vwap == null || vr == null || rsi == null) return null;
  // Penalize RSI score if EMA9 < EMA21 downtrend or weak rejection wick
  if (!trendAligned && rsi >= 50.0 && rsi < 70.0) {
    rsi = 44.0;
  }"""

new_conf = """  final range = last.h - last.l;
  // PULLBACK SNIPER LOGIC: Look for a lower wick (hammer) showing buyers defending the dip
  final bodyBottom = math.min(last.o, last.c);
  final lowerWick = bodyBottom - last.l;
  final isDipRejection = range <= 0 ? true : (lowerWick / range) >= 0.35; // 35% lower wick

  // Support proximity: price should be resting near the EMA9/21, not flying away from it
  import 'dart:math' as math; // ensure math is available for max
  final distFromEma = (price - math.max(ema9, ema21)).abs() / price;
  final nearSupport = distFromEma < 0.0035; // Within 0.35% of moving average

  final trendAligned = ema9 >= ema21 && price >= ema21 && isDipRejection && nearSupport;

  final vwap = sessionVwap(session);
  final vr = volumeRatio(done.map((c) => c.v).toList(), avgBars: ConfluenceRules.volAvgBars);
  var rsi = wilderRsi(closes, period: ConfluenceRules.rsiPeriod);
  if (vwap == null || vr == null || rsi == null) return null;

  // Penalize RSI if it's not a valid pullback to support
  if (!trendAligned && rsi >= 45.0 && rsi <= 60.0) {
    rsi = 40.0; // Force it out of the constructive band
  }"""

if old_conf in ctrl:
    ctrl = ctrl.replace(old_conf, new_conf, 1)

with open('lib/shield_controller.dart', 'w', encoding='utf-8') as f:
    f.write(ctrl)
print("[OK] Patched lib/shield_controller.dart (Pullback Sniper Engine)")

# 3. Patch lib/main.dart for v4.9
with open('lib/main.dart', 'r', encoding='utf-8') as f:
    main_code = f.read()
main_code = main_code.replace("SHIELD v4.8", "SHIELD v4.9")
with open('lib/main.dart', 'w', encoding='utf-8') as f:
    f.write(main_code)
print("[OK] Patched lib/main.dart (SHIELD v4.9)")
