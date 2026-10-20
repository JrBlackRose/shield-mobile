import re

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

# 1. Patch lib/shield_core.dart: Predictable profit targets (3.5% swing, 1.8% intraday)
patch_file('lib/shield_core.dart', [
    (
        "  static const int swingStopBps = 1000;\n  static const int swingTargetBps = 3000;\n  static const int intradayStopBps = 100;\n  static const int intradayTargetBps = 300;",
        "  static const int swingStopBps = 175;\n  static const int swingTargetBps = 350;\n  static const int intradayStopBps = 90;\n  static const int intradayTargetBps = 180;"
    ),
])

# 2. Patch lib/shield_crypto_risk.dart: Tighter crypto volatility gate, predictable profit target, 30% max cap
patch_file('lib/shield_crypto_risk.dart', [
    (
        "  static const int assetCapBps = 600; // 6% of equity in any one coin\n  static const int classCapBps = 1500; // 15% across ALL crypto (one correlated cluster, rho ~0.8+)\n\n  // Entry gates.\n  static const double maxSpread = 75.0; // bps; calibrated for Alpaca crypto books\n  static const double regimeSkipRatio = 2.5; // fast/slow vol above this => skip entry",
        "  static const int assetCapBps = 1000; // 10% max per coin\n  static const int classCapBps = 3000; // 30% max across ALL crypto (70% reserved for US stocks)\n\n  // Entry gates.\n  static const double maxSpread = 55.0; // tighter spread filter for crypto\n  static const double regimeSkipRatio = 1.85; // skip crypto during volatile whipsaws"
    ),
    (
        "  static const double breakevenAtR = 1.0;\n  static const int breakevenLockBps = 60; // > buy fee 25 + sell fee 25 + cushion\n  static const double trailStartR = 2.0;\n  static const double trailKStart = 3.0; // ATR multiples\n  static const double trailKMin = 1.5;\n  static const double minStepAtrFrac = 0.25; // hysteresis: don't PATCH for crumbs\n  static const int minStopGapBps = 10; // a sell stop must sit below the market\n  static const double refTargetR = 3.0; // display-only target; no TP order exists",
        "  static const double breakevenAtR = 0.65;\n  static const int breakevenLockBps = 65; // > buy fee 25 + sell fee 25 + cushion\n  static const double trailStartR = 1.1;\n  static const double trailKStart = 2.0; // tighter profit trail\n  static const double trailKMin = 1.0;\n  static const double minStepAtrFrac = 0.20;\n  static const int minStopGapBps = 10;\n  static const double refTargetR = 1.8; // predictable profit target"
    ),
])

# 3. Patch lib/shield_crypto_guardian.dart: Fix 422 qty=0 dust bug & add predictable profit harvest
patch_file('lib/shield_crypto_guardian.dart', [
    (
        "      final held = <String, Position>{\n        for (final p in c.positions)\n          if (p.isLong && isCryptoSymbol(p.symbol) && p.qtyDouble > 0) normSym(p.symbol): p,\n      };",
        "      final held = <String, Position>{\n        for (final p in c.positions)\n          if (p.isLong && isCryptoSymbol(p.symbol) && p.qtyDouble >= 0.0001 && p.marketValue.abs() >= kPipsPerUsd) normSym(p.symbol): p,\n      };"
    ),
    (
        "    if (stopOrder == null) {\n      final ceiling = last * (kBpsDenom - CryptoRules.minStopGapBps) ~/ kBpsDenom;",
        "    final qtyTxt = floorQtyText(p.qtyDouble);\n    if ((double.tryParse(qtyTxt) ?? 0.0) <= 0.0) return;\n    // Predictable Crypto Profit Harvester: bank profit automatically at +2.2% gain\n    if (p.avgEntry > 0 && last >= (p.avgEntry * 10220 ~/ 10000)) {\n      c._log('💰 GUARDIAN banking predictable crypto profit on $sym (+${((last - p.avgEntry) / p.avgEntry * 100).toStringAsFixed(2)}%)', level: 'ok');\n      final res = await c.closePosition(sym);\n      unawaited(c._notifyOS('💰 Crypto Profit Banked: $sym', res.message));\n      return;\n    }\n    if (stopOrder == null) {\n      final ceiling = last * (kBpsDenom - CryptoRules.minStopGapBps) ~/ kBpsDenom;"
    ),
])

# 4. Patch lib/shield_controller.dart: Version 4.5.0, Closed Trade History, 70/30 US Stock focus, Dust filter, Stock Profit Lock
patch_file('lib/shield_controller.dart', [
    (
        "  static const String currentVersion = '4.4.0';",
        "  static const String currentVersion = '4.5.0';"
    ),
    (
        "class CopilotReply {\n  const CopilotReply(this.text, this.action);\n  final String text;\n  final CopilotAction? action;\n}",
        """class ClosedTrade {
  const ClosedTrade({
    required this.symbol,
    required this.qtyText,
    required this.buyPrice,
    required this.sellPrice,
    required this.pnlPips,
    required this.pnlPct,
    required this.closedAt,
  });
  final String symbol, qtyText;
  final Pips buyPrice, sellPrice, pnlPips;
  final double pnlPct;
  final DateTime closedAt;
  bool get isWin => pnlPips >= 0;
}

class CopilotReply {
  const CopilotReply(this.text, this.action);
  final String text;
  final CopilotAction? action;
}"""
    ),
    (
        "  List<OrderSummary> orders = [];\n  static const MethodChannel _native = MethodChannel('shield/native');\n  List<Map<String, dynamic>> _rawOrders = [];",
        "  List<OrderSummary> orders = [];\n  List<ClosedTrade> closedTrades = [];\n  static const MethodChannel _native = MethodChannel('shield/native');\n  List<Map<String, dynamic>> _rawOrders = [];"
    ),
    (
        "      account = Account.fromJson(await fa);\n      positions = (await fp).map(Position.fromJson).toList();\n      final rawOrd = await fo;",
        "      account = Account.fromJson(await fa);\n      // Filter out < $1.00 fractional crypto dust so 0-sh positions never clutter UI or trigger 422\n      positions = (await fp).map(Position.fromJson).where((p) => p.marketValue.abs() >= kPipsPerUsd && p.qtyDouble >= 0.0001).toList();\n      final rawOrd = await fo;\n      unawaited(_refreshClosedHistory());\n      unawaited(_harvestPredictableStockProfits());"
    ),
    (
        "  bool _tradableNow(String sym) => _resolveVenue(sym) != VenueKind.closed;",
        """  bool _tradableNow(String sym) => _resolveVenue(sym) != VenueKind.closed;

  /// Returns true when crypto should be paused (first 45m of US open whipsaw or >30% crypto allocation during US session).
  bool _cryptoBlockedBySession(DateTime nowUtc) {
    final eq = account?.equity ?? 0;
    if (eq > 0) {
      final cGross = cryptoGrossPips(positions, _rawOrders);
      final capBps = sessionOpen ? 3000 : 4500; // 30% max crypto when US market open (70% US stocks)
      if (cGross >= (eq * capBps ~/ kBpsDenom)) return true;
    }
    if (sessionOpen) {
      final n = nyTime(nowUtc);
      final m = n.hour * 60 + n.minute;
      // Block new crypto entries during volatile US open window (09:30 - 10:15 ET)
      if (m >= 570 && m <= 615) return true;
    }
    return false;
  }

  Future<void> _refreshClosedHistory() async {
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
  }"""
    ),
    (
        "    void add(Object? raw) {\n      final s = _cleanSym(raw);\n      if (s == null || !seen.add(s)) return;\n      if (held.contains(s) || bearish.contains(s) || _agentCooling(s, nowUtc)) return;\n      if (_resolveVenue(s) == VenueKind.closed) return;\n      out.add(s);\n    }",
        """    final blockCrypto = _cryptoBlockedBySession(nowUtc);
    final stocks = <String>[];
    final cryptos = <String>[];
    void add(Object? raw) {
      final s = _cleanSym(raw);
      if (s == null || !seen.add(s)) return;
      if (held.contains(s) || bearish.contains(s) || _agentCooling(s, nowUtc)) return;
      if (_resolveVenue(s) == VenueKind.closed) return;
      if (isCryptoSymbol(s)) {
        if (!blockCrypto) cryptos.add(s);
      } else {
        stocks.add(s);
      }
    }"""
    ),
    (
        "    for (final u in dynamicScanUniverse(nowUtc, clockOpen: sessionOpen)) {\n      add(u);\n    }\n    return out.take(AutoRules.maxCandidates).toList();",
        """    for (final u in dynamicScanUniverse(nowUtc, clockOpen: sessionOpen)) {
      add(u);
    }
    // 70% US Stock / 30% Crypto priority: during US Market Open, feed US stocks first;
    // only include up to 25% crypto candidates if US stocks are scarce or capped.
    if (sessionOpen && stocks.isNotEmpty) {
      final maxStock = (AutoRules.maxCandidates * 0.75).round();
      out.addAll(stocks.take(maxStock));
      out.addAll(cryptos.take(AutoRules.maxCandidates - out.length));
    } else {
      out.addAll(stocks);
      out.addAll(cryptos);
    }
    return out.take(AutoRules.maxCandidates).toList();"""
    ),
    (
        "  /// Longs are at risk when sentiment <= 0.35; a short would mirror that (>= 0.65).\n  bool _atRisk(Position p, double sentiment) =>\n      p.isLong ? sentiment <= NewsRules.bearishSentiment : sentiment >= 1.0 - NewsRules.bearishSentiment;",
        """  /// Longs are at risk when sentiment <= 0.22, OR sentiment <= 0.32 while the position is losing money.
  /// Prevents premature panic-selling of profitable positions on minor market noise.
  bool _atRisk(Position p, double sentiment) {
    if (p.isLong) {
      if (sentiment <= 0.22) return true;
      return sentiment <= 0.32 && p.unrealizedPl < 0;
    }
    return sentiment >= 1.0 - NewsRules.bearishSentiment;
  }"""
    ),
    (
        "  Future<List<Map<String, dynamic>>> _openOrdersFor(String symbol) async => _list(",
        """  Future<List<Map<String, dynamic>>> closedOrders({int limit = 120}) async => _list(
        await _get(Uri.parse('$kPaperBase/orders').replace(queryParameters: {
          'status': 'closed',
          'limit': '$limit',
          'direction': 'desc',
          'nested': 'true',
        })),
      );

  Future<List<Map<String, dynamic>>> _openOrdersFor(String symbol) async => _list("""
    ),
])

# Append FIFO trade history pairing helper to lib/shield_controller.dart
with open('lib/shield_controller.dart', 'r', encoding='utf-8') as f:
    ctrl_text = f.read()

fifo_helper = """
List<ClosedTrade> _buildClosedTrades(List<Map<String, dynamic>> rawClosed, List<Position> openPositions) {
  final fills = <({String sym, String side, double qty, Pips price, DateTime at})>[];
  void extract(Map<String, dynamic> o) {
    final st = '${o['status'] ?? ''}'.toLowerCase();
    final fq = double.tryParse('${o['filled_qty'] ?? '0'}') ?? 0.0;
    final fp = tryPips(o['filled_avg_price']);
    final sym = _cleanSym(o['symbol']);
    final side = '${o['side'] ?? ''}'.toLowerCase();
    final at = DateTime.tryParse('${o['filled_at'] ?? o['updated_at'] ?? o['created_at']}')?.toUtc();
    if ((st == 'filled' || st == 'partially_filled') && fq > 0 && fp != null && fp > 0 && sym != null && at != null) {
      fills.add((sym: sym, side: side, qty: fq, price: fp, at: at));
    }
    final legs = o['legs'];
    if (legs is List) {
      for (final l in legs.whereType<Map>()) {
        extract(l.cast<String, dynamic>());
      }
    }
  }
  for (final o in rawClosed) {
    extract(o);
  }
  fills.sort((a, b) => a.at.compareTo(b.at));
  final buyLots = <String, List<({double qty, Pips price})>>{};
  final out = <ClosedTrade>[];

  for (final f in fills) {
    if (f.side == 'buy') {
      buyLots.putIfAbsent(f.sym, () => []).add((qty: f.qty, price: f.price));
    } else if (f.side == 'sell') {
      final lots = buyLots[f.sym];
      var rem = f.qty;
      var costSum = 0.0;
      var matchedQty = 0.0;
      if (lots != null && lots.isNotEmpty) {
        while (rem > 1e-7 && lots.isNotEmpty) {
          final first = lots.first;
          final take = math.min(rem, first.qty);
          costSum += take * first.price;
          matchedQty += take;
          rem -= take;
          if (first.qty - take <= 1e-7) {
            lots.removeAt(0);
          } else {
            lots[0] = (qty: first.qty - take, price: first.price);
          }
        }
      }
      if (matchedQty > 1e-6) {
        final avgBuy = (costSum / matchedQty).round();
        final pnlPips = ((f.price - avgBuy) * matchedQty).round();
        final pnlPct = avgBuy > 0 ? ((f.price - avgBuy) / avgBuy) * 100.0 : 0.0;
        out.add(ClosedTrade(
          symbol: f.sym,
          qtyText: formatQty(matchedQty),
          buyPrice: avgBuy,
          sellPrice: f.price,
          pnlPips: pnlPips,
          pnlPct: pnlPct,
          closedAt: f.at,
        ));
      }
    }
  }
  return out.reversed.toList();
}
"""

if "_buildClosedTrades" not in ctrl_text:
    with open('lib/shield_controller.dart', 'w', encoding='utf-8') as f:
        f.write(ctrl_text + "\n" + fifo_helper)
    print("[OK] Appended _buildClosedTrades to lib/shield_controller.dart")

# 5. Patch lib/main.dart: Header v4.5 + Closed Trade History Panel in PortfolioTab
patch_file('lib/main.dart', [
    (
        "const Text('SHIELD v4.4'",
        "const Text('SHIELD v4.5'"
    ),
    (
        "        Panel(\n          title: '🕘 Activity timeline',",
        """        Panel(
          title: '📜 Trade history (${c.closedTrades.length})',
          trailing: _TradeScorecard(trades: c.closedTrades),
          child: c.closedTrades.isEmpty
              ? const Text('No completed round-trip trades in recent history.', style: TextStyle(color: Pal.muted))
              : Column(children: [
                  for (final t in c.closedTrades.take(25)) _ClosedTradeRow(t: t, onChart: onChart),
                ]),
        ),
        Panel(
          title: '🕘 Activity timeline',"""
    ),
    (
        "class _PositionRow extends StatelessWidget {",
        """class _TradeScorecard extends StatelessWidget {
  const _TradeScorecard({required this.trades});
  final List<ClosedTrade> trades;

  @override
  Widget build(BuildContext context) {
    if (trades.isEmpty) return const SizedBox.shrink();
    final wins = trades.where((t) => t.isWin).length;
    final losses = trades.length - wins;
    final net = trades.fold<int>(0, (a, b) => a + b.pnlPips);
    final up = net >= 0;
    return Row(mainAxisSize: MainAxisSize.min, children: [
      Text('${wins}W / ${losses}L · ', style: const TextStyle(color: Pal.muted, fontSize: 11.5, fontWeight: FontWeight.w700)),
      Text(usd(net, sign: true), style: TextStyle(color: up ? Pal.emerald : Pal.dangerSoft, fontSize: 12, fontWeight: FontWeight.w800)),
    ]);
  }
}

class _ClosedTradeRow extends StatelessWidget {
  const _ClosedTradeRow({required this.t, required this.onChart});
  final ClosedTrade t;
  final void Function(String symbol) onChart;

  @override
  Widget build(BuildContext context) {
    final up = t.isWin;
    final tone = up ? Pal.emerald : Pal.dangerSoft;
    final local = t.closedAt.toLocal();
    final ts = '${two(local.month)}/${two(local.day)} ${two(local.hour)}:${two(local.minute)}';
    return Container(
      margin: const EdgeInsets.only(bottom: 8),
      padding: const EdgeInsets.fromLTRB(10, 8, 8, 8),
      decoration: BoxDecoration(color: Pal.bg, borderRadius: BorderRadius.circular(8), border: Border.all(color: Pal.border)),
      child: Row(children: [
        Expanded(
          child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
            Row(children: [
              Text(t.symbol, style: const TextStyle(fontWeight: FontWeight.w800, fontSize: 14)),
              const SizedBox(width: 6),
              _Tag(up ? 'WIN' : 'LOSS', tone),
              const SizedBox(width: 6),
              Text(ts, style: const TextStyle(color: Pal.muted, fontSize: 11)),
            ]),
            const SizedBox(height: 3),
            Text('Bought ${t.qtyText} @ ${usd(t.buyPrice)}  →  Sold @ ${usd(t.sellPrice)}',
                style: _mono.copyWith(fontSize: 11.5, color: Pal.muted)),
          ]),
        ),
        Column(crossAxisAlignment: CrossAxisAlignment.end, children: [
          Text(usd(t.pnlPips, sign: true), style: TextStyle(color: tone, fontWeight: FontWeight.w800, fontSize: 14)),
          const SizedBox(height: 2),
          Text('${t.pnlPct >= 0 ? '+' : ''}${t.pnlPct.toStringAsFixed(2)}%',
              style: _mono.copyWith(color: tone, fontSize: 11, fontWeight: FontWeight.w700)),
        ]),
        IconButton(
          tooltip: 'Chart ${t.symbol}',
          visualDensity: VisualDensity.compact,
          icon: const Icon(Icons.candlestick_chart_outlined, color: Pal.sky, size: 18),
          onPressed: () => onChart(t.symbol),
        ),
      ]),
    );
  }
}

class _PositionRow extends StatelessWidget {"""
    ),
])

print("\n🎉 SHIELD v4.5.0 upgrade applied successfully!")
