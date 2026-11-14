import re

with open('lib/shield_crypto_guardian.dart', 'r', encoding='utf-8') as f:
    cg = f.read()

# 1. Lower Crypto Hard Take-Profit from +4.20% (10420) to +1.45% (10145)
cg, n1 = re.subn(
    r"if \(p\.avgEntry > 0 && last >= \(p\.avgEntry \* 10420 ~/ 10000\)\) \{",
    "if (p.avgEntry > 0 && last >= (p.avgEntry * 10145 ~/ 10000)) {",
    cg
)
cg = cg.replace("banking >2R crypto surge", "banking +1.45% crypto target")

# 2. Inject Two-Stage Crypto Profit-Lock Ratchet (+0.50% BE / +0.85% Profit Trail)
ratchet_Replacement = """final hwBefore = t.highWater;
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
      if (r.moved) {"""

cg, n2 = re.subn(
    r"final hwBefore = t\.highWater;\s+final r = ratchetStop\(t, last\);\s+if \(r\.moved\) \{",
    ratchet_Replacement,
    cg
)

# 3. Increase cancel settle delay from 350ms to 900ms to prevent Alpaca 403 wash-trade error
cg, n3 = re.subn(r"milliseconds:\s*350", "milliseconds: 900", cg)

with open('lib/shield_crypto_guardian.dart', 'w', encoding='utf-8') as f:
    f.write(cg)
print(f"[OK] Crypto Guardian patched: TP={n1}, Ratchet={n2}, SettleDelay={n3}")

# 4. Silence malformed news-scoring & transient background network log spam
with open('lib/shield_controller.dart', 'r', encoding='utf-8') as f:
    ctrl = f.read()

ctrl, n4 = re.subn(
    r"throw const PlanAbort\('Gemini returned a malformed news-scoring response\.'\);",
    "return const <_Scored>[];",
    ctrl
)

with open('lib/shield_controller.dart', 'w', encoding='utf-8') as f:
    f.write(ctrl)
print(f"[OK] Controller news parser patched: {n4}")
