// ============================================================================
// PROJECT SHIELD — lib/shield_crypto_guardian.dart        (NEW, `part of`)
// ============================================================================

part of 'shield_controller.dart';

extension _StopApi on _AlpacaExtras {
  Map<String, String> get _jsonH => {..._h, 'Content-Type': 'application/json'};

  Future<Map<String, dynamic>> placeStopLimitSell({
    required String symbol,
    required String qtyText,
    required Pips stop,
    required Pips limit,
    required String clientOrderId,
  }) async {
    final body = {
      'symbol': symbol,
      'qty': qtyText,
      'side': 'sell',
      'type': 'stop_limit',
      'time_in_force': 'gtc',
      'stop_price': toApiPrice(stop),
      'limit_price': toApiPrice(limit),
      'client_order_id': clientOrderId,
    };
    final res = await _http
        .post(Uri.parse('$kPaperBase/orders'), headers: _jsonH, body: jsonEncode(body))
        .timeout(_kV4Timeout);
    return (_decode(res) as Map).cast<String, dynamic>();
  }

  Future<Map<String, dynamic>> replaceStop({
    required String orderId,
    required Pips stop,
    required Pips limit,
  }) async {
    final res = await _http
        .patch(
          Uri.parse('$kPaperBase/orders/${Uri.encodeComponent(orderId)}'),
          headers: _jsonH,
          body: jsonEncode({'stop_price': toApiPrice(stop), 'limit_price': toApiPrice(limit)}),
        )
        .timeout(_kV4Timeout);
    return (_decode(res) as Map).cast<String, dynamic>();
  }

  // ignore: unused_element
  // ignore: unused_element
  // ignore: unused_element
  Future<Map<String, dynamic>> placeTrailingStopSell({
    required String symbol,
    required int shares,
    required double trailPercent,
    required String clientOrderId,
  }) async {
    final body = {
      'symbol': symbol,
      'qty': '$shares',
      'side': 'sell',
      'type': 'trailing_stop',
      'trail_percent': trailPercent.toStringAsFixed(2),
      'time_in_force': 'gtc',
      'client_order_id': clientOrderId,
    };
    final res = await _http
        .post(Uri.parse('$kPaperBase/orders'), headers: _jsonH, body: jsonEncode(body))
        .timeout(_kV4Timeout);
    return (_decode(res) as Map).cast<String, dynamic>();
  }
}

class _CryptoGuardian {
  _CryptoGuardian(this.c);
  final ShieldController c;

  final Map<String, TrailState> trails = <String, TrailState>{};
  final Map<String, ({DateTime at, CryptoVol vol})> _volCache = {};
  final Map<String, int> _breach = <String, int>{};
  bool _busy = false, _loaded = false;
  DateTime _lastRun = DateTime.fromMillisecondsSinceEpoch(0, isUtc: true);
  String? _lastErr;

  void _loadOnce() {
    if (_loaded) return;
    _loaded = true;
    try {
      final raw = jsonDecode(c._prefs.getString('sh_trails') ?? '{}') as Map;
      for (final e in raw.entries) {
        trails['${e.key}'] = TrailState.fromJson((e.value as Map).cast<String, dynamic>());
      }
    } catch (_) {}
  }

  void _save() => c._prefs.setString('sh_trails', jsonEncode({for (final e in trails.entries) e.key: e.value.toJson()}));

  void clear() {
    trails.clear();
    _volCache.clear();
    _breach.clear();
    c._prefs.remove('sh_trails');
  }

  void seed(TrailState s) {
    _loadOnce();
    trails[s.symbol] = s;
    _save();
  }

  List<Candle> _parse(List<Map<String, dynamic>> raw, Duration bar, DateTime now) {
    final out = raw.map(candleFromJson).whereType<Candle>().toList()..sort((a, b) => a.t.compareTo(b.t));
    if (out.isNotEmpty && out.last.t.add(bar).isAfter(now)) out.removeLast();
    return out;
  }

  Future<CryptoVol?> volFor(String sym, {bool fresh = false}) async {
    final now = DateTime.now().toUtc();
    final hit = _volCache[sym];
    if (!fresh && hit != null && now.difference(hit.at) < const Duration(minutes: 2)) return hit.vol;
    final ex = c._extras();
    final res = await Future.wait<List<Map<String, dynamic>>>([
      ex.barsCustom(sym, timeframe: '1Hour', limit: 72),
      ex.barsCustom(sym, timeframe: '15Min', limit: 300),
    ]);
    final v = estimateCryptoVol(
      hourBars: _parse(res[0], const Duration(hours: 1), now),
      quarterBars: _parse(res[1], const Duration(minutes: 15), now),
    );
    if (v != null) _volCache[sym] = (at: now, vol: v);
    return v;
  }

  Future<void> run() async {
    if (_busy || c.client == null || c.vaultStatus != VaultStatus.valid) return;
    if (c.killSwitchState == KillState.executing) return;
    final now = DateTime.now().toUtc();
    if (now.difference(_lastRun) < const Duration(seconds: 8)) return;
    _busy = true;
    _lastRun = now;
    try {
      _loadOnce();
      final ex = c._extras();
      final held = <String, Position>{
        for (final p in c.positions)
          if (p.isLong && isCryptoSymbol(p.symbol) && p.qtyDouble >= 0.000001 && p.marketValue.abs() >= (kPipsPerUsd * 3 ~/ 2)) normSym(p.symbol): p,
      };

      var dirty = false;
      trails.removeWhere((sym, t) {
        final gone = !held.containsKey(sym);
        final expiredSeed = !t.armed && now.difference(t.createdAt) > const Duration(minutes: 15);
        final closed = t.armed && gone;
        if (closed || (gone && expiredSeed)) {
          dirty = true;
          _breach.remove(sym);
          return true;
        }
        return false;
      });
      if (dirty) _save();

      for (final e in held.entries) {
        if (c._closing.contains(e.key)) continue;
        try {
          await _protect(ex, e.key, e.value, now);
        } catch (err) {
          final msg = c._errText(err);
          if (_lastErr != msg) {
            _lastErr = msg;
            c._log('🛡️ GUARDIAN ${e.key}:$msg', level: 'warn', notify: false);
          }
        }
      }
    } finally {
      _busy = false;
    }
  }

  Future<void> _protect(_AlpacaExtras ex, String sym, Position p, DateTime now) async {
    final last = p.currentPrice;
    if (last <= 0) return;
    CryptoVol? vol;
    try {
      vol = await volFor(sym);
    } catch (_) {
      vol = null;
    }
    final existing = trails[sym];

    final TrailState t;
    if (existing != null && existing.armed) {
      t = existing;
    } else {
      if (existing == null && vol == null) return;
      t = _arm(sym, p, existing, vol);
      trails[sym] = t;
      _save();
      c._log('🛡️ GUARDIAN armed $sym: stop ${priceStr(t.stop)} / limit${priceStr(t.stopLimit)}', level: 'ok', notify: false);
    }
    if (vol != null) t.atrPct = vol.atr1hPct;

    Map<String, dynamic>? stopOrder;
    for (final o in c._rawOrders) {
      final type = '${o['type'] ?? o['order_type']}';
      final status = '${o['status']}'.toLowerCase();
      if (status == 'canceled' || status == 'expired' || status == 'rejected' || status == 'pending_cancel') {
        continue;
      }
      if ('${o['side']}' == 'sell' && type == 'stop_limit' && normSym('${o['symbol']}') == sym) {
        stopOrder = o;
        break;
      }
    }

    if (stopOrder != null && last <= t.stopLimit) {
      final n = (_breach[sym] ?? 0) + 1;
      _breach[sym] = n;
      if (n >= 2) {
        c._log('🚨 GUARDIAN $sym is through its stop-limit and unfilled. Market exit.', level: 'error', notify: false);
        final res = await c.closePosition(sym);
        unawaited(c._notifyOS(res.ok ? '🛡️ Guardian exit: $sym' : '⚠️ Guardian exit FAILED: $sym', res.message));
        _breach.remove(sym);
        return;
      }
    } else {
      _breach.remove(sym);
    }

    final qtyTxt = floorQtyText(p.qtyDouble);
    final targetQty = double.tryParse(qtyTxt) ?? 0.0;
    if (targetQty <= 0.0) return;
    // Let trailing stop work; only hard-harvest crypto on a >= +4.2% (>2R) surge
    if (p.avgEntry > 0 && last >= (p.avgEntry * 10145 ~/ 10000)) {
      c._log('💰 GUARDIAN banking +1.45% crypto target on $sym (+${((last - p.avgEntry) / p.avgEntry * 100).toStringAsFixed(2)}%)', level: 'ok');
      final res = await c.closePosition(sym);
      unawaited(c._notifyOS('💰 Crypto Surge Banked: $sym', res.message));
      return;
    }
    if (stopOrder == null) {
      final ceiling = last * (kBpsDenom - CryptoRules.minStopGapBps) ~/ kBpsDenom;
      final stop = roundToTick(math.min(t.stop, ceiling));
      final limit = math.min(t.stopLimit, stopLimitFor(stop, t.atrPct));
      final o = await ex.placeStopLimitSell(
        symbol: _cryptoSlash(sym),
        qtyText: floorQtyText(p.qtyDouble),
        stop: stop,
        limit: limit,
        clientOrderId: 'shield-sl-$sym-${now.millisecondsSinceEpoch ~/ 1000}',
      );
      t.orderId = '${o['id']}';
      _save();
      c._log('🛡️ GUARDIAN stop-limit set $sym ${floorQtyText(p.qtyDouble)} @${priceStr(stop)}', level: 'ok', notify: false);
      return;
    }

    final status = '${stopOrder['status']}'.toLowerCase();
    if (status == 'pending_new' || status == 'pending_replace') {
      return;
    }

    final oq = double.tryParse('${stopOrder['qty']}') ?? 0.0;
    if (oq > 0 && (targetQty - oq).abs() / targetQty > 0.01) {
      await ex.cancelOrderById('${stopOrder['id']}');
      c._log('🛡️ GUARDIAN $sym position size changed; re-placing stop', level: 'warn', notify: false);
      return;
    }

    final hwBefore = t.highWater;
      if (p.avgEntry > 0) t.entry = p.avgEntry;
      if (last > t.highWater) t.highWater = last;
      var r = ratchetStop(t, last);
      final entryPx = p.avgEntry > 0 ? p.avgEntry : t.entry;
      if (entryPx > 0 && t.highWater > entryPx) {
        final peakBps = ((t.highWater - entryPx) * kBpsDenom) ~/ entryPx;
        final ceiling = last * (kBpsDenom - CryptoRules.minStopGapBps) ~/ kBpsDenom;
        Pips desiredStop = 0;
        String lockNote = '';
        if (peakBps >= 85) {
          desiredStop = math.max(entryPx * 10025 ~/ 10000, t.highWater * 9965 ~/ 10000);
          lockNote = 'profit-trail +${(peakBps / 100).toStringAsFixed(2)}% peak';
        } else if (peakBps >= 50) {
          desiredStop = entryPx * 10012 ~/ 10000;
          lockNote = 'break-even lock';
        }
        if (desiredStop > 0) {
          final clampedStop = roundToTick(math.min(desiredStop, ceiling));
          final minStep = math.max(1, entryPx * 8 ~/ 10000);
          final baseStop = r.moved ? r.stop : t.stop;
          if (clampedStop > baseStop + minStep) {
            final newLim = stopLimitFor(clampedStop, t.atrPct);
            r = (moved: true, stop: clampedStop, stopLimit: newLim, r: peakBps / 100.0, note: lockNote);
          }
        }
      }
      if (r.moved) {
      t.stop = r.stop;
      t.stopLimit = r.stopLimit;
      _save();
      Map<String, dynamic> o;
      try {
        o = await ex.replaceStop(orderId: '${stopOrder['id']}', stop: r.stop, limit: r.stopLimit);
      } catch (_) {
        await ex.cancelOrderById('${stopOrder['id']}');
        await Future<void>.delayed(const Duration(milliseconds: 900));
        o = await ex.placeStopLimitSell(
          symbol: _cryptoSlash(sym),
          qtyText: floorQtyText(p.qtyDouble),
          stop: r.stop,
          limit: r.stopLimit,
          clientOrderId: 'shield-sl-$sym-${now.millisecondsSinceEpoch ~/ 1000}',
        );
      }
      t.orderId = '${o['id']}';
      _save();
      c._log('🛡️ GUARDIAN trail $sym → ${priceStr(r.stop)} (${r.r.toStringAsFixed(1)}R,${r.note})', level: 'ok', notify: false);
    } else if (t.highWater != hwBefore) {
      _save();
    }
  }

  TrailState _arm(String sym, Position p, TrailState? seed, CryptoVol? volOrNull) {
    final last = p.currentPrice;
    final ceiling = last * (kBpsDenom - CryptoRules.minStopGapBps) ~/ kBpsDenom;
    if (seed != null) {
      final entry = p.avgEntry > 0 ? p.avgEntry : last;
      final stop = roundToTick(math.min(seed.initialStop, ceiling));
      seed
        ..entry = entry
        ..stop = stop
        ..stopLimit = stopLimitFor(stop, seed.atrPct)
        ..highWater = math.max(last, entry)
        ..armed = true;
      return seed;
    }
    final vol = volOrNull!;
    var pct = math.max(CryptoRules.kAtr * vol.atr1hPct, CryptoRules.kSigma * vol.sigmaHorizonPct);
    pct = pct.clamp(CryptoRules.stopFloorBps / kBpsDenom, CryptoRules.stopCeilBps / kBpsDenom).toDouble();
    final stop = roundToTick(math.min(last * ((kBpsDenom - (pct * kBpsDenom).round())) ~/ kBpsDenom, ceiling));
    return TrailState(
      symbol: sym,
      entry: last,
      initialStop: stop,
      stop: stop,
      stopLimit: stopLimitFor(stop, vol.atr1hPct),
      highWater: last,
      atrPct: vol.atr1hPct,
      createdAt: DateTime.now().toUtc(),
      armed: true,
    );
  }
}

ConfluenceScore? computeCryptoConfluence({
  required String symbol,
  required double price,
  required List<Candle> candles,
  required DateTime nowUtc,
}) {
  if (price <= 0 || candles.isEmpty) return null;
  final bar = Duration(minutes: ConfluenceRules.barMinutes);
  final done = candles.where((b) => !b.t.add(bar).isAfter(nowUtc)).toList();
  if (done.length < 60) return null;
  if (nowUtc.difference(done.last.t.add(bar)) > const Duration(minutes: 20)) return null;

  final window = done.length > 288 ? done.sublist(done.length - 288) : done;
  final vwap = sessionVwap(window);
  final vols = done.map((b) => b.v).toList();
  final closes = done.map((b) => b.c).toList();
  final nonZeroPrior = vols.sublist(vols.length - 49, vols.length - 1).where((v) => v > 0).toList()..sort();
  final med = nonZeroPrior.isEmpty
      ? 0.0
      : nonZeroPrior[nonZeroPrior.length ~/ 2];
  // Use max volume of last 3 bars to avoid Alpaca's 0.00x empty bar quirk
  final recentVol = math.max(vols.last, math.max(vols[vols.length - 2], vols[vols.length - 3]));
  var vr = med > 0 ? (recentVol / med).clamp(0.1, 9.9) : 1.6;
  var rsi = wilderRsi(closes, period: ConfluenceRules.rsiPeriod);
  if (vwap == null || rsi == null) return null;

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
  final last = done.last;
  final range = last.h - last.l;
  final lowerWick = math.min(last.o, last.c) - last.l;
  final bounceCandle = range <= 0 || last.c >= last.o || (lowerWick / range) >= 0.20;
  final notOverextended = ((price - ema9) / price) <= 0.0085; // Not >0.85% extended above EMA9
  final ema50 = ema(closes, 50);
  final trendAligned = ema9 >= ema21 * 0.9985 && ema21 >= ema50 * 0.996 && price >= ema21 * 0.995 && bounceCandle && notOverextended;
  if (trendAligned && vr < 1.5 && price >= vwap) {
    vr = 1.55; // Prevent Alpaca's sparse US crypto book volume from blocking a confirmed EMA+VWAP pullback
  }

  if (!trendAligned) {
    // Hard gate: force < 2 points so non-pullback spikes NEVER pass triggerTrade
    rsi = 38.0;
  }
  return ConfluenceScore(
    symbol: symbol,
    price: price,
    vwapValue: trendAligned ? vwap : price * 1.01,
    volumeRatio: vr,
    rsiValue: rsi,
    barsUsed: done.length,
    asOf: done.last.t,
  );
}
