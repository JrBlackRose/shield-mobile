with open('lib/shield_crypto_guardian.dart', 'r', encoding='utf-8') as f:
    cg = f.read()

# 1. Lower Crypto Hard Take-Profit from +4.20% (10420) to +1.45% (10145)
old_tp = """      // Let trailing stop work; only hard-harvest crypto on a >= +4.2% (>2R) surge
      if (p.avgEntry > 0 && last >= (p.avgEntry * 10420 ~/ 10000)) {
        c._log('💰 GUARDIAN banking >2R crypto surge on $sym (+${((last - p.avgEntry) / p.avgEntry * 100).toStringAsFixed(2)}%)', level: 'ok');
        final res = await c.closePosition(sym);
        unawaited(c._notifyOS('💰 Crypto Surge Banked: $sym', res.message));
        return;
      }"""

new_tp = """      // Hard-harvest crypto on a >= +1.45% surge so weekend/intraday bounces never round-trip
      if (p.avgEntry > 0 && last >= (p.avgEntry * 10145 ~/ 10000)) {
        c._log('💰 GUARDIAN banking +1.45% crypto target on $sym (+${((last - p.avgEntry) / p.avgEntry * 100).toStringAsFixed(2)}%)', level: 'ok');
        final res = await c.closePosition(sym);
        unawaited(c._notifyOS('💰 Crypto Target Banked: $sym', res.message));
        return;
      }"""

assert old_tp in cg, "Could not find old_tp block"
cg = cg.replace(old_tp, new_tp, 1)

# 2. Inject Two-Stage Crypto Profit-Lock Ratchet & 900ms wash-trade prevention delay
old_ratchet = """      final hwBefore = t.highWater;
      final r = ratchetStop(t, last);
      if (r.moved) {
        t.stop = r.stop;
        t.stopLimit = r.stopLimit;
        _save();
        Map<String, dynamic> o;
        try {
          o = await ex.replaceStop(orderId: '${stopOrder['id']}', stop: r.stop, limit: r.stopLimit);
        } catch (_) {
          await ex.cancelOrderById('${stopOrder['id']}');
          await Future<void>.delayed(const Duration(milliseconds: 350));"""

new_ratchet = """      final hwBefore = t.highWater;
      if (p.avgEntry > 0) t.entry = p.avgEntry;
      if (last > t.highWater) t.highWater = last;
      var r = ratchetStop(t, last);

      // Two-Stage Crypto Profit-Lock Ratchet (overrides wide 1R wait on +0.50% / +0.85% bounces)
      final entryPx = p.avgEntry > 0 ? p.avgEntry : t.entry;
      if (entryPx > 0 && t.highWater > entryPx) {
        final peakBps = ((t.highWater - entryPx) * kBpsDenom) ~/ entryPx;
        final ceiling = last * (kBpsDenom - CryptoRules.minStopGapBps) ~/ kBpsDenom;
        Pips desiredStop = 0;
        String lockNote = '';
        if (peakBps >= 85) {
          // Stage 2: Peak >= +0.85% -> Trail 0.35% below High Watermark (min +0.25% above entry)
          desiredStop = math.max(entryPx * 10025 ~/ 10000, t.highWater * 9965 ~/ 10000);
          lockNote = 'profit-trail +${(peakBps / 100).toStringAsFixed(2)}% peak';
        } else if (peakBps >= 50) {
          // Stage 1: Peak >= +0.50% -> Lock Break-Even + 0.12%
          desiredStop = entryPx * 10012 ~/ 10000;
          lockNote = 'break-even lock';
        }
        if (desiredStop > 0) {
          final clampedStop = roundToTick(math.min(desiredStop, ceiling));
          final minStep = math.max(1, entryPx * 8 ~/ 10000); // require >= 0.08% upward step
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
          await Future<void>.delayed(const Duration(milliseconds: 900));"""

assert old_ratchet in cg, "Could not find old_ratchet block"
cg = cg.replace(old_ratchet, new_ratchet, 1)

with open('lib/shield_crypto_guardian.dart', 'w', encoding='utf-8') as f:
    f.write(cg)
print("[OK] Patched lib/shield_crypto_guardian.dart (1.45% TP + Two-Stage Crypto Profit Ratchet + 900ms settle)")

# 3. Patch lib/shield_controller.dart to eliminate malformed news-scoring & offline log spam
with open('lib/shield_controller.dart', 'r', encoding='utf-8') as f:
    ctrl = f.read()

old_news_err = "      throw const PlanAbort('Gemini returned a malformed news-scoring response.');"
new_news_err = "      return const <_Scored>[]; // Silently skip truncated/malformed AI news batch"

if old_news_err in ctrl:
    ctrl = ctrl.replace(old_news_err, new_news_err, 1)
    print("[OK] Silenced malformed news-scoring abort in lib/shield_controller.dart")

with open('lib/shield_controller.dart', 'w', encoding='utf-8') as f:
    f.write(ctrl)
