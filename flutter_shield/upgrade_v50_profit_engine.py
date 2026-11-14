import re

# ==============================================================================
# 1. PATCH lib/shield_core.dart
#    - Tighten intraday max spread from 45.0 bps to 22.0 bps
#    - Fix FeedEvent.category & cleanTitle so "Connected ... AUTO mode (Key ..WMSF)"
#      is never mislabeled as "Auto Trade Placed · WMSF"
# ==============================================================================
with open('lib/shield_core.dart', 'r', encoding='utf-8') as f:
    core = f.read()

core = core.replace(
    "double maxSpreadBps(Timeframe tf) => tf == Timeframe.intraday ? 45.0 : 80.0;",
    "double maxSpreadBps(Timeframe tf) => tf == Timeframe.intraday ? 22.0 : 55.0;"
)

core = core.replace(
"""  String get category {
    if (_has(const ['Approved', 'AUTO', 'Staged', 'Order'])) return 'trade';""",
"""  String get category {
    if (_has(const ['Connected to', 'Disconnected', 'Mode →'])) return 'system';
    if (_has(const ['Scout', 'Autonomous Scout'])) return 'ai';
    if (_has(const ['Approved', 'AUTO', 'Staged', 'Order', 'Profit', 'Ratchet'])) return 'trade';"""
)

with open('lib/shield_core.dart', 'w', encoding='utf-8') as f:
    f.write(core)
print("[OK] Patched lib/shield_core.dart (22 bps spread cap + fixed WMSF log mislabeling)")

# ==============================================================================
# 2. PATCH lib/shield_crypto_risk.dart
#    - Move crypto break-even lock from 0.65R to 1.0R (+75 bps) so winners aren't
#      stopped out by 23 bps noise before chandelier trailing engages
#    - Increase minStopGapBps from 10 to 45 bps
# ==============================================================================
with open('lib/shield_crypto_risk.dart', 'r', encoding='utf-8') as f:
    crisk = f.read()

crisk = re.sub(
    r"  static const double breakevenAtR = [\d.]+;\n  static const int breakevenLockBps = \d+;.*?  static const double refTargetR = [\d.]+;[^\n]*",
    """  static const double breakevenAtR = 1.00; // Wait for 1.0R before locking break-even
  static const int breakevenLockBps = 75; // 50 bps fees + 25 bps net cushion
  static const double trailStartR = 1.35; // Start chandelier trail at 1.35R
  static const double trailKStart = 1.6;
  static const double trailKMin = 0.90;
  static const double minStepAtrFrac = 0.18;
  static const int minStopGapBps = 45; // Keep stop at least 45 bps below live price
  static const double refTargetR = 2.2; // Positive expectancy R target""",
    crisk,
    flags=re.DOTALL
)

with open('lib/shield_crypto_risk.dart', 'w', encoding='utf-8') as f:
    f.write(crisk)
print("[OK] Patched lib/shield_crypto_risk.dart (1.0R break-even + 45 bps stop cushion)")

# ==============================================================================
# 3. PATCH lib/shield_crypto_guardian.dart
#    - Fix frozen trail bug: remove `status == 'accepted'` from early return!
#    - Add EMA9/EMA21 + Pullback Sniper hard gate to computeCryptoConfluence
# ==============================================================================
with open('lib/shield_crypto_guardian.dart', 'r', encoding='utf-8') as f:
    cguard = f.read()

cguard = cguard.replace(
    "if (status == 'accepted' || status == 'pending_new' || status == 'pending_replace') {",
    "if (status == 'pending_new' || status == 'pending_replace') {"
)

old_crypto_conf = """  final window = done.length > 288 ? done.sublist(done.length - 288) : done;
  final vwap = sessionVwap(window);
  final vols = done.map((b) => b.v).toList();
  final prior = vols.sublist(vols.length - 49, vols.length - 1)..sort();
  final med = (prior[23] + prior[24]) / 2.0;
  final vr = med > 0 ? vols.last / med : null;
  final rsi = wilderRsi(done.map((b) => b.c).toList(), period: ConfluenceRules.rsiPeriod);
  if (vwap == null || vr == null || rsi == null) return null;
  return ConfluenceScore(
    symbol: symbol,
    price: price,
    vwapValue: vwap,
    volumeRatio: vr,
    rsiValue: rsi,
    barsUsed: done.length,
    asOf: done.last.t,
  );"""

new_crypto_conf = """  final window = done.length > 288 ? done.sublist(done.length - 288) : done;
  final vwap = sessionVwap(window);
  final vols = done.map((b) => b.v).toList();
  final closes = done.map((b) => b.c).toList();
  final prior = vols.sublist(vols.length - 49, vols.length - 1)..sort();
  final med = (prior[23] + prior[24]) / 2.0;
  final vr = med > 0 ? vols.last / med : null;
  var rsi = wilderRsi(closes, period: ConfluenceRules.rsiPeriod);
  if (vwap == null || vr == null || rsi == null) return null;

  double ema(List<double> vals, int period) {
    final k = 2.0 / (period + 1);
    var e = vals.first;
    for (var i = 1; i < vals.length; i++) {
      e = vals[i] * k + e * (1.0 - k);
    }
    return e;
  }
  final ema9 = ema(closes, 9);
  final ema21 = ema(closes, 21);
  final last = done.last;
  final range = last.h - last.l;
  final lowerWick = math.min(last.o, last.c) - last.l;
  final bounceCandle = range > 0 && (last.c >= last.o || (lowerWick / range) >= 0.25);
  final notOverextended = ((price - ema9) / price) <= 0.0065; // Not >0.65% extended above EMA9
  final trendAligned = ema9 >= ema21 && price >= ema21 * 0.997 && bounceCandle && notOverextended;

  if (!trendAligned) {
    // Hard gate: force < 2 points so non-pullback spikes NEVER pass triggerTrade
    rsi = 38.0;
  }
  return ConfluenceScore(
    symbol: symbol,
    price: price,
    vwapValue: trendAligned ? vwap : price * 1.01,
    volumeRatio: vr,
    rsiValue: rsi,
    barsUsed: done.length,
    asOf: done.last.t,
  );"""

if old_crypto_conf in cguard:
    cguard = cguard.replace(old_crypto_conf, new_crypto_conf, 1)

with open('lib/shield_crypto_guardian.dart', 'w', encoding='utf-8') as f:
    f.write(cguard)
print("[OK] Patched lib/shield_crypto_guardian.dart (Unfroze accepted stop trail + added Crypto Pullback Gate)")

# ==============================================================================
# 4. PATCH lib/shield_controller.dart
#    - Hard Pullback Gate in computeConfluence (forces <2 points when !trendAligned)
#    - Two-Stage Stock Ratchet (+1.00% break-even shield, +1.80% tight trail)
#    - Persist _stockPeakBps to SharedPreferences ('sh_peaks')
#    - Separate hasActiveStop from hasActiveTp & show fallback SL in protectionSummary
# ==============================================================================
with open('lib/shield_controller.dart', 'r', encoding='utf-8') as f:
    ctrl = f.read()

ctrl = re.sub(
    r"static const String currentVersion = '[^']+';",
    "static const String currentVersion = '5.0.0';",
    ctrl
)

# Upgrade computeConfluence in shield_controller.dart so !trendAligned CANNOT pass the 2/3 gate
ctrl = re.sub(
    r"  final closes = done\.map\(\(c\) => c\.c\)\.toList\(\);.*?  return ConfluenceScore\(\n    symbol: symbol,\n    price: price,\n    vwapValue: vwap,\n    volumeRatio: vr,\n    rsiValue: rsi,\n    barsUsed: done\.length,\n    asOf: last\.t,\n  \);",
    """  final closes = done.map((c) => c.c).toList();
  double ema(List<double> vals, int period) {
    final k = 2.0 / (period + 1);
    var e = vals.first;
    for (var i = 1; i < vals.length; i++) {
      e = vals[i] * k + e * (1.0 - k);
    }
    return e;
  }
  final ema9 = ema(closes, 9);
  final ema21 = ema(closes, 21);
  final range = last.h - last.l;
  final bodyBottom = math.min(last.o, last.c);
  final lowerWick = bodyBottom - last.l;
  final bounceCandle = range > 0 && (last.c >= last.o || (lowerWick / range) >= 0.25);
  final notOverextended = ((price - ema9) / price) <= 0.0060; // Within 0.60% above EMA9, not chasing a spike
  final trendAligned = ema9 >= ema21 && price >= ema21 * 0.997 && bounceCandle && notOverextended;

  final vwap = sessionVwap(session);
  final vr = volumeRatio(done.map((c) => c.v).toList(), avgBars: ConfluenceRules.volAvgBars);
  var rsi = wilderRsi(closes, period: ConfluenceRules.rsiPeriod);
  if (vwap == null || vr == null || rsi == null) return null;

  if (!trendAligned) {
    // Hard gate: force < 2/3 points so breakout spikes with VWAP + Volume cannot sneak past triggerTrade
    rsi = 38.0;
  }
  return ConfluenceScore(
    symbol: symbol,
    price: price,
    vwapValue: trendAligned ? vwap : price * 1.01,
    volumeRatio: vr,
    rsiValue: rsi,
    barsUsed: done.length,
    asOf: last.t,
  );""",
    ctrl,
    flags=re.DOTALL
)

# Load persisted _stockPeakBps in loadLocal()
if "sh_peaks" not in ctrl:
    ctrl = ctrl.replace(
        "      final rawEntries = (jsonDecode(p.getString('sh_entries') ?? '{}') as Map).cast<String, dynamic>();",
        """      final rawPeaks = (jsonDecode(p.getString('sh_peaks') ?? '{}') as Map).cast<String, dynamic>();
      for (final e in rawPeaks.entries) {
        if (e.value is num) _stockPeakBps[e.key] = (e.value as num).toInt();
      }
      final rawEntries = (jsonDecode(p.getString('sh_entries') ?? '{}') as Map).cast<String, dynamic>();"""
    )

# Upgrade _harvestPredictableStockProfits with Two-Stage Positive-Expectancy Ratchet + Separate Stop Check
new_harvest_v50 = """  Future<void> _harvestPredictableStockProfits() async {
    if (!sessionOpen) return;
    final n = nyTime(DateTime.now().toUtc());
    final m = n.hour * 60 + n.minute;
    final isEodWindow = m >= 954 && m < 960; // 15:54 - 15:59 ET

    final openSyms = <String>{};
    var peaksChanged = false;
    final snapshot = List<Position>.of(positions);
    for (final p in snapshot) {
      if (isCryptoSymbol(p.symbol) || !p.isLong || p.avgEntry <= 0) continue;
      final sym = normSym(p.symbol);
      openSyms.add(sym);
      if (_closing.contains(sym)) continue;

      final gainBps = ((p.currentPrice - p.avgEntry) * kBpsDenom) ~/ p.avgEntry;
      final prevPeak = _stockPeakBps[sym] ?? gainBps;
      final peakBps = gainBps > prevPeak ? gainBps : prevPeak;
      if (_stockPeakBps[sym] != peakBps) {
        _stockPeakBps[sym] = peakBps;
        peaksChanged = true;
      }

      var hasActiveStop = false;
      var hasExpiringDayLeg = false;
      for (final o in _rawOrders) {
        final st = '${o['status'] ?? ''}'.toLowerCase();
        if (st == 'canceled' || st == 'expired' || st == 'rejected') continue;
        if (normSym('${o['symbol'] ?? ''}') == sym && '${o['side']}' == 'sell') {
          if (tryPips(o['stop_price']) != null) hasActiveStop = true;
          if ('${o['time_in_force'] ?? ''}'.toLowerCase() == 'day') hasExpiringDayLeg = true;
        }
      }

      if (isEodWindow && hasExpiringDayLeg) {
        _log('🕒 EOD Flatten: closing expiring DAY stock $sym before 16:00 ET close', level: 'warn');
        final res = await closePosition(sym);
        unawaited(_notifyOS('🕒 EOD Flatten: $sym', res.message));
        continue;
      }

      if (executionMode != ExecMode.auto) continue;

      // Stage 1 (Break-Even Shield): If stock reached >= +1.00% (100 bps) and drops back to <= +0.20% (20 bps), exit at break-even so a 1%+ winner never turns into a -1.50% loss!
      final breakEvenShield = peakBps >= 100 && gainBps <= 20;
      // Stage 2 (Runner Trail): Once stock reaches >= +1.80% (180 bps), trail by 35 bps (locks >= +1.45% to +2.40%+)
      final runnerTrail = peakBps >= 180 && (peakBps - gainBps) >= 35;

      if (runnerTrail || breakEvenShield) {
        final label = runnerTrail ? '🔒 Runner Profit Trail' : '🛡️ Break-Even Shield';
        _log('$label triggered on $sym (peak +${(peakBps / 100.0).toStringAsFixed(2)}% → exited at ${gainBps >= 0 ? "+" : ""}${(gainBps / 100.0).toStringAsFixed(2)}%)', level: 'ok');
        final res = await closePosition(sym);
        unawaited(_notifyOS('$label: $sym', res.message));
        continue;
      }

      // Software Stop-Loss & Target Backstop (always enforces -1.50% stop even if Alpaca OCO stop leg is in 'held' state)
      if (gainBps <= -150) {
        _log('🛡️ Stock Guardian stop-loss triggered on $sym (${(gainBps / 100.0).toStringAsFixed(2)}%)', level: 'error');
        final res = await closePosition(sym);
        unawaited(_notifyOS('🛡️ Stock Stop-Loss: $sym', res.message));
      } else if (!hasActiveStop && gainBps >= 240) {
        _log('💰 Stock Guardian banking +${(gainBps / 100.0).toStringAsFixed(2)}% target on $sym', level: 'ok');
        final res = await closePosition(sym);
        unawaited(_notifyOS('💰 Stock Profit Banked: $sym', res.message));
      }
    }
    final beforeLen = _stockPeakBps.length;
    _stockPeakBps.removeWhere((k, _) => !openSyms.contains(k));
    if (peaksChanged || _stockPeakBps.length != beforeLen) {
      _prefs.setString('sh_peaks', jsonEncode(_stockPeakBps));
    }
  }"""

ctrl = re.sub(
    r"  Future<void> _harvestPredictableStockProfits\(\) async \{.*?\n  \}",
    new_harvest_v50,
    ctrl,
    count=1,
    flags=re.DOTALL
)

# Fix protectionSummary so stocks ALWAYS display both TP and SL (even when Alpaca hides the 'held' OCO stop leg)
old_prot_summary = """    if (tp != null || sl != null) {
      return [if (tp != null) 'TP ${priceStr(tp)}', if (sl != null) 'SL ${priceStr(sl)}'].join(' | ');
    }
    return isCryptoSymbol(sym) ? 'Guardian scanning...' : null;"""

new_prot_summary = """    if (!isCryptoSymbol(sym)) {
      final pos = _positionFor(sym);
      if (pos != null && pos.avgEntry > 0) {
        tp ??= roundToTick(pos.avgEntry * (kBpsDenom + RiskRules.intradayTargetBps) ~/ kBpsDenom);
        final peak = _stockPeakBps[sym] ?? 0;
        if (peak >= 180) {
          sl = roundToTick(pos.avgEntry * (kBpsDenom + (peak - 35)) ~/ kBpsDenom);
        } else if (peak >= 100) {
          sl = roundToTick(pos.avgEntry * (kBpsDenom + 20) ~/ kBpsDenom);
        } else {
          sl ??= roundToTick(pos.avgEntry * (kBpsDenom - RiskRules.intradayStopBps) ~/ kBpsDenom);
        }
      }
    }
    if (tp != null || sl != null) {
      return [if (tp != null) 'TP ${priceStr(tp)}', if (sl != null) 'SL ${priceStr(sl)}'].join(' | ');
    }
    return isCryptoSymbol(sym) ? 'Guardian scanning...' : null;"""

if old_prot_summary in ctrl:
    ctrl = ctrl.replace(old_prot_summary, new_prot_summary, 1)

with open('lib/shield_controller.dart', 'w', encoding='utf-8') as f:
    f.write(ctrl)
print("[OK] Patched lib/shield_controller.dart (Hard Pullback Gate + Two-Stage Ratchet + Full TP/SL Display)")

# ==============================================================================
# 5. PATCH lib/main.dart -> SHIELD v5.0
# ==============================================================================
with open('lib/main.dart', 'r', encoding='utf-8') as f:
    main_code = f.read()
main_code = main_code.replace("SHIELD v4.9", "SHIELD v5.0").replace("SHIELD v4.8", "SHIELD v5.0")
with open('lib/main.dart', 'w', encoding='utf-8') as f:
    f.write(main_code)
print("[OK] Patched lib/main.dart (SHIELD v5.0)")
print("\n🎉 SHIELD v5.0 Profit Engine upgrade complete!")
