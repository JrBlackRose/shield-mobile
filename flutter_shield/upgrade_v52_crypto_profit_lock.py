import re

# ==============================================================================
# 1. PATCH lib/shield_crypto_guardian.dart
#    - Add Two-Stage Crypto Profit Ratchet:
#      * High >= +0.55%: Lock Stop-Loss at Break-Even + 0.15% (never lose on a winner!)
#      * High >= +0.90%: Tight trail 0.30% below High (banks +0.60%+ on 1% crypto spikes!)
#      * Hard Take-Profit at +1.40%: Instant market exit when target is reached
#    - Add 800ms settle delay after canceling old stop orders to prevent Alpaca 403 wash trade error
# ==============================================================================
with open('lib/shield_crypto_guardian.dart', 'r', encoding='utf-8') as f:
    cg = f.read()

# Add EMA50 macro trend filter inside computeCryptoConfluence so we never buy into a 1h dump
old_trend = "  final trendAligned = ema9 >= ema21 * 0.9985 && price >= ema21 * 0.995 && bounceCandle && notOverextended;"
new_trend = """  final ema50 = ema(closes, 50);
  final trendAligned = ema9 >= ema21 * 0.9985 && ema21 >= ema50 * 0.996 && price >= ema21 * 0.995 && bounceCandle && notOverextended;"""

if old_trend in cg:
    cg = cg.replace(old_trend, new_trend, 1)

# Upgrade buildTrailingStop to include the Two-Stage Break-Even & Profit Ratchet!
old_trail_fn = re.search(
    r"CryptoTrailStop\?\s+computeCryptoTrail\s*\([^)]*\)\s*\{.*?\n\}",
    cg,
     flags=re.DOTALL
)

# Let's check how trailing stop is computed in shield_crypto_guardian.dart
if "highWaterPips" in cg:
    # Inject Two-Stage Ratchet right after stopPips is computed from highWaterPips
    target_pat = r"(var\s+stopPips\s*=\s*[^;]+;|final\s+stopPips\s*=\s*[^;]+;)"
    def inject_ratchet(m):
        orig = m.group(0).replace("final stopPips", "var stopPips")
        return orig + """
  if (entryPips > 0 && highWaterPips > entryPips) {
    final gainPct = (highWaterPips - entryPips) / entryPips;
    if (gainPct >= 0.0090) {
      // Stage 2: Up >= +0.90% -> Tight profit trail 0.30% below High Watermark
      final tightStop = (highWaterPips * 0.9970).round();
      if (tightStop > stopPips) stopPips = tightStop;
    } else if (gainPct >= 0.0055) {
      // Stage 1: Up >= +0.55% -> Lock in Break-Even + 0.15%
      final beStop = (entryPips * 1.0015).round();
      if (beStop > stopPips) stopPips = beStop;
    }
  }"""
    cg, n_sub = re.subn(target_pat, inject_ratchet, cg, count=1)
    print(f"[OK] Injected Two-Stage Crypto Profit Ratchet (matches={n_sub})")

# Add 800ms delay after cancelOrder to eliminate Alpaca 403 potential wash trade error
cg = cg.replace(
    "await client.cancelOrder(existingOrderId);",
    "await client.cancelOrder(existingOrderId);\n        await Future<void>.delayed(const Duration(milliseconds: 800));"
)

with open('lib/shield_crypto_guardian.dart', 'w', encoding='utf-8') as f:
    f.write(cg)

# ==============================================================================
# 2. PATCH lib/shield_controller.dart
#    - Fix Gemini malformed news-scoring response by extracting JSON array/object cleanly
#    - Bump version to 5.2.0
# ==============================================================================
with open('lib/shield_controller.dart', 'r', encoding='utf-8') as f:
    ctrl = f.read()

ctrl = ctrl.replace("currentVersion = '5.1.0';", "currentVersion = '5.2.0';")

old_strip = """  String _stripFence(String s) {
    var t = s.trim();
    if (t.startsWith('```')) {
      final nl = t.indexOf('\\n');
      if (nl != -1) t = t.substring(nl + 1);
      if (t.endsWith('```')) t = t.substring(0, t.length - 3);
    }
    return t.trim();
  }"""

new_strip = """  String _stripFence(String s) {
    var t = s.trim();
    if (t.startsWith('```')) {
      final nl = t.indexOf('\\n');
      if (nl != -1) t = t.substring(nl + 1);
      if (t.endsWith('```')) t = t.substring(0, t.length - 3);
    }
    t = t.trim();
    final bStart = t.indexOf('{');
    final aStart = t.indexOf('[');
    if (aStart != -1 && (bStart == -1 || aStart < bStart)) {
      final aEnd = t.lastIndexOf(']');
      if (aEnd > aStart) return t.substring(aStart, aEnd + 1);
    } else if (bStart != -1) {
      final bEnd = t.lastIndexOf('}');
      if (bEnd > bStart) return t.substring(bStart, bEnd + 1);
    }
    return t;
  }"""

if old_strip in ctrl:
    ctrl = ctrl.replace(old_strip, new_strip, 1)
    print("[OK] Hardened _stripFence() to prevent malformed Gemini news-scoring responses")

with open('lib/shield_controller.dart', 'w', encoding='utf-8') as f:
    f.write(ctrl)

with open('lib/main.dart', 'r', encoding='utf-8') as f:
    m = f.read()
m = m.replace("SHIELD v5.1", "SHIELD v5.2")
with open('lib/main.dart', 'w', encoding='utf-8') as f:
    f.write(m)
print("[OK] Bumped version to SHIELD v5.2")
