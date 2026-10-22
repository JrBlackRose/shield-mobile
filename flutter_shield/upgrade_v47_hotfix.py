import re

with open('lib/shield_core.dart', 'r', encoding='utf-8') as f:
    core = f.read()

# 1. Purge deprecated gemini-2.0-flash / gemini-1.5-flash from shield_core.dart
core = re.sub(
    r"const String kGeminiModel\s*=\s*'[^']+';",
    "const String kGeminiModel = 'gemini-2.5-flash';",
    core
)
core = re.sub(
    r"const List<String> kGeminiFallbackModels\s*=\s*\[[^\]]+\];",
    "const List<String> kGeminiFallbackModels = ['gemini-2.5-flash', 'gemini-3.8-flash', 'gemini-2.5-flash-lite', 'gemini-2.5-pro'];",
    core
)
# Also catch any inline gemini-2.0-flash strings anywhere in shield_core.dart
core = core.replace("'gemini-2.0-flash-exp'", "'gemini-2.5-flash'")
core = core.replace("'gemini-2.0-flash'", "'gemini-2.5-flash'")
core = core.replace("'gemini-1.5-flash'", "'gemini-2.5-flash-lite'")

# 2. Lower max single-stock notional cap from 15% (1500 bps) to 10% (1000 bps) for normal accounts, 35% for micro accounts (<$2,500)
core = core.replace(
    "  static const int notionalCapBps = 1500;",
    "  static const int notionalCapBps = 1000;"
)
if "equity < 2500 * kPipsPerUsd" not in core:
    core = core.replace(
        "int notionalCapShares({required Pips equity, required Pips entry}) {\n  return (equity * RiskRules.notionalCapBps) ~/ (kBpsDenom * entry);\n}",
        "int notionalCapShares({required Pips equity, required Pips entry}) {\n  final capBps = equity < 2500 * kPipsPerUsd ? 3500 : RiskRules.notionalCapBps;\n  return (equity * capBps) ~/ (kBpsDenom * entry);\n}"
    )

with open('lib/shield_core.dart', 'w', encoding='utf-8') as f:
    f.write(core)
print("[OK] Patched lib/shield_core.dart (Purged gemini-2.0-flash 404 model & capped single-position size at 10%)")

# 3. Ensure v4.7 crypto risk rules are applied
with open('lib/shield_crypto_risk.dart', 'r', encoding='utf-8') as f:
    crisk = f.read()

crisk = crisk.replace(
    "  static const double kAtr = 3.0; // x ATR(14) on 1h bars\n  static const double kSigma = 2.5; // x sigma scaled to the holding horizon\n  static const double horizonHours = 6.0;\n  static const int stopFloorBps = 150;\n  static const int stopCeilBps = 800;",
    "  static const double kAtr = 2.0; // x ATR(14) on 1h bars\n  static const double kSigma = 1.6; // x sigma scaled to holding horizon\n  static const double horizonHours = 3.0;\n  static const int stopFloorBps = 135;\n  static const int stopCeilBps = 240; // cap crypto initial stop at -2.40% for positive payoff"
).replace(
    "  static const double trailStartR = 1.1;\n  static const double trailKStart = 2.0; // tighter profit trail\n  static const double trailKMin = 1.0;\n  static const double minStepAtrFrac = 0.20;\n  static const int minStopGapBps = 10;\n  static const double refTargetR = 1.8; // predictable profit target",
    "  static const double trailStartR = 1.0;\n  static const double trailKStart = 1.5; // tighter chandelier trail\n  static const double trailKMin = 0.85;\n  static const double minStepAtrFrac = 0.18;\n  static const int minStopGapBps = 10;\n  static const double refTargetR = 2.2; // positive expectancy R target"
)

with open('lib/shield_crypto_risk.dart', 'w', encoding='utf-8') as f:
    f.write(crisk)
print("[OK] Patched lib/shield_crypto_risk.dart")

# 4. Ensure v4.7 crypto guardian fixes are applied
with open('lib/shield_crypto_guardian.dart', 'r', encoding='utf-8') as f:
    cguard = f.read()

cguard = cguard.replace(
    "if (p.isLong && isCryptoSymbol(p.symbol) && p.qtyDouble >= 0.0001 && p.marketValue.abs() >= kPipsPerUsd)",
    "if (p.isLong && isCryptoSymbol(p.symbol) && p.qtyDouble >= 0.000001 && p.marketValue.abs() >= (kPipsPerUsd * 3 ~/ 2))"
).replace(
    "    final qtyTxt = floorQtyText(p.qtyDouble);\n    if ((double.tryParse(qtyTxt) ?? 0.0) <= 0.0) return;\n    // Predictable Crypto Profit Harvester: bank profit automatically at +2.2% gain\n    if (p.avgEntry > 0 && last >= (p.avgEntry * 10220 ~/ 10000)) {\n      c._log('💰 GUARDIAN banking predictable crypto profit on $sym (+${((last - p.avgEntry) / p.avgEntry * 100).toStringAsFixed(2)}%)', level: 'ok');\n      final res = await c.closePosition(sym);\n      unawaited(c._notifyOS('💰 Crypto Profit Banked: $sym', res.message));\n      return;\n    }\n    if (stopOrder == null) {",
    "    final qtyTxt = floorQtyText(p.qtyDouble);\n    final targetQty = double.tryParse(qtyTxt) ?? 0.0;\n    if (targetQty <= 0.0) return;\n    if (p.avgEntry > 0 && last >= (p.avgEntry * 10420 ~/ 10000)) {\n      c._log('💰 GUARDIAN banking >2R crypto surge on $sym (+${((last - p.avgEntry) / p.avgEntry * 100).toStringAsFixed(2)}%)', level: 'ok');\n      final res = await c.closePosition(sym);\n      unawaited(c._notifyOS('💰 Crypto Surge Banked: $sym', res.message));\n      return;\n    }\n    if (stopOrder == null) {"
).replace(
    "    final oq = double.tryParse('${stopOrder['qty']}') ?? 0.0;\n    if (oq > 0 && (p.qtyDouble - oq).abs() / p.qtyDouble > 0.002) {",
    "    final oq = double.tryParse('${stopOrder['qty']}') ?? 0.0;\n    if (oq > 0 && (targetQty - oq).abs() / targetQty > 0.01) {"
)

with open('lib/shield_crypto_guardian.dart', 'w', encoding='utf-8') as f:
    f.write(cguard)
print("[OK] Patched lib/shield_crypto_guardian.dart")

# 5. Patch lib/shield_controller.dart (v4.7.1 + Network Self-Healing + Cached History + Stock Guardian in Pre/Extended Hours)
with open('lib/shield_controller.dart', 'r', encoding='utf-8') as f:
    ctrl = f.read()

ctrl = ctrl.replace("currentVersion = '4.6.0';", "currentVersion = '4.7.1';").replace("currentVersion = '4.7.0';", "currentVersion = '4.7.1';")
ctrl = ctrl.replace("'gemini-2.0-flash'", "'gemini-2.5-flash'")

# Self-healing HTTP client
if "_resetHttpClient" not in ctrl:
    ctrl = ctrl.replace(
        "  final http.Client _http = http.Client();",
        """  http.Client _http = http.Client();

  void _resetHttpClient() {
    try {
      _http.close();
    } catch (_) {}
    _http = http.Client();
    if (vault.hasRequired) {
      client = AlpacaClient(_http, vault.alpacaKey, vault.alpacaSecret);
    }
  }"""
    )

# Persistent ClosedTrade JSON serialization & loadLocal restore
if "ClosedTrade.fromJson" not in ctrl:
    ctrl = ctrl.replace(
        "  bool get isWin => pnlPips >= 0;\n}",
        """  bool get isWin => pnlPips >= 0;

  Map<String, dynamic> toJson() => {
        's': symbol,
        'q': qtyText,
        'b': buyPrice,
        'sl': sellPrice,
        'p': pnlPips,
        'pct': pnlPct,
        't': closedAt.toIso8601String(),
      };

  factory ClosedTrade.fromJson(Map<String, dynamic> j) => ClosedTrade(
        symbol: '${j['s'] ?? ''}',
        qtyText: '${j['q'] ?? '0'}',
        buyPrice: (j['b'] as num?)?.toInt() ?? 0,
        sellPrice: (j['sl'] as num?)?.toInt() ?? 0,
        pnlPips: (j['p'] as num?)?.toInt() ?? 0,
        pnlPct: (j['pct'] as num?)?.toDouble() ?? 0.0,
        closedAt: DateTime.tryParse('${j['t']}')?.toUtc() ?? DateTime.now().toUtc(),
      );
}"""
    )
    ctrl = ctrl.replace(
        "    try {\n      feed = (jsonDecode(p.getString('sh_feed') ?? '[]') as List)",
        """    try {
      closedTrades = (jsonDecode(p.getString('sh_closed') ?? '[]') as List)
          .map((e) => ClosedTrade.fromJson((e as Map).cast<String, dynamic>()))
          .toList();
    } catch (_) {
      closedTrades = [];
    }
    try {
      feed = (jsonDecode(p.getString('sh_feed') ?? '[]') as List)"""
    )

# Fault-tolerant _refreshClosedHistory
ctrl = re.sub(
    r"  Future<void> _refreshClosedHistory\(\) async \{.*?\n  \}",
    """  Future<void> _refreshClosedHistory() async {
    final cl = client;
    if (cl == null || vaultStatus != VaultStatus.valid) return;
    try {
      final raw = await _extras().closedOrders(limit: 60);
      final built = _buildClosedTrades(raw, positions);
      if (built.isNotEmpty || closedTrades.isEmpty) {
        closedTrades = built;
        _prefs.setString('sh_closed', jsonEncode(closedTrades.take(40).map((t) => t.toJson()).toList()));
        notifyListeners();
      }
    } catch (_) {}
  }""",
    ctrl,
    flags=re.DOTALL
)

# Stagger startup scanNews
ctrl = ctrl.replace(
    "unawaited(refresh().then((_) => scanNews(force: true)));",
    "unawaited(refresh().then((_) => Future<void>.delayed(const Duration(seconds: 2), () => scanNews(force: true))));"
)

# Fault-tolerant refresh() with auto socket-reset retry
old_refresh_wait = """      final fa = cl.getAccount(), fp = cl.getPositions(), fo = cl.getOpenOrders(), fc = cl.getClock();
      await Future.wait<Object?>([fa, fp, fo, fc]);
      account = Account.fromJson(await fa);"""

new_refresh_wait = """      Future<T> retryCall<T>(Future<T> Function(AlpacaClient c) fn) async {
        try {
          return await fn(client!);
        } catch (e) {
          if (_isNetwork(e)) {
            _resetHttpClient();
            await Future<void>.delayed(const Duration(milliseconds: 400));
            return await fn(client!);
          }
          rethrow;
        }
      }
      final fa = retryCall((c) => c.getAccount());
      final fp = retryCall((c) => c.getPositions());
      final fo = retryCall((c) => c.getOpenOrders());
      final fc = retryCall((c) => c.getClock());
      await Future.wait<Object?>([fa, fp, fo, fc]);
      account = Account.fromJson(await fa);"""

if old_refresh_wait in ctrl:
    ctrl = ctrl.replace(old_refresh_wait, new_refresh_wait, 1)

# $1.50 notional dust filter
ctrl = ctrl.replace(
    "where((p) => p.marketValue.abs() >= kPipsPerUsd && p.qtyDouble >= 0.0001).toList();",
    "where((p) => p.marketValue.abs() >= (kPipsPerUsd * 3 ~/ 2) && p.qtyDouble >= 0.000001).toList();"
)

# Upgrade _harvestPredictableStockProfits with EOD flatten + Stock Guardian
if "_activeSlotCount" not in ctrl:
    old_harvest_block = """  Future<void> _harvestPredictableStockProfits() async {
    if (executionMode != ExecMode.auto || !sessionOpen) return;
    for (final p in positions) {
      if (isCryptoSymbol(p.symbol) || !p.isLong || p.avgEntry <= 0) continue;
      final sym = normSym(p.symbol);
      if (_closing.contains(sym)) continue;
      final gainBps = ((p.currentPrice - p.avgEntry) * kBpsDenom) ~/ p.avgEntry;
      // Predictable profit lock: if a stock has no TP order or hits +1.85% intraday gain, harvest cleanly
      if (gainBps >= 185) {
        _log('💰 Predictable Profit Lock triggered on $sym (+${(gainBps / 100.0).toStringAsFixed(2)}%) — banking win', level: 'ok');
        final res = await closePosition(sym);
        unawaited(_notifyOS('💰 Profit Banked: $sym', res.message));
      }
    }
  }"""
    new_harvest_block = """  int get _activeSlotCount => {...heldSymbols, ...queuedSymbols}.length;

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
    ctrl = ctrl.replace(old_harvest_block, new_harvest_block, 1)

ctrl = ctrl.replace(
    "if (positions.length >= AutoRules.maxAutoPositions) return; // Cap concurrent positions to prevent over-diversified bleed",
    "if (_activeSlotCount >= AutoRules.maxAutoPositions) return; // Cap held + queued positions at 4"
).replace(
    "if (positions.length >= AutoRules.maxAutoPositions) return;",
    "if (_activeSlotCount >= AutoRules.maxAutoPositions) return;"
)

if "Penny / sub-" not in ctrl:
    ctrl = ctrl.replace(
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
    )

# Network retry in _AlpacaExtras._get
old_extras_get = "  Future<dynamic> _get(Uri u) async => _decode(await _http.get(u, headers: _h).timeout(_kV4Timeout));"
new_extras_get = """  Future<dynamic> _get(Uri u) async {
    for (var attempt = 0; attempt < 2; attempt++) {
      try {
        return _decode(await _http.get(u, headers: _h).timeout(_kV4Timeout));
      } catch (e) {
        final isNet = e is TimeoutException || e is SocketException || e is http.ClientException;
        if (isNet && attempt == 0) {
          await Future<void>.delayed(const Duration(milliseconds: 450));
          continue;
        }
        rethrow;
      }
    }
  }"""
if old_extras_get in ctrl:
    ctrl = ctrl.replace(old_extras_get, new_extras_get, 1)

with open('lib/shield_controller.dart', 'w', encoding='utf-8') as f:
    f.write(ctrl)
print("[OK] Patched lib/shield_controller.dart")

with open('lib/main.dart', 'r', encoding='utf-8') as f:
    main_code = f.read()
main_code = main_code.replace("SHIELD v4.6", "SHIELD v4.7").replace("15% CAP", "10% CAP")
with open('lib/main.dart', 'w', encoding='utf-8') as f:
    f.write(main_code)
print("[OK] Patched lib/main.dart (SHIELD v4.7)")
print("\n🎉 SHIELD v4.7.1 Hotfix complete!")
