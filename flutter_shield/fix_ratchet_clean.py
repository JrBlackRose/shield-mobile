import re

with open('lib/shield_controller.dart', 'r', encoding='utf-8') as f:
    ctrl = f.read()

new_harvest_fn = """  Future<void> _harvestPredictableStockProfits() async {
    if (!sessionOpen) return;
    final n = nyTime(DateTime.now().toUtc());
    final m = n.hour * 60 + n.minute;
    final isEodWindow = m >= 954 && m < 960; // 15:54 - 15:59 ET: flatten expiring DAY brackets

    final openSyms = <String>{};
    final snapshot = List<Position>.of(positions);
    for (final p in snapshot) {
      if (isCryptoSymbol(p.symbol) || !p.isLong || p.avgEntry <= 0) continue;
      final sym = normSym(p.symbol);
      openSyms.add(sym);
      if (_closing.contains(sym)) continue;

      final gainBps = ((p.currentPrice - p.avgEntry) * kBpsDenom) ~/ p.avgEntry;
      final prevPeak = _stockPeakBps[sym] ?? gainBps;
      final peakBps = gainBps > prevPeak ? gainBps : prevPeak;
      _stockPeakBps[sym] = peakBps;

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
        _log('🕒 EOD Flatten: closing intraday stock $sym before 16:00 ET close', level: 'warn');
        final res = await closePosition(sym);
        unawaited(_notifyOS('🕒 EOD Flatten: $sym', res.message));
        continue;
      }

      if (executionMode != ExecMode.auto) continue;

      // 1. Peak-Profit Trail Ratchet: if a stock reached >= +1.15% (115 bps) and pulls back 45 bps from peak, lock in profit!
      final trailTriggered = peakBps >= 115 && gainBps >= 40 && (peakBps - gainBps) >= 45;
      if (trailTriggered) {
        _log('🔒 Peak-Profit Ratchet triggered on $sym (peak +${(peakBps / 100.0).toStringAsFixed(2)}% → locking +${(gainBps / 100.0).toStringAsFixed(2)}%)', level: 'ok');
        final res = await closePosition(sym);
        unawaited(_notifyOS('🔒 Profit Ratchet Locked: $sym', res.message));
        continue;
      }

      // 2. Unprotected Stock Guardian Backstop (if bracket legs are missing)
      if (!hasActiveSell) {
        if (gainBps <= -150) {
          _log('🛡️ Stock Guardian stop-loss triggered on unprotected $sym (${(gainBps / 100.0).toStringAsFixed(2)}%)', level: 'error');
          final res = await closePosition(sym);
          unawaited(_notifyOS('🛡️ Unprotected Stock Stop: $sym', res.message));
        } else if (gainBps >= 220) {
          _log('💰 Stock Guardian banking +${(gainBps / 100.0).toStringAsFixed(2)}% on unprotected $sym', level: 'ok');
          final res = await closePosition(sym);
          unawaited(_notifyOS('💰 Stock Profit Banked: $sym', res.message));
        }
      }
    }
    _stockPeakBps.removeWhere((k, _) => !openSyms.contains(k));
  }"""

ctrl, count = re.subn(
    r"  Future<void> _harvestPredictableStockProfits\(\) async \{.*?\n  \}",
    new_harvest_fn,
    ctrl,
    count=1,
    flags=re.DOTALL
)
print(f"[OK] Wired _stockPeakBps into _harvestPredictableStockProfits (matches={count})")

with open('lib/shield_controller.dart', 'w', encoding='utf-8') as f:
    f.write(ctrl)
