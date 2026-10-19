// ============================================================================
// PROJECT SHIELD — lib/shield_crypto_risk.dart            (NEW, pure Dart)
// ============================================================================

import 'dart:math' as math;

import 'shield_core.dart';

typedef CryptoBar = ({DateTime t, double o, double h, double l, double c, double v, double? vw});

enum AssetClass { equity, crypto }

AssetClass assetClassOf(String sym) => isCryptoSymbol(sym) ? AssetClass.crypto : AssetClass.equity;

abstract final class CryptoRules {
  // Sizing: the smallest of three limits wins.
  static const int riskBudgetBps = 50; // lose <= 0.50% of equity if the stop-limit fills, fees included
  static const int assetCapBps = 600; // 6% of equity in any one coin
  static const int classCapBps = 1500; // 15% across ALL crypto (one correlated cluster, rho ~0.8+)

  // Entry gates.
  static const double maxSpread = 75.0; // bps; calibrated for Alpaca crypto books
  static const double regimeSkipRatio = 2.5; // fast/slow vol above this => skip entry
  static const double minRiskScale = 0.4; // floor for the "hot regime" risk haircut
  static const int maxDriftOfStopBps = 500; // 5% of stop distance (equities: 20%)

  // Stop construction.
  static const double kAtr = 3.0; // x ATR(14) on 1h bars
  static const double kSigma = 2.5; // x sigma scaled to the holding horizon
  static const double horizonHours = 6.0;
  static const int stopFloorBps = 150;
  static const int stopCeilBps = 800;
  static const int stopLimitMinOffsetBps = 40; // gap protection under the trigger
  static const double stopLimitAtrFrac = 0.25;

  // Alpaca crypto fees, Tier 1 (0-100k 30d volume). Charged on what you RECEIVE.
  static const int takerFeeBps = 25;
  static const int makerFeeBps = 15;

  // Trailing.
  static const double breakevenAtR = 1.0;
  static const int breakevenLockBps = 60; // > buy fee 25 + sell fee 25 + cushion
  static const double trailStartR = 2.0;
  static const double trailKStart = 3.0; // ATR multiples
  static const double trailKMin = 1.5;
  static const double minStepAtrFrac = 0.25; // hysteresis: don't PATCH for crumbs
  static const int minStopGapBps = 10; // a sell stop must sit below the market
  static const double refTargetR = 3.0; // display-only target; no TP order exists
}

double? wilderAtr(List<CryptoBar> bars, {int period = 14}) {
  if (bars.length < period + 1) return null;
  double tr(int i) {
    final b = bars[i];
    final pc = bars[i - 1].c;
    return math.max(b.h - b.l, math.max((b.h - pc).abs(), (b.l - pc).abs()));
  }

  var atr = 0.0;
  for (var i = 1; i <= period; i++) {
    atr += tr(i);
  }
  atr /= period;
  for (var i = period + 1; i < bars.length; i++) {
    atr = (atr * (period - 1) + tr(i)) / period;
  }
  return atr;
}

double? ewmaVol(List<CryptoBar> bars, {double lambda = 0.94}) {
  if (bars.length < 31) return null;
  final r = <double>[];
  for (var i = 1; i < bars.length; i++) {
    final a = bars[i - 1].c, b = bars[i].c;
    if (a <= 0 || b <= 0) continue;
    r.add(math.log(b / a));
  }
  if (r.length < 30) return null;
  var v = 0.0;
  for (var i = 0; i < 20; i++) {
    v += r[i] * r[i];
  }
  v /= 20;
  for (var i = 20; i < r.length; i++) {
    v = lambda * v + (1 - lambda) * r[i] * r[i];
  }
  return math.sqrt(v);
}

class CryptoVol {
  const CryptoVol({
    required this.lastPrice,
    required this.atr1hPct,
    required this.sigmaHorizonPct,
    required this.regimeRatio,
  });
  final double lastPrice;
  final double atr1hPct;
  final double sigmaHorizonPct;
  final double regimeRatio;
}

CryptoVol? estimateCryptoVol({
  required List<CryptoBar> hourBars,
  required List<CryptoBar> quarterBars,
  double horizonHours = CryptoRules.horizonHours,
}) {
  if (hourBars.isEmpty) return null;
  final atr = wilderAtr(hourBars);
  final fast = ewmaVol(quarterBars, lambda: 0.94);
  final slow = ewmaVol(quarterBars, lambda: 0.99);
  final px = hourBars.last.c;
  if (atr == null || fast == null || slow == null || slow <= 0 || px <= 0) return null;
  final sigmaBar = math.max(fast, slow);
  final barsInHorizon = 4.0 * horizonHours;
  return CryptoVol(
    lastPrice: px,
    atr1hPct: atr / px,
    sigmaHorizonPct: sigmaBar * math.sqrt(barsInHorizon),
    regimeRatio: fast / slow,
  );
}

Pips stopLimitFor(Pips stop, double atrPct) {
  final offBps = math.max(CryptoRules.stopLimitMinOffsetBps, (atrPct * kBpsDenom * CryptoRules.stopLimitAtrFrac).round());
  return roundToTick(stop * (kBpsDenom - offBps) ~/ kBpsDenom);
}

String floorQtyText(double q, {int dp = 6}) {
  final scale = math.pow(10, dp).toInt();
  final units = (q * scale + 1e-9).floor();
  return '${units ~/ scale}.${(units % scale).toString().padLeft(dp, '0')}';
}

Pips cryptoGrossPips(List<Position> positions, List<Map<String, dynamic>> rawOrders) {
  var total = 0;
  for (final p in positions) {
    if (isCryptoSymbol(p.symbol)) total += p.marketValue.abs();
  }
  for (final o in rawOrders) {
    if ('${o['side']}' != 'buy' || !isCryptoSymbol('${o['symbol']}')) continue;
    final qty = double.tryParse('${o['qty']}') ?? 0.0;
    final px = tryPips(o['limit_price']);
    if (px == null || qty <= 0) continue;
    total += (qty * px).round();
  }
  return total;
}

class CryptoPlan {
  const CryptoPlan({
    required this.plan,
    required this.stopLimit,
    required this.stopPct,
    required this.atrPct,
    required this.riskScale,
    required this.binding,
    required this.lossPerUnit,
  });

  final BracketPlan plan;
  final Pips stopLimit;
  final double stopPct, atrPct, riskScale;
  final String binding;
  final Pips lossPerUnit;
}

CryptoPlan buildCryptoPlan({
  required Pips equity,
  required Pips ask,
  required CryptoVol vol,
  required Pips cryptoGross,
  Timeframe timeframe = Timeframe.intraday,
}) {
  if (equity <= 0 || ask <= 0) throw const PlanAbort('Equity and entry price must be positive.');
  if (vol.regimeRatio >= CryptoRules.regimeSkipRatio) {
    throw RiskReject('Volatility regime too hot (${vol.regimeRatio.toStringAsFixed(2)}x its 3-day norm). Entry skipped.');
  }
  final scale = vol.regimeRatio > 1.0 ? (1.0 / vol.regimeRatio).clamp(CryptoRules.minRiskScale, 1.0).toDouble() : 1.0;
  final scaleMil = (scale * 1000).round();

  var stopPct = math.max(CryptoRules.kAtr * vol.atr1hPct, CryptoRules.kSigma * vol.sigmaHorizonPct);
  stopPct = stopPct.clamp(CryptoRules.stopFloorBps / kBpsDenom, CryptoRules.stopCeilBps / kBpsDenom).toDouble();
  final stopBps = (stopPct * kBpsDenom).round();

  final collar = limitCollarPrice(ask);
  final stop = roundToTick(ask * (kBpsDenom - stopBps) ~/ kBpsDenom);
  final stopLimit = stopLimitFor(stop, vol.atr1hPct);

  final keep = (kBpsDenom - CryptoRules.takerFeeBps) * (kBpsDenom - CryptoRules.takerFeeBps);
  final proceeds = stopLimit * keep ~/ (kBpsDenom * kBpsDenom);
  final lossPerUnit = collar - proceeds;
  if (lossPerUnit <= 0) throw const PlanAbort('Invalid crypto stop.');

  final riskPips = equity * CryptoRules.riskBudgetBps * scaleMil ~/ (kBpsDenom * 1000);
  final riskUnits = riskPips * kQtyScale ~/ lossPerUnit;
  final assetUnits = (equity * CryptoRules.assetCapBps ~/ kBpsDenom) * kQtyScale ~/ collar;
  final room = equity * CryptoRules.classCapBps ~/ kBpsDenom - cryptoGross;
  final classUnits = room <= 0 ? 0 : room * kQtyScale ~/ collar;
  final units = [riskUnits, assetUnits, classUnits].reduce(math.min);
  final binding = units == classUnits ? 'crypto-class cap' : (units == assetUnits ? 'per-coin cap' : 'risk budget');
  if (units < 1) throw RiskReject('Crypto limits leave no room for $binding.');

  final qty = units / kQtyScale;
  final risk = ask - stop;
  final plan = BracketPlan(
    timeframe: timeframe,
    equity: equity,
    entry: ask,
    limitCollar: collar,
    stopLoss: stop,
    takeProfit: ask + (risk * CryptoRules.refTargetR).round(),
    shares: math.max(1, qty.round()),
    qty: qty,
  );
  return CryptoPlan(
    plan: plan,
    stopLimit: stopLimit,
    stopPct: stopPct,
    atrPct: vol.atr1hPct,
    riskScale: scale,
    binding: binding,
    lossPerUnit: lossPerUnit,
  );
}

class TrailState {
  TrailState({
    required this.symbol,
    required this.entry,
    required this.initialStop,
    required this.stop,
    required this.stopLimit,
    required this.highWater,
    required this.atrPct,
    required this.createdAt,
    this.kStart = CryptoRules.trailKStart,
    this.armed = false,
    this.orderId,
  });

  final String symbol;
  Pips entry, initialStop, stop, stopLimit, highWater;
  double atrPct, kStart;
  bool armed;
  String? orderId;
  final DateTime createdAt;

  factory TrailState.fromPlan(String sym, CryptoPlan cp, DateTime now) => TrailState(
        symbol: sym,
        entry: cp.plan.entry,
        initialStop: cp.plan.stopLoss,
        stop: cp.plan.stopLoss,
        stopLimit: cp.stopLimit,
        highWater: cp.plan.entry,
        atrPct: cp.atrPct,
        createdAt: now,
      );

  Map<String, dynamic> toJson() => {
        's': symbol,
        'e': entry,
        'i': initialStop,
        'st': stop,
        'sl': stopLimit,
        'hw': highWater,
        'a': atrPct,
        'k': kStart,
        'ar': armed,
        'o': orderId,
        't': createdAt.toIso8601String(),
      };

  factory TrailState.fromJson(Map<String, dynamic> j) => TrailState(
        symbol: j['s'] as String,
        entry: j['e'] as int,
        initialStop: j['i'] as int,
        stop: j['st'] as int,
        stopLimit: j['sl'] as int,
        highWater: j['hw'] as int,
        atrPct: (j['a'] as num).toDouble(),
        kStart: (j['k'] as num?)?.toDouble() ?? CryptoRules.trailKStart,
        armed: j['ar'] == true,
        orderId: j['o'] as String?,
        createdAt: DateTime.parse(j['t'] as String),
      );
}

double trailMultiple(double r, double kStart) {
  if (r < 3.0) return kStart;
  if (r < 5.0) return math.max(CryptoRules.trailKMin, kStart - 0.5);
  return math.max(CryptoRules.trailKMin, kStart - 1.0);
}

typedef Ratchet = ({Pips stop, Pips stopLimit, bool moved, double r, String note});

Ratchet ratchetStop(TrailState s, Pips last) {
  if (last > s.highWater) s.highWater = last;
  final risk = s.entry - s.initialStop;
  if (risk <= 0) return (stop: s.stop, stopLimit: s.stopLimit, moved: false, r: 0.0, note: 'invalid initial risk');

  final r = (s.highWater - s.entry) / risk;
  final atrPips = (s.highWater * s.atrPct).round();
  var cand = s.stop;
  var note = 'hold';

  if (r >= CryptoRules.breakevenAtR) {
    final be = s.entry * (kBpsDenom + CryptoRules.breakevenLockBps) ~/ kBpsDenom;
    if (be > cand) {
      cand = be;
      note = 'break-even lock';
    }
  }
  if (r >= CryptoRules.trailStartR) {
    final k = trailMultiple(r, s.kStart);
    final chandelier = s.highWater - (atrPips * k).round();
    if (chandelier > cand) {
      cand = chandelier;
      note = 'chandelier ${k.toStringAsFixed(1)}xATR';
    }
  }

  final ceiling = last * (kBpsDenom - CryptoRules.minStopGapBps) ~/ kBpsDenom;
  if (cand > ceiling) cand = ceiling;
  cand = roundToTick(cand);

  final minStep = math.max(1, (atrPips * CryptoRules.minStepAtrFrac).round());
  if (cand - s.stop < minStep) return (stop: s.stop, stopLimit: s.stopLimit, moved: false, r: r, note: note);
  return (stop: cand, stopLimit: stopLimitFor(cand, s.atrPct), moved: true, r: r, note: note);
}
