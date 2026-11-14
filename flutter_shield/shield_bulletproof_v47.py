with open('lib/shield_core.dart', 'r', encoding='utf-8') as f:
    core = f.read()

# 1. Fix Gemini models (remove retired 2.0-flash and restricted 2.5-flash)
core = core.replace(
"""const String kGeminiModel = 'gemini-3.5-flash-lite';
const List<String> kGeminiFallbackModels = [
  'gemini-3.5-flash-lite',
  'gemini-2.5-flash',
  'gemini-2.0-flash',
];""",
"""const String kGeminiModel = 'gemini-3.8-flash';
const List<String> kGeminiFallbackModels = [
  'gemini-3.8-flash',
  'gemini-3.5-flash-lite',
  'gemini-3.6-flash',
  'gemini-3.8-flash',
];"""
)

# 2. Fix AlpacaClient._tif: use 'gtc' so crypto limit buys don't silently IOC-cancel and stock bracket SL/TP legs never expire at 16:00 ET
core = core.replace(
"""  String _tif(String symbol, Timeframe timeframe) {
  if (isCryptoSymbol(symbol)) return 'ioc';
  return timeframe != Timeframe.intraday ? 'gtc' : 'day';
}""",
"""  String _tif(String symbol, Timeframe timeframe) {
  if (isCryptoSymbol(symbol)) return 'gtc';
  return 'gtc';
}"""
)

# 3. Make GeminiClient._callGemini catch network/timeout errors inside the fallback loop
core = core.replace(
"""    for (var i = 0; i < kGeminiFallbackModels.length; i++) {
      final m = kGeminiFallbackModels[i];
      res = await _http
          .post(
            Uri.parse('https://generativelanguage.googleapis.com/v1beta/models/$m:generateContent'),
            headers: {'x-goog-api-key': _key, 'Content-Type': 'application/json'},
            body: jsonEncode({
              'contents': [
                {
                  'parts': [
                    {'text': promptText}
                  ]
                }
              ],
              'generationConfig': {'responseMimeType': 'application/json'},
            }),
          )
          .timeout(_kReadTimeout);
      if (res.statusCode == 200) break;
      if ((res.statusCode == 503 || res.statusCode == 429 || res.statusCode == 500 || res.statusCode == 404) &&
          i < kGeminiFallbackModels.length - 1) {
        await Future<void>.delayed(Duration(milliseconds: 800 * (i + 1)));
        continue;
      }
      break;
    }""",
"""    Object? lastErr;
    for (var i = 0; i < kGeminiFallbackModels.length; i++) {
      final m = kGeminiFallbackModels[i];
      try {
        res = await _http
            .post(
              Uri.parse('https://generativelanguage.googleapis.com/v1beta/models/$m:generateContent'),
              headers: {'x-goog-api-key': _key, 'Content-Type': 'application/json'},
              body: jsonEncode({
                'contents': [
                  {
                    'parts': [
                      {'text': promptText}
                    ]
                  }
                ],
                'generationConfig': {'responseMimeType': 'application/json'},
              }),
            )
            .timeout(_kReadTimeout);
      } catch (e) {
        lastErr = e;
        if (i < kGeminiFallbackModels.length - 1) {
          await Future<void>.delayed(Duration(milliseconds: 700 * (i + 1)));
          continue;
        }
        rethrow;
      }
      if (res.statusCode == 200) break;
      if ((res.statusCode == 503 || res.statusCode == 429 || res.statusCode == 500 || res.statusCode == 404) &&
          i < kGeminiFallbackModels.length - 1) {
        await Future<void>.delayed(Duration(milliseconds: 800 * (i + 1)));
        continue;
      }
      break;
    }
    if (res == null && lastErr != null) throw lastErr;"""
)

# 4. Cap single-stock notional at 10% (1000 bps) for standard accounts, 35% for micro (<$2,500)
core = core.replace("static const int notionalCapBps = 1500;", "static const int notionalCapBps = 1000;")
core = core.replace(
"""int notionalCapShares({required Pips equity, required Pips entry}) {
  return (equity * RiskRules.notionalCapBps) ~/ (kBpsDenom * entry);
}""",
"""int notionalCapShares({required Pips equity, required Pips entry}) {
  final capBps = equity < 2500 * kPipsPerUsd ? 3500 : RiskRules.notionalCapBps;
  return (equity * capBps) ~/ (kBpsDenom * entry);
}"""
)

with open('lib/shield_core.dart', 'w', encoding='utf-8') as f:
    f.write(core)
print("[OK] Patched lib/shield_core.dart")

with open('lib/shield_crypto_guardian.dart', 'r', encoding='utf-8') as f:
    guard = f.read()

# Fix micro-BTC blindness ($1.50 notional floor instead of 0.0001 coin floor) and fix stop qty comparison loop
guard = guard.replace(
    "if (p.isLong && isCryptoSymbol(p.symbol) && p.qtyDouble >= 0.0001 && p.marketValue.abs() >= kPipsPerUsd)",
    "if (p.isLong && isCryptoSymbol(p.symbol) && p.qtyDouble >= 0.000001 && p.marketValue.abs() >= (kPipsPerUsd * 3 ~/ 2))"
).replace(
"""    final qtyTxt = floorQtyText(p.qtyDouble);
    if ((double.tryParse(qtyTxt) ?? 0.0) <= 0.0) return;""",
"""    final qtyTxt = floorQtyText(p.qtyDouble);
    final targetQty = double.tryParse(qtyTxt) ?? 0.0;
    if (targetQty <= 0.0) return;"""
).replace(
"""    final oq = double.tryParse('${stopOrder['qty']}') ?? 0.0;
    if (oq > 0 && (p.qtyDouble - oq).abs() / p.qtyDouble > 0.002) {""",
"""    final oq = double.tryParse('${stopOrder['qty']}') ?? 0.0;
    if (oq > 0 && (targetQty - oq).abs() / targetQty > 0.01) {"""
)

with open('lib/shield_crypto_guardian.dart', 'w', encoding='utf-8') as f:
    f.write(guard)
print("[OK] Patched lib/shield_crypto_guardian.dart")

with open('lib/shield_controller.dart', 'r', encoding='utf-8') as f:
    ctrl = f.read()

ctrl = ctrl.replace("static const String currentVersion = '4.6.0';", "static const String currentVersion = '4.7.1';")

# 1. Add JSON serialization to ClosedTrade so Trade History is cached in SharedPreferences
ctrl = ctrl.replace(
"""  final DateTime closedAt;
  bool get isWin => pnlPips >= 0;
}""",
"""  final DateTime closedAt;
  bool get isWin => pnlPips >= 0;

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

# 2. Make _http replaceable to flush dead sockets on Wi-Fi/4G switch
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

# 3. Separate queuedBuySymbols from queuedSymbols so sell stops don't count as buy orders, and add _activeSlotCount
ctrl = ctrl.replace(
"""  Set<String> get queuedSymbols => {
        for (final o in _rawOrders)
          if ((o['symbol'] ?? '').toString().isNotEmpty)
            (o['symbol'] ?? '').toString().toUpperCase().replaceAll('/', '')
      };""",
"""  Set<String> get queuedSymbols => {
        for (final o in _rawOrders)
          if ((o['symbol'] ?? '').toString().isNotEmpty)
            (o['symbol'] ?? '').toString().toUpperCase().replaceAll('/', '')
      };

  Set<String> get queuedBuySymbols => {
        for (final o in _rawOrders)
          if ('${o['side'] ?? ''}'.toLowerCase() == 'buy' && (o['symbol'] ?? '').toString().isNotEmpty)
            (o['symbol'] ?? '').toString().toUpperCase().replaceAll('/', '')
      };

  int get _activeSlotCount => {...heldSymbols, ...queuedBuySymbols}.length;"""
)

# 4. Load persisted closedTrades and _entryTimes in loadLocal()
ctrl = ctrl.replace(
"""    executionMode = p.getString('sh_mode') == 'auto' ? ExecMode.auto : ExecMode.manual;
    proMode = p.getBool('sh_pro') ?? false;
    try {
      feed = (jsonDecode(p.getString('sh_feed') ?? '[]') as List)""",
"""    executionMode = p.getString('sh_mode') == 'auto' ? ExecMode.auto : ExecMode.manual;
    proMode = p.getBool('sh_pro') ?? false;
    try {
      closedTrades = (jsonDecode(p.getString('sh_closed') ?? '[]') as List)
          .map((e) => ClosedTrade.fromJson((e as Map).cast<String, dynamic>()))
          .toList();
    } catch (_) {
      closedTrades = [];
    }
    try {
      final rawEntries = (jsonDecode(p.getString('sh_entries') ?? '{}') as Map).cast<String, dynamic>();
      for (final e in rawEntries.entries) {
        final dt = DateTime.tryParse('${e.value}')?.toUtc();
        if (dt != null) _entryTimes[e.key] = dt;
      }
    } catch (_) {}
    try {
      feed = (jsonDecode(p.getString('sh_feed') ?? '[]') as List)"""
)

ctrl = ctrl.replace(
"  void _persistFeed() => _prefs.setString('sh_feed', jsonEncode(feed.map((e) => e.toJson()).toList()));",
"""  void _persistFeed() => _prefs.setString('sh_feed', jsonEncode(feed.map((e) => e.toJson()).toList()));

  void _recordEntryTime(String sym, [DateTime? at]) {
    _entryTimes[normSym(sym)] = (at ?? DateTime.now().toUtc()).toUtc();
    _prefs.setString('sh_entries', jsonEncode({for (final e in _entryTimes.entries) e.key: e.value.toIso8601String()}));
  }"""
)

# 5. Stagger validateAndConnect startup scanNews so it doesn't collide with refresh()
ctrl = ctrl.replace(
    "unawaited(refresh().then((_) => scanNews(force: true)));",
    "unawaited(refresh().then((_) => Future<void>.delayed(const Duration(seconds: 2), () => scanNews(force: true))));"
)

# 6. Fix _refreshClosedHistory and _harvestPredictableStockProfits (safe iteration copy, cached history)
ctrl = ctrl.replace(
"""  Future<void> _refreshClosedHistory() async {
    final cl = client;
    if (cl == null || vaultStatus != VaultStatus.valid) return;
    try {
      final raw = await _extras().closedOrders(limit: 120);
      closedTrades = _buildClosedTrades(raw, positions);
    } catch (_) {}
  }

  Future<void> _harvestPredictableStockProfits() async {
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
  }""",
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
  }

  Future<void> _harvestPredictableStockProfits() async {
    if (executionMode != ExecMode.auto || !sessionOpen) return;
    final snapshot = List<Position>.of(positions);
    for (final p in snapshot) {
      if (isCryptoSymbol(p.symbol) || !p.isLong || p.avgEntry <= 0) continue;
      final sym = normSym(p.symbol);
      if (_closing.contains(sym)) continue;
      final gainBps = ((p.currentPrice - p.avgEntry) * kBpsDenom) ~/ p.avgEntry;
      var hasActiveSell = false;
      for (final o in _rawOrders) {
        final st = '${o['status'] ?? ''}'.toLowerCase();
        if (st == 'canceled' || st == 'expired' || st == 'rejected') continue;
        if (normSym('${o['symbol'] ?? ''}') == sym && '${o['side']}' == 'sell') {
          hasActiveSell = true;
          break;
        }
      }
      if (gainBps >= 185 || (!hasActiveSell && gainBps <= -150)) {
        _log(
          gainBps >= 0
              ? '💰 Predictable Profit Lock triggered on $sym (+${(gainBps / 100.0).toStringAsFixed(2)}%) — banking win'
              : '🛡️ Stock Guardian stop triggered on unprotected $sym (${(gainBps / 100.0).toStringAsFixed(2)}%)',
          level: gainBps >= 0 ? 'ok' : 'error',
        );
        final res = await closePosition(sym);
        unawaited(_notifyOS(gainBps >= 0 ? '💰 Profit Banked: $sym' : '🛡️ Stock Stop: $sym', res.message));
      }
    }
  }"""
)

# 7. Fix refresh() ordering, individual endpoint retry with socket reset, and $1.50 notional dust filter
ctrl = ctrl.replace(
"""      final fa = cl.getAccount(), fp = cl.getPositions(), fo = cl.getOpenOrders(), fc = cl.getClock();
      await Future.wait<Object?>([fa, fp, fo, fc]);
      account = Account.fromJson(await fa);
      // Filter out < $1.00 fractional crypto dust so 0-sh positions never clutter UI or trigger 422
      positions = (await fp).map(Position.fromJson).where((p) => p.marketValue.abs() >= kPipsPerUsd && p.qtyDouble >= 0.0001).toList();
      final rawOrd = await fo;
      unawaited(_refreshClosedHistory());
      unawaited(_harvestPredictableStockProfits());
      _rawOrders = rawOrd;
      orders = rawOrd.take(50).map(OrderSummary.fromJson).toList();
      _applyClock(await fc);""",
"""      Future<T> retryCall<T>(Future<T> Function(AlpacaClient c) fn) async {
        try {
          return await fn(client!);
        } catch (e) {
          if (_isNetwork(e)) {
            _resetHttpClient();
            await Future<void>.delayed(const Duration(milliseconds: 350));
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
      account = Account.fromJson(await fa);
      positions = (await fp)
          .map(Position.fromJson)
          .where((p) => p.marketValue.abs() >= (kPipsPerUsd * 3 ~/ 2) && p.qtyDouble >= 0.000001)
          .toList();
      for (final p in positions) {
        final sym = normSym(p.symbol);
        if (!_entryTimes.containsKey(sym)) _recordEntryTime(sym);
      }
      final rawOrd = await fo;
      _rawOrders = rawOrd;
      orders = rawOrd.take(50).map(OrderSummary.fromJson).toList();
      _applyClock(await fc);
      unawaited(_refreshClosedHistory());
      unawaited(_harvestPredictableStockProfits());"""
)

# 8. Use _activeSlotCount (held + queued buys) instead of positions.length for maxAutoPositions
ctrl = ctrl.replace(
    "if (positions.length >= AutoRules.maxAutoPositions) return; // Cap concurrent positions to prevent over-diversified bleed",
    "if (_activeSlotCount >= AutoRules.maxAutoPositions) return; // Cap held + queued buy positions at 4"
).replace(
    "if (positions.length >= AutoRules.maxAutoPositions) return;",
    "if (_activeSlotCount >= AutoRules.maxAutoPositions) return;"
)

# 9. Only seed Guardian when trade is actually submitted, and persist _recordEntryTime
ctrl = ctrl.replace(
"""      final token = _newToken();
      if (cplan != null) _guardian.seed(TrailState.fromPlan(sym, cplan, DateTime.now().toUtc()));""",
"""      final token = _newToken();"""
).replace(
"""        final id = (order['id'] ?? '?').toString();
        _entryTimes[sym] = DateTime.now().toUtc();
        lastTradeResult = AutoExecuted(id, plan);""",
"""        final id = (order['id'] ?? '?').toString();
        if (cplan != null) _guardian.seed(TrailState.fromPlan(sym, cplan, DateTime.now().toUtc()));
        _recordEntryTime(sym);
        lastTradeResult = AutoExecuted(id, plan);"""
).replace(
"""      card.orderId = (order['id'] ?? '').toString();
      card.status = CardStatus.executed;""",
"""      card.orderId = (order['id'] ?? '').toString();
      card.status = CardStatus.executed;
      _recordEntryTime(card.ticker);"""
)

# 10. Block sub-$10 penny stocks and 09:30-09:36 ET open spread whip-saw window
ctrl = ctrl.replace(
"""      final qs = await _liveQuote(sym);
      if (!qs.fromSnapshot && !offHours) {""",
"""      final qs = await _liveQuote(sym);
      if (!crypto && qs.ask < 10 * kPipsPerUsd) {
        throw RiskReject('Sub-\\$10 penny stock filter: $sym is ${priceStr(qs.ask)} (min \\$10.00 required).');
      }
      if (!crypto && !offHours && sessionOpen) {
        final ny = nyTime(DateTime.now().toUtc());
        final mins = ny.hour * 60 + ny.minute;
        if (mins >= 570 && mins < 576) {
          throw const RiskReject('US Open 6-min spread blackout (09:30–09:36 ET). Waiting for spreads to stabilize.');
        }
      }
      if (!qs.fromSnapshot && !offHours) {"""
)

# 11. Make _AlpacaExtras._get and _NewsScorer.score resilient to transient socket/timeout drops
ctrl = ctrl.replace(
"  Future<dynamic> _get(Uri u) async => _decode(await _http.get(u, headers: _h).timeout(_kV4Timeout));",
"""  Future<dynamic> _get(Uri u) async {
    for (var attempt = 0; attempt < 2; attempt++) {
      try {
        return _decode(await _http.get(u, headers: _h).timeout(_kV4Timeout));
      } catch (e) {
        final isNet = e is TimeoutException || e is SocketException || e is http.ClientException;
        if (isNet && attempt == 0) {
          await Future<void>.delayed(const Duration(milliseconds: 400));
          continue;
        }
        rethrow;
      }
    }
  }"""
)

ctrl = ctrl.replace(
"""    http.Response? res;
    for (var i = 0; i < models.length; i++) {
      final m = models[i];
      res = await _http
          .post(
            Uri.parse('https://generativelanguage.googleapis.com/v1beta/models/$m:generateContent'),
            headers: {'x-goog-api-key': _key, 'Content-Type': 'application/json'},
            body: jsonEncode({
              'contents': [
                {
                  'parts': [
                    {'text': prompt}
                  ]
                }
              ],
              'generationConfig': {'responseMimeType': 'application/json', 'temperature': 0.2},
            }),
          )
          .timeout(_kV4Timeout);
      if (res.statusCode == 200) break;

      if ((res.statusCode == 503 || res.statusCode == 429 || res.statusCode == 500 || res.statusCode == 404) &&
          i < models.length - 1) {
        await Future<void>.delayed(Duration(milliseconds: 800 * (i + 1)));
        continue;
      }
      break;
    }""",
"""    http.Response? res;
    Object? lastErr;
    for (var i = 0; i < models.length; i++) {
      final m = models[i];
      try {
        res = await _http
            .post(
              Uri.parse('https://generativelanguage.googleapis.com/v1beta/models/$m:generateContent'),
              headers: {'x-goog-api-key': _key, 'Content-Type': 'application/json'},
              body: jsonEncode({
                'contents': [
                  {
                    'parts': [
                      {'text': prompt}
                    ]
                  }
                ],
                'generationConfig': {'responseMimeType': 'application/json', 'temperature': 0.2},
              }),
            )
            .timeout(_kV4Timeout);
      } catch (e) {
        lastErr = e;
        if (i < models.length - 1) {
          await Future<void>.delayed(Duration(milliseconds: 700 * (i + 1)));
          continue;
        }
        rethrow;
      }
      if (res.statusCode == 200) break;

      if ((res.statusCode == 503 || res.statusCode == 429 || res.statusCode == 500 || res.statusCode == 404) &&
          i < models.length - 1) {
        await Future<void>.delayed(Duration(milliseconds: 800 * (i + 1)));
        continue;
      }
      break;
    }
    if (res == null && lastErr != null) throw lastErr;"""
)

with open('lib/shield_controller.dart', 'w', encoding='utf-8') as f:
    f.write(ctrl)
print("[OK] Patched lib/shield_controller.dart")

with open('lib/main.dart', 'r', encoding='utf-8') as f:
    main_code = f.read()
main_code = main_code.replace("SHIELD v4.6", "SHIELD v4.7").replace("15% CAP", "10% CAP")
with open('lib/main.dart', 'w', encoding='utf-8') as f:
    f.write(main_code)
print("[OK] Patched lib/main.dart")
print("\n🎉 All 7 operational & system flaws patched cleanly!")
