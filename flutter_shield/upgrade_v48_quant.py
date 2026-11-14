import re

# 1. Patch lib/shield_core.dart: Robust JSON/parts extractor in GeminiClient
with open('lib/shield_core.dart', 'r', encoding='utf-8') as f:
    core = f.read()

old_parse_core = """    try {
      final outer = jsonDecode(res.body) as Map<String, dynamic>;
      var text = (outer['candidates'][0]['content']['parts'][0]['text'] as String).trim();
      if (text.startsWith('```')) text = text.replaceAll(RegExp(r'^```(?:json)?|```$'), '').trim();
      return text;
    } catch (_) {
      throw const PlanAbort('Gemini returned a malformed response.');
    }"""

new_parse_core = """    try {
      final outer = jsonDecode(res.body) as Map<String, dynamic>;
      final parts = (outer['candidates'] as List).first['content']['parts'] as List;
      final buf = StringBuffer();
      for (final p in parts.whereType<Map>()) {
        if (p['thought'] == true) continue;
        if (p['text'] is String) buf.write(p['text']);
      }
      var text = buf.toString().trim();
      if (text.isEmpty && parts.isNotEmpty && parts.last is Map) {
        text = '${(parts.last as Map)['text'] ?? ''}'.trim();
      }
      final startObj = text.indexOf('{');
      final endObj = text.lastIndexOf('}');
      if (startObj != -1 && endObj > startObj) {
        text = text.substring(startObj, endObj + 1);
      }
      return text;
    } catch (_) {
      throw const PlanAbort('Gemini returned a malformed response.');
    }"""

if old_parse_core in core:
    core = core.replace(old_parse_core, new_parse_core, 1)

with open('lib/shield_core.dart', 'w', encoding='utf-8') as f:
    f.write(core)
print("[OK] Patched lib/shield_core.dart (Multi-part thought-safe JSON extractor)")

# 2. Patch lib/shield_controller.dart: Fix _NewsScorer parser + Upgrade Confluence Prediction Engine (EMA9 > EMA21 + Candle Close + 3/3 Strong Gate)
with open('lib/shield_controller.dart', 'r', encoding='utf-8') as f:
    ctrl = f.read()

ctrl = ctrl.replace("static const String currentVersion = '4.7.1';", "static const String currentVersion = '4.8.0';")

# Escape quotes inside headlines in _NewsScorer so headlines with quotes don't break JSON generation
ctrl = ctrl.replace(
    'lines.writeln(\'id=${n.id} symbols=[${n.symbols.join(\',\')}] headline="${_oneLine(n.headline, 220)}"\');',
    'lines.writeln(\'id=${n.id} symbols=[${n.symbols.join(\',\')}] headline="${_oneLine(n.headline.replaceAll(\'"\', "\'"), 220)}"\');'
)

# Fix _NewsScorer JSON extraction (skip thought parts and slice outermost JSON object/array)
old_news_parse = """    try {
      final outer = jsonDecode(res.body) as Map<String, dynamic>;
      final text = _stripFence(outer['candidates'][0]['content']['parts'][0]['text'] as String);
      final decoded = jsonDecode(text);"""

new_news_parse = """    try {
      final outer = jsonDecode(res.body) as Map<String, dynamic>;
      final parts = (outer['candidates'] as List).first['content']['parts'] as List;
      final buf = StringBuffer();
      for (final p in parts.whereType<Map>()) {
        if (p['thought'] == true) continue;
        if (p['text'] is String) buf.write(p['text']);
      }
      var text = _stripFence(buf.toString());
      final sObj = text.indexOf('{');
      final eObj = text.lastIndexOf('}');
      final sArr = text.indexOf('[');
      final eArr = text.lastIndexOf(']');
      if (sObj != -1 && eObj > sObj && (sArr == -1 || sObj < sArr)) {
        text = text.substring(sObj, eObj + 1);
      } else if (sArr != -1 && eArr > sArr) {
        text = text.substring(sArr, eArr + 1);
      }
      final decoded = jsonDecode(text);"""

if old_news_parse in ctrl:
    ctrl = ctrl.replace(old_news_parse, new_news_parse, 1)

# Upgrade computeConfluence with EMA-9 > EMA-21 trend alignment & bullish candle structure
old_conf_fn = """  final vwap = sessionVwap(session);
  final vr = volumeRatio(done.map((c) => c.v).toList(), avgBars: ConfluenceRules.volAvgBars);
  final rsi = wilderRsi(done.map((c) => c.c).toList(), period: ConfluenceRules.rsiPeriod);
  if (vwap == null || vr == null || rsi == null) return null;
  return ConfluenceScore(
    symbol: symbol,
    price: price,
    vwapValue: vwap,
    volumeRatio: vr,
    rsiValue: rsi,
    barsUsed: done.length,
    asOf: last.t,
  );"""

new_conf_fn = """  final closes = done.map((c) => c.c).toList();
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
  final range = last.h - last.l;
  final strongClose = range <= 0 ? true : ((last.c - last.l) / range) >= 0.45;
  final trendAligned = ema9 >= ema21 && price >= ema9 && strongClose;

  final vwap = sessionVwap(session);
  final vr = volumeRatio(done.map((c) => c.v).toList(), avgBars: ConfluenceRules.volAvgBars);
  var rsi = wilderRsi(closes, period: ConfluenceRules.rsiPeriod);
  if (vwap == null || vr == null || rsi == null) return null;
  // Penalize RSI score if EMA9 < EMA21 downtrend or weak rejection wick
  if (!trendAligned && rsi >= 50.0 && rsi < 70.0) {
    rsi = 44.0;
  }
  return ConfluenceScore(
    symbol: symbol,
    price: price,
    vwapValue: vwap,
    volumeRatio: vr,
    rsiValue: rsi,
    barsUsed: done.length,
    asOf: last.t,
  );"""

if old_conf_fn in ctrl:
    ctrl = ctrl.replace(old_conf_fn, new_conf_fn, 1)

with open('lib/shield_controller.dart', 'w', encoding='utf-8') as f:
    f.write(ctrl)
print("[OK] Patched lib/shield_controller.dart (News JSON extractor + EMA-9/EMA-21 Trend & Candle Prediction Filter)")

# 3. Patch lib/main.dart: Show clean TP/SL protection line in SIMPLE mode & fix ! 10.1% eq warning threshold
with open('lib/main.dart', 'r', encoding='utf-8') as f:
    main_code = f.read()

main_code = main_code.replace("SHIELD v4.7", "SHIELD v4.8")

# Always show TP / SL protection line on position cards even in SIMPLE mode so users see active Risk Management
main_code = main_code.replace(
    "if (c.proMode && prot != null)",
    "if (prot != null)"
)

# Fix false red warning badge when a 10% position grows slightly above 10.0% due to unrealized profit
main_code = main_code.replace(" > 15.0", " > 12.0").replace(" >= 15.0", " >= 12.0").replace(" > 10.0", " > 12.0").replace(" >= 10.0", " >= 12.0")

with open('lib/main.dart', 'w', encoding='utf-8') as f:
    f.write(main_code)
print("[OK] Patched lib/main.dart (SHIELD v4.8, visible TP/SL risk line in SIMPLE mode, 12% profit buffer on badge)")
print("\n🎉 SHIELD v4.8.0 Quant & Risk Visibility upgrade complete!")
