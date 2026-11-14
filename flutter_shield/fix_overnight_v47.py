with open('lib/shield_controller.dart', 'r', encoding='utf-8') as f:
    ctrl = f.read()

# 1. Block US stocks from entering the AUTO candidate pool when the regular US session is closed
ctrl = ctrl.replace(
"""      if (isCryptoSymbol(s)) {
        if (!blockCrypto) cryptos.add(s);
      } else {
        stocks.add(s);
      }""",
"""      if (isCryptoSymbol(s)) {
        if (!blockCrypto) cryptos.add(s);
      } else {
        // Never feed US stocks to the Autonomous Scout when the regular US session is closed
        if (sessionOpen && intradayWindowMessage(nowUtc) == null) stocks.add(s);
      }"""
)

# 2. Enforce intraday window inside _venueGate even when sessionOpen is false (blocks overnight auto stock entries)
ctrl = ctrl.replace(
"""    if (sessionOpen) {
      if (tf == Timeframe.intraday) {
        final ts = DateTime.tryParse('${clock['timestamp']}')?.toUtc() ?? DateTime.now().toUtc();
        final m = intradayWindowMessage(ts);
        if (m != null) throw RiskReject(m);
      }
      return;
    }""",
"""    if (tf == Timeframe.intraday) {
      if (!sessionOpen) {
        throw const RiskReject('US stock market is closed for intraday trading. Auto-trading 24/7 Crypto only.');
      }
      final ts = DateTime.tryParse('${clock['timestamp']}')?.toUtc() ?? DateTime.now().toUtc();
      final m = intradayWindowMessage(ts);
      if (m != null) throw RiskReject(m);
      return;
    }
    if (sessionOpen) return;"""
)

# 3. Also prevent _ingestSignals from firing _autoNewsEntry on a US stock when sessionOpen is false
ctrl = ctrl.replace(
    "_tradableNow(r.ticker) &&",
    "(isCryptoSymbol(r.ticker) ? !_cryptoBlockedBySession(nowUtc) : (sessionOpen && intradayWindowMessage(nowUtc) == null)) &&"
)

# 4. Increase closedOrders limit to 200 and merge with existing cached closedTrades so wins never get pushed out by canceled orders
old_hist = """  Future<void> _refreshClosedHistory() async {
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
  }"""

new_hist = """  Future<void> _refreshClosedHistory() async {
    final cl = client;
    if (cl == null || vaultStatus != VaultStatus.valid) return;
    try {
      final raw = await _extras().closedOrders(limit: 200);
      final built = _buildClosedTrades(raw, positions);
      if (built.isNotEmpty) {
        final byKey = <String, ClosedTrade>{
          for (final t in closedTrades) '${t.symbol}_${t.closedAt.millisecondsSinceEpoch ~/ 1000}': t,
          for (final t in built) '${t.symbol}_${t.closedAt.millisecondsSinceEpoch ~/ 1000}': t,
        };
        final merged = byKey.values.toList()..sort((a, b) => b.closedAt.compareTo(a.closedAt));
        closedTrades = merged.take(60).toList();
        _prefs.setString('sh_closed', jsonEncode(closedTrades.map((t) => t.toJson()).toList()));
        notifyListeners();
      }
    } catch (_) {}
  }"""

if old_hist in ctrl:
    ctrl = ctrl.replace(old_hist, new_hist, 1)

with open('lib/shield_controller.dart', 'w', encoding='utf-8') as f:
    f.write(ctrl)
print("[OK] Patched lib/shield_controller.dart (Overnight Crypto-Only Gate + 200-Order Merged Trade History)")
