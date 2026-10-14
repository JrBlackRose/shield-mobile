// ============================================================================
// PROJECT SHIELD v3.0 — lib/main.dart  (com.projectshield.trader)
// Standalone BYOK paper-trading cockpit: Alpaca (paper) + optional Gemini.
//
// Sections
//   1. Risk math (integer pips, exact — from the blueprint, unchanged)
//   2. Formatting, time helpers (NY DST without extra packages)
//   3. Models
//   4. Clients: AlpacaClient, GeminiClient
//   5. Watch link (phone-relay abstraction + in-app preview link)
//   6. ShieldController (state, polling, trade / approve / kill flows)
//   7. UI: theme, key vault, dashboard, approval cards, watch HUD
//
// Executive decisions applied: MANUAL default · fail-closed on no ask ·
// price-drift guard on approval · roundToTick · watch phone-relay with loud
// "NOT SENT" · /v2/clock session gate · 5-minute card TTL.
// ============================================================================

import 'dart:async';
import 'dart:convert';
import 'dart:io' show SocketException;
import 'dart:math' as math;

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:http/http.dart' as http;
import 'package:shared_preferences/shared_preferences.dart';

// ============================================================================
// 1. RISK MATH — money is NEVER a double: int "pips" (1 pip = $0.0001).
// ============================================================================

typedef Pips = int;

const int kPipsPerUsd = 10000;
const int kBpsDenom = 10000;

enum Timeframe { swing, intraday }

abstract final class RiskRules {
  static const int riskBudgetBps = 100; // 1.00% of equity risked per trade
  static const int notionalCapBps = 1500; // 15.00% of equity max per position
  static const int collarBps = 15; // +0.15% limit collar on entry
  static const int swingStopBps = 1000; // -10%
  static const int swingTargetBps = 3000; // +30%
  static const int intradayStopBps = 100; // -1%
  static const int intradayTargetBps = 300; // +3%
  static const int maxDriftOfStopBps = 2000; // approval drift <= 20% of stop distance
}

/// A rule safely blocked the trade (Python RuleReject).
class RiskReject implements Exception {
  final String message;
  const RiskReject(this.message);
  @override
  String toString() => message;
}

/// Bad/unusable input — pipeline aborted (Python PipelineAbort).
class PlanAbort implements Exception {
  final String message;
  const PlanAbort(this.message);
  @override
  String toString() => message;
}

/// Alpaca returns money as decimal strings. Parse without touching double.
Pips parsePips(String? raw) {
  if (raw == null || raw.trim().isEmpty) throw const PlanAbort('Missing numeric value.');
  final t = raw.trim();
  final neg = t.startsWith('-');
  final parts = (neg ? t.substring(1) : t).split('.');
  final whole = int.parse(parts[0].isEmpty ? '0' : parts[0]);
  final frac = (parts.length > 1 ? parts[1] : '').padRight(5, '0');
  var fracPips = int.parse(frac.substring(0, 4));
  if (frac.codeUnitAt(4) >= 0x35) fracPips += 1; // half-up on 5th decimal
  final v = whole * kPipsPerUsd + fracPips;
  return neg ? -v : v;
}

/// Wire format for Alpaca price fields: 2 dp at/above $1, 4 dp below $1.
String toApiPrice(Pips p) {
  final usd = p ~/ kPipsPerUsd;
  final rem = p % kPipsPerUsd;
  return p >= kPipsPerUsd
      ? '$usd.${(rem ~/ 100).toString().padLeft(2, '0')}'
      : '$usd.${rem.toString().padLeft(4, '0')}';
}

/// Round-half-up of (num/den) pips to the tick grid.
/// Tick is chosen from the UNROUNDED price: >= $1.00 -> $0.01 ; < $1.00 -> $0.0001.
Pips _roundToTickRatio(int num, int den) {
  final atOrAboveOne = num >= kPipsPerUsd * den;
  final tick = atOrAboveOne ? 100 : 1; // in pips
  final ticks = (2 * num + tick * den) ~/ (2 * tick * den);
  return ticks * tick;
}

Pips roundToTick(Pips price) => _roundToTickRatio(price, 1);

int riskBudgetShares({required Pips equity, required Pips entry, required Pips stop}) {
  final riskPerShare = entry - stop;
  if (riskPerShare <= 0) throw const PlanAbort('Invalid stop loss price.');
  return (equity * RiskRules.riskBudgetBps) ~/ (kBpsDenom * riskPerShare);
}

int notionalCapShares({required Pips equity, required Pips entry}) {
  return (equity * RiskRules.notionalCapBps) ~/ (kBpsDenom * entry);
}

Pips limitCollarPrice(Pips entry) =>
    _roundToTickRatio(entry * (kBpsDenom + RiskRules.collarBps), kBpsDenom);

int positionSize({required Pips equity, required Pips entry, required Pips stop}) {
  final shares = math.min(
    riskBudgetShares(equity: equity, entry: entry, stop: stop),
    notionalCapShares(equity: equity, entry: entry),
  );
  if (shares < 1) {
    throw const RiskReject('0 shares sized. Risk budget or 15% position cap too small for this price.');
  }
  return shares;
}

class BracketPlan {
  final Timeframe timeframe;
  final Pips equity, entry, limitCollar, stopLoss, takeProfit;
  final int shares;
  const BracketPlan({
    required this.timeframe,
    required this.equity,
    required this.entry,
    required this.limitCollar,
    required this.stopLoss,
    required this.takeProfit,
    required this.shares,
  });

  Pips get riskPerShare => entry - stopLoss;
  Pips get capitalRequired => shares * entry;
  Pips get maxRisk => shares * riskPerShare;
  Pips get maxRiskAtCollar => shares * (limitCollar - stopLoss);
  double get rewardRisk => (takeProfit - entry) / riskPerShare;
}

BracketPlan buildBracketPlan({
  required Pips equity,
  required Pips ask,
  required Timeframe timeframe,
}) {
  if (equity <= 0 || ask <= 0) throw const PlanAbort('Equity and entry price must be positive.');
  final intraday = timeframe == Timeframe.intraday;
  final stopBps = intraday ? RiskRules.intradayStopBps : RiskRules.swingStopBps;
  final targetBps = intraday ? RiskRules.intradayTargetBps : RiskRules.swingTargetBps;

  final stop = _roundToTickRatio(ask * (kBpsDenom - stopBps), kBpsDenom);
  final target = _roundToTickRatio(ask * (kBpsDenom + targetBps), kBpsDenom);
  final shares = positionSize(equity: equity, entry: ask, stop: stop);

  return BracketPlan(
    timeframe: timeframe,
    equity: equity,
    entry: ask,
    limitCollar: limitCollarPrice(ask),
    stopLoss: stop,
    takeProfit: target,
    shares: shares,
  );
}

/// Approval-time guard. Returns |drift| in pips, or throws RiskReject if stale.
Pips checkPriceDrift({required Pips stagedEntry, required Pips stopLoss, required Pips currentAsk}) {
  final stopDistance = stagedEntry - stopLoss;
  if (stopDistance <= 0) throw const PlanAbort('Staged plan has an invalid stop distance.');
  if (currentAsk <= 0) throw const PlanAbort('Current ask must be positive.');
  final drift = (currentAsk - stagedEntry).abs();
  if (drift * kBpsDenom > stopDistance * RiskRules.maxDriftOfStopBps) {
    throw const RiskReject('Stale price: live ask moved too far from the approved entry. Re-stage the trade.');
  }
  return drift;
}

({Pips pnl, double pct}) dailyPnl({required Pips equity, required Pips lastEquity}) {
  final base = lastEquity > 0 ? lastEquity : (equity > 0 ? equity : 1);
  final pnl = equity - lastEquity;
  return (pnl: pnl, pct: pnl / base * 100.0);
}

/// Circuit breaker: true => HALT (down >= 2.5% on the day).
bool dailyDrawdownHalted({required Pips equity, required Pips lastEquity}) {
  if (lastEquity <= 0) return false;
  return (equity - lastEquity) * 10000 <= -250 * lastEquity;
}

typedef MicroQuote = ({double mid, double micro, double imbalance, double spreadBps});

MicroQuote calcImbalanceAndMicro(double bidP, double askP, double bidS, double askS) {
  if (bidP <= 0 || askP <= 0 || askP < bidP) {
    return (mid: 0.0, micro: 0.0, imbalance: 0.0, spreadBps: 9999.0);
  }
  final mid = (bidP + askP) / 2.0;
  final spread = askP - bidP;
  final spreadBps = spread / mid * 10000.0;
  final total = bidS + askS;
  if (total <= 0) return (mid: mid, micro: mid, imbalance: 0.0, spreadBps: spreadBps);
  final imb = (bidS - askS) / total;
  return (mid: mid, micro: mid + 0.5 * imb * spread, imbalance: imb, spreadBps: spreadBps);
}

double _tanh(double x) {
  if (x > 20) return 1.0;
  if (x < -20) return -1.0;
  final e = math.exp(2 * x);
  return (e - 1) / (e + 1);
}

/// D_tape = S_NLP - tanh(Z_OFI / 2). (Helper kept for when a Z_OFI feed is wired — see notes.)
double claimToTapeDivergence(double sNlp, double ofiZ) => sNlp - _tanh(ofiZ / 2.0);

bool pumpAndDumpVeto(double sNlp, double ofiZ) =>
    sNlp > 0.50 && claimToTapeDivergence(sNlp, ofiZ) >= 1.10;

double maxSpreadBps(Timeframe tf) => tf == Timeframe.intraday ? 45.0 : 80.0;
bool spreadAllowed(double spreadBps, Timeframe tf) => spreadBps <= maxSpreadBps(tf);

// ============================================================================
// 2. FORMATTING & TIME
// ============================================================================

const int kPollSec = 5;
const Duration kCardTtl = Duration(minutes: 5); // Decision 7
const String kGeminiModel = 'gemini-2.5-flash'; // config constant: verify still served
const int kFeedMax = 15;
const String kPaperBase = 'https://paper-api.alpaca.markets/v2';
const String kDataBase = 'https://data.alpaca.markets/v2';
final RegExp kTickerPattern = RegExp(r'^[A-Z][A-Z0-9.\-]{0,9}$');

String usd(Pips p, {bool sign = false}) {
  final neg = p < 0;
  final cents = ((neg ? -p : p) + 50) ~/ 100;
  final whole = (cents ~/ 100).toString();
  final buf = StringBuffer();
  for (var i = 0; i < whole.length; i++) {
    if (i > 0 && (whole.length - i) % 3 == 0) buf.write(',');
    buf.write(whole[i]);
  }
  final c = (cents % 100).toString().padLeft(2, '0');
  final s = '\$${buf.toString()}.$c';
  return neg ? '-$s' : (sign ? '+$s' : s);
}

String priceStr(Pips p) => '\$${toApiPrice(p)}';
String two(int n) => n.toString().padLeft(2, '0');
String hms(DateTime d) => '${two(d.hour)}:${two(d.minute)}:${two(d.second)}';
String tfLabel(Timeframe t) => t == Timeframe.swing ? 'SWING' : 'INTRADAY';

DateTime _sundayUtc(int year, int month, int nth, int hourUtc) {
  final first = DateTime.utc(year, month, 1);
  final day = 1 + (DateTime.sunday - first.weekday) % 7 + 7 * (nth - 1);
  return DateTime.utc(year, month, day, hourUtc);
}

/// US DST: 2nd Sunday of March 02:00 EST (07:00Z) → 1st Sunday of Nov 02:00 EDT (06:00Z).
bool isNyDst(DateTime utc) {
  final u = utc.toUtc();
  return !u.isBefore(_sundayUtc(u.year, 3, 2, 7)) && u.isBefore(_sundayUtc(u.year, 11, 1, 6));
}

DateTime nyTime(DateTime utc) => utc.toUtc().add(Duration(hours: isNyDst(utc) ? -4 : -5));
DateTime mytTime(DateTime utc) => utc.toUtc().add(const Duration(hours: 8));

/// Intraday window 09:35–15:50 ET (weekday). Null = inside window.
String? intradayWindowMessage(DateTime utc) {
  final ny = nyTime(utc);
  final mins = ny.hour * 60 + ny.minute;
  if (ny.weekday >= 6 || mins < 9 * 60 + 35 || mins > 15 * 60 + 50) {
    return 'Outside Intraday Window (NY Time ${two(ny.hour)}:${two(ny.minute)} not in 09:35-15:50)';
  }
  return null;
}

String _newToken() {
  final r = math.Random.secure();
  return List.generate(8, (_) => r.nextInt(16).toRadixString(16)).join();
}

Pips? tryPips(dynamic v) {
  if (v == null) return null;
  try {
    return parsePips(v.toString());
  } catch (_) {
    return null;
  }
}

// ============================================================================
// 3. MODELS
// ============================================================================

enum VaultStatus { empty, unvalidated, validating, valid, invalid }

enum ConnectionStatus { disconnected, connecting, connected, authError, networkError }

enum ExecMode { auto, manual }

enum KillState { idle, confirming, executing, done, failed }

enum CardStatus { pending, approving, executed, rejected, expired, stale, failed }

class Vault {
  Vault({this.alpacaKey = '', this.alpacaSecret = '', this.geminiKey = '', this.tgToken = '', this.tgChat = ''});
  String alpacaKey, alpacaSecret, geminiKey, tgToken, tgChat;
  bool get hasRequired => alpacaKey.isNotEmpty && alpacaSecret.isNotEmpty;
}

class ApiException implements Exception {
  ApiException(this.service, this.status, this.message);
  final String service;
  final int status;
  final String message;
  @override
  String toString() {
    if (status == 401) return '$service rejected your keys (401). Re-check Key ID and Secret.';
    if (status == 429) return '$service rate limit hit (429). Slow down and retry shortly.';
    return '$service error $status: $message';
  }
}

class Account {
  Account({
    required this.equity,
    required this.lastEquity,
    required this.buyingPower,
    required this.cash,
    required this.tradingBlocked,
    required this.accountBlocked,
    required this.status,
    required this.currency,
  });
  final Pips equity, lastEquity, buyingPower, cash;
  final bool tradingBlocked, accountBlocked;
  final String status, currency;

  factory Account.fromJson(Map<String, dynamic> j) {
    final eq = parsePips(j['equity']?.toString());
    if (eq <= 0) throw const PlanAbort('Account equity must be greater than zero.');
    return Account(
      equity: eq,
      lastEquity: tryPips(j['last_equity']) ?? eq,
      buyingPower: tryPips(j['buying_power']) ?? 0,
      cash: tryPips(j['cash']) ?? 0,
      tradingBlocked: j['trading_blocked'] == true,
      accountBlocked: j['account_blocked'] == true,
      status: (j['status'] ?? 'UNKNOWN').toString(),
      currency: (j['currency'] ?? 'USD').toString(),
    );
  }

  ({Pips pnl, double pct}) get pnl => dailyPnl(equity: equity, lastEquity: lastEquity);
  bool get drawdownHalted => dailyDrawdownHalted(equity: equity, lastEquity: lastEquity);
  bool get blocked => tradingBlocked || accountBlocked;
}

class Position {
  Position({
    required this.symbol,
    required this.qty,
    required this.isLong,
    required this.avgEntry,
    required this.currentPrice,
    required this.marketValue,
    required this.unrealizedPl,
    required this.unrealizedPlpc,
  });
  final String symbol;
  final int qty;
  final bool isLong;
  final Pips avgEntry, currentPrice, marketValue, unrealizedPl;
  final double unrealizedPlpc;

  factory Position.fromJson(Map<String, dynamic> j) => Position(
        symbol: (j['symbol'] ?? '?').toString(),
        qty: (double.tryParse('${j['qty']}') ?? 0).abs().round(),
        isLong: j['side'] != 'short',
        avgEntry: tryPips(j['avg_entry_price']) ?? 0,
        currentPrice: tryPips(j['current_price']) ?? 0,
        marketValue: tryPips(j['market_value']) ?? 0,
        unrealizedPl: tryPips(j['unrealized_pl']) ?? 0,
        unrealizedPlpc: double.tryParse('${j['unrealized_plpc']}') ?? 0,
      );
}

class OrderSummary {
  OrderSummary({
    required this.id,
    required this.symbol,
    required this.side,
    required this.qty,
    required this.type,
    required this.status,
    this.limitPrice,
    this.stopPrice,
    this.legs = const [],
  });
  final String id, symbol, side, qty, type, status;
  final Pips? limitPrice, stopPrice;
  final List<OrderSummary> legs;

  factory OrderSummary.fromJson(Map<String, dynamic> j) => OrderSummary(
        id: (j['id'] ?? '').toString(),
        symbol: (j['symbol'] ?? '').toString(),
        side: (j['side'] ?? '').toString(),
        qty: (j['qty'] ?? '').toString(),
        type: (j['type'] ?? j['order_type'] ?? '').toString(),
        status: (j['status'] ?? '').toString(),
        limitPrice: tryPips(j['limit_price']),
        stopPrice: tryPips(j['stop_price']),
        legs: (j['legs'] is List)
            ? (j['legs'] as List).whereType<Map>().map((e) => OrderSummary.fromJson(e.cast<String, dynamic>())).toList()
            : const [],
      );

  String get line {
    final px = limitPrice != null
        ? 'Limit ${priceStr(limitPrice!)}'
        : (stopPrice != null ? 'Stop ${priceStr(stopPrice!)}' : 'MKT');
    return '$symbol $side $qty @ $px ($status)';
  }
}

class FeedEvent {
  FeedEvent(this.time, this.message, this.level);
  final DateTime time;
  final String message, level; // info | ok | warn | error
  Map<String, dynamic> toJson() => {'t': time.toIso8601String(), 'm': message, 'l': level};
  factory FeedEvent.fromJson(Map<String, dynamic> j) =>
      FeedEvent(DateTime.parse(j['t'] as String), j['m'] as String, (j['l'] ?? 'info') as String);
  String get text => '[${hms(mytTime(time))}] $message';
}

class PendingCard {
  PendingCard({
    required this.token,
    required this.ticker,
    required this.timeframe,
    required this.shares,
    required this.entry,
    required this.limitCollar,
    required this.stopLoss,
    required this.takeProfit,
    required this.maxRisk,
    required this.microPrice,
    required this.imbalance,
    required this.spreadBps,
    required this.source,
    required this.createdAt,
    required this.expiresAt,
    required this.offHours,
    this.status = CardStatus.pending,
    this.driftAtApproval,
    this.orderId,
    this.errorMessage,
  });
  final String token, ticker, source;
  final Timeframe timeframe;
  final int shares;
  final Pips entry, limitCollar, stopLoss, takeProfit, maxRisk;
  final double microPrice, imbalance, spreadBps;
  final DateTime createdAt, expiresAt;
  final bool offHours;
  CardStatus status;
  Pips? driftAtApproval;
  String? orderId, errorMessage;

  String get clientOrderId => 'shield-$token';

  Map<String, dynamic> toJson() => {
        'token': token,
        'ticker': ticker,
        'tf': timeframe.name,
        'shares': shares,
        'entry': entry,
        'collar': limitCollar,
        'stop': stopLoss,
        'target': takeProfit,
        'maxRisk': maxRisk,
        'micro': microPrice,
        'imb': imbalance,
        'spread': spreadBps,
        'source': source,
        'created': createdAt.toIso8601String(),
        'expires': expiresAt.toIso8601String(),
        'off': offHours,
        'status': status.name,
        'drift': driftAtApproval,
        'orderId': orderId,
        'err': errorMessage,
      };

  factory PendingCard.fromJson(Map<String, dynamic> j) => PendingCard(
        token: j['token'] as String,
        ticker: j['ticker'] as String,
        timeframe: Timeframe.values.byName(j['tf'] as String),
        shares: j['shares'] as int,
        entry: j['entry'] as int,
        limitCollar: j['collar'] as int,
        stopLoss: j['stop'] as int,
        takeProfit: j['target'] as int,
        maxRisk: j['maxRisk'] as int,
        microPrice: (j['micro'] as num).toDouble(),
        imbalance: (j['imb'] as num).toDouble(),
        spreadBps: (j['spread'] as num).toDouble(),
        source: j['source'] as String,
        createdAt: DateTime.parse(j['created'] as String),
        expiresAt: DateTime.parse(j['expires'] as String),
        offHours: j['off'] == true,
        status: CardStatus.values.byName(j['status'] as String),
        driftAtApproval: j['drift'] as int?,
        orderId: j['orderId'] as String?,
        errorMessage: j['err'] as String?,
      );
}

class AiDecision {
  AiDecision(this.trade, this.ticker, this.sourceTier, this.sentiment, this.reason);
  final bool trade;
  final String ticker, reason;
  final int sourceTier;
  final double sentiment;
}

sealed class TradeResult {
  const TradeResult();
}

class AutoExecuted extends TradeResult {
  const AutoExecuted(this.orderId, this.plan);
  final String orderId;
  final BracketPlan plan;
}

class Staged extends TradeResult {
  const Staged(this.token, this.plan);
  final String token;
  final BracketPlan plan;
}

class Rejected extends TradeResult {
  const Rejected(this.reason);
  final String reason;
}

class Failed extends TradeResult {
  const Failed(this.error);
  final String error;
}

String planSummary(BracketPlan p) =>
    '${p.shares} sh | Collar ≤ ${priceStr(p.limitCollar)} | Stop ${priceStr(p.stopLoss)} | '
    'Target ${priceStr(p.takeProfit)} | Max risk ${usd(p.maxRisk)}';

// ============================================================================
// 4. CLIENTS
// ============================================================================

const Duration _kReadTimeout = Duration(seconds: 20);

class AlpacaClient {
  AlpacaClient(this._http, this._keyId, this._secret);
  final http.Client _http;
  final String _keyId, _secret;

  Map<String, String> get _h => {
        'APCA-API-KEY-ID': _keyId,
        'APCA-API-SECRET-KEY': _secret,
        'accept': 'application/json',
      };
  Map<String, String> get _jh => {..._h, 'Content-Type': 'application/json'};

  dynamic _decode(http.Response r) {
    dynamic body;
    if (r.body.isNotEmpty) {
      try {
        body = jsonDecode(r.body);
      } catch (_) {
        body = null;
      }
    }
    if (r.statusCode >= 200 && r.statusCode < 300) return body; // includes 207 multi-status
    var msg = 'HTTP ${r.statusCode}';
    if (body is Map && body['message'] != null) msg = body['message'].toString();
    throw ApiException('Alpaca', r.statusCode, msg);
  }

  List<Map<String, dynamic>> _list(dynamic b) =>
      b is List ? b.whereType<Map>().map((e) => e.cast<String, dynamic>()).toList() : <Map<String, dynamic>>[];

  Future<dynamic> _get(String url) async =>
      _decode(await _http.get(Uri.parse(url), headers: _h).timeout(_kReadTimeout));

  Future<Map<String, dynamic>> getAccount() async =>
      ((await _get('$kPaperBase/account')) as Map).cast<String, dynamic>();

  Future<List<Map<String, dynamic>>> getPositions() async => _list(await _get('$kPaperBase/positions'));

  Future<List<Map<String, dynamic>>> getOpenOrders() async =>
      _list(await _get('$kPaperBase/orders?status=open&limit=10&direction=desc&nested=true'));

  Future<Map<String, dynamic>> getClock() async =>
      ((await _get('$kPaperBase/clock')) as Map).cast<String, dynamic>();

  /// Latest IEX quote map: bp, bs, ap, as.
  Future<Map<String, dynamic>> latestQuote(String symbol) async {
    final b = await _get('$kDataBase/stocks/$symbol/quotes/latest?feed=iex');
    if (b is Map && b['quote'] is Map) return (b['quote'] as Map).cast<String, dynamic>();
    throw PlanAbort('No quote returned for $symbol.');
  }

  Future<Map<String, dynamic>> submitBracket({
    required String symbol,
    required int shares,
    required Timeframe timeframe,
    required Pips limitCollar,
    required Pips takeProfit,
    required Pips stopLoss,
    required String clientOrderId,
  }) async {
    final body = {
      'symbol': symbol,
      'qty': shares.toString(),
      'side': 'buy',
      'type': 'limit',
      'time_in_force': timeframe == Timeframe.intraday ? 'day' : 'gtc',
      'limit_price': toApiPrice(limitCollar),
      'order_class': 'bracket',
      'take_profit': {'limit_price': toApiPrice(takeProfit)},
      'stop_loss': {'stop_price': toApiPrice(stopLoss)},
      'client_order_id': clientOrderId,
    };
    final res = await _http
        .post(Uri.parse('$kPaperBase/orders'), headers: _jh, body: jsonEncode(body))
        .timeout(_kReadTimeout);
    return (_decode(res) as Map).cast<String, dynamic>();
  }

  /// Returns null on 404 (order does not exist).
  Future<Map<String, dynamic>?> getOrderByClientId(String cid) async {
    try {
      final b = await _get('$kPaperBase/orders:by_client_order_id?client_order_id=${Uri.encodeQueryComponent(cid)}');
      return (b as Map).cast<String, dynamic>();
    } on ApiException catch (e) {
      if (e.status == 404) return null;
      rethrow;
    }
  }

  Future<List<Map<String, dynamic>>> cancelAllOrders() async =>
      _list(_decode(await _http.delete(Uri.parse('$kPaperBase/orders'), headers: _h).timeout(_kReadTimeout)));

  Future<List<Map<String, dynamic>>> closeAllPositions() async => _list(_decode(await _http
      .delete(Uri.parse('$kPaperBase/positions?cancel_orders=true'), headers: _h)
      .timeout(_kReadTimeout)));
}

class GeminiClient {
  GeminiClient(this._http, this._key, {this.model = kGeminiModel});
  final http.Client _http;
  final String _key, model;

  static const String _prompt =
      'Analyze this breaking news headline. Determine if it is a severe bullish macro/supply/contract catalyst for a specific US stock.\n'
      'Return raw JSON only: {"trade": true/false, "ticker": "SYMBOL_OR_NONE", "source_tier": 1|2|3, "sentiment": 0.0_to_1.0, "reason": "short explanation"}\n'
      'Headline: ';

  Future<AiDecision> analyze(String headline) async {
    final res = await _http
        .post(
          Uri.parse('https://generativelanguage.googleapis.com/v1beta/models/$model:generateContent'),
          headers: {'x-goog-api-key': _key, 'Content-Type': 'application/json'},
          body: jsonEncode({
            'contents': [
              {
                'parts': [
                  {'text': _prompt + headline}
                ]
              }
            ],
            'generationConfig': {'responseMimeType': 'application/json'},
          }),
        )
        .timeout(_kReadTimeout);
    if (res.statusCode != 200) {
      var msg = 'HTTP ${res.statusCode}';
      try {
        msg = (jsonDecode(res.body)['error']['message'] ?? msg).toString();
      } catch (_) {}
      throw ApiException('Gemini', res.statusCode, msg);
    }
    // Model output is UNTRUSTED: validate everything.
    try {
      final outer = jsonDecode(res.body) as Map<String, dynamic>;
      var text = (outer['candidates'][0]['content']['parts'][0]['text'] as String).trim();
      if (text.startsWith('```')) text = text.replaceAll(RegExp(r'^```(?:json)?|```$'), '').trim();
      final j = jsonDecode(text) as Map<String, dynamic>;
      final trade = j['trade'] == true;
      final ticker = (j['ticker'] ?? 'NONE').toString().trim().toUpperCase();
      final tier = ((j['source_tier'] as num?)?.toInt() ?? 3).clamp(1, 3).toInt();
      final sent = ((j['sentiment'] as num?)?.toDouble() ?? 0.0).clamp(0.0, 1.0).toDouble();
      var reason = (j['reason'] ?? '').toString().trim();
      if (reason.length > 200) reason = reason.substring(0, 200);
      return AiDecision(trade, ticker, tier, sent, reason);
    } catch (_) {
      throw const PlanAbort('Gemini returned a malformed response. No trade was placed.');
    }
  }
}

// ============================================================================
// 5. WATCH LINK — phone relay (Decision 5). Watch holds NO keys.
// ============================================================================

enum WatchCommand { setModeAuto, setModeManual, killSwitch }

enum WatchSendResult { sent, phoneOffline }

class WatchSnapshot {
  WatchSnapshot({
    required this.equity,
    required this.pnlText,
    required this.pnlPositive,
    required this.mode,
    required this.pendingCount,
    required this.feedTop3,
    required this.connectionOk,
    required this.updatedAt,
  });
  final String equity, pnlText, mode; // strings pre-formatted on the phone
  final bool pnlPositive, connectionOk;
  final int pendingCount;
  final List<String> feedTop3;
  final DateTime updatedAt;
}

typedef WatchAck = ({WatchSendResult result, String? detail});

/// Transport between phone and watch. A real Wear OS build implements this over the
/// Wear Data Layer (DataClient for snapshots, MessageClient for commands). Contract:
/// if the phone node cannot be reached, send() MUST return phoneOffline — never succeed silently.
abstract class WatchLink extends ChangeNotifier {
  bool get phoneReachable;
  WatchSnapshot? get snapshot;
  Future<WatchAck> send(WatchCommand cmd, String requestId);
}

/// In-process link used by the in-app Watch HUD preview. Lets you simulate a lost phone link.
class PreviewWatchLink extends WatchLink {
  ShieldController? phone;
  bool _reachable = true;
  WatchSnapshot? _snap;

  @override
  bool get phoneReachable => _reachable;
  @override
  WatchSnapshot? get snapshot => _snap;

  set reachable(bool v) {
    _reachable = v;
    notifyListeners();
  }

  void push(WatchSnapshot s) {
    if (!_reachable) return; // snapshots stop arriving when the link is down
    _snap = s;
    notifyListeners();
  }

  @override
  Future<WatchAck> send(WatchCommand cmd, String requestId) async {
    if (!_reachable || phone == null) return (result: WatchSendResult.phoneOffline, detail: null);
    final detail = await phone!.handleWatchCommand(cmd);
    return (result: WatchSendResult.sent, detail: detail);
  }
}

// ============================================================================
// 6. CONTROLLER
// ============================================================================

class ShieldController extends ChangeNotifier with WidgetsBindingObserver {
  ShieldController(this._prefs, this.watchLink);
  final SharedPreferences _prefs;
  final PreviewWatchLink watchLink;
  final http.Client _http = http.Client();
  AlpacaClient? client;
  void Function(String msg, bool error)? onMessage;

  // --- state
  bool booted = false, autoConnecting = false, isForeground = true;
  Vault vault = Vault();
  VaultStatus vaultStatus = VaultStatus.empty;
  ConnectionStatus connectionStatus = ConnectionStatus.disconnected;
  String? lastError;
  DateTime? lastSyncAt;
  Account? account;
  List<Position> positions = [];
  List<OrderSummary> orders = [];
  bool sessionOpen = false;
  String sessionMessage = '';
  ExecMode executionMode = ExecMode.manual; // Decision 1
  KillState killSwitchState = KillState.idle;
  String? killSwitchLastResult;
  List<PendingCard> pendingCards = [];
  List<FeedEvent> feed = [];
  bool allowOffHoursStaging = false, tradeSubmitting = false, scanInProgress = false;
  TradeResult? lastTradeResult;
  AiDecision? lastAiDecision;
  final ValueNotifier<DateTime> now = ValueNotifier(DateTime.now().toUtc());

  Timer? _pollTimer, _tickTimer;
  bool _polling = false;
  String? _lastToasted;

  // --- derived
  String get keyTail =>
      vault.alpacaKey.length >= 4 ? vault.alpacaKey.substring(vault.alpacaKey.length - 4) : '';
  bool get aiScannerEnabled => vault.geminiKey.isNotEmpty;
  String get lastEvent => feed.isEmpty ? 'Waiting for connection' : feed.first.text;

  String? get autoLockReason {
    if (connectionStatus == ConnectionStatus.authError) return 'API authentication error';
    final a = account;
    if (a != null && a.blocked) return 'Account or trading blocked';
    if (a != null && a.drawdownHalted) return 'Daily drawdown halt (-2.5%)';
    if (!sessionOpen) return 'US market closed';
    return null;
  }

  void toast(String msg, bool error) => onMessage?.call(msg, error);

  // --- lifecycle
  Future<void> loadLocal() async {
    final p = _prefs;
    vault = Vault(
      alpacaKey: p.getString('sh_ak') ?? '',
      alpacaSecret: p.getString('sh_as') ?? '',
      geminiKey: p.getString('sh_gk') ?? '',
      tgToken: p.getString('sh_tt') ?? '',
      tgChat: p.getString('sh_tc') ?? '',
    );
    executionMode = p.getString('sh_mode') == 'auto' ? ExecMode.auto : ExecMode.manual;
    try {
      feed = (jsonDecode(p.getString('sh_feed') ?? '[]') as List)
          .map((e) => FeedEvent.fromJson((e as Map).cast<String, dynamic>()))
          .toList();
    } catch (_) {
      feed = [];
    }
    try {
      pendingCards = (jsonDecode(p.getString('sh_cards') ?? '[]') as List)
          .map((e) => PendingCard.fromJson((e as Map).cast<String, dynamic>()))
          .toList();
    } catch (_) {
      pendingCards = [];
    }
    final nowUtc = DateTime.now().toUtc();
    for (final c in pendingCards) {
      if (c.status == CardStatus.approving) {
        // Process died mid-approval: outcome unknown. Never allow a blind re-approve.
        c.status = CardStatus.failed;
        c.errorMessage = 'App restarted during approval. Check Open Orders before re-staging.';
      } else if (c.status == CardStatus.pending && !nowUtc.isBefore(c.expiresAt)) {
        c.status = CardStatus.expired;
      }
    }
    _persistCards();
    vaultStatus = vault.hasRequired ? VaultStatus.unvalidated : VaultStatus.empty;
    autoConnecting = vault.hasRequired;
    WidgetsBinding.instance.addObserver(this);
    _tickTimer = Timer.periodic(const Duration(seconds: 1), (_) => _tick());
    booted = true;
    notifyListeners();
  }

  Future<void> autoConnect() async {
    if (!vault.hasRequired) return;
    await validateAndConnect();
    autoConnecting = false;
    notifyListeners();
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState s) {
    isForeground = s == AppLifecycleState.resumed;
    if (isForeground) {
      if (vaultStatus == VaultStatus.valid) {
        _startPolling();
        unawaited(refresh());
      }
    } else {
      _pollTimer?.cancel();
      _pollTimer = null;
    }
  }

  void _tick() {
    now.value = DateTime.now().toUtc();
    var changed = false;
    for (final c in pendingCards) {
      if (c.status == CardStatus.pending && !now.value.isBefore(c.expiresAt)) {
        c.status = CardStatus.expired; // Decision 7
        c.errorMessage = 'Expired after 5 minutes. Re-stage the trade.';
        changed = true;
        _log('⌛ ${c.ticker} card expired (5 min TTL)', level: 'warn', notify: false);
      }
    }
    if (changed) {
      _persistCards();
      notifyListeners();
    }
  }

  void _startPolling() {
    _pollTimer?.cancel();
    _pollTimer = Timer.periodic(const Duration(seconds: kPollSec), (_) => refresh());
  }

  void _stopPolling() {
    _pollTimer?.cancel();
    _pollTimer = null;
  }

  // --- persistence
  void _persistCards() =>
      _prefs.setString('sh_cards', jsonEncode(pendingCards.map((c) => c.toJson()).toList()));
  void _persistFeed() => _prefs.setString('sh_feed', jsonEncode(feed.map((e) => e.toJson()).toList()));

  void _log(String msg, {String level = 'info', bool notify = true}) {
    feed.insert(0, FeedEvent(DateTime.now().toUtc(), msg, level));
    if (feed.length > kFeedMax) feed.removeRange(kFeedMax, feed.length);
    _persistFeed();
    _pushWatch();
    if (notify) notifyListeners();
  }

  // --- vault
  Future<String?> saveVaultAndConnect({
    required String alpacaKey,
    required String alpacaSecret,
    required String geminiKey,
    required String tgToken,
    required String tgChat,
  }) async {
    final keyChanged = alpacaKey.trim() != vault.alpacaKey;
    vault = Vault(
      alpacaKey: alpacaKey.trim(),
      alpacaSecret: alpacaSecret.trim(),
      geminiKey: geminiKey.trim(),
      tgToken: tgToken.trim(),
      tgChat: tgChat.trim(),
    );
    Future<void> put(String k, String v) async => v.isEmpty ? _prefs.remove(k) : _prefs.setString(k, v);
    await put('sh_ak', vault.alpacaKey);
    await put('sh_as', vault.alpacaSecret);
    await put('sh_gk', vault.geminiKey);
    await put('sh_tt', vault.tgToken);
    await put('sh_tc', vault.tgChat);
    if (keyChanged && pendingCards.isNotEmpty) {
      pendingCards.clear(); // never let cards staged on another account be approved here
      _persistCards();
    }
    return validateAndConnect();
  }

  /// Returns an error message, or null on success.
  Future<String?> validateAndConnect() async {
    if (!vault.hasRequired) {
      vaultStatus = VaultStatus.empty;
      notifyListeners();
      return 'Alpaca API Key and Secret are required.';
    }
    if (vaultStatus != VaultStatus.valid) vaultStatus = VaultStatus.validating;
    connectionStatus = ConnectionStatus.connecting;
    notifyListeners();
    client = AlpacaClient(_http, vault.alpacaKey, vault.alpacaSecret);
    try {
      final a = Account.fromJson(await client!.getAccount());
      account = a;
      vaultStatus = VaultStatus.valid;
      connectionStatus = ConnectionStatus.connected;
      lastError = null;
      lastSyncAt = DateTime.now().toUtc();
      if (a.status != 'ACTIVE') toast('Account status is ${a.status}, not ACTIVE.', true);
      _log('🔐 Connected to Alpaca PAPER (Key ..$keyTail) — ${executionMode.name.toUpperCase()} mode', level: 'ok');
      if (isForeground) _startPolling();
      unawaited(refresh());
      notifyListeners();
      return null;
    } catch (e) {
      final msg = _errText(e);
      lastError = msg;
      _stopPolling();
      if (e is ApiException && (e.status == 401 || e.status == 403)) {
        vaultStatus = VaultStatus.invalid;
        connectionStatus = ConnectionStatus.authError;
      } else {
        vaultStatus = VaultStatus.unvalidated;
        connectionStatus = ConnectionStatus.networkError;
      }
      notifyListeners();
      return msg;
    }
  }

  Future<void> clearVault() async {
    for (final k in ['sh_ak', 'sh_as', 'sh_gk', 'sh_tt', 'sh_tc']) {
      await _prefs.remove(k);
    }
    _stopPolling();
    vault = Vault();
    client = null;
    account = null;
    positions = [];
    orders = [];
    pendingCards.clear();
    _persistCards();
    executionMode = ExecMode.manual;
    await _prefs.setString('sh_mode', 'manual');
    vaultStatus = VaultStatus.empty;
    connectionStatus = ConnectionStatus.disconnected;
    lastError = null;
    notifyListeners();
  }

  // --- polling
  Future<void> refresh() async {
    final cl = client;
    if (_polling || cl == null || vaultStatus != VaultStatus.valid) return;
    _polling = true;
    try {
      final fa = cl.getAccount(), fp = cl.getPositions(), fo = cl.getOpenOrders(), fc = cl.getClock();
      await Future.wait<Object?>([fa, fp, fo, fc]);
      account = Account.fromJson(await fa);
      positions = (await fp).map(Position.fromJson).toList();
      orders = (await fo).map(OrderSummary.fromJson).toList();
      final clock = await fc;
      sessionOpen = clock['is_open'] == true;
      final windowMsg = intradayWindowMessage(DateTime.now().toUtc());
      sessionMessage = sessionOpen ? (windowMsg ?? '') : 'US market closed';
      connectionStatus = ConnectionStatus.connected;
      lastError = null;
      lastSyncAt = DateTime.now().toUtc();
      _lastToasted = null;
      final a = account!;
      if (executionMode == ExecMode.auto && (a.blocked || a.drawdownHalted)) {
        executionMode = ExecMode.manual;
        await _prefs.setString('sh_mode', 'manual');
        _log('⛔ AUTO disabled → MANUAL (${a.blocked ? 'account blocked' : 'daily drawdown halt'})', level: 'error', notify: false);
      }
    } catch (e) {
      final msg = _errText(e);
      lastError = msg;
      connectionStatus = (e is ApiException && (e.status == 401 || e.status == 403))
          ? ConnectionStatus.authError
          : ConnectionStatus.networkError;
      if (_lastToasted != msg) {
        _lastToasted = msg;
        toast(msg, true);
      }
    } finally {
      _polling = false;
      _pushWatch();
      notifyListeners();
    }
  }

  // --- errors
  String _errText(Object e) {
    if (e is ApiException) return e.toString();
    if (e is RiskReject) return e.message;
    if (e is PlanAbort) return e.message;
    if (e is TimeoutException) return 'Request timed out. Check your connection.';
    if (e is SocketException || e is http.ClientException) return 'No network connection.';
    return 'Unexpected error: $e';
  }

  bool _isNetwork(Object e) => e is TimeoutException || e is SocketException || e is http.ClientException;

  // --- mode
  Future<void> setMode(ExecMode m) async {
    if (m == ExecMode.auto && autoLockReason != null) {
      toast('AUTO is locked: $autoLockReason', true);
      return;
    }
    executionMode = m;
    await _prefs.setString('sh_mode', m.name);
    _log(m == ExecMode.auto ? '🤖 Mode → AUTO' : '📱 Mode → MANUAL', level: 'warn');
  }

  void setAllowOffHours(bool v) {
    allowOffHoursStaging = v;
    notifyListeners();
  }

  // --- guards
  Future<Account> _accountGuard() async {
    final a = Account.fromJson(await client!.getAccount());
    account = a;
    if (a.blocked) throw const RiskReject('Account or trading is blocked by Alpaca. All orders aborted.');
    if (a.drawdownHalted) {
      throw RiskReject('Daily drawdown halt: down ${a.pnl.pct.toStringAsFixed(2)}% today (limit -2.50%). New entries blocked.');
    }
    return a;
  }

  /// Decision 6: market holidays respected through GET /v2/clock `is_open`.
  Future<void> _sessionGate(Timeframe tf) async {
    final clock = await client!.getClock();
    sessionOpen = clock['is_open'] == true;
    if (!sessionOpen) {
      throw const RiskReject('US market is closed (Alpaca clock). Turn on Off-Hours Staging to stage a MANUAL card.');
    }
    if (tf == Timeframe.intraday) {
      final ts = DateTime.tryParse('${clock['timestamp']}')?.toUtc() ?? DateTime.now().toUtc();
      final m = intradayWindowMessage(ts);
      if (m != null) throw RiskReject(m);
    }
  }

  /// Decision 2: no live ask => PlanAbort. No fallback price, ever.
  Future<({Pips ask, double micro, double imbalance, double spreadBps})> _liveQuote(String sym) async {
    final q = await client!.latestQuote(sym);
    final ap = q['ap'];
    final bp = q['bp'];
    if (ap is! num || ap <= 0) {
      throw PlanAbort('No live ask for $sym (market closed or no liquidity). Trade aborted — no fallback price is used.');
    }
    final s = ap.toString();
    if (s.contains('e') || s.contains('E')) throw PlanAbort('Unusable ask price format for $sym.');
    final m = calcImbalanceAndMicro(
      bp is num ? bp.toDouble() : 0.0,
      ap.toDouble(),
      (q['bs'] is num) ? (q['bs'] as num).toDouble() : 0.0,
      (q['as'] is num) ? (q['as'] as num).toDouble() : 0.0,
    );
    return (ask: parsePips(s), micro: m.micro, imbalance: m.imbalance, spreadBps: m.spreadBps);
  }

  // --- trade
  Future<void> triggerTrade({
    required String ticker,
    required Timeframe timeframe,
    required String source,
  }) async {
    if (tradeSubmitting) return;
    tradeSubmitting = true;
    lastTradeResult = null;
    notifyListeners();
    try {
      final sym = ticker.trim().toUpperCase();
      if (!kTickerPattern.hasMatch(sym)) throw const PlanAbort('Invalid ticker symbol.');
      if (vaultStatus != VaultStatus.valid || client == null) throw const PlanAbort('Connect your Alpaca keys first.');
      final acct = await _accountGuard();
      final offHours = allowOffHoursStaging;
      if (!offHours) await _sessionGate(timeframe);

      final qs = await _liveQuote(sym);
      if (qs.spreadBps >= 9999) throw PlanAbort('No valid two-sided quote for $sym. Trade aborted.');
      if (!spreadAllowed(qs.spreadBps, timeframe)) {
        throw RiskReject(
            'Spread ${qs.spreadBps.toStringAsFixed(1)} bps exceeds the ${maxSpreadBps(timeframe).toStringAsFixed(0)} bps ${tfLabel(timeframe)} limit.');
      }
      final plan = buildBracketPlan(equity: acct.equity, ask: qs.ask, timeframe: timeframe);
      final token = _newToken();
      final auto = executionMode == ExecMode.auto && !offHours; // off-hours is never AUTO

      if (auto) {
        final order = await _submitWithRecovery(
          symbol: sym,
          shares: plan.shares,
          timeframe: timeframe,
          limitCollar: plan.limitCollar,
          takeProfit: plan.takeProfit,
          stopLoss: plan.stopLoss,
          clientOrderId: 'shield-$token',
        );
        final id = (order['id'] ?? '?').toString();
        lastTradeResult = AutoExecuted(id, plan);
        _log('🤖 AUTO ${tfLabel(timeframe)} $sym ${plan.shares} sh ≤ ${priceStr(plan.limitCollar)} → order ${id.length > 8 ? id.substring(0, 8) : id}', level: 'ok', notify: false);
        toast('AUTO order accepted: $sym × ${plan.shares}', false);
        unawaited(refresh());
      } else {
        final t = DateTime.now().toUtc();
        pendingCards.insert(
          0,
          PendingCard(
            token: token,
            ticker: sym,
            timeframe: timeframe,
            shares: plan.shares,
            entry: plan.entry,
            limitCollar: plan.limitCollar,
            stopLoss: plan.stopLoss,
            takeProfit: plan.takeProfit,
            maxRisk: plan.maxRisk,
            microPrice: qs.micro,
            imbalance: qs.imbalance,
            spreadBps: qs.spreadBps,
            source: source,
            createdAt: t,
            expiresAt: t.add(kCardTtl),
            offHours: offHours,
          ),
        );
        _persistCards();
        lastTradeResult = Staged(token, plan);
        _log('📱 Staged ${tfLabel(timeframe)} $sym ${plan.shares} sh${offHours ? ' (OFF-HOURS)' : ''} — awaiting approval', level: 'info', notify: false);
      }
    } on RiskReject catch (e) {
      lastTradeResult = Rejected(e.message);
      _log('⛔ ${e.message}', level: 'warn', notify: false);
    } catch (e) {
      final msg = _errText(e);
      lastTradeResult = Failed(msg);
      _log('❌ $msg', level: 'error', notify: false);
      if (e is! PlanAbort) toast(msg, true);
    } finally {
      tradeSubmitting = false;
      notifyListeners();
    }
  }

  /// Idempotent submit: on unknown outcome, look up by client_order_id BEFORE any retry.
  Future<Map<String, dynamic>> _submitWithRecovery({
    required String symbol,
    required int shares,
    required Timeframe timeframe,
    required Pips limitCollar,
    required Pips takeProfit,
    required Pips stopLoss,
    required String clientOrderId,
  }) async {
    Future<Map<String, dynamic>> post() => client!.submitBracket(
          symbol: symbol,
          shares: shares,
          timeframe: timeframe,
          limitCollar: limitCollar,
          takeProfit: takeProfit,
          stopLoss: stopLoss,
          clientOrderId: clientOrderId,
        );
    try {
      return await post();
    } on ApiException catch (e) {
      if (e.status == 422) {
        // Duplicate client_order_id returns 422: if our order exists, it already went through.
        final existing = await client!.getOrderByClientId(clientOrderId);
        if (existing != null) return existing;
      }
      rethrow;
    } catch (e) {
      if (!_isNetwork(e)) rethrow;
      try {
        final existing = await client!.getOrderByClientId(clientOrderId);
        if (existing != null) return existing;
        return await post(); // 404 => the first POST never landed
      } catch (e2) {
        if (e2 is ApiException) rethrow;
        throw const PlanAbort('Order outcome unknown (network failure). Check Open Orders before retrying.');
      }
    }
  }

  // --- approval cards
  PendingCard? _card(String token) {
    for (final c in pendingCards) {
      if (c.token == token) return c;
    }
    return null;
  }

  Future<void> approveCard(String token) async {
    final card = _card(token);
    if (card == null || card.status != CardStatus.pending) return;
    if (!DateTime.now().toUtc().isBefore(card.expiresAt)) {
      card.status = CardStatus.expired;
      card.errorMessage = 'Expired after 5 minutes. Re-stage the trade.';
      _persistCards();
      notifyListeners();
      return;
    }
    card.status = CardStatus.approving; // locks the buttons against double-tap
    card.errorMessage = null;
    _persistCards();
    notifyListeners();
    try {
      if (client == null) throw const PlanAbort('Not connected.');
      // 1. Decision 3: price-drift guard against a fresh ask.
      final qs = await _liveQuote(card.ticker);
      try {
        card.driftAtApproval =
            checkPriceDrift(stagedEntry: card.entry, stopLoss: card.stopLoss, currentAsk: qs.ask);
      } on RiskReject catch (e) {
        card.status = CardStatus.stale;
        card.errorMessage = e.message;
        _log('🕒 ${card.ticker} stale — ask ${priceStr(qs.ask)} vs staged ${priceStr(card.entry)}', level: 'warn', notify: false);
        return;
      }
      // 2. Re-check account + session (off-hours cards were staged knowingly).
      await _accountGuard();
      if (!card.offHours) await _sessionGate(card.timeframe);
      if (!DateTime.now().toUtc().isBefore(card.expiresAt)) {
        card.status = CardStatus.expired;
        card.errorMessage = 'Expired after 5 minutes. Re-stage the trade.';
        return;
      }
      // 3. Submit (idempotent).
      final order = await _submitWithRecovery(
        symbol: card.ticker,
        shares: card.shares,
        timeframe: card.timeframe,
        limitCollar: card.limitCollar,
        takeProfit: card.takeProfit,
        stopLoss: card.stopLoss,
        clientOrderId: card.clientOrderId,
      );
      card.orderId = (order['id'] ?? '').toString();
      card.status = CardStatus.executed;
      final id = card.orderId!;
      _log('✅ Approved ${card.ticker} ${card.shares} sh ≤ ${priceStr(card.limitCollar)} → order ${id.length > 8 ? id.substring(0, 8) : id}', level: 'ok', notify: false);
      toast('Order accepted: ${card.ticker} × ${card.shares}', false);
      pendingCards.removeWhere((c) => c.token == card.token);
      unawaited(refresh());
    } catch (e) {
      final msg = _errText(e);
      card.status = CardStatus.failed;
      card.errorMessage = msg;
      _log('❌ ${card.ticker} approval failed: $msg', level: 'error', notify: false);
      if (e is ApiException || _isNetwork(e)) toast(msg, true);
    } finally {
      _persistCards();
      notifyListeners();
    }
  }

  void rejectCard(String token) {
    final c = _card(token);
    if (c == null || c.status == CardStatus.approving) return;
    pendingCards.removeWhere((x) => x.token == token);
    _persistCards();
    _log('❌ Rejected ${c.ticker} card');
  }

  void dismissCard(String token) {
    final c = _card(token);
    if (c == null || c.status == CardStatus.approving) return;
    pendingCards.removeWhere((x) => x.token == token);
    _persistCards();
    notifyListeners();
  }

  // --- kill switch
  Future<void> runKillSwitch() async {
    if (killSwitchState == KillState.executing) return;
    killSwitchState = KillState.executing;
    killSwitchLastResult = null;
    // Safest ordering: stop AUTO and drop staged cards first so nothing can fire mid-flatten.
    executionMode = ExecMode.manual;
    await _prefs.setString('sh_mode', 'manual');
    pendingCards.clear();
    _persistCards();
    notifyListeners();

    final lines = <String>[];
    var failed = false;
    bool ok(dynamic s) => s is! int || (s >= 200 && s < 300);
    final cl = client;
    if (cl == null) {
      failed = true;
      lines.add('Not connected — nothing was sent to Alpaca.');
    } else {
      try {
        for (final r in await cl.cancelAllOrders()) {
          final good = ok(r['status']);
          if (!good) failed = true;
          final id = (r['id'] ?? '?').toString();
          lines.add('Order ${id.length > 8 ? id.substring(0, 8) : id}: ${good ? 'cancel requested' : 'FAILED (${r['status']})'}');
        }
      } catch (e) {
        failed = true;
        lines.add('Cancel orders FAILED: ${_errText(e)}');
      }
      try {
        for (final r in await cl.closeAllPositions()) {
          final good = ok(r['status']);
          if (!good) failed = true;
          lines.add('${r['symbol'] ?? '?'}: ${good ? 'close requested' : 'FAILED (${r['status']})'}');
        }
      } catch (e) {
        failed = true;
        lines.add('Flatten positions FAILED: ${_errText(e)}');
      }
    }
    if (lines.isEmpty) lines.add('Nothing to cancel. Account is flat.');
    killSwitchLastResult = lines.join('\n');
    killSwitchState = failed ? KillState.failed : KillState.done;
    _log(failed ? '🚨 KILL SWITCH finished WITH FAILURES — verify in Alpaca' : '🚨 KILL SWITCH executed — MANUAL mode', level: failed ? 'error' : 'warn', notify: false);
    toast(failed ? 'Kill switch had failures — review the result.' : 'Kill switch executed. Orders cancelled, positions closing.', failed);
    await refresh();
    notifyListeners();
  }

  // --- AI scanner
  Future<void> evaluateHeadline(String headline) async {
    if (!aiScannerEnabled) {
      toast('Add a Gemini API key in the Key Vault to use the AI Scanner.', true);
      return;
    }
    var h = headline.trim();
    if (h.isEmpty) {
      toast('Enter a headline first.', true);
      return;
    }
    if (h.length > 500) h = h.substring(0, 500);
    scanInProgress = true;
    lastAiDecision = null;
    notifyListeners();
    try {
      final d = await GeminiClient(_http, vault.geminiKey).analyze(h);
      lastAiDecision = d;
      if (!d.trade || d.ticker == 'NONE' || d.ticker.isEmpty) {
        _log('🧠 AI Filtered Noise: ${d.reason}');
        return;
      }
      if (!kTickerPattern.hasMatch(d.ticker)) {
        _log('🧠 AI returned an invalid ticker — ignored', level: 'warn');
        return;
      }
      final r = d.reason.length > 45 ? d.reason.substring(0, 45) : d.reason;
      scanInProgress = false;
      notifyListeners();
      await triggerTrade(ticker: d.ticker, timeframe: Timeframe.swing, source: 'AI Shock ($r)');
    } catch (e) {
      final msg = _errText(e);
      toast(msg, true);
      _log('🧠 AI scan failed: $msg', level: 'error');
    } finally {
      scanInProgress = false;
      notifyListeners();
    }
  }

  // --- watch bridge
  void _pushWatch() {
    final a = account;
    final p = a?.pnl;
    watchLink.push(WatchSnapshot(
      equity: a == null ? '—' : usd(a.equity),
      pnlText: p == null ? '—' : '${usd(p.pnl, sign: true)} (${p.pct >= 0 ? '+' : ''}${p.pct.toStringAsFixed(2)}%)',
      pnlPositive: (p?.pnl ?? 0) >= 0,
      mode: executionMode == ExecMode.auto ? 'AUTO' : 'MANUAL',
      pendingCount: pendingCards.where((c) => c.status == CardStatus.pending).length,
      feedTop3: feed.take(3).map((e) => e.text).toList(),
      connectionOk: connectionStatus == ConnectionStatus.connected,
      updatedAt: DateTime.now().toUtc(),
    ));
  }

  /// Executes a command relayed from the watch. Returns a human-readable detail (or null).
  Future<String?> handleWatchCommand(WatchCommand cmd) async {
    switch (cmd) {
      case WatchCommand.setModeManual:
        await setMode(ExecMode.manual);
        return 'MANUAL set';
      case WatchCommand.setModeAuto:
        final lock = autoLockReason;
        if (lock != null) {
          toast('Watch tried AUTO but it is locked: $lock', true);
          return 'AUTO refused: $lock';
        }
        await setMode(ExecMode.auto);
        return 'AUTO set';
      case WatchCommand.killSwitch:
        await runKillSwitch();
        return killSwitchState == KillState.failed ? 'Kill ran WITH FAILURES' : 'Kill executed';
    }
  }

  @override
  void dispose() {
    _pollTimer?.cancel();
    _tickTimer?.cancel();
    WidgetsBinding.instance.removeObserver(this);
    _http.close();
    super.dispose();
  }
}

// ============================================================================
// 7. UI
// ============================================================================

abstract final class Pal {
  static const bg = Color(0xFF020617);
  static const surface = Color(0xFF0F172A);
  static const border = Color(0xFF1E293B);
  static const emerald = Color(0xFF10B981);
  static const sky = Color(0xFF0EA5E9);
  static const danger = Color(0xFFDC2626);
  static const indigo = Color(0xFF4F46E5);
  static const amber = Color(0xFFF59E0B);
  static const muted = Color(0xFF94A3B8);
  static const text = Color(0xFFE2E8F0);
}

Color _a(Color c, double o) => c.withAlpha((o * 255).round());
const TextStyle _mono = TextStyle(fontFamily: 'monospace', fontSize: 12.5, color: Pal.text);

final GlobalKey<ScaffoldMessengerState> rootMessengerKey = GlobalKey<ScaffoldMessengerState>();

void showToast(String msg, bool error) {
  rootMessengerKey.currentState
    ?..hideCurrentSnackBar()
    ..showSnackBar(SnackBar(
      content: Text(msg),
      backgroundColor: error ? Pal.danger : const Color(0xFF047857),
      behavior: SnackBarBehavior.floating,
      duration: Duration(seconds: error ? 6 : 3),
    ));
}

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();
  final prefs = await SharedPreferences.getInstance();
  final link = PreviewWatchLink();
  final c = ShieldController(prefs, link);
  link.phone = c;
  c.onMessage = showToast;
  await c.loadLocal();
  runApp(ShieldApp(c: c));
  unawaited(c.autoConnect());
}

class ShieldApp extends StatelessWidget {
  const ShieldApp({super.key, required this.c});
  final ShieldController c;

  @override
  Widget build(BuildContext context) {
    final base = ThemeData(brightness: Brightness.dark, useMaterial3: true);
    return MaterialApp(
      title: 'Shield',
      debugShowCheckedModeBanner: false,
      scaffoldMessengerKey: rootMessengerKey,
      theme: base.copyWith(
        scaffoldBackgroundColor: Pal.bg,
        colorScheme: const ColorScheme.dark(primary: Pal.emerald, secondary: Pal.sky, surface: Pal.surface, error: Pal.danger),
        appBarTheme: const AppBarTheme(backgroundColor: Pal.surface, elevation: 0, foregroundColor: Pal.text),
        inputDecorationTheme: InputDecorationTheme(
          filled: true,
          fillColor: Pal.bg,
          isDense: true,
          border: OutlineInputBorder(borderRadius: BorderRadius.circular(8), borderSide: const BorderSide(color: Pal.border)),
          enabledBorder: OutlineInputBorder(borderRadius: BorderRadius.circular(8), borderSide: const BorderSide(color: Pal.border)),
          focusedBorder: OutlineInputBorder(borderRadius: BorderRadius.circular(8), borderSide: const BorderSide(color: Pal.emerald)),
          labelStyle: const TextStyle(color: Pal.muted),
        ),
      ),
      home: ListenableBuilder(
        listenable: c,
        builder: (context, _) {
          if (!c.booted || c.autoConnecting) return const _Splash();
          return c.vaultStatus == VaultStatus.valid ? DashboardScreen(c: c) : KeyVaultScreen(c: c);
        },
      ),
    );
  }
}

class _Splash extends StatelessWidget {
  const _Splash();
  @override
  Widget build(BuildContext context) => const Scaffold(
        body: Center(
          child: Column(mainAxisSize: MainAxisSize.min, children: [
            Text('🛡️', style: TextStyle(fontSize: 48)),
            SizedBox(height: 16),
            CircularProgressIndicator(color: Pal.emerald),
            SizedBox(height: 16),
            Text('Connecting to Alpaca…', style: TextStyle(color: Pal.muted)),
          ]),
        ),
      );
}

// ---------- shared widgets ----------

class Panel extends StatelessWidget {
  const Panel({super.key, required this.title, required this.child, this.border = Pal.border, this.trailing});
  final String title;
  final Widget child;
  final Color border;
  final Widget? trailing;
  @override
  Widget build(BuildContext context) => Container(
        width: double.infinity,
        margin: const EdgeInsets.only(bottom: 12),
        padding: const EdgeInsets.all(12),
        decoration: BoxDecoration(color: Pal.surface, borderRadius: BorderRadius.circular(10), border: Border.all(color: border)),
        child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
          Row(children: [
            Expanded(child: Text(title, style: const TextStyle(fontWeight: FontWeight.w700, fontSize: 14))),
            if (trailing != null) trailing!,
          ]),
          const SizedBox(height: 10),
          child,
        ]),
      );
}

class Pill extends StatelessWidget {
  const Pill(this.text, this.color, {super.key, this.onTap});
  final String text;
  final Color color;
  final VoidCallback? onTap;
  @override
  Widget build(BuildContext context) => InkWell(
        onTap: onTap,
        borderRadius: BorderRadius.circular(20),
        child: Container(
          padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 5),
          decoration: BoxDecoration(color: _a(color, .15), border: Border.all(color: color), borderRadius: BorderRadius.circular(20)),
          child: Text(text, style: TextStyle(color: color, fontSize: 12, fontWeight: FontWeight.w600)),
        ),
      );
}

class MetricTile extends StatelessWidget {
  const MetricTile(this.label, this.value, {super.key, this.color = Pal.text, this.sub});
  final String label, value;
  final String? sub;
  final Color color;
  @override
  Widget build(BuildContext context) => Container(
        padding: const EdgeInsets.all(12),
        decoration: BoxDecoration(color: Pal.surface, borderRadius: BorderRadius.circular(10), border: Border.all(color: Pal.border)),
        child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
          Text(label, style: const TextStyle(color: Pal.muted, fontSize: 11)),
          const SizedBox(height: 4),
          FittedBox(fit: BoxFit.scaleDown, child: Text(value, style: TextStyle(color: color, fontSize: 19, fontWeight: FontWeight.w700))),
          if (sub != null) Text(sub!, style: TextStyle(color: color, fontSize: 12)),
        ]),
      );
}

class SecretField extends StatefulWidget {
  const SecretField({super.key, required this.controller, required this.label, this.hint});
  final TextEditingController controller;
  final String label;
  final String? hint;
  @override
  State<SecretField> createState() => _SecretFieldState();
}

class _SecretFieldState extends State<SecretField> {
  bool _reveal = false;
  @override
  Widget build(BuildContext context) => TextField(
        controller: widget.controller,
        obscureText: !_reveal, // masked by default
        enableSuggestions: false,
        autocorrect: false,
        style: _mono,
        decoration: InputDecoration(
          labelText: widget.label,
          hintText: widget.hint,
          suffixIcon: IconButton(
            icon: Icon(_reveal ? Icons.visibility_off : Icons.visibility, size: 20, color: Pal.muted),
            onPressed: () => setState(() => _reveal = !_reveal),
          ),
        ),
      );
}

// ---------- key vault ----------

class KeyVaultScreen extends StatefulWidget {
  const KeyVaultScreen({super.key, required this.c, this.modal = false});
  final ShieldController c;
  final bool modal;
  @override
  State<KeyVaultScreen> createState() => _KeyVaultScreenState();
}

class _KeyVaultScreenState extends State<KeyVaultScreen> {
  late final TextEditingController _ak, _as, _gk, _tt, _tc;
  bool _busy = false;
  String? _error;

  @override
  void initState() {
    super.initState();
    final v = widget.c.vault;
    _ak = TextEditingController(text: v.alpacaKey);
    _as = TextEditingController(text: v.alpacaSecret);
    _gk = TextEditingController(text: v.geminiKey);
    _tt = TextEditingController(text: v.tgToken);
    _tc = TextEditingController(text: v.tgChat);
  }

  @override
  void dispose() {
    for (final t in [_ak, _as, _gk, _tt, _tc]) {
      t.dispose();
    }
    super.dispose();
  }

  Future<void> _save() async {
    if (_ak.text.trim().isEmpty || _as.text.trim().isEmpty) {
      setState(() => _error = 'Alpaca Key ID and Secret Key are both required.');
      return;
    }
    setState(() {
      _busy = true;
      _error = null;
    });
    final err = await widget.c.saveVaultAndConnect(
      alpacaKey: _ak.text,
      alpacaSecret: _as.text,
      geminiKey: _gk.text,
      tgToken: _tt.text,
      tgChat: _tc.text,
    );
    if (!mounted) return;
    setState(() {
      _busy = false;
      _error = err;
    });
    if (err == null && widget.modal) Navigator.of(context).pop();
  }

  Future<void> _clear() async {
    final ok = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: const Text('Clear all keys?'),
        content: const Text('This wipes every saved key and staged card from this device.'),
        actions: [
          TextButton(onPressed: () => Navigator.pop(ctx, false), child: const Text('Cancel')),
          TextButton(onPressed: () => Navigator.pop(ctx, true), child: const Text('Clear', style: TextStyle(color: Pal.danger))),
        ],
      ),
    );
    if (ok != true) return;
    await widget.c.clearVault();
    if (!mounted) return;
    for (final t in [_ak, _as, _gk, _tt, _tc]) {
      t.clear();
    }
    if (widget.modal) Navigator.of(context).pop();
  }

  @override
  Widget build(BuildContext context) => Scaffold(
        appBar: AppBar(
          title: const Text('Connect Your Own Accounts'),
          leading: widget.modal ? IconButton(icon: const Icon(Icons.close), onPressed: () => Navigator.pop(context)) : null,
          automaticallyImplyLeading: false,
        ),
        body: SafeArea(
          child: Center(
            child: ConstrainedBox(
              constraints: const BoxConstraints(maxWidth: 520),
              child: ListView(padding: const EdgeInsets.all(16), children: [
                const Text('🛡️ Project Shield v3.0 — paper trading', style: TextStyle(fontSize: 16, fontWeight: FontWeight.w700)),
                const SizedBox(height: 6),
                const Text(
                  'Keys are stored only on this device and sent only to Alpaca and Google. '
                  'Shield has no server. Use Alpaca PAPER keys (they start with PK).',
                  style: TextStyle(color: Pal.muted, fontSize: 13),
                ),
                const SizedBox(height: 16),
                SecretField(controller: _ak, label: 'Alpaca API Key ID *', hint: 'PK…'),
                const SizedBox(height: 12),
                SecretField(controller: _as, label: 'Alpaca Secret Key *'),
                const SizedBox(height: 12),
                SecretField(controller: _gk, label: 'Gemini API Key (optional — AI Scanner only)'),
                const SizedBox(height: 12),
                SecretField(controller: _tt, label: 'Telegram Bot Token (optional, not used in v3.0)'),
                const SizedBox(height: 12),
                TextField(controller: _tc, style: _mono, decoration: const InputDecoration(labelText: 'Telegram Chat ID (optional)')),
                if (_error != null) ...[
                  const SizedBox(height: 12),
                  Container(
                    padding: const EdgeInsets.all(10),
                    decoration: BoxDecoration(color: _a(Pal.danger, .15), border: Border.all(color: Pal.danger), borderRadius: BorderRadius.circular(8)),
                    child: Text(_error!, style: const TextStyle(color: Color(0xFFFCA5A5))),
                  ),
                ],
                const SizedBox(height: 16),
                FilledButton(
                  style: FilledButton.styleFrom(backgroundColor: Pal.emerald, foregroundColor: Colors.black, padding: const EdgeInsets.symmetric(vertical: 14)),
                  onPressed: _busy ? null : _save,
                  child: _busy
                      ? const SizedBox(width: 20, height: 20, child: CircularProgressIndicator(strokeWidth: 2, color: Colors.black))
                      : const Text('Save & Connect', style: TextStyle(fontWeight: FontWeight.w700)),
                ),
                const SizedBox(height: 8),
                TextButton(onPressed: _busy ? null : _clear, child: const Text('Clear all keys', style: TextStyle(color: Pal.danger))),
              ]),
            ),
          ),
        ),
      );
}

// ---------- dashboard ----------

class DashboardScreen extends StatelessWidget {
  const DashboardScreen({super.key, required this.c});
  final ShieldController c;

  @override
  Widget build(BuildContext context) {
    final auto = c.executionMode == ExecMode.auto;
    return Scaffold(
      appBar: AppBar(
        title: const Text('🛡️ SHIELD v3.0', style: TextStyle(fontWeight: FontWeight.w800, fontSize: 17)),
        actions: [
          IconButton(
            tooltip: 'Connect API',
            icon: const Icon(Icons.vpn_key_outlined),
            onPressed: () => Navigator.of(context).push(MaterialPageRoute(builder: (_) => KeyVaultScreen(c: c, modal: true))),
          ),
          IconButton(
            tooltip: 'Watch preview',
            icon: const Icon(Icons.watch_outlined),
            onPressed: () => Navigator.of(context).push(MaterialPageRoute(builder: (_) => WatchPreviewScreen(link: c.watchLink))),
          ),
        ],
        bottom: PreferredSize(
          preferredSize: const Size.fromHeight(44),
          child: Padding(
            padding: const EdgeInsets.fromLTRB(12, 0, 12, 8),
            child: Row(children: [
              Pill(
                c.connectionStatus == ConnectionStatus.connected ? 'Key ..${c.keyTail}' : 'Tap Connect API',
                c.connectionStatus == ConnectionStatus.connected ? Pal.emerald : Pal.amber,
                onTap: () => Navigator.of(context).push(MaterialPageRoute(builder: (_) => KeyVaultScreen(c: c, modal: true))),
              ),
              const SizedBox(width: 8),
              Pill(auto ? '🤖 AUTO' : '📱 MANUAL', auto ? Pal.emerald : Pal.sky),
            ]),
          ),
        ),
      ),
      body: SafeArea(
        child: RefreshIndicator(
          color: Pal.emerald,
          onRefresh: c.refresh,
          child: LayoutBuilder(builder: (context, cons) {
            final wide = cons.maxWidth >= 600;
            final left = <Widget>[
              StatusBanner(c: c),
              ModeAndKillBar(c: c),
              PendingApprovalsSection(c: c),
              PortfolioMetrics(c: c, wide: wide),
            ];
            final right = <Widget>[
              TradePanel(c: c),
              AiScannerPanel(c: c),
              PositionsOrdersPanel(c: c),
              ExecutionFeedPanel(c: c),
            ];
            return SingleChildScrollView(
              physics: const AlwaysScrollableScrollPhysics(),
              padding: const EdgeInsets.all(12),
              child: wide
                  ? Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
                      Expanded(child: Column(children: left)),
                      const SizedBox(width: 12),
                      Expanded(child: Column(children: right)),
                    ])
                  : Column(children: [...left, ...right]),
            );
          }),
        ),
      ),
    );
  }
}

class StatusBanner extends StatelessWidget {
  const StatusBanner({super.key, required this.c});
  final ShieldController c;

  @override
  Widget build(BuildContext context) {
    final err = c.connectionStatus == ConnectionStatus.authError || c.connectionStatus == ConnectionStatus.networkError;
    return Panel(
      title: 'Status',
      trailing: Pill(c.sessionOpen ? '🟢 US MARKET OPEN' : '⏸️ ${c.sessionMessage.isEmpty ? 'Market status unknown' : c.sessionMessage}',
          c.sessionOpen ? Pal.emerald : Pal.amber),
      child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
        if (err)
          Container(
            width: double.infinity,
            margin: const EdgeInsets.only(bottom: 8),
            padding: const EdgeInsets.all(8),
            decoration: BoxDecoration(color: _a(Pal.danger, .15), border: Border.all(color: Pal.danger), borderRadius: BorderRadius.circular(8)),
            child: Text(
              '⚠️ ${c.lastError ?? 'Connection problem'}\n'
              'Showing last-known data${c.lastSyncAt == null ? '' : ' from ${hms(mytTime(c.lastSyncAt!))} MYT'}.',
              style: const TextStyle(color: Color(0xFFFCA5A5), fontSize: 12.5),
            ),
          ),
        if (c.sessionOpen && c.sessionMessage.isNotEmpty)
          Padding(padding: const EdgeInsets.only(bottom: 6), child: Text('Intraday: ${c.sessionMessage}', style: const TextStyle(color: Pal.amber, fontSize: 12))),
        Text('⚡ ${c.lastEvent}', style: _mono),
        const SizedBox(height: 6),
        ValueListenableBuilder<DateTime>(
          valueListenable: c.now,
          builder: (_, t, __) => Text('MYT: ${hms(mytTime(t))} | ${isNyDst(t) ? 'EDT' : 'EST'}: ${hms(nyTime(t))}',
              style: const TextStyle(color: Pal.muted, fontSize: 12, fontFamily: 'monospace')),
        ),
      ]),
    );
  }
}

class ModeAndKillBar extends StatelessWidget {
  const ModeAndKillBar({super.key, required this.c});
  final ShieldController c;

  Future<void> _toggle(BuildContext context) async {
    if (c.executionMode == ExecMode.auto) {
      await c.setMode(ExecMode.manual);
      return;
    }
    final lock = c.autoLockReason;
    if (lock != null) {
      c.toast('AUTO is locked: $lock', true);
      return;
    }
    final ok = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: const Text('Enable AUTO mode?'),
        content: const Text(
          'In AUTO, a trigger or AI signal submits a (paper) bracket order immediately — no approval card. '
          'Off-hours staging always stays MANUAL. The kill switch returns you to MANUAL.',
        ),
        actions: [
          TextButton(onPressed: () => Navigator.pop(ctx, false), child: const Text('Cancel')),
          FilledButton(
            style: FilledButton.styleFrom(backgroundColor: Pal.emerald, foregroundColor: Colors.black),
            onPressed: () => Navigator.pop(ctx, true),
            child: const Text('Enable AUTO'),
          ),
        ],
      ),
    );
    if (ok == true) await c.setMode(ExecMode.auto);
  }

  Future<void> _kill(BuildContext context) async {
    final ok = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: const Text('🚨 Kill switch'),
        content: const Text('Cancel all open orders, flatten all positions & return to MANUAL mode.\n\nThis cannot be undone.'),
        actions: [
          TextButton(onPressed: () => Navigator.pop(ctx, false), child: const Text('Cancel')),
          FilledButton(
            style: FilledButton.styleFrom(backgroundColor: Pal.danger, foregroundColor: Colors.white),
            onPressed: () => c.toast('Press and HOLD the button to confirm.', true),
            onLongPress: () => Navigator.pop(ctx, true),
            child: const Text('HOLD TO FLATTEN'),
          ),
        ],
      ),
    );
    if (ok != true) return;
    await c.runKillSwitch();
    if (!context.mounted) return;
    showModalBottomSheet<void>(
      context: context,
      backgroundColor: Pal.surface,
      builder: (_) => Padding(
        padding: const EdgeInsets.all(16),
        child: Column(mainAxisSize: MainAxisSize.min, crossAxisAlignment: CrossAxisAlignment.start, children: [
          Text(c.killSwitchState == KillState.failed ? '⚠️ Kill switch finished with failures' : '🚨 Kill switch executed',
              style: TextStyle(fontWeight: FontWeight.w700, fontSize: 16, color: c.killSwitchState == KillState.failed ? Pal.danger : Pal.text)),
          const SizedBox(height: 10),
          Text(c.killSwitchLastResult ?? '', style: _mono),
          const SizedBox(height: 12),
        ]),
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    final auto = c.executionMode == ExecMode.auto;
    final lock = c.autoLockReason;
    final busy = c.killSwitchState == KillState.executing;
    return Panel(
      title: 'Mode & safety',
      child: Column(children: [
        SizedBox(
          width: double.infinity,
          child: Tooltip(
            message: (!auto && lock != null) ? 'AUTO locked: $lock' : '',
            child: FilledButton(
              style: FilledButton.styleFrom(
                backgroundColor: auto ? _a(Pal.emerald, .2) : _a(Pal.sky, .2),
                foregroundColor: auto ? Pal.emerald : Pal.sky,
                side: BorderSide(color: auto ? Pal.emerald : Pal.sky),
                padding: const EdgeInsets.symmetric(vertical: 13),
              ),
              onPressed: () => _toggle(context),
              child: Text(auto ? '🤖 Mode: AUTO (Tap for MANUAL)' : '📱 Mode: MANUAL (Tap for AUTO)', style: const TextStyle(fontWeight: FontWeight.w700)),
            ),
          ),
        ),
        if (!auto && lock != null)
          Padding(padding: const EdgeInsets.only(top: 6), child: Text('AUTO locked: $lock', style: const TextStyle(color: Pal.amber, fontSize: 12))),
        const SizedBox(height: 10),
        SizedBox(
          width: double.infinity,
          child: FilledButton(
            style: FilledButton.styleFrom(backgroundColor: Pal.danger, foregroundColor: Colors.white, padding: const EdgeInsets.symmetric(vertical: 13)),
            onPressed: busy ? null : () => _kill(context),
            child: busy
                ? const SizedBox(width: 18, height: 18, child: CircularProgressIndicator(strokeWidth: 2, color: Colors.white))
                : const Text('🚨 KILL SWITCH', style: TextStyle(fontWeight: FontWeight.w800)),
          ),
        ),
      ]),
    );
  }
}

// ---------- pending approvals ----------

class PendingApprovalsSection extends StatelessWidget {
  const PendingApprovalsSection({super.key, required this.c});
  final ShieldController c;
  @override
  Widget build(BuildContext context) {
    if (c.pendingCards.isEmpty) return const SizedBox.shrink();
    return Panel(
      title: '📱 Pending Manual Trade Approvals (Review & Tap to Execute)',
      border: Pal.sky,
      child: Column(children: [for (final card in c.pendingCards) PendingApprovalCard(c: c, card: card)]),
    );
  }
}

class PendingApprovalCard extends StatelessWidget {
  const PendingApprovalCard({super.key, required this.c, required this.card});
  final ShieldController c;
  final PendingCard card;

  Widget _kv(String k, String v, {Color color = Pal.text}) => Container(
        padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 6),
        decoration: BoxDecoration(color: Pal.bg, borderRadius: BorderRadius.circular(6), border: Border.all(color: Pal.border)),
        child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
          Text(k, style: const TextStyle(color: Pal.muted, fontSize: 10.5)),
          Text(v, style: _mono.copyWith(color: color, fontWeight: FontWeight.w700)),
        ]),
      );

  @override
  Widget build(BuildContext context) {
    final live = card.status == CardStatus.pending;
    final busy = card.status == CardStatus.approving;
    return Container(
      margin: const EdgeInsets.only(bottom: 10),
      padding: const EdgeInsets.all(10),
      decoration: BoxDecoration(color: Pal.surface, borderRadius: BorderRadius.circular(10), border: Border.all(color: _a(Pal.sky, .6))),
      child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
        Row(children: [
          Text('📈 ${card.ticker}', style: const TextStyle(fontWeight: FontWeight.w800, fontSize: 16)),
          const SizedBox(width: 8),
          Pill(tfLabel(card.timeframe), Pal.sky),
          if (card.offHours) ...[const SizedBox(width: 6), const Pill('OFF-HOURS', Pal.amber)],
          const Spacer(),
          Text('${card.shares} shares', style: const TextStyle(color: Pal.emerald, fontWeight: FontWeight.w700)),
        ]),
        const SizedBox(height: 8),
        Wrap(spacing: 6, runSpacing: 6, children: [
          _kv('Collar Buy ≤', priceStr(card.limitCollar), color: Pal.emerald),
          _kv('Stop', priceStr(card.stopLoss), color: const Color(0xFFF87171)),
          _kv('Target', priceStr(card.takeProfit), color: Pal.sky),
          _kv('Max Risk', usd(card.maxRisk), color: Pal.amber),
        ]),
        const SizedBox(height: 6),
        Text(
          'Entry ${priceStr(card.entry)} · Micro \$${card.microPrice.toStringAsFixed(2)} · '
          'Imb ${card.imbalance >= 0 ? '+' : ''}${card.imbalance.toStringAsFixed(2)} · Spread ${card.spreadBps.toStringAsFixed(1)} bps',
          style: const TextStyle(color: Pal.muted, fontSize: 11.5),
        ),
        const SizedBox(height: 2),
        Text('Source: ${card.source}', style: const TextStyle(color: Pal.muted, fontSize: 11.5)),
        const SizedBox(height: 6),
        if (live)
          ValueListenableBuilder<DateTime>(
            valueListenable: c.now,
            builder: (_, t, __) {
              final left = card.expiresAt.difference(t);
              final s = left.inSeconds.clamp(0, 9999);
              return Text('⏱ ${s ~/ 60}:${two(s % 60)} left', style: TextStyle(color: s < 60 ? Pal.amber : Pal.muted, fontSize: 12, fontWeight: FontWeight.w600));
            },
          ),
        if (busy) const Padding(padding: EdgeInsets.symmetric(vertical: 4), child: LinearProgressIndicator(color: Pal.emerald)),
        if (card.errorMessage != null && !live && !busy)
          Container(
            width: double.infinity,
            margin: const EdgeInsets.only(top: 4),
            padding: const EdgeInsets.all(8),
            decoration: BoxDecoration(
              color: _a(card.status == CardStatus.failed ? Pal.danger : Pal.amber, .15),
              borderRadius: BorderRadius.circular(6),
            ),
            child: Text(
              '${card.status == CardStatus.stale ? '🕒 Price moved too far — re-stage. ' : ''}${card.errorMessage}',
              style: const TextStyle(fontSize: 12.5),
            ),
          ),
        const SizedBox(height: 8),
        if (live || busy)
          Row(children: [
            Expanded(
              child: FilledButton(
                style: FilledButton.styleFrom(backgroundColor: Pal.emerald, foregroundColor: Colors.black),
                onPressed: busy ? null : () => c.approveCard(card.token),
                child: const Text('✅ Approve Fill', style: TextStyle(fontWeight: FontWeight.w700)),
              ),
            ),
            const SizedBox(width: 8),
            Expanded(
              child: OutlinedButton(
                style: OutlinedButton.styleFrom(foregroundColor: const Color(0xFFF87171), side: const BorderSide(color: Pal.border)),
                onPressed: busy ? null : () => c.rejectCard(card.token),
                child: const Text('❌ Reject'),
              ),
            ),
          ])
        else
          SizedBox(
            width: double.infinity,
            child: OutlinedButton(onPressed: () => c.dismissCard(card.token), child: const Text('Dismiss')),
          ),
      ]),
    );
  }
}

// ---------- metrics ----------

class PortfolioMetrics extends StatelessWidget {
  const PortfolioMetrics({super.key, required this.c, required this.wide});
  final ShieldController c;
  final bool wide;
  @override
  Widget build(BuildContext context) {
    final a = c.account;
    final p = a?.pnl;
    final up = (p?.pnl ?? 0) >= 0;
    final tiles = <Widget>[
      MetricTile('MY EQUITY', a == null ? '—' : usd(a.equity)),
      MetricTile('BUYING POWER', a == null ? '—' : usd(a.buyingPower), color: Pal.sky),
      MetricTile(
        'TODAY P&L',
        p == null ? '—' : usd(p.pnl, sign: true),
        color: up ? Pal.emerald : const Color(0xFFF87171),
        sub: p == null ? null : '${p.pct >= 0 ? '+' : ''}${p.pct.toStringAsFixed(2)}%',
      ),
      Container(
        padding: const EdgeInsets.all(12),
        decoration: BoxDecoration(color: Pal.surface, borderRadius: BorderRadius.circular(10), border: Border.all(color: Pal.border)),
        child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
          const Text('PRECISION COLLAR', style: TextStyle(color: Pal.muted, fontSize: 11)),
          const SizedBox(height: 4),
          const Text('✅ +15 bps Limit Collar', style: TextStyle(fontSize: 12)),
          const Text('✅ 1% Risk / 15% Cap', style: TextStyle(fontSize: 12)),
          if (a != null && a.drawdownHalted) const Text('⛔ Daily drawdown halt', style: TextStyle(fontSize: 12, color: Color(0xFFF87171), fontWeight: FontWeight.w700)),
          if (a != null && a.blocked) const Text('⛔ Account blocked', style: TextStyle(fontSize: 12, color: Color(0xFFF87171), fontWeight: FontWeight.w700)),
        ]),
      ),
    ];
    return Padding(
      padding: const EdgeInsets.only(bottom: 12),
      child: GridView.count(
        crossAxisCount: 2,
        shrinkWrap: true,
        physics: const NeverScrollableScrollPhysics(),
        mainAxisSpacing: 8,
        crossAxisSpacing: 8,
        childAspectRatio: wide ? 1.9 : 1.45,
        children: tiles,
      ),
    );
  }
}

// ---------- trade panel ----------

class TradePanel extends StatefulWidget {
  const TradePanel({super.key, required this.c});
  final ShieldController c;
  @override
  State<TradePanel> createState() => _TradePanelState();
}

class _TradePanelState extends State<TradePanel> {
  final _ticker = TextEditingController(text: 'NVDA');
  Timeframe _tf = Timeframe.swing;

  @override
  void dispose() {
    _ticker.dispose();
    super.dispose();
  }

  String _resultText(TradeResult r) => switch (r) {
        AutoExecuted(:final orderId, :final plan) =>
          '✅ AUTO order accepted (${orderId.length > 8 ? orderId.substring(0, 8) : orderId})\n${planSummary(plan)}',
        Staged(:final token, :final plan) => '📱 Staged card $token — approve it above.\n${planSummary(plan)}',
        Rejected(:final reason) => '⛔ $reason',
        Failed(:final error) => '❌ $error',
      };

  Color _resultColor(TradeResult r) => switch (r) {
        AutoExecuted() => Pal.emerald,
        Staged() => Pal.sky,
        Rejected() => Pal.amber,
        Failed() => Pal.danger,
      };

  @override
  Widget build(BuildContext context) {
    final c = widget.c;
    final r = c.lastTradeResult;
    return Panel(
      title: '🚀 1-Click Precision Trade (obeys AUTO vs MANUAL mode)',
      child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
        TextField(
          controller: _ticker,
          style: _mono.copyWith(fontSize: 15),
          textCapitalization: TextCapitalization.characters,
          inputFormatters: [TextInputFormatter.withFunction((o, n) => n.copyWith(text: n.text.toUpperCase()))],
          decoration: const InputDecoration(labelText: 'Ticker'),
        ),
        const SizedBox(height: 10),
        DropdownButtonFormField<Timeframe>(
          value: _tf,
          dropdownColor: Pal.surface,
          decoration: const InputDecoration(labelText: 'Timeframe'),
          items: const [
            DropdownMenuItem(value: Timeframe.swing, child: Text('SWING (GTC Bracket)')),
            DropdownMenuItem(value: Timeframe.intraday, child: Text('INTRADAY (Day Bracket)')),
          ],
          onChanged: (v) => setState(() => _tf = v ?? _tf),
        ),
        SwitchListTile(
          contentPadding: EdgeInsets.zero,
          activeColor: Pal.amber,
          title: const Text('Allow Off-Hours Staging', style: TextStyle(fontSize: 14)),
          subtitle: const Text('Stages a MANUAL card only (never AUTO). Still needs a live ask.', style: TextStyle(fontSize: 12, color: Pal.muted)),
          value: c.allowOffHoursStaging,
          onChanged: c.setAllowOffHours,
        ),
        SizedBox(
          width: double.infinity,
          child: FilledButton(
            style: FilledButton.styleFrom(backgroundColor: Pal.emerald, foregroundColor: Colors.black, padding: const EdgeInsets.symmetric(vertical: 13)),
            onPressed: c.tradeSubmitting ? null : () => c.triggerTrade(ticker: _ticker.text, timeframe: _tf, source: 'Manual Trigger'),
            child: c.tradeSubmitting
                ? const SizedBox(width: 18, height: 18, child: CircularProgressIndicator(strokeWidth: 2, color: Colors.black))
                : const Text('Calculate Risk & Route Trade', style: TextStyle(fontWeight: FontWeight.w700)),
          ),
        ),
        if (r != null)
          Container(
            width: double.infinity,
            margin: const EdgeInsets.only(top: 10),
            padding: const EdgeInsets.all(10),
            decoration: BoxDecoration(color: _a(_resultColor(r), .12), border: Border.all(color: _resultColor(r)), borderRadius: BorderRadius.circular(8)),
            child: Text(_resultText(r), style: _mono.copyWith(fontSize: 12)),
          ),
      ]),
    );
  }
}

class AiScannerPanel extends StatefulWidget {
  const AiScannerPanel({super.key, required this.c});
  final ShieldController c;
  @override
  State<AiScannerPanel> createState() => _AiScannerPanelState();
}

class _AiScannerPanelState extends State<AiScannerPanel> {
  final _headline = TextEditingController();
  @override
  void dispose() {
    _headline.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final c = widget.c;
    final d = c.lastAiDecision;
    return Panel(
      title: '🧠 Live Gemini AI Catalyst Scanner',
      border: _a(Pal.indigo, .7),
      child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
        TextField(controller: _headline, minLines: 2, maxLines: 4, decoration: const InputDecoration(labelText: 'Breaking news headline')),
        const SizedBox(height: 10),
        SizedBox(
          width: double.infinity,
          child: FilledButton(
            style: FilledButton.styleFrom(backgroundColor: Pal.indigo, foregroundColor: Colors.white, padding: const EdgeInsets.symmetric(vertical: 13)),
            onPressed: (!c.aiScannerEnabled || c.scanInProgress) ? null : () => c.evaluateHeadline(_headline.text),
            child: c.scanInProgress
                ? const SizedBox(width: 18, height: 18, child: CircularProgressIndicator(strokeWidth: 2, color: Colors.white))
                : const Text('Evaluate with Gemini & Route', style: TextStyle(fontWeight: FontWeight.w700)),
          ),
        ),
        if (!c.aiScannerEnabled)
          const Padding(padding: EdgeInsets.only(top: 6), child: Text('Add a Gemini API key in the Key Vault to enable this.', style: TextStyle(color: Pal.muted, fontSize: 12))),
        if (d != null)
          Container(
            width: double.infinity,
            margin: const EdgeInsets.only(top: 10),
            padding: const EdgeInsets.all(10),
            decoration: BoxDecoration(color: _a(Pal.indigo, .12), border: Border.all(color: Pal.indigo), borderRadius: BorderRadius.circular(8)),
            child: Text(
              'Trade: ${d.trade ? 'YES' : 'NO'} · Ticker: ${d.ticker}\nTier ${d.sourceTier} · Sentiment ${d.sentiment.toStringAsFixed(2)}\n${d.reason}',
              style: _mono.copyWith(fontSize: 12),
            ),
          ),
      ]),
    );
  }
}

// ---------- positions, orders, feed ----------

class PositionsOrdersPanel extends StatelessWidget {
  const PositionsOrdersPanel({super.key, required this.c});
  final ShieldController c;
  @override
  Widget build(BuildContext context) {
    final eq = c.account?.equity ?? 0;
    return Panel(
      title: '📂 My Open Positions & Collared Orders',
      child: (c.positions.isEmpty && c.orders.isEmpty)
          ? const Text('Account is flat.', style: TextStyle(color: Pal.muted))
          : Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
              for (final p in c.positions)
                Builder(builder: (_) {
                  final pct = eq > 0 ? p.marketValue / eq * 100 : 0.0;
                  final over = pct > 15.0;
                  return Padding(
                    padding: const EdgeInsets.only(bottom: 6),
                    child: Text.rich(TextSpan(style: _mono, children: [
                      TextSpan(text: '🟢 POS: ${p.symbol} (${p.qty} sh @ ${usd(p.avgEntry)}) | P&L: '),
                      TextSpan(text: usd(p.unrealizedPl, sign: true), style: TextStyle(color: p.unrealizedPl >= 0 ? Pal.emerald : const Color(0xFFF87171))),
                      TextSpan(text: '  ${pct.toStringAsFixed(1)}% of equity', style: TextStyle(color: over ? Pal.amber : Pal.muted)),
                      if (over) const TextSpan(text: ' ⚠ >15%', style: TextStyle(color: Pal.amber)),
                    ])),
                  );
                }),
              if (c.orders.isNotEmpty) const Divider(color: Pal.border),
              for (final o in c.orders)
                o.legs.isEmpty
                    ? Padding(padding: const EdgeInsets.only(bottom: 6), child: Text('⏳ ORD: ${o.line}', style: _mono))
                    : Theme(
                        data: Theme.of(context).copyWith(dividerColor: Colors.transparent),
                        child: ExpansionTile(
                          tilePadding: EdgeInsets.zero,
                          title: Text('⏳ ORD: ${o.line}', style: _mono),
                          children: [for (final l in o.legs) Padding(padding: const EdgeInsets.only(left: 16, bottom: 4), child: Align(alignment: Alignment.centerLeft, child: Text('↳ ${l.line}', style: _mono.copyWith(color: Pal.muted))))],
                        ),
                      ),
            ]),
    );
  }
}

class ExecutionFeedPanel extends StatelessWidget {
  const ExecutionFeedPanel({super.key, required this.c});
  final ShieldController c;
  @override
  Widget build(BuildContext context) => Panel(
        title: '⚡ Instant Automation & Mode Feed',
        child: c.feed.isEmpty
            ? const Text('No events yet.', style: TextStyle(color: Pal.muted))
            : Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                for (final e in c.feed)
                  Padding(
                    padding: const EdgeInsets.only(bottom: 4),
                    child: Text(e.text,
                        style: _mono.copyWith(
                          fontSize: 11.5,
                          color: switch (e.level) {
                            'error' => const Color(0xFFF87171),
                            'warn' => Pal.amber,
                            'ok' => Pal.emerald,
                            _ => Pal.text,
                          },
                        )),
                  ),
              ]),
      );
}

// ---------- watch HUD ----------

/// Renders only a WatchSnapshot and sends WatchCommands through a WatchLink.
/// Holds no keys and no trading logic — reusable verbatim in a Wear OS entry point.
class WatchHudScreen extends StatefulWidget {
  const WatchHudScreen({super.key, required this.link});
  final WatchLink link;
  @override
  State<WatchHudScreen> createState() => _WatchHudScreenState();
}

class _WatchHudScreenState extends State<WatchHudScreen> {
  Timer? _t, _confirmTimer;
  DateTime _now = DateTime.now().toUtc();
  String? _banner;
  bool _bannerBad = false, _confirmAuto = false, _busy = false;

  @override
  void initState() {
    super.initState();
    _t = Timer.periodic(const Duration(seconds: 1), (_) {
      if (mounted) setState(() => _now = DateTime.now().toUtc());
    });
    widget.link.addListener(_onLink);
  }

  void _onLink() {
    if (mounted) setState(() {});
  }

  @override
  void dispose() {
    _t?.cancel();
    _confirmTimer?.cancel();
    widget.link.removeListener(_onLink);
    super.dispose();
  }

  Future<void> _send(WatchCommand cmd) async {
    setState(() {
      _busy = true;
      _banner = null;
    });
    final ack = await widget.link.send(cmd, DateTime.now().microsecondsSinceEpoch.toString());
    if (!mounted) return;
    setState(() {
      _busy = false;
      if (ack.result == WatchSendResult.phoneOffline) {
        // Decision 5: never fail silently.
        _banner = '⛔ PHONE OFFLINE – NOT SENT\n${cmd == WatchCommand.killSwitch ? 'Use the PHONE app NOW to flatten.' : 'Mode was NOT changed.'}';
        _bannerBad = true;
        HapticFeedback.heavyImpact();
      } else {
        _banner = '✓ Sent to phone${ack.detail == null ? '' : ' — ${ack.detail}'}';
        _bannerBad = ack.detail?.contains('REFUSED') == true || ack.detail?.contains('refused') == true || ack.detail?.contains('FAILURES') == true;
      }
    });
  }

  void _onModeTap(WatchSnapshot? s) {
    if (s?.mode == 'AUTO') {
      _send(WatchCommand.setModeManual);
    } else if (!_confirmAuto) {
      setState(() => _confirmAuto = true);
      _confirmTimer?.cancel();
      _confirmTimer = Timer(const Duration(seconds: 4), () {
        if (mounted) setState(() => _confirmAuto = false);
      });
    } else {
      _confirmTimer?.cancel();
      setState(() => _confirmAuto = false);
      _send(WatchCommand.setModeAuto);
    }
  }

  @override
  Widget build(BuildContext context) {
    final link = widget.link;
    final s = link.snapshot;
    final offline = !link.phoneReachable || s == null || _now.difference(s.updatedAt) > const Duration(seconds: 30);
    final auto = s?.mode == 'AUTO';
    return Scaffold(
      backgroundColor: Colors.black,
      body: SafeArea(
        child: SingleChildScrollView(
          padding: const EdgeInsets.all(10),
          child: Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
            Row(mainAxisAlignment: MainAxisAlignment.center, children: [
              Icon(Icons.circle, size: 9, color: offline ? Pal.danger : Pal.emerald),
              const SizedBox(width: 6),
              Text(offline ? 'Phone offline' : 'MYT ${hms(mytTime(_now))}', style: TextStyle(fontSize: 12, color: offline ? Pal.danger : Pal.muted, fontWeight: FontWeight.w700)),
            ]),
            const SizedBox(height: 6),
            const Text('MY ACCOUNT EQUITY', textAlign: TextAlign.center, style: TextStyle(color: Pal.muted, fontSize: 10)),
            FittedBox(fit: BoxFit.scaleDown, child: Text(s?.equity ?? '—', style: const TextStyle(color: Colors.white, fontSize: 30, fontWeight: FontWeight.w800))),
            Text(s?.pnlText ?? '—', textAlign: TextAlign.center, style: TextStyle(fontSize: 13, color: (s?.pnlPositive ?? true) ? Pal.emerald : const Color(0xFFF87171), fontWeight: FontWeight.w700)),
            const SizedBox(height: 8),
            FilledButton(
              style: FilledButton.styleFrom(
                backgroundColor: _confirmAuto ? _a(Pal.amber, .3) : (auto ? _a(Pal.emerald, .2) : _a(Pal.sky, .2)),
                foregroundColor: _confirmAuto ? Pal.amber : (auto ? Pal.emerald : Pal.sky),
                padding: const EdgeInsets.symmetric(vertical: 10),
              ),
              onPressed: _busy ? null : () => _onModeTap(s),
              child: Text(
                _confirmAuto ? 'TAP AGAIN TO CONFIRM AUTO' : (auto ? '🤖 AUTO EXECUTION ON\nTap to switch to MANUAL' : '📱 MANUAL PHONE MODE'),
                textAlign: TextAlign.center,
                style: const TextStyle(fontSize: 12, fontWeight: FontWeight.w700),
              ),
            ),
            if ((s?.pendingCount ?? 0) > 0)
              Padding(
                padding: const EdgeInsets.only(top: 6),
                child: GestureDetector(
                  onTap: HapticFeedback.mediumImpact,
                  child: Text('${s!.pendingCount} pending on phone', textAlign: TextAlign.center, style: const TextStyle(color: Pal.sky, fontSize: 12, fontWeight: FontWeight.w700)),
                ),
              ),
            const SizedBox(height: 8),
            const Text('⚡ INSTANT EXECUTION FEED', style: TextStyle(color: Pal.muted, fontSize: 9.5)),
            const SizedBox(height: 2),
            for (final line in (s?.feedTop3 ?? const <String>[])) Text(line, maxLines: 2, overflow: TextOverflow.ellipsis, style: const TextStyle(fontFamily: 'monospace', fontSize: 10, color: Colors.white70)),
            const SizedBox(height: 8),
            FilledButton(
              style: FilledButton.styleFrom(backgroundColor: Pal.danger, foregroundColor: Colors.white, padding: const EdgeInsets.symmetric(vertical: 10)),
              onPressed: _busy ? null : () => setState(() {
                    _banner = 'Press and HOLD to send the kill switch';
                    _bannerBad = false;
                  }),
              onLongPress: _busy ? null : () {
                HapticFeedback.heavyImpact();
                _send(WatchCommand.killSwitch);
              },
              child: const Text('🚨 KILL SWITCH & FLATTEN\n(hold to confirm)', textAlign: TextAlign.center, style: TextStyle(fontSize: 12, fontWeight: FontWeight.w800)),
            ),
            if (_busy) const Padding(padding: EdgeInsets.only(top: 6), child: LinearProgressIndicator(color: Pal.danger)),
            if (_banner != null)
              Container(
                margin: const EdgeInsets.only(top: 8),
                padding: const EdgeInsets.all(8),
                decoration: BoxDecoration(
                  color: _bannerBad ? Pal.danger : _a(Pal.emerald, .2),
                  border: Border.all(color: _bannerBad ? Colors.white : Pal.emerald, width: _bannerBad ? 2 : 1),
                  borderRadius: BorderRadius.circular(8),
                ),
                child: Text(_banner!, textAlign: TextAlign.center, style: TextStyle(fontSize: _bannerBad ? 13 : 11.5, fontWeight: FontWeight.w800, color: Colors.white)),
              ),
          ]),
        ),
      ),
    );
  }
}

class WatchPreviewScreen extends StatelessWidget {
  const WatchPreviewScreen({super.key, required this.link});
  final PreviewWatchLink link;
  @override
  Widget build(BuildContext context) => Scaffold(
        appBar: AppBar(title: const Text('Watch HUD preview')),
        body: ListenableBuilder(
          listenable: link,
          builder: (context, _) => ListView(padding: const EdgeInsets.all(16), children: [
            Center(
              child: Container(
                width: 300,
                height: 380,
                decoration: BoxDecoration(border: Border.all(color: Pal.border, width: 3), borderRadius: BorderRadius.circular(36)),
                clipBehavior: Clip.antiAlias,
                child: WatchHudScreen(link: link),
              ),
            ),
            const SizedBox(height: 16),
            SwitchListTile(
              activeColor: Pal.danger,
              title: const Text('Simulate phone link lost'),
              subtitle: const Text('Then tap the watch kill switch to see the "NOT SENT" state.', style: TextStyle(color: Pal.muted, fontSize: 12)),
              value: !link.phoneReachable,
              onChanged: (v) => link.reachable = !v,
            ),
            const Text(
              'This preview runs in-process. A physical Wear OS watch needs a separate Wear module whose WatchLink '
              'uses the Wear Data Layer (DataClient for snapshots, MessageClient for commands).',
              style: TextStyle(color: Pal.muted, fontSize: 12),
            ),
          ]),
        ),
      );
}
