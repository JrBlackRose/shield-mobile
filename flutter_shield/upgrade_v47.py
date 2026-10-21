def patch_file(path, replacements):
    with open(path, 'r', encoding='utf-8') as f:
        text = f.read()
    for old, new in replacements:
        if old not in text:
            print(f"[WARN] Pattern not found in {path}:\n{old[:80]}...")
        else:
            text = text.replace(old, new, 1)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(text)
    print(f"[OK] Patched {path}")

# 1. Patch lib/shield_core.dart: Adaptive notional cap for micro-accounts (< $2,500)
patch_file('lib/shield_core.dart', [
    (
        "int notionalCapShares({required Pips equity, required Pips entry}) {\n  return (equity * RiskRules.notionalCapBps) ~/ (kBpsDenom * entry);\n}",
        "int notionalCapShares({required Pips equity, required Pips entry}) {\n  final capBps = equity < 2500 * kPipsPerUsd ? 3500 : RiskRules.notionalCapBps;\n  return (equity * capBps) ~/ (kBpsDenom * entry);\n}"
    ),
])

# 2. Patch lib/shield_crypto_risk.dart: Tighter crypto stop ceiling (2.4% max vs 8.0%) for positive R:R payoff
patch_file('lib/shield_crypto_risk.dart', [
    (
        "  static const double kAtr = 3.0; // x ATR(14) on 1h bars\n  static const double kSigma = 2.5; // x sigma scaled to the holding horizon\n  static const double horizonHours = 6.0;\n  static const int stopFloorBps = 150;\n  static const int stopCeilBps = 800;",
        "  static const double kAtr = 2.0; // x ATR(14) on 1h bars\n  static const double kSigma = 1.6; // x sigma scaled to holding horizon\n  static const double horizonHours = 3.0;\n  static const int stopFloorBps = 135;\n  static const int stopCeilBps = 240; // cap crypto initial stop at -2.40% for positive payoff"
    ),
    (
        "  static const double breakevenAtR = 0.65;\n  static const int breakevenLockBps = 65; // > buy fee 25 + sell fee 25 + cushion\n  static const double trailStartR = 1.1;\n  static const double trailKStart = 2.0; // tighter profit trail\n  static const double trailKMin = 1.0;\n  static const double minStepAtrFrac = 0.20;\n  static const int minStopGapBps = 10;\n  static const double refTargetR = 1.8; // predictable profit target",
        "  static const double breakevenAtR = 0.65;\n  static const int breakevenLockBps = 65; // > buy fee 25 + sell fee 25 + cushion\n  static const double trailStartR = 1.0;\n  static const double trailKStart = 1.5; // tighter chandelier trail\n  static const double trailKMin = 0.85;\n  static const double minStepAtrFrac = 0.18;\n  static const int minStopGapBps = 10;\n  static const double refTargetR = 2.2; // positive expectancy R target"
    ),
])

# 3. Patch lib/shield_crypto_guardian.dart: Fix micro-BTC blindness, stop-replace loop, and premature 2.2% cut
patch_file('lib/shield_crypto_guardian.dart', [
    (
        "      final held = <String, Position>{\n        for (final p in c.positions)\n          if (p.isLong && isCryptoSymbol(p.symbol) && p.qtyDouble >= 0.0001 && p.marketValue.abs() >= kPipsPerUsd) normSym(p.symbol): p,\n      };",
        "      final held = <String, Position>{\n        for (final p in c.positions)\n          if (p.isLong && isCryptoSymbol(p.symbol) && p.qtyDouble >= 0.000001 && p.marketValue.abs() >= (kPipsPerUsd * 3 ~/ 2)) normSym(p.symbol): p,\n      };"
    ),
    (
        "    final qtyTxt = floorQtyText(p.qtyDouble);\n    if ((double.tryParse(qtyTxt) ?? 0.0) <= 0.0) return;\n    // Predictable Crypto Profit Harvester: bank profit automatically at +2.2% gain\n    if (p.avgEntry > 0 && last >= (p.avgEntry * 10220 ~/ 10000)) {\n      c._log('💰 GUARDIAN banking predictable crypto profit on $sym (+${((last - p.avgEntry) / p.avgEntry * 100).toStringAsFixed(2)}%)', level: 'ok');\n      final res = await c.closePosition(sym);\n      unawaited(c._notifyOS('💰 Crypto Profit Banked: $sym', res.message));\n      return;\n    }\n    if (stopOrder == null) {",
        "    final qtyTxt = floorQtyText(p.qtyDouble);\n    final targetQty = double.tryParse(qtyTxt) ?? 0.0;\n    if (targetQty <= 0.0) return;\n    // Let trailing stop work; only hard-harvest crypto on a >= +4.2% (>2R) surge\n    if (p.avgEntry > 0 && last >= (p.avgEntry * 10420 ~/ 10000)) {\n      c._log('💰 GUARDIAN banking >2R crypto surge on $sym (+${((last - p.avgEntry) / p.avgEntry * 100).toStringAsFixed(2)}%)', level: 'ok');\n      final res = await c.closePosition(sym);\n      unawaited(c._notifyOS('💰 Crypto Surge Banked: $sym', res.message));\n      return;\n    }\n    if (stopOrder == null) {"
    ),
    (
        "    final oq = double.tryParse('${stopOrder['qty']}') ?? 0.0;\n    if (oq > 0 && (p.qtyDouble - oq).abs() / p.qtyDouble > 0.002) {",
        "    final oq = double.tryParse('${stopOrder['qty']}') ?? 0.0;\n    if (oq > 0 && (targetQty - oq).abs() / targetQty > 0.01) {"
    ),
])

# 4. Patch lib/shield_controller.dart: v4.7.0, $1.50 dust floor, EOD flatten + Stock Guardian, $10 penny gate, 9:30 blackout, PDT guard
patch_file('lib/shield_controller.dart', [
    (
        "  static const String currentVersion = '4.6.0';",
        "  static const String currentVersion = '4.7.0';"
    ),
    (
        "      // Filter out < $1.00 fractional crypto dust so 0-sh positions never clutter UI or trigger 422\n      positions = (await fp).map(Position.fromJson).where((p) => p.marketValue.abs() >= kPipsPerUsd && p.qtyDouble >= 0.0001).toList();",
        "      // Filter out < $1.50 dust using notional value so micro-BTC positions (0.00009975 BTC) stay protected\n      positions = (await fp).map(Position.fromJson).where((p) => p.marketValue.abs() >= (kPipsPerUsd * 3 ~/ 2) && p.qtyDouble >= 0.000001).toList();"
    ),
    (
        "  Future<void> _harvestPredictableStockProfits() async {\n    if (executionMode != ExecMode.auto || !sessionOpen) return;\n    for (final p in positions) {\n      if (isCryptoSymbol(p.symbol) || !p.isLong || p.avgEntry <= 0) continue;\n      final sym = normSym(p.symbol);\n      if (_closing.contains(sym)) continue;\n      final gainBps = ((p.currentPrice - p.avgEntry) * kBpsDenom) ~/ p.avgEntry;\n      // Predictable profit lock: if a stock has no TP order or hits +1.85% intraday gain, harvest cleanly\n      if (gainBps >= 185) {\n        _log('💰 Predictable Profit Lock triggered on $sym (+${(gainBps / 100.0).toStringAsFixed(2)}%) — banking win', level: 'ok');\n        final res = await closePosition(sym);\n        unawaited(_notifyOS('💰 Profit Banked: $sym', res.message));\n      }\n    }\n  }",
        """  int get _activeSlotCount => {...heldSymbols, ...queuedSymbols}.length;

  int _recentStockDayTrades() {
    final cutoff = DateTime.now().toUtc().subtract(const Duration(days: 5));
    var count = 0;
    for (final t in closedTrades) {
      if (!isCryptoSymbol(t.symbol) && t.closedAt.isAfter(cutoff)) count++;
    }
    return count;
  }

  Future<void> _harvestPredictableStockProfits() async {
    if (!sessionOpen) return;
    final n = nyTime(DateTime.now().toUtc());
    final m = n.hour * 60 + n.minute;
    final isEodWindow = m >= 954 && m < 960; // 15:54 - 15:59 ET: flatten expiring DAY brackets before 16:00 close

    for (final p in positions) {
      if (isCryptoSymbol(p.symbol) || !p.isLong || p.avgEntry <= 0) continue;
      final sym = normSym(p.symbol);
      if (_closing.contains(sym)) continue;

      var hasActiveSell = false;
      var hasExpiringDayLeg = false;
      for (final o in _rawOrders) {
        final st = '${o['status'] ?? ''}'.toLowerCase();
        if (st == 'canceled' || st == 'expired' || st == 'rejected') continue;
        if (normSym('${o['symbol'] ?? ''}') == sym && '${o['side']}' == 'sell') {
          hasActiveSell = true;
          if ('${o['time_in_force'] ?? ''}'.toLowerCase() == 'day') hasExpiringDayLeg = true;
        }
      }

      if (isEodWindow && (hasExpiringDayLeg || !hasActiveSell)) {
        _log('🕒 EOD Flatten: closing intraday stock $sym before 16:00 ET leg expiration', level: 'warn');
        final res = await closePosition(sym);
        unawaited(_notifyOS('🕒 EOD Flatten: $sym', res.message));
        continue;
      }

      // Stock Guardian Backstop: only step in if a stock has NO active bracket sell orders on Alpaca
      if (!hasActiveSell && executionMode == ExecMode.auto) {
        final gainBps = ((p.currentPrice - p.avgEntry) * kBpsDenom) ~/ p.avgEntry;
        if (gainBps <= -150) {
          _log('🛡️ Stock Guardian stop-loss triggered on unprotected $sym (${(gainBps / 100.0).toStringAsFixed(2)}%)', level: 'error');
          final res = await closePosition(sym);
          unawaited(_notifyOS('🛡️ Unprotected Stock Stop: $sym', res.message));
        } else if (gainBps >= 240) {
          _log('💰 Stock Guardian banking +${(gainBps / 100.0).toStringAsFixed(2)}% on unprotected $sym', level: 'ok');
          final res = await closePosition(sym);
          unawaited(_notifyOS('💰 Stock Profit Banked: $sym', res.message));
        }
      }
    }
  }"""
    ),
    (
        "    if (positions.length >= AutoRules.maxAutoPositions) return; // Cap concurrent positions to prevent over-diversified bleed",
        "    if (_activeSlotCount >= AutoRules.maxAutoPositions) return; // Cap held + queued positions at 4"
    ),
    (
        "    if (positions.length >= AutoRules.maxAutoPositions) return;",
        "    if (_activeSlotCount >= AutoRules.maxAutoPositions) return;"
    ),
    (
        "      final qs = await _liveQuote(sym);\n      if (!qs.fromSnapshot && !offHours) {",
        """      final qs = await _liveQuote(sym);
      if (!crypto && qs.ask < 10 * kPipsPerUsd) {
        throw RiskReject('Penny / sub-\\$10 stock filter: $sym is ${priceStr(qs.ask)} (min \\$10.00 required).');
      }
      if (!crypto && !offHours && sessionOpen) {
        final ny = nyTime(DateTime.now().toUtc());
        final mins = ny.hour * 60 + ny.minute;
        if (mins >= 570 && mins < 576) {
          throw const RiskReject('US Open 6-min spread blackout (09:30–09:36 ET). Waiting for spreads to stabilize.');
        }
      }
      if (!crypto && timeframe == Timeframe.intraday && acct.equity < 25000 * kPipsPerUsd && _recentStockDayTrades() >= 3) {
        throw const RiskReject('PDT Shield: 3/3 rolling stock day-trades used on sub-\\$25k account.');
      }
      if (!qs.fromSnapshot && !offHours) {"""
    ),
])

# 5. Patch lib/main.dart: Update header label to SHIELD v4.7
patch_file('lib/main.dart', [
    (
        "SHIELD v4.6",
        "SHIELD v4.7"
    ),
])

print("\n🎉 SHIELD v4.7.0 upgrade complete!")
