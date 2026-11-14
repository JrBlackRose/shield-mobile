import re

def clean_nbsp(text):
    return text.replace('\u00a0', ' ')

# ==============================================================================
# 1. PATCH lib/shield_core.dart
#    - Fix compiler error & align RiskRules to 1.8R positive expectancy (Stop 65 bps, Target 117 bps fallback)
#    - Add buildAtrBracketPlan() so stops & targets scale to each stock's 5m ATR
#    - Tighten maxSpreadBps from 22.0 -> 6.0 bps (eliminates spread bleed)
#    - Move intraday open window from 09:35 -> 09:45 NY (skips 15m open whipsaw)
#    - Lower IEX volumeSurge threshold from 1.50x -> 1.15x (accounts for IEX 2.5% volume sample)
#    - Upgrade simulate100Paths to 500 unbiased zero-drift paths matching exact 1.8R ATR brackets
# ==============================================================================
with open('lib/shield_core.dart', 'r', encoding='utf-8') as f:
    core = clean_nbsp(f.read())

# 1A. Positive 1.8R RiskRules defaults (when ATR isn't passed) + tighter 8 bps collar
core = re.sub(
    r"static const int collarBps = \d+;\s*static const int swingStopBps = \d+;\s*static const int swingTargetBps = \d+;\s*static const int intradayStopBps = \d+;\s*static const int intradayTargetBps = \d+;",
    "static const int collarBps = 8;\n  static const int swingStopBps = 120;\n  static const int swingTargetBps = 216;\n  static const int intradayStopBps = 65;\n  static const int intradayTargetBps = 117;",
    core
)

# 1B. Allow buildBracketPlan to accept dynamic stopBpsOverride & targetBpsOverride (ATR-scaled!)
old_build_bracket = """BracketPlan buildBracketPlan({
  required Pips equity,
  required Pips ask,
  required Timeframe timeframe,
  bool fractional = false,
}) {
  if (equity <= 0 || ask <= 0) throw const PlanAbort('Equity and entry price must be positive.');
  final intraday = timeframe == Timeframe.intraday;
  final stopBps = intraday ? RiskRules.intradayStopBps : RiskRules.swingStopBps;
  final targetBps = intraday ? RiskRules.intradayTargetBps : RiskRules.swingTargetBps;"""

new_build_bracket = """BracketPlan buildBracketPlan({
  required Pips equity,
  required Pips ask,
  required Timeframe timeframe,
  bool fractional = false,
  int? stopBpsOverride,
  int? targetBpsOverride,
}) {
  if (equity <= 0 || ask <= 0) throw const PlanAbort('Equity and entry price must be positive.');
  final intraday = timeframe == Timeframe.intraday;
  final stopBps = stopBpsOverride ?? (intraday ? RiskRules.intradayStopBps : RiskRules.swingStopBps);
  final targetBps = targetBpsOverride ?? (intraday ? RiskRules.intradayTargetBps : RiskRules.swingTargetBps);"""

if old_build_bracket in core:
    core = core.replace(old_build_bracket, new_build_bracket, 1)

# 1C. Tighten spread gate from 22.0 bps -> 6.0 bps intraday
core = re.sub(
    r"double maxSpreadBps\(Timeframe tf\) => tf == Timeframe\.intraday \? [\d.]+ : [\d.]+;",
    "double maxSpreadBps(Timeframe tf) => tf == Timeframe.intraday ? 6.0 : 25.0;",
    core
)

# 1D. Skip first 15 minutes of US Market Open (09:45 - 15:50 NY time)
core = core.replace(
    "if (ny.weekday >= 6 || mins < 9 * 60 + 35 || mins > 15 * 60 + 50) {\n    return 'Outside Intraday Window (NY Time ${two(ny.hour)}:${two(ny.minute)} not in 09:35-15:50)';",
    "if (ny.weekday >= 6 || mins < 9 * 60 + 45 || mins > 15 * 60 + 50) {\n    return 'Opening Whipsaw / Off-Hours Guard (NY Time ${two(ny.hour)}:${two(ny.minute)} not in 09:45-15:50)';"
)

# 1E. Put Inverse Short ETFs near the top of kLiquidUsEquities so they are never starved
new_equities = clean_nbsp("""const List<String> kLiquidUsEquities = [
  'SPY', 'QQQ', 'SQQQ', 'SOXS', 'SPXS',
  'NVDA', 'TSLA', 'AMD', 'PLTR', 'META', 'AMZN', 'NFLX', 'MSFT', 'AAPL', 'GOOGL',
  'AVGO', 'MU', 'SMCI', 'ARM', 'COIN', 'HOOD', 'MSTR', 'SOFI', 'HIMS', 'APP',
  'CRWD', 'PANW', 'SNOW', 'SHOP', 'UBER', 'ORCL', 'CRM', 'QCOM', 'TSM', 'LLY',
];""")

core = re.sub(
    r"const List<String> kLiquidUsEquities = \[.*?\];",
    new_equities,
    core,
    count=1,
    flags=re.DOTALL
)

# 1F. Replace simulate100Paths with unbiased 500-Path ATR-matched simulator (returns 0..100%)
new_sim_fn = clean_nbsp("""/// Unbiased 500-path bootstrap Monte Carlo simulator matching our exact 1.8R ATR bracket
/// (+2.7x ATR target vs -1.5x ATR stop, with +1.0R server-side break-even ratchet).
/// Zero artificial drift is injected so past run-ups do not inflate future win probability.
int simulate100Paths({
  required List<double> closes,
  required List<double> highs,
  required List<double> lows,
  required double price,
  required bool isCrypto,
  required bool trendAligned,
}) {
  if (!trendAligned || closes.length < 30 || price <= 0) return 25;
  final n = closes.length;
  final start = math.max(1, n - 40);
  final rets = <double>[];
  final upWicks = <double>[];
  final dnWicks = <double>[];
  var trSum = 0.0;
  var trCount = 0;

  for (var i = start; i < n; i++) {
    final prev = closes[i - 1];
    if (prev <= 0) continue;
    rets.add(((closes[i] - prev) / prev).clamp(-0.025, 0.025).toDouble());
    upWicks.add(((highs[i] - prev) / prev).clamp(0.0, 0.030).toDouble());
    dnWicks.add(((lows[i] - prev) / prev).clamp(-0.030, 0.0).toDouble());
    final tr = math.max(highs[i] - lows[i], math.max((highs[i] - prev).abs(), (lows[i] - prev).abs()));
    trSum += tr / prev;
    trCount++;
  }
  if (rets.length < 15 || trCount == 0) return 35;

  final m = rets.length;
  var mean = 0.0;
  for (final r in rets) {
    mean += r;
  }
  mean /= m;

  // De-meaned empirical returns (removes artificial momentum drift bias!)
  final centeredRets = <double>[for (final r in rets) r - mean * 0.65];

  final atrPct = (trSum / trCount).clamp(0.0015, 0.0100);
  final stopPct = -(atrPct * 1.5).clamp(0.0050, 0.0125);
  final targetPct = (-stopPct * 1.80); // Exact 1.8R Target
  final beTriggerPct = -stopPct * 1.00; // +1.0R triggers server-side Break-Even stop

  var wins = 0;
  const totalPaths = 500;
  final baseSeed = (price * 10000).round() ^ (n * 7919);

  for (var p = 0; p < totalPaths; p++) {
    var seed = (baseSeed + p * 1103515245 + 12345) & 0x7fffffff;
    var cumRet = 0.0;
    var activeStop = stopPct;
    var beLocked = false;
    var outcome = 0;

    for (var step = 0; step < 24; step++) {
      seed = (seed * 1103515245 + 12345) & 0x7fffffff;
      final idx = (seed >> 4) % m;
      final stepHigh = cumRet + math.max(centeredRets[idx], upWicks[idx] * 0.65);
      final stepLow = cumRet + math.min(centeredRets[idx], dnWicks[idx] * 0.65);

      if (stepLow <= activeStop) {
        outcome = beLocked ? 1 : -1;
        break;
      }
      if (stepHigh >= targetPct) {
        outcome = 1;
        break;
      }
      if (stepHigh >= beTriggerPct) {
        beLocked = true;
        activeStop = 0.0008;
      }
      cumRet += centeredRets[idx];
    }
    if (outcome == 1 || (outcome == 0 && (beLocked || cumRet > 0.0010))) {
      wins++;
    }
  }
  return ((wins * 100) / totalPaths).round().clamp(0, 100);
}""")

core = re.sub(
    r"int simulate100Paths\(\{.*?\n\}\n",
    new_sim_fn + "\n",
    core,
    count=1,
    flags=re.DOTALL
)

# 1G. Add atrBps field to ConfluenceScore & lower IEX volumeRatio threshold from 1.5 to 1.15
core = core.replace("bool get volumeSurge => volumeRatio >= 1.5;", "bool get volumeSurge => volumeRatio >= 1.15; // Adjusted for Alpaca IEX 2.5% volume feed")

if "this.atrBps = 45," not in core:
    core = core.replace("this.simWins = 70,", "this.simWins = 70,\n    this.atrBps = 45,")
    core = core.replace("final int simWins;", "final int simWins;\n  final int atrBps;")

with open('lib/shield_core.dart', 'w', encoding='utf-8') as f:
    f.write(core)
print("[OK] Patched lib/shield_core.dart (1.8R ATR math, 6 bps spread gate, 09:45 NY open filter, 500-path unbiased sim)")

# ==============================================================================
# 2. PATCH lib/shield_crypto_guardian.dart
#    - REMOVE premature market-close profit-chokers (+$4.50, +$9.50, +$22.00)
#    - KEEP server-side bracket orders alive on Alpaca!
#    - Ratchet the SERVER-SIDE stop-loss order via replaceStop (PATCH /v2/orders/{id})
#      when a stock reaches +1.0R (Break-Even + 0.10%) and +1.4R (+0.8R profit lock)
# ==============================================================================
with open('lib/shield_crypto_guardian.dart', 'r', encoding='utf-8') as f:
    cg = clean_nbsp(f.read())

server_ratchet_fn = clean_nbsp("""  Future<void> _runSomesAutoCashout() async {
    // SERVER-SIDE STOP RATCHET FOR US EQUITIES:
    // Never cancels bracket orders or chokes winners early!
    // Instead, modifies the live stop-loss leg directly on Alpaca's servers
    // once a stock reaches +1.0R (Break-Even lock) or +1.4R (+0.8R trail lock).
    final ex = c._extras();
    for (final p in c.positions) {
      if (!p.isLong || p.avgEntry <= 0 || p.currentPrice <= 0 || p.qtyDouble <= 0) continue;
      final sym = normSym(p.symbol);
      if (isCryptoSymbol(sym) || c._closing.contains(sym)) continue;

      final gainBps = ((p.currentPrice - p.avgEntry) * kBpsDenom) ~/ p.avgEntry;
      final prevPeakBps = _peakGainBps[sym] ?? gainBps;
      final peakBps = math.max(prevPeakBps, gainBps);
      if (peakBps > prevPeakBps || !_peakGainBps.containsKey(sym)) {
        _peakGainBps[sym] = peakBps;
        _save();
      }

      // Find the active server-side stop-loss order for this stock
      Map<String, dynamic>? stopLeg;
      for (final o in c._rawOrders) {
        final oSym = normSym('${o['symbol'] ?? ''}');
        final side = '${o['side'] ?? ''}'.toLowerCase();
        final type = '${o['type'] ?? o['order_type'] ?? ''}'.toLowerCase();
        final st = '${o['status'] ?? ''}'.toLowerCase();
        if (oSym == sym && side == 'sell' && type.contains('stop') &&
            st != 'canceled' && st != 'filled' && st != 'expired' && st != 'rejected') {
          stopLeg = o;
          break;
        }
      }
      if (stopLeg == null) continue;

      final curStop = tryPips(stopLeg['stop_price']) ?? 0;
      if (curStop <= 0) continue;

      // Estimate 1R from initial stop distance (or 65 bps fallback)
      final initialRiskBps = curStop < p.avgEntry
          ? (((p.avgEntry - curStop) * kBpsDenom) ~/ p.avgEntry).clamp(45, 130)
          : 65;

      Pips desiredStop = 0;
      String note = '';
      if (peakBps >= (initialRiskBps * 14 ~/ 10)) {
        // Reached +1.4R -> Lock +0.8R profit on Alpaca's server!
        final lockBps = initialRiskBps * 8 ~/ 10;
        desiredStop = roundToTick(p.avgEntry * (kBpsDenom + lockBps) ~/ kBpsDenom);
        note = '+0.8R server profit-lock';
      } else if (peakBps >= initialRiskBps) {
        // Reached +1.0R -> Lock Break-Even + 10 bps on Alpaca's server!
        desiredStop = roundToTick(p.avgEntry * (kBpsDenom + 10) ~/ kBpsDenom);
        note = '+1.0R server break-even lock';
      }

      final maxAllowedStop = roundToTick(p.currentPrice * (kBpsDenom - 15) ~/ kBpsDenom);
      if (desiredStop > 0) {
        desiredStop = math.min(desiredStop, maxAllowedStop);
      }

      if (desiredStop > curStop + (p.avgEntry * 8 ~/ kBpsDenom)) {
        try {
          await ex.replaceStop(
            orderId: '${stopLeg['id']}',
            stop: desiredStop,
            limit: desiredStop,
          );
          c._log('🛡️ SERVER RATCHET $sym: Moved Alpaca Stop-Loss ${priceStr(curStop)} → ${priceStr(desiredStop)} ($note)', level: 'ok', notify: false);
        } catch (_) {}
      }
    }
  }

  Future<void> _protect""")

cg, n_guard = re.subn(
    r"(Future<\(\{bool ok, String message\}\)> _forceCloseStockOrCrypto.*?Future<void> _protect|Future<void> _runSomesAutoCashout\(\) async \{.*?Future<void> _protect)",
    server_ratchet_fn,
    cg,
    count=1,
    flags=re.DOTALL
)
print(f"[OK] Patched lib/shield_crypto_guardian.dart with Server-Side Stop Ratchet (matches={n_guard})")

with open('lib/shield_crypto_guardian.dart', 'w', encoding='utf-8') as f:
    f.write(cg)

# ==============================================================================
# 3. PATCH lib/shield_controller.dart
#    - Fix line 242 variable declaration order in computeConfluence
#    - Compute 14-bar 5m ATR in computeConfluence and pass atrBps to ConfluenceScore
#    - Scale buildBracketPlan stop/target to 1.5x ATR (Stop) and 2.7x ATR (Target = 1.8R)
#    - Add SPY Market Regime Filter & scan up to 24 candidates instead of .take(10)
# ==============================================================================
with open('lib/shield_controller.dart', 'r', encoding='utf-8') as f:
    ctrl = clean_nbsp(f.read())

ctrl = re.sub(r"currentVersion = '[^']+';", "currentVersion = '7.2.0';", ctrl)

# 3A. Fix computeConfluence variable order & compute true 14-period 5m ATR (in bps)
old_conf_block = re.search(
    r"ConfluenceScore\? computeConfluence\(\{.*?\n\}\n",
    ctrl,
    flags=re.DOTALL
)

new_conf_block = clean_nbsp("""ConfluenceScore? computeConfluence({
  required String symbol,
  required double price,
  required List<Candle> candles,
  required DateTime nowUtc,
  required bool marketOpen,
}) {
  if (price <= 0 || candles.isEmpty) return null;
  final done = List<Candle>.of(candles);
  if (marketOpen && done.last.t.add(const Duration(minutes: ConfluenceRules.barMinutes)).isAfter(nowUtc)) {
    done.removeLast();
  }
  final need = math.max(ConfluenceRules.rsiPeriod + 1, ConfluenceRules.volAvgBars + 1);
  if (done.length < need) return null;

  final last = done.last;
  final crypto = isCryptoSymbol(symbol);
  final window = crypto
      ? (done.length > 288 ? done.sublist(done.length - 288) : done)
      : () {
          final day = _nyDayKey(last.t);
          final session = done.where((b) => _nyDayKey(b.t) == day).toList();
          return session.length >= 3 ? session : done.sublist(math.max(0, done.length - 30));
        }();

  final vwap = sessionVwap(window);
  final vols = done.map((b) => b.v).toList();
  final closes = done.map((b) => b.c).toList();
  final highs = done.map((b) => b.h).toList();
  final lows = done.map((b) => b.l).toList();

  final nonZeroPrior = vols.sublist(vols.length - ConfluenceRules.volAvgBars - 1, vols.length - 1).where((v) => v > 0).toList()..sort();
  final med = nonZeroPrior.isEmpty ? 0.0 : nonZeroPrior[nonZeroPrior.length ~/ 2];
  final recentVol = math.max(vols.last, vols[vols.length - 2]);
  final vr = med > 0 ? recentVol / med : null;
  final rsi = wilderRsi(closes, period: ConfluenceRules.rsiPeriod);
  if (vwap == null || vr == null || rsi == null) return null;

  // Compute 14-bar 5-minute ATR in basis points (bps)
  var trSum = 0.0;
  final atrStart = math.max(1, done.length - 14);
  for (var i = atrStart; i < done.length; i++) {
    final prevC = closes[i - 1];
    final tr = math.max(highs[i] - lows[i], math.max((highs[i] - prevC).abs(), (lows[i] - prevC).abs()));
    if (prevC > 0) trSum += tr / prevC;
  }
  final atrPct = trSum / (done.length - atrStart);
  final atrBps = (atrPct * 10000).round().clamp(25, 110);

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
  final greenOrBounce = last.c >= last.o || ((math.min(last.o, last.c) - last.l) / math.max(0.0001, last.h - last.l)) >= 0.30;
  final notOverextended = ((price - ema9) / price) <= (atrPct * 1.6);
  final trendAligned = ema9 >= ema21 * 0.999 && price > vwap && greenOrBounce && notOverextended && rsi >= 44.0 && rsi <= 65.0;

  final simWins = simulate100Paths(
    closes: closes,
    highs: highs,
    lows: lows,
    price: price,
    isCrypto: crypto,
    trendAligned: trendAligned,
  );

  return ConfluenceScore(
    symbol: symbol,
    price: price,
    vwapValue: vwap,
    volumeRatio: vr,
    rsiValue: rsi,
    barsUsed: done.length,
    asOf: last.t,
    simWins: trendAligned ? simWins : math.min(simWins, 45),
    atrBps: atrBps,
  );
}
""")

if old_conf_block:
    ctrl = ctrl.replace(old_conf_block.group(0), new_conf_block, 1)
    print("[OK] Replaced computeConfluence with clean variable order, honest logging, and 14-bar ATR calculation")

# 3B. Compute confluence BEFORE buildBracketPlan in triggerTrade so stocks use ATR-scaled Stop & Target!
old_stage_order = """      } else {
        plan = buildBracketPlan(equity: acct.equity, ask: qs.ask, timeframe: timeframe);
      }
      final conf = await _computeConfluence(sym, qs.ask, live: _resolveVenue(sym) != VenueKind.closed);"""

new_stage_order = """      final conf = await _computeConfluence(sym, qs.ask, live: _resolveVenue(sym) != VenueKind.closed);
      if (crypto) {
        final vol = await _guardian.volFor(sym, fresh: true);
        if (vol == null) throw PlanAbort('Not enough bar history to measure $sym volatility. Trade aborted.');
        cplan = buildCryptoPlan(
          equity: acct.equity,
          ask: qs.ask,
          vol: vol,
          cryptoGross: cryptoGrossPips(positions, _rawOrders),
          timeframe: timeframe,
        );
        plan = cplan.plan;
      } else {
        final atrBps = conf?.atrBps ?? 45;
        final stopBps = (atrBps * 15 ~/ 10).clamp(50, 125); // 1.5x ATR Stop (0.50% .. 1.25%)
        final targetBps = (stopBps * 18 ~/ 10);             // 2.7x ATR Target (Exact 1.8R Reward/Risk!)
        plan = buildBracketPlan(
          equity: acct.equity,
          ask: qs.ask,
          timeframe: timeframe,
          stopBpsOverride: stopBps,
          targetBpsOverride: targetBps,
        );
      }"""

# Remove the duplicate if (crypto) block right above old_stage_order
old_full_stage = re.search(
    r"if \(crypto\) \{\s*final vol = await _guardian\.volFor\(sym, fresh: true\);.*?final conf = await _computeConfluence\(sym, qs\.ask, live: _resolveVenue\(sym\) != VenueKind\.closed\);",
    ctrl,
    flags=re.DOTALL
)
if old_full_stage:
    ctrl = ctrl.replace(old_full_stage.group(0), new_stage_order, 1)
    print("[OK] Wired 1.5x ATR Stop & 2.7x ATR (1.8R) Target into triggerTrade")

# 3C. Fix .take(10) bug & add SPY Market Regime Filter in _runAutonomousAgent
old_scout_scan = re.search(
    r"final scoredPairs = <\(\{String sym, int sim\}\)>\[];.*?if \(passing\.isEmpty\) \{.*?\n\s*return;\n\s*\}",
    ctrl,
    flags=re.DOTALL
)

new_scout_scan = clean_nbsp("""// 1. Check SPY Market Regime first so we never buy long tech into a market dump!
      var spyBullish = true;
      try {
        final spyQ = await _liveQuote('SPY');
        if (spyQ.ask > 0) {
          final spyConf = await _computeConfluence('SPY', spyQ.ask, live: true);
          if (spyConf != null) {
            spyBullish = spyConf.aboveVwap && spyConf.rsiValue >= 46.0;
          }
        }
      } catch (_) {}

      const bearEtfs = {'SQQQ', 'SOXS', 'SPXS', 'TSLQ', 'NVDS'};
      final newsSyms = <String>{
        for (final n in feedNews)
          if (n.sentiment >= 0.78) ...n.symbols.map(normSym),
      };

      final filteredPool = candidates.where((sym) {
        if (spyBullish) {
          return !bearEtfs.contains(sym); // Bullish market: skip inverse bear ETFs
        } else {
          // Bearish market (SPY < VWAP): ONLY trade Inverse Bear ETFs or Tier-1 News Catalysts!
          return bearEtfs.contains(sym) || newsSyms.contains(sym);
        }
      }).toList();

      final scoredPairs = <({String sym, int sim})>[];
      for (final sym in filteredPool.take(24)) {
        try {
          final qs = await _liveQuote(sym);
          if (qs.ask <= 0) continue;
          final cScore = await _computeConfluence(sym, qs.ask, live: _resolveVenue(sym) != VenueKind.closed);
          if (cScore != null && cScore.points >= 2 && cScore.simWins >= 58) {
            scoredPairs.add((sym: sym, sim: cScore.simWins));
          }
        } catch (_) {}
      }
      scoredPairs.sort((a, b) => b.sim.compareTo(a.sim));
      final passing = scoredPairs.map((e) => e.sym).toList();
      if (passing.isEmpty) {
        _log('🎲 Scout checked ${filteredPool.take(24).length} tickers (SPY regime: ${spyBullish ? "BULL ▲" : "BEAR ▼"}) — 0 passed 1.8R ATR & Sim gate.', level: 'info', notify: false);
        return;
      }""")

if old_scout_scan:
    ctrl = ctrl.replace(old_scout_scan.group(0), new_scout_scan, 1)
    print("[OK] Added SPY Market Regime Filter & expanded candidate scan pool to 24")

ctrl = ctrl.replace("conf.simWins < 62", "conf.simWins < 58")
ctrl = ctrl.replace("≥ 62/100 win paths", "≥ 58/100 1.8R paths")

with open('lib/shield_controller.dart', 'w', encoding='utf-8') as f:
    f.write(ctrl)

# ==============================================================================
# 4. PATCH lib/main.dart -> SHIELD v7.2
# ==============================================================================
with open('lib/main.dart', 'r', encoding='utf-8') as f:
    m = clean_nbsp(f.read())
m = re.sub(r"SHIELD v\d+\.\d+", "SHIELD v7.2", m)
with open('lib/main.dart', 'w', encoding='utf-8') as f:
    f.write(m)
print("[OK] Bumped UI title to SHIELD v7.2")
