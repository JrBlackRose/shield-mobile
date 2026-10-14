// ============================================================================
// PROJECT SHIELD v4.0 — lib/shield_core.dart
// Exact integer-pips risk math, shared models, Alpaca/Gemini clients & WatchLink
// ============================================================================

import 'dart:convert';
import 'dart:math' as math;

import 'package:flutter/foundation.dart';
import 'package:http/http.dart' as http;

typedef Pips = int;

const int kPipsPerUsd = 10000;
const int kBpsDenom = 10000;

enum Timeframe { swing, intraday }
enum VaultStatus { empty, unvalidated, validating, valid, invalid }
enum ConnectionStatus { disconnected, connecting, connected, authError, networkError }
enum ExecMode { auto, manual }
enum KillState { idle, confirming, executing, done, failed }
enum CardStatus { pending, approving, executed, rejected, expired, stale, failed }

abstract final class RiskRules {
  static const int riskBudgetBps = 100;
  static const int notionalCapBps = 1500;
  static const int collarBps = 15;
  static const int swingStopBps = 1000;
  static const int swingTargetBps = 3000;
  static const int intradayStopBps = 100;
  static const int intradayTargetBps = 300;
  static const int maxDriftOfStopBps = 2000;
}

class RiskReject implements Exception {
  final String message;
  const RiskReject(this.message);
  @override
  String toString() => message;
}

class PlanAbort implements Exception {
  final String message;
  const PlanAbort(this.message);
  @override
  String toString() => message;
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
    return '$service error $status:$message';
  }
}

Pips parsePips(String? raw) {
  if (raw == null || raw.trim().isEmpty) throw const PlanAbort('Missing numeric value.');
  final t = raw.trim();
  try {
    final neg = t.startsWith('-');
    final parts = (neg ? t.substring(1) : t).split('.');
    final whole = int.parse(parts[0].isEmpty ? '0' : parts[0]);
    final frac = (parts.length > 1 ? parts[1] : '').padRight(5, '0');
    var fracPips = int.parse(frac.substring(0, 4));
    if (frac.codeUnitAt(4) >= 0x35) fracPips += 1;
    final v = whole * kPipsPerUsd + fracPips;
    return neg ? -v : v;
  } on FormatException {
    throw PlanAbort('Malformed numeric value: "$t".');
  }
}

Pips? tryPips(dynamic v) {
  if (v == null) return null;
  try {
    return parsePips(v.toString());
  } catch (_) {
    return null;
  }
}

String toApiPrice(Pips p) {
  final usd = p ~/ kPipsPerUsd;
  final rem = p % kPipsPerUsd;
  return p >= kPipsPerUsd
      ? '$usd.${(rem ~/ 100).toString().padLeft(2, '0')}'
      : '$usd.${rem.toString().padLeft(4, '0')}';
}

Pips _roundToTickRatio(int num, int den) {
  final atOrAboveOne = num >= kPipsPerUsd * den;
  final tick = atOrAboveOne ? 100 : 1;
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

double maxSpreadBps(Timeframe tf) => tf == Timeframe.intraday ? 45.0 : 80.0;
bool spreadAllowed(double spreadBps, Timeframe tf) => spreadBps <= maxSpreadBps(tf);

const int kPollSec = 5;
const Duration kCardTtl = Duration(minutes: 5);
const String kGeminiModel = 'gemini-2.5-flash';
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

bool isNyDst(DateTime utc) {
  final u = utc.toUtc();
  return !u.isBefore(_sundayUtc(u.year, 3, 2, 7)) && u.isBefore(_sundayUtc(u.year, 11, 1, 6));
}

DateTime nyTime(DateTime utc) => utc.toUtc().add(Duration(hours: isNyDst(utc) ? -4 : -5));
DateTime mytTime(DateTime utc) => utc.toUtc().add(const Duration(hours: 8));

String? intradayWindowMessage(DateTime utc) {
  final ny = nyTime(utc);
  final mins = ny.hour * 60 + ny.minute;
  if (ny.weekday >= 6 || mins < 9 * 60 + 35 || mins > 15 * 60 + 50) {
    return 'Outside Intraday Window (NY Time ${two(ny.hour)}:${two(ny.minute)} not in 09:35-15:50)';
  }
  return null;
}

class Vault {
  Vault({this.alpacaKey = '', this.alpacaSecret = '', this.geminiKey = '', this.tgToken = '', this.tgChat = ''});
  String alpacaKey, alpacaSecret, geminiKey, tgToken, tgChat;
  bool get hasRequired => alpacaKey.isNotEmpty && alpacaSecret.isNotEmpty;
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
    return '$symbol $side$qty @ $px ($status)';
  }
}

class FeedEvent {
  FeedEvent(this.time, this.message, this.level);
  final DateTime time;
  final String message, level;
  Map<String, dynamic> toJson() => {'t': time.toIso8601String(), 'm': message, 'l': level};
  factory FeedEvent.fromJson(Map<String, dynamic> j) =>
      FeedEvent(DateTime.parse(j['t'] as String), j['m'] as String, (j['l'] ?? 'info') as String);
  String get text => '[${hms(mytTime(time))}]$message';
}

class ConfluenceSignal {
  const ConfluenceSignal({required this.detail, required this.strength});
  final String detail;
  final double strength;
}

class ConfluenceScore {
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
  final DateTime asOf;

  bool get aboveVwap => price > vwapValue;
  bool get volumeSurge => volumeRatio >= 1.5;
  bool get rsiConstructive => rsiValue >= 50.0 && rsiValue < 70.0;
  bool get rsiOverbought => rsiValue >= 70.0;

  int get points => (aboveVwap ? 1 : 0) + (volumeSurge ? 1 : 0) + (rsiConstructive ? 1 : 0);
  int get score => ((points / 3.0) * 100).round();
  String get grade => points == 3 ? 'STRONG' : (points == 2 ? 'MODERATE' : 'WEAK');

  ConfluenceSignal get vwap => ConfluenceSignal(
        detail: '${aboveVwap ? "▲" : "▼"} \$${vwapValue.toStringAsFixed(2)}',
        strength: aboveVwap ? 1.0 : 0.25,
      );
  ConfluenceSignal get rsi => ConfluenceSignal(
        detail: '${rsiValue.toStringAsFixed(1)}${rsiOverbought ? " (OB)" : ""}',
        strength: rsiConstructive ? 1.0 : (rsiOverbought ? 0.2 : 0.5),
      );
  ConfluenceSignal get volume => ConfluenceSignal(
        detail: '${volumeRatio.toStringAsFixed(2)}x avg',
        strength: (volumeRatio / 2.0).clamp(0.0, 1.0).toDouble(),
      );

  String get summary => '$points/3$grade · VWAP ${aboveVwap ? "▲" : "▼"} ${vwapValue.toStringAsFixed(2)}'
      ' · Vol ${volumeRatio.toStringAsFixed(2)}x'
      ' · RSI ${rsiValue.toStringAsFixed(1)}${rsiOverbought ? " (overbought)" : ""}';

  Map<String, dynamic> toJson() => {
        's': symbol,
        'p': price,
        'vw': vwapValue,
        'vr': volumeRatio,
        'rsi': rsiValue,
        'n': barsUsed,
        't': asOf.toIso8601String(),
      };

  factory ConfluenceScore.fromJson(Map<String, dynamic> j) => ConfluenceScore(
        symbol: (j['s'] ?? '').toString(),
        price: (j['p'] as num).toDouble(),
        vwapValue: (j['vw'] as num).toDouble(),
        volumeRatio: (j['vr'] as num).toDouble(),
        rsiValue: (j['rsi'] as num).toDouble(),
        barsUsed: (j['n'] as num).toInt(),
        asOf: DateTime.parse(j['t'] as String),
      );
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
    this.confluence,
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
  final ConfluenceScore? confluence;
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
        'conf': confluence?.toJson(),
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
        confluence: j['conf'] is Map ? ConfluenceScore.fromJson((j['conf'] as Map).cast<String, dynamic>()) : null,
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

class ChatAction {
  const ChatAction({required this.label, this.level = 'info'});
  final String label;
  final String level;
}

class ChatMessage {
  const ChatMessage({
    required this.isUser,
    required this.text,
    required this.time,
    this.actions = const [],
  });
  final bool isUser;
  final String text;
  final DateTime time;
  final List<ChatAction> actions;
}

class NewsItem {
  const NewsItem({
    required this.id,
    required this.headline,
    required this.summary,
    required this.source,
    required this.symbols,
    required this.createdAt,
    required this.sentiment,
  });
  final String id, headline, summary, source;
  final List<String> symbols;
  final DateTime createdAt;
  final double sentiment;
}

class DefenseAlert {
  const DefenseAlert({required this.position, required this.news});
  final Position position;
  final NewsItem news;
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
    '${p.shares} sh | Collar ≤ ${priceStr(p.limitCollar)} | Stop${priceStr(p.stopLoss)} | '
    'Target ${priceStr(p.takeProfit)} | Max risk${usd(p.maxRisk)}';

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
    if (r.statusCode >= 200 && r.statusCode < 300) return body;
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

  Future<Map<String, dynamic>> latestQuote(String symbol) async {
    final b = await _get('$kDataBase/stocks/$symbol/quotes/latest?feed=iex');
    if (b is Map && b['quote'] is Map) return (b['quote'] as Map).cast<String, dynamic>();
    throw PlanAbort('No quote returned for $symbol.');
  }

  Future<Map<String, dynamic>> getSnapshotFallback(String symbol) async {
    final b = await _get('$kDataBase/stocks/${Uri.encodeComponent(symbol)}/snapshot?feed=iex');
    if (b is Map) return b.cast<String, dynamic>();
    throw PlanAbort('No snapshot returned for $symbol.');
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
    final text = await _callGemini(_prompt + headline);
    try {
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

  Future<String> chatCopilot(String prompt) => _callGemini(prompt);

  Future<String> _callGemini(String promptText) async {
    final res = await _http
        .post(
          Uri.parse('https://generativelanguage.googleapis.com/v1beta/models/$model:generateContent'),
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
    if (res.statusCode != 200) {
      var msg = 'HTTP ${res.statusCode}';
      try {
        msg = (jsonDecode(res.body)['error']['message'] ?? msg).toString();
      } catch (_) {}
      throw ApiException('Gemini', res.statusCode, msg);
    }
    try {
      final outer = jsonDecode(res.body) as Map<String, dynamic>;
      var text = (outer['candidates'][0]['content']['parts'][0]['text'] as String).trim();
      if (text.startsWith('```')) text = text.replaceAll(RegExp(r'^```(?:json)?|```$'), '').trim();
      return text;
    } catch (_) {
      throw const PlanAbort('Gemini returned a malformed response.');
    }
  }
}

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
  final String equity, pnlText, mode;
  final bool pnlPositive, connectionOk;
  final int pendingCount;
  final List<String> feedTop3;
  final DateTime updatedAt;
}

typedef WatchAck = ({WatchSendResult result, String? detail});

abstract class WatchLink extends ChangeNotifier {
  bool get phoneReachable;
  WatchSnapshot? get snapshot;
  Future<WatchAck> send(WatchCommand cmd, String requestId);
}

class PreviewWatchLink extends WatchLink {
  dynamic phone;
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
    if (!_reachable) return;
    _snap = s;
    notifyListeners();
  }

  @override
  Future<WatchAck> send(WatchCommand cmd, String requestId) async {
    if (!_reachable || phone == null) return (result: WatchSendResult.phoneOffline, detail: null);
    final detail = await phone.handleWatchCommand(cmd) as String?;
    return (result: WatchSendResult.sent, detail: detail);
  }
}
