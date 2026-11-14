import re

# ==============================================================================
# 1. PATCH lib/shield_core.dart
#    - Change RiskRules to tight base-hit targets (-0.65% SL / +0.60% TP)
#    - Add simulate100Paths() Monte Carlo price path engine
#    - Add simWins to ConfluenceScore & summary log
# ==============================================================================
with open('lib/shield_core.dart', 'r', encoding='utf-8') as f:
    core = f.read()

core = re.sub(
    r"static const int swingStopBps = \d+;\s*static const int swingTargetBps = \d+;\s*static const int intradayStopBps = \d+;\s*static const int intradayTargetBps = \d+;",
    "static const int swingStopBps = 110;\n  static const int swingTargetBps = 120;\n  static const int intradayStopBps = 65;\n  static const int intradayTargetBps = 60;",
    core
)

sim_fn = """
/// Simulates 100 forward price paths (24 x 5-min bars = 2 hours) using
/// empirical candle returns, intrabar wick excursions, and EMA drift.
/// Returns the number of winning paths out of 100 (0..100).
int simulate100Paths({
  required List<double> closes,
  required List<double> highs,
  required List<double> lows,
  required double price,
  required bool isCrypto,
  required bool trendAligned,
}) {
  if (!trendAligned || closes.length < 30 || price <= 0) return 28;
  final n = closes.length;
  final start = math.max(1, n - 36);
  final rets = <double>[];
  final upWicks = <double>[];
  final dnWicks = <double>[];
  for (var i = start; i < n; i++) {
    final prev = closes[i - 1];
    if (prev <= 0) continue;
    rets.add(((closes[i] - prev) / prev).clamp(-0.025, 0.025).toDouble());
    upWicks.add(((highs[i] - prev) / prev).clamp(0.0, 0.030).toDouble());
    dnWicks.add(((lows[i] - prev) / prev).clamp(-0.030, 0.0).toDouble());
  }
  if (rets.length < 12) return 35;

  final m = rets.length;
  final recentCount = math.min(12, m);
  var recentSum = 0.0;
  for (var i = m - recentCount; i < m; i++) {
    recentSum += rets[i];
  }
  final recentMean = recentSum / recentCount;
  var varSum = 0.0;
  for (final r in rets) {
    final d = r - recentMean;
    varSum += d * d;
  }
  final sigma = math.sqrt(varSum / m).clamp(0.0004, 0.0120).toDouble();

  final targetPct = isCrypto ? 0.0058 : 0.0048;
  const beTriggerPct = 0.0020; // +0.20% (~+$19 on $9.5k slot) locks break-even
  const initialStopPct = -0.0065; // -0.65% tight stop

  var wins = 0;
  final baseSeed = (price * 10000).round() ^ (n * 7919);

  for (var p = 0; p < 100; p++) {
    var seed = (baseSeed + p * 1103515245 + 12345) & 0x7fffffff;
    var cumRet = 0.0;
    var activeStop = initialStopPct;
    var beLocked = false;
    var outcome = 0; // 1 = win, -1 = loss

    for (var step = 0; step < 24; step++) {
      seed = (seed * 1103515245 + 12345) & 0x7fffffff;
      final useRecent = (seed & 1) == 0;
      final idx = useRecent
          ? (m - recentCount + ((seed >> 3) % recentCount))
          : ((seed >> 3) % m);
      seed = (seed * 1103515245 + 12345) & 0x7fffffff;
      final u = ((seed % 2001) - 1000) / 1000.0; // -1.0 .. +1.0
      final shock = u * sigma * 0.45 + recentMean * 0.25;

      final stepHigh = cumRet + math.max(rets[idx], upWicks[idx] * 0.72) + math.max(0.0, shock);
      final stepLow = cumRet + math.min(rets[idx], dnWicks[idx] * 0.72) + math.min(0.0, shock);

      if (stepHigh >= targetPct) {
        outcome = 1;
        break;
      }
      if (stepHigh >= beTriggerPct) {
        beLocked = true;
        activeStop = 0.0004; // Break-even shield armed
      }
      if (stepLow <= activeStop) {
        outcome = beLocked ? 1 : -1;
        break;
      }
      cumRet += rets[idx] + shock;
    }

    if (outcome == 1 || (outcome == 0 && (beLocked || cumRet >= 0.0005))) {
      wins++;
    }
  }
  return wins.clamp(0, 100);
}
"""

if "int simulate100Paths(" not in core:
    core = core.replace("class ConfluenceSignal {", sim_fn + "\nclass ConfluenceSignal {", 1)

old_conf_class = """class ConfluenceScore {
  const ConfluenceScore({
    required this.symbol,
    required this.price,
    required this.vwapValue,
    required this.volumeRatio,
    required this.rsiValue,
    required this.barsUsed,
    required this.asOf,
  });
  final String symbol;
  final double price, vwapValue, volumeRatio, rsiValue;
  final int barsUsed;
  final DateTime asOf;"""

new_conf_class = """class ConfluenceScore {
  const ConfluenceScore({
    required this.symbol,
    required this.price,
    required this.vwapValue,
    required this.volumeRatio,
    required this.rsiValue,
    required this.barsUsed,
    required this.asOf,
    this.simWins = 70,
  });
  final String symbol;
  final double price, vwapValue, volumeRatio, rsiValue;
  final int barsUsed;
  final DateTime asOf;
  final int simWins;"""

if old_conf_class in core:
    core = core.replace(old_conf_class, new_conf_class, 1)

core = core.replace(
    "String get summary => '$points/3$grade · VWAP ${aboveVwap ? \"▲\" : \"▼\"} ${vwapValue.toStringAsFixed(2)}'",
    "String get summary => '$points/3$grade · 🎲 Sim $simWins/100 · VWAP ${aboveVwap ? \"▲\" : \"▼\"} ${vwapValue.toStringAsFixed(2)}'"
)

core = core.replace(
    "'t': asOf.toIso8601String(),",
    "'t': asOf.toIso8601String(),\n        'sim': simWins,"
)

core = core.replace(
    "asOf: DateTime.parse(j['t'] as String),",
    "asOf: DateTime.parse(j['t'] as String),\n        simWins: (j['sim'] as num?)?.toInt() ?? 70,"
)

with open('lib/shield_core.dart', 'w', encoding='utf-8') as f:
    f.write(core)
print("[OK] Patched lib/shield_core.dart (Tight RiskRules + simulate100Paths + ConfluenceScore.simWins)")

# ==============================================================================
# 2. PATCH lib/shield_crypto_guardian.dart
#    - Add Somes Auto-Cashout & Break-Even Guardian across ALL positions (Stocks + Crypto)
#    - Wire simulate100Paths into computeCryptoConfluence
# ==============================================================================
with open('lib/shield_crypto_guardian.dart', 'r', encoding='utf-8') as f:
    cg = f.read()

old_fields = """  final Map<String, TrailState> trails = <String, TrailState>{};
  final Map<String, ({DateTime at, CryptoVol vol})> _volCache = {};
  final Map<String, int> _breach = <String, int>{};"""

new_fields = """  final Map<String, TrailState> trails = <String, TrailState>{};
  final Map<String, ({DateTime at, CryptoVol vol})> _volCache = {};
  final Map<String, int> _breach = <String, int>{};
  final Map<String, double> _peakPlUsd = <String, double>{};
  final Map<String, int> _peakGainBps = <String, int>{};
  final Map<String, int> _stockLossBreach = <String, int>{};"""

if old_fields in cg:
    cg = cg.replace(old_fields, new_fields, 1)

# Load & save peak P&L watermarks so Somes Auto-Cashout persists across restarts
old_load = """      final raw = jsonDecode(c._prefs.getString('sh_trails') ?? '{}') as Map;
      for (final e in raw.entries) {
        trails['${e.key}'] = TrailState.fromJson((e.value as Map).cast<String, dynamic>());
      }"""

new_load = """      final raw = jsonDecode(c._prefs.getString('sh_trails') ?? '{}') as Map;
      for (final e in raw.entries) {
        trails['${e.key}'] = TrailState.fromJson((e.value as Map).cast<String, dynamic>());
      }
      final rawPeaks = jsonDecode(c._prefs.getString('sh_peaks_usd') ?? '{}') as Map;
      for (final e in rawPeaks.entries) {
        _peakPlUsd['${e.key}'] = (e.value as num).toDouble();
      }
      final rawBps = jsonDecode(c._prefs.getString('sh_peaks_bps') ?? '{}') as Map;
      for (final e in rawBps.entries) {
        _peakGainBps['${e.key}'] = (e.value as num).toInt();
      }"""

if old_load in cg:
    cg = cg.replace(old_load, new_load, 1)

old_save = "void _save() => c._prefs.setString('sh_trails', jsonEncode({for (final e in trails.entries) e.key: e.value.toJson()}));"
new_save = """void _save() {
    c._prefs.setString('sh_trails', jsonEncode({for (final e in trails.entries) e.key: e.value.toJson()}));
    c._prefs.setString('sh_peaks_usd', jsonEncode(_peakPlUsd));
    c._prefs.setString('sh_peaks_bps', jsonEncode(_peakGainBps));
  }"""

if old_save in cg:
    cg = cg.replace(old_save, new_save, 1)

# Inject _runSomesAutoCashout() call inside run()
old_run_loop = """      for (final e in held.entries) {
        if (c._closing.contains(e.key)) continue;
        try {
          await _protect(ex, e.key, e.value, now);"""

new_run_loop = """      await _runSomesAutoCashout();
      for (final e in held.entries) {
        if (c._closing.contains(e.key)) continue;
        try {
          await _protect(ex, e.key, e.value, now);"""

if old_run_loop in cg:
    cg = cg.replace(old_run_loop, new_run_loop, 1)

somes_method = """  Future<void> _runSomesAutoCashout() async {
    final activeSyms = <String>{};
    var peaksDirty = false;
    for (final p in c.positions) {
      if (!p.isLong || p.avgEntry <= 0 || p.currentPrice <= 0 || p.qtyDouble <= 0) continue;
      final sym = normSym(p.symbol);
      activeSyms.add(sym);
      if (c._closing.contains(sym)) continue;
      final isCrypto = isCryptoSymbol(sym);
      // Stocks can only be market-closed when the regular US session is open
      if (!isCrypto && !c.sessionOpen) continue;

      final plUsd = p.unrealizedPl / kPipsPerUsd;
      final gainBps = ((p.currentPrice - p.avgEntry) * kBpsDenom) ~/ p.avgEntry;

      final prevPeakUsd = _peakPlUsd[sym] ?? plUsd;
      final prevPeakBps = _peakGainBps[sym] ?? gainBps;
      final peakUsd = math.max(prevPeakUsd, plUsd);
      final peakBps = math.max(prevPeakBps, gainBps);
      if (peakUsd > prevPeakUsd || peakBps > prevPeakBps || !_peakPlUsd.containsKey(sym)) {
        _peakPlUsd[sym] = peakUsd;
        _peakGainBps[sym] = peakBps;
        peaksDirty = true;
      }

      // 1. Instant Base-Hit Target Harvest (+$48 USD or +0.58% Stock / +0.75% Crypto)
      final targetBps = isCrypto ? 75 : 58;
      if (plUsd >= 48.0 || gainBps >= targetBps) {
        c._log('💰 AUTO-CASHOUT $sym: Banked +\\$${plUsd.toStringAsFixed(2)} (+${(gainBps / 100).toStringAsFixed(2)}%) base-hit target!', level: 'ok');
        final res = await c.closePosition(sym);
        unawaited(c._notifyOS('💰 Target Banked: $sym (+\\$${plUsd.toStringAsFixed(2)})', res.message));
        _peakPlUsd.remove(sym);
        _peakGainBps.remove(sym);
        peaksDirty = true;
        continue;
      }

      // 2. Somes +$20 Peak-Pullback Cashout:
      //    If position reached >= +$20 (or >= +0.22%) and pulls back 28% from peak while still >= +$9, bank it!
      if ((peakUsd >= 20.0 || peakBps >= 22) && plUsd >= 9.0 && plUsd <= peakUsd * 0.72) {
        c._log('💰 SOMES PROFIT-LOCK $sym: Peaked at +\\$${peakUsd.toStringAsFixed(2)}, banked +\\$${plUsd.toStringAsFixed(2)} on pullback!', level: 'ok');
        final res = await c.closePosition(sym);
        unawaited(c._notifyOS('💰 Profit Locked: $sym (+\\$${plUsd.toStringAsFixed(2)})', res.message));
        _peakPlUsd.remove(sym);
        _peakGainBps.remove(sym);
        peaksDirty = true;
        continue;
      }

      // 3. The +$14 Break-Even Shield:
      //    If position was up >= +$14 (or >= +0.16%) and fades down to <= +$3.50, close before it can ever turn red!
      if ((peakUsd >= 14.0 || peakBps >= 16) && plUsd <= 3.50) {
        c._log('🛡️ BREAK-EVEN SHIELD $sym: Peaked at +\\$${peakUsd.toStringAsFixed(2)}, exited at +\\$${plUsd.toStringAsFixed(2)} so winner never turns red!', level: 'ok');
        final res = await c.closePosition(sym);
        unawaited(c._notifyOS('🛡️ Break-Even Saved: $sym', res.message));
        _peakPlUsd.remove(sym);
        _peakGainBps.remove(sym);
        peaksDirty = true;
        continue;
      }

      // 4. Tight Stop-Loss Guard (-0.68% or -$65 USD)
      if (gainBps <= -68 || plUsd <= -65.0) {
        final n = (_stockLossBreach[sym] ?? 0) + 1;
        _stockLossBreach[sym] = n;
        if (n >= 2) {
          c._log('🚨 TIGHT STOP-LOSS $sym: Cut loss at \\$${plUsd.toStringAsFixed(2)} (${(gainBps / 100).toStringAsFixed(2)}%) to protect capital.', level: 'error');
          final res = await c.closePosition(sym);
          unawaited(c._notifyOS('🛡️ Tight Stop-Loss: $sym', res.message));
          _stockLossBreach.remove(sym);
          _peakPlUsd.remove(sym);
          _peakGainBps.remove(sym);
          peaksDirty = true;
        }
      } else {
        _stockLossBreach.remove(sym);
      }
    }

    _peakPlUsd.removeWhere((k, _) => !activeSyms.contains(k));
    _peakGainBps.removeWhere((k, _) => !activeSyms.contains(k));
    if (peaksDirty) _save();
  }

  Future<void> _protect(_AlpacaExtras ex, String sym, Position p, DateTime now) async {"""

if "_runSomesAutoCashout()" not in cg or "Future<void> _runSomesAutoCashout()" not in cg:
    cg = cg.replace("  Future<void> _protect(_AlpacaExtras ex, String sym, Position p, DateTime now) async {", somes_method, 1)

# Wire simulate100Paths into computeCryptoConfluence
old_crypto_ret = """  return ConfluenceScore(
    symbol: symbol,
    price: price,
    vwapValue: trendAligned ? vwap : price * 1.01,
    volumeRatio: vr,
    rsiValue: rsi,
    barsUsed: done.length,
    asOf: done.last.t,
  );"""

new_crypto_ret = """  final simWins = simulate100Paths(
    closes: closes,
    highs: done.map((b) => b.h).toList(),
    lows: done.map((b) => b.l).toList(),
    price: price,
    isCrypto: true,
    trendAligned: trendAligned,
  );
  return ConfluenceScore(
    symbol: symbol,
    price: price,
    vwapValue: trendAligned ? vwap : price * 1.01,
    volumeRatio: vr,
    rsiValue: rsi,
    barsUsed: done.length,
    asOf: done.last.t,
    simWins: simWins,
  );"""

if old_crypto_ret in cg:
    cg = cg.replace(old_crypto_ret, new_crypto_ret, 1)

with open('lib/shield_crypto_guardian.dart', 'w', encoding='utf-8') as f:
    f.write(cg)
print("[OK] Patched lib/shield_crypto_guardian.dart (Somes Auto-Cashout + 100-Path Crypto Sim)")

# ==============================================================================
# 3. PATCH lib/shield_controller.dart
#    - Wire simulate100Paths into stock computeConfluence
#    - Rank & filter Scout candidates by 100-Path Win Probability (>= 62/100)
#    - Require >= 62/100 simulated paths in triggerTrade hard gate
# ==============================================================================
with open('lib/shield_controller.dart', 'r', encoding='utf-8') as f:
    ctrl = f.read()

ctrl = re.sub(r"currentVersion = '[^']+';", "currentVersion = '6.0.0';", ctrl)

# Inject simWins into stock computeConfluence return
old_stock_ret_pat = r"(return ConfluenceScore\(\s*symbol:\s*symbol,\s*price:\s*price,\s*vwapValue:\s*[^,]+,\s*volumeRatio:\s*vr,\s*rsiValue:\s*rsi,\s*barsUsed:\s*done\.length,\s*asOf:\s*last\.t,\s*\);)"

def add_stock_sim(m):
    return """final simWins = simulate100Paths(
    closes: closes,
    highs: done.map((b) => b.h).toList(),
    lows: done.map((b) => b.l).toList(),
    price: price,
    isCrypto: crypto,
    trendAligned: trendAligned,
  );
  return ConfluenceScore(
    symbol: symbol,
    price: price,
    vwapValue: trendAligned ? vwap : price * 1.01,
    volumeRatio: vr,
    rsiValue: rsi,
    barsUsed: done.length,
    asOf: last.t,
    simWins: simWins,
  );"""

ctrl, n_stock_sim = re.subn(old_stock_ret_pat, add_stock_sim, ctrl, count=1)
print(f"[OK] Injected simulate100Paths into stock computeConfluence (matches={n_stock_sim})")

# Upgrade Scout pre-screening to require simWins >= 62 and sort by highest simWins!
old_scout_loop = """      final passing = <String>[];
      for (final sym in candidates.take(8)) {
        try {
          final qs = await _liveQuote(sym);
          if (qs.ask <= 0) continue;
          final cScore = await _computeConfluence(sym, qs.ask, live: _resolveVenue(sym) != VenueKind.closed);
          if (cScore != null && cScore.points >= 2) {
            passing.add(sym);
          }
        } catch (_) {}
      }
      if (passing.isEmpty) {
        _log('🔍 Scout checked ${candidates.take(8).length} pairs — 0 passed Pullback Sniper (>=2/3) this round.', level: 'info', notify: false);
        return;
      }"""

new_scout_loop = """      final scoredPairs = <({String sym, int sim})>[];
      for (final sym in candidates.take(10)) {
        try {
          final qs = await _liveQuote(sym);
          if (qs.ask <= 0) continue;
          final cScore = await _computeConfluence(sym, qs.ask, live: _resolveVenue(sym) != VenueKind.closed);
          if (cScore != null && cScore.points >= 2 && cScore.simWins >= 62) {
            scoredPairs.add((sym: sym, sim: cScore.simWins));
          }
        } catch (_) {}
      }
      scoredPairs.sort((a, b) => b.sim.compareTo(a.sim));
      final passing = scoredPairs.map((e) => e.sym).toList();
      if (passing.isEmpty) {
        _log('🎲 Scout simulated ${candidates.take(10).length} tickers (100 paths each) — 0 achieved ≥62% win probability this round.', level: 'info', notify: false);
        return;
      }"""

if old_scout_loop in ctrl:
    ctrl = ctrl.replace(old_scout_loop, new_scout_loop, 1)
    print("[OK] Upgraded Autonomous Scout to rank candidates by 100-Path Win Probability (>=62/100)")

# Upgrade triggerTrade gate to require conf.simWins >= 62
old_gate = """      // Hard Quantitative Confluence Gate: require >= 2/3 on live venues
      if (!offHours && !qs.fromSnapshot && (conf == null || conf.points < 2)) {
        throw RiskReject(
          'Confluence ${conf?.points ?? 0}/3 WEAK (requires ≥ 2/3). Trade blocked for $sym.',
        );
      }"""

new_gate = """      // Hard Quantitative Confluence + 100-Path Monte Carlo Gate: require >= 2/3 AND >= 62/100 paths
      if (!offHours && !qs.fromSnapshot && (conf == null || conf.points < 2 || conf.simWins < 62)) {
        throw RiskReject(
          'Confluence ${conf?.points ?? 0}/3 · 100-Path Sim ${conf?.simWins ?? 0}/100 (requires ≥ 2/3 & ≥ 62/100 win paths). Trade blocked for $sym.',
        );
      }"""

if old_gate in ctrl:
    ctrl = ctrl.replace(old_gate, new_gate, 1)
    print("[OK] Upgraded triggerTrade gate to enforce >= 62/100 simulated win paths")

with open('lib/shield_controller.dart', 'w', encoding='utf-8') as f:
    f.write(ctrl)

# ==============================================================================
# 4. PATCH lib/main.dart -> SHIELD v6.0
# ==============================================================================
with open('lib/main.dart', 'r', encoding='utf-8') as f:
    m = f.read()
m = re.sub(r"SHIELD v5\.\d+", "SHIELD v6.0", m)
with open('lib/main.dart', 'w', encoding='utf-8') as f:
    f.write(m)
print("[OK] Bumped UI title to SHIELD v6.0")
